from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIReplyDraft, Contact, EmailAccount, EmailMessage, EmailThread
from app.providers import AIProviderInterface, EmailProviderInterface, ProviderMessage
from app.providers.factory import build_ai_provider
from app.services.audit import AuditService
from app.services.senders import SenderService
from app.services.suppression import SuppressionService

_SENDER_UNAVAILABLE_STATUSES = {"DISABLED", "DISCONNECTED", "SUSPENDED", "REAUTH_REQUIRED"}


class InboxReplyError(ValueError):
    pass


class InboxReplyNotFoundError(LookupError):
    pass


class InboxReplyBlockedError(InboxReplyError):
    """The reply was blocked by a compliance/suppression rule."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class InboxReplyService:
    """Manual + AI-drafted replies to inbox threads.

    AI output is ALWAYS a draft. Nothing is ever auto-sent: the user reviews,
    edits, and explicitly sends. Replies are treated as one-to-one messages,
    not bulk campaign sends.
    """

    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        provider_factory: Callable[[EmailAccount], object] | None = None,
        ai_provider: AIProviderInterface | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.provider_factory = provider_factory
        self.ai_provider = ai_provider or build_ai_provider()
        self.sender_service = SenderService(session, tenant_id)
        self.audit = AuditService(session, tenant_id, actor_id)

    # -------------------------------------------------------------- reads

    def _thread(self, thread_id: UUID) -> EmailThread:
        thread = self.session.scalar(
            select(EmailThread).where(
                EmailThread.id == thread_id,
                EmailThread.tenant_id == self.tenant_id,
            )
        )
        if thread is None:
            raise InboxReplyNotFoundError("Inbox thread not found")
        return thread

    def _last_inbound(self, thread: EmailThread) -> EmailMessage | None:
        return self.session.scalar(
            select(EmailMessage)
            .where(
                EmailMessage.thread_id == thread.id,
                EmailMessage.tenant_id == self.tenant_id,
                EmailMessage.direction == "INBOUND",
            )
            .order_by(EmailMessage.received_at.desc().nullslast())
        )

    def _recipient_email(self, thread: EmailThread) -> str:
        if thread.contact_id is not None:
            contact = self.session.scalar(
                select(Contact).where(
                    Contact.id == thread.contact_id,
                    Contact.tenant_id == self.tenant_id,
                )
            )
            if contact is not None:
                return contact.email
        last = self._last_inbound(thread)
        if last is not None and last.from_email:
            return last.from_email
        raise InboxReplyError("Cannot determine the reply recipient for this thread")

    # ---------------------------------------------------------------- AI

    def draft(self, thread_id: UUID) -> AIReplyDraft:
        thread = self._thread(thread_id)
        last = self._last_inbound(thread)
        inbound_text = (last.body_text if last is not None else "") or ""
        context = {
            "classification": "NEEDS_INFORMATION",
            "thread_subject": thread.subject or "",
            "contact_email": self._recipient_email(thread),
        }
        body = "Thanks for your note. I'll get back to you with what you need shortly."
        error: str | None = None
        try:
            body = self.ai_provider.generate_reply(inbound_text, context) or body
        except Exception as exc:  # pragma: no cover - provider failure path
            error = str(exc)[:500]
        subject = "Re: " + (thread.subject or "")
        if subject.startswith("Re: Re: "):
            subject = subject[len("Re: ") :]
        draft = AIReplyDraft(
            tenant_id=self.tenant_id,
            thread_id=thread.id,
            created_by_id=self.actor_id,
            subject=subject,
            body=body,
            status="FAILED" if error else "DRAFT",
            provider=self.ai_provider.__class__.__name__,
        )
        self.session.add(draft)
        self.session.flush()
        self.audit.record(
            "INBOX_REPLY_DRAFTED",
            "email_thread",
            thread.id,
            {
                "draft_id": str(draft.id),
                "subject": subject,
                "ai_provider": draft.provider,
                "error": error,
            },
        )
        if error is None:
            from app.billing import EVENT_AI_GENERATION, UsageService

            UsageService(self.session, self.tenant_id, self.actor_id).meter(
                EVENT_AI_GENERATION,
                resource_type="ai_reply_draft",
                resource_id=draft.id,
            )
        # A draft alone never changes the thread; AI output is never sent.
        self.session.commit()
        return draft

    def approve(self, draft_id: UUID) -> AIReplyDraft:
        draft = self.session.scalar(
            select(AIReplyDraft).where(
                AIReplyDraft.id == draft_id,
                AIReplyDraft.tenant_id == self.tenant_id,
            )
        )
        if draft is None:
            raise InboxReplyNotFoundError("AI reply draft not found")
        if draft.status == "FAILED":
            raise InboxReplyError("A failed AI draft cannot be approved")
        draft.status = "APPROVED"
        draft.approved_at = datetime.now(UTC)
        self.audit.record(
            "INBOX_REPLY_DRAFT_APPROVED",
            "ai_reply_draft",
            draft.id,
            {"thread_id": str(draft.thread_id)},
        )
        self.audit.record(
            "AI_REPLY_APPROVED",
            "ai_reply_draft",
            draft.id,
            {"thread_id": str(draft.thread_id)},
        )
        self.session.commit()
        return draft

    def send(
        self,
        thread_id: UUID,
        subject: str,
        body: str,
        *,
        draft_id: UUID | None = None,
    ) -> EmailMessage:
        """Manually send a reviewed reply. Sends EXACTLY the passed content."""
        if not subject.strip():
            raise InboxReplyError("Reply subject is required")
        if not body.strip():
            raise InboxReplyError("Reply body is required")
        thread = self._thread(thread_id)
        sender = self.sender_service.get(thread.sender_id)
        self._check_can_reply(sender, thread)
        recipient = self._recipient_email(thread)
        last = self._last_inbound(thread)
        if draft_id is not None:
            self._require_approved_draft(draft_id, thread.id)

        headers: dict[str, str] = {}
        if last is not None and last.external_message_id:
            headers["In-Reply-To"] = f"<{last.external_message_id}>"
            refs = [ref for ref in (last.references or "").split() if ref]
            if last.external_message_id not in refs:
                refs.append(f"<{last.external_message_id}>")
            headers["References"] = " ".join(refs[-12:])

        provider: EmailProviderInterface = (
            cast(EmailProviderInterface, self.provider_factory(sender))
            if self.provider_factory is not None
            else self.sender_service.provider(sender)
        )
        provider.connect()
        try:
            result = provider.send(
                ProviderMessage(
                    recipient,
                    subject,
                    self._body_to_html(body),
                    body,
                    sender.reply_to,
                    headers=headers,
                )
            )
        except Exception:
            provider.disconnect()
            raise InboxReplyError("The provider rejected the reply") from None
        finally:
            provider.disconnect()

        outgoing = EmailMessage(
            tenant_id=self.tenant_id,
            thread_id=thread.id,
            external_message_id=result.provider_message_id,
            direction="OUTBOUND",
            from_email=sender.email,
            to_email=recipient,
            subject=subject,
            body_reference=result.provider_message_id,
            body_text=body,
            body_html=self._body_to_html(body),
            received_at=result.accepted_at,
            provider=sender.provider,
            status="SENT",
        )
        self.session.add(outgoing)
        thread.status = "REPLIED"
        thread.last_message_at = result.accepted_at

        if draft_id is not None:
            draft = self.session.scalar(
                select(AIReplyDraft).where(
                    AIReplyDraft.id == draft_id,
                    AIReplyDraft.tenant_id == self.tenant_id,
                )
            )
            if draft is not None:
                draft.status = "SENT"
        self.audit.record(
            "INBOX_REPLY_SENT",
            "email_thread",
            thread.id,
            {
                "sender_id": str(sender.id),
                "recipient": recipient,
                "provider": sender.provider,
                "draft_id": str(draft_id) if draft_id is not None else None,
            },
        )
        self.session.commit()
        return outgoing

    def _require_approved_draft(self, draft_id: UUID, thread_id: UUID) -> None:
        """Enforce the AI-approval gate before sending a drafted reply.

        AI output is never auto-sent. When a caller passes ``draft_id``, the
        referenced draft must already be APPROVED (human in the loop) and must
        belong to the same thread; otherwise the send is blocked. This prevents
        bypassing the review gate by referencing a draft that was never, or was
        only partially, approved.
        """
        draft = self.session.scalar(
            select(AIReplyDraft).where(
                AIReplyDraft.id == draft_id,
                AIReplyDraft.tenant_id == self.tenant_id,
            )
        )
        if draft is None:
            raise InboxReplyBlockedError(
                "Cannot send: AI reply draft not found in this tenant"
            )
        if draft.thread_id != thread_id:
            raise InboxReplyBlockedError(
                "Cannot send: AI reply draft belongs to a different thread"
            )
        if draft.status != "APPROVED":
            raise InboxReplyBlockedError(
                "AI reply draft must be approved before it can be sent"
            )

    # --------------------------------------------------------- compliance

    def _check_can_reply(self, sender: EmailAccount, thread: EmailThread) -> None:
        # Sender authorization: must belong to this tenant and be usable.
        if sender.tenant_id != self.tenant_id:
            raise InboxReplyBlockedError("Sender is not authorized for this tenant")
        if sender.status in _SENDER_UNAVAILABLE_STATUSES:
            raise InboxReplyBlockedError("Sender is not connected and cannot reply")
        # Compliance: never reply to a suppressed recipient (do not continue
        # a conversation with someone who asked to stop).
        recipient = self._recipient_email(thread)
        if SuppressionService(self.session, self.tenant_id).is_suppressed(recipient):
            raise InboxReplyBlockedError(
                "Recipient is suppressed; replying is blocked by compliance"
            )
        self.audit.record(
            "INBOX_REPLY_VETTED",
            "email_thread",
            thread.id,
            {"sender_id": str(sender.id), "recipient": recipient},
        )
        self.session.commit()

    @staticmethod
    def _body_to_html(body: str) -> str:
        paragraphs = [
            f"<p>{line.strip()}</p>"
            for line in str(body).splitlines()
            if line.strip()
        ]
        return "".join(paragraphs) if paragraphs else f"<p>{body}</p>"
