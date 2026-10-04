from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Campaign,
    Contact,
    EmailAccount,
    EmailMessage,
    EmailThread,
    Message,
)
from app.providers import EmailProviderInterface, ProviderInboxMessage
from app.services.audit import AuditService
from app.services.senders import SenderService

THREAD_STATUSES = ("UNREAD", "READ", "REPLIED", "ARCHIVED", "REQUIRES_ACTION")


def _cmp_dt(value: datetime | None) -> datetime | None:
    """Return a timezone-naive UTC value for safe comparisons.

    SQLite returns naive datetimes from DATETIME columns while many inputs are
    timezone-aware; normalizing both sides avoids aware/naive errors.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


class InboxError(ValueError):
    pass


class InboxNotFoundError(LookupError):
    pass


class InboxNotSupportedError(InboxError):
    """Raised when the sender's provider cannot read a mailbox (e.g. SMTP)."""


class InboxSyncError(RuntimeError):
    pass


class InboxService:
    """Synchronizes and serves the unified inbox.

    Incoming replies are stored durably in ``email_threads``/``email_messages``
    and associated with a campaign/recipient only when the match is confident.
    Otherwise the thread is left ``UNMATCHED`` rather than guessed.
    """

    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        provider_factory: Callable[[EmailAccount], object] | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.provider_factory = provider_factory
        self.sender_service = SenderService(session, tenant_id)
        self.audit = AuditService(session, tenant_id, actor_id)

    # -------------------------------------------------------------- reads

    def list_threads(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: str | None = None,
        sender_id: UUID | None = None,
    ) -> tuple[list[EmailThread], int]:
        query = select(EmailThread).where(EmailThread.tenant_id == self.tenant_id)
        count_query = select(func.count(EmailThread.id)).where(
            EmailThread.tenant_id == self.tenant_id
        )
        if status is not None:
            if status not in THREAD_STATUSES:
                raise InboxError(f"Unsupported thread status: {status}")
            query = query.where(EmailThread.status == status)
            count_query = count_query.where(EmailThread.status == status)
        if sender_id is not None:
            query = query.where(EmailThread.sender_id == sender_id)
            count_query = count_query.where(EmailThread.sender_id == sender_id)
        total = int(self.session.scalar(count_query) or 0)
        items = list(
            self.session.scalars(
                query.order_by(EmailThread.last_message_at.desc().nullslast())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
        )
        return items, total

    def get_thread(self, thread_id: UUID) -> EmailThread:
        thread = self.session.scalar(
            select(EmailThread)
            .options(selectinload(EmailThread.messages))
            .where(
                EmailThread.id == thread_id,
                EmailThread.tenant_id == self.tenant_id,
            )
        )
        if thread is None:
            raise InboxNotFoundError("Inbox thread not found")
        return thread

    def set_status(self, thread_id: UUID, status: str) -> EmailThread:
        if status not in THREAD_STATUSES:
            raise InboxError(f"Unsupported thread status: {status}")
        thread = self.get_thread(thread_id)
        previous = thread.status
        thread.status = status
        self.audit.record(
            "INBOX_THREAD_STATUS",
            "email_thread",
            thread.id,
            {"previous": previous, "status": status},
        )
        self.session.commit()
        return thread

    def recipient_context(self, thread: EmailThread) -> dict[str, object] | None:
        """Right-pane details: contact, company, campaign, last contact."""
        if thread.contact_id is None:
            return None
        contact = self.session.scalar(
            select(Contact).where(
                Contact.id == thread.contact_id,
                Contact.tenant_id == self.tenant_id,
            )
        )
        if contact is None:
            return None
        campaign: Campaign | None = None
        if thread.campaign_id is not None:
            campaign = self.session.scalar(
                select(Campaign).where(
                    Campaign.id == thread.campaign_id,
                    Campaign.tenant_id == self.tenant_id,
                )
            )
        last_contact = self.session.scalar(
            select(func.max(Message.created_at))
            .where(
                Message.tenant_id == self.tenant_id,
                Message.contact_id == contact.id,
            )
        )
        name = " ".join(
            part for part in (contact.first_name, contact.last_name) if part
        )
        return {
            "contact_id": str(contact.id),
            "name": name or contact.email,
            "email": contact.email,
            "company": contact.company,
            "designation": contact.designation,
            "campaign_name": campaign.name if campaign is not None else None,
            "campaign_id": str(campaign.id) if campaign is not None else None,
            "last_contact": last_contact.isoformat()
            if last_contact is not None
            else None,
        }

    # -------------------------------------------------------------- sync

    def _provider_for(self, sender: EmailAccount) -> EmailProviderInterface:
        if self.provider_factory is not None:
            return cast(EmailProviderInterface, self.provider_factory(sender))
        return cast(EmailProviderInterface, self.sender_service.provider(sender))

    def sync_sender(self, sender_id: UUID) -> dict[str, object]:
        sender = self.sender_service.get(sender_id)
        provider = self._provider_for(sender)
        supports_inbox = bool(getattr(provider, "supports_inbox", False))
        if not supports_inbox:
            raise InboxNotSupportedError(
                f"{sender.provider} does not support mailbox synchronization"
            )
        try:
            sync_fn = getattr(provider, "sync_messages", None)
            if not callable(sync_fn):
                raise InboxNotSupportedError(
                    f"{sender.provider} does not support mailbox synchronization"
                )
            items = sync_fn(50)
        except InboxNotSupportedError:
            raise
        except Exception as exc:
            self.audit.record(
                "INBOX_SYNC_FAILED",
                "sender",
                sender.id,
                {"provider": sender.provider, "error": str(exc)[:500]},
            )
            self.session.commit()
            raise InboxSyncError("Inbox synchronization failed") from exc

        new_threads = 0
        new_messages = 0
        for item in items or []:
            created_message, created_thread = self._upsert_message(sender, item)
            if created_thread:
                new_threads += 1
            if created_message:
                new_messages += 1
        self.session.commit()
        self.audit.record(
            "INBOX_SYNCED",
            "sender",
            sender.id,
            {
                "provider": sender.provider,
                "new_threads": new_threads,
                "new_messages": new_messages,
            },
        )
        self.session.commit()
        return {
            "sender_id": str(sender.id),
            "provider": sender.provider,
            "new_threads": new_threads,
            "new_messages": new_messages,
        }

    def _upsert_message(
        self, sender: EmailAccount, item: ProviderInboxMessage
    ) -> tuple[bool, bool]:
        existing = self.session.scalar(
            select(EmailMessage).where(
                EmailMessage.tenant_id == self.tenant_id,
                EmailMessage.external_message_id == item.id,
                EmailMessage.provider == sender.provider,
            )
        )
        if existing is not None:
            return False, False

        thread = self.session.scalar(
            select(EmailThread).where(
                EmailThread.tenant_id == self.tenant_id,
                EmailThread.sender_id == sender.id,
                EmailThread.external_thread_id == item.thread_id,
            )
        )
        created_thread = False
        if thread is None:
            thread = EmailThread(
                tenant_id=self.tenant_id,
                sender_id=sender.id,
                external_thread_id=item.thread_id,
                subject=item.subject,
                last_message_at=item.received_at,
                status="UNREAD",
                provider=sender.provider,
            )
            self.session.add(thread)
            self.session.flush()
            self._associate_thread(thread, item)
            created_thread = True

        latest_at = item.received_at or datetime.now(UTC)
        latest_naive = (
            latest_at.astimezone(UTC).replace(tzinfo=None)
            if latest_at.tzinfo is not None
            else latest_at
        )
        stored_naive = _cmp_dt(thread.last_message_at)
        if stored_naive is None or latest_naive > stored_naive:
            thread.last_message_at = latest_at
        if not thread.subject:
            thread.subject = item.subject
        message = EmailMessage(
            tenant_id=self.tenant_id,
            thread_id=thread.id,
            external_message_id=item.id,
            direction=item.direction or "INBOUND",
            from_email=item.from_email,
            to_email=item.to_email,
            subject=item.subject,
            body_reference=item.id,
            body_text=item.body_text,
            body_html=item.body_html,
            received_at=item.received_at,
            provider=sender.provider,
            status="SENT" if item.direction == "OUTBOUND" else "RECEIVED",
            in_reply_to=item.in_reply_to,
            references=item.references,
        )
        self.session.add(message)
        return True, created_thread

    def _associate_thread(
        self, thread: EmailThread, item: ProviderInboxMessage
    ) -> None:
        """Conservatively associate an inbound reply with campaign/recipient.

        Only matched when the evidence is unambiguous (a unique contact by
        email, and at most one campaign sent to them through this sender).
        Otherwise the thread stays UNMATCHED.
        """
        from_email = (item.from_email or "").strip().lower()
        if not from_email:
            return
        contacts = list(
            self.session.scalars(
                select(Contact).where(
                    Contact.tenant_id == self.tenant_id,
                    func.lower(Contact.email) == from_email,
                )
            ).all()
        )
        if len(contacts) != 1:
            return
        contact = contacts[0]
        thread.contact_id = contact.id

        campaigns = self.session.scalars(
            select(Campaign.id)
            .join(Message, Message.campaign_id == Campaign.id)
            .where(
                Message.tenant_id == self.tenant_id,
                Message.contact_id == contact.id,
                Message.sender_id == thread.sender_id,
            )
            .distinct()
        ).all()
        if len(campaigns) == 1:
            thread.campaign_id = campaigns[0]
            thread.match_status = "MATCHED"
        elif not campaigns:
            thread.match_status = "MATCHED"
