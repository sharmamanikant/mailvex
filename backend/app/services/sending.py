from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.billing import EVENT_MESSAGE_SENT, UsageService
from app.core.config import settings
from app.email_providers import EmailMessage, get_provider
from app.email_providers.base import (
    EmailProviderError,
    ProviderConnectionConfig,
    ProviderErrorCode,
)
from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignSender,
    Contact,
    DeliveryJob,
    EmailAccount,
    Message,
    MessageEvent,
    ScheduledMessage,
    SenderAccount,
    SenderConnection,
    TemplateVersion,
)
from app.providers import ProviderMessage, SenderUnavailableError
from app.security.rate_limit import RateLimitService
from app.services.audit import AuditService
from app.services.compliance import ComplianceResult, ComplianceService
from app.services.sender_health import SenderHealthService
from app.services.sender_quotas import SenderQuotaService
from app.services.senders import SenderService
from app.services.templates import TemplateService

SENDER_DAILY_SEND_LIMIT = 500
SENDER_DAILY_WINDOW_SECONDS = 86400


class SendBlockedError(RuntimeError):
    def __init__(self, result: ComplianceResult) -> None:
        super().__init__("Message blocked by compliance")
        self.result = result


class SenderThrottledError(RuntimeError):
    """Raised when a sender exceeds a provider-aware rate limit.

    Carries retry_after so the worker defers (throttle backoff) instead of
    retrying immediately.
    """

    def __init__(self, retry_after: int = 3600, message: str = "Sender rate limit exceeded") -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SendingService:
    """Single provider-agnostic sending path. No caller may send around compliance."""

    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        provider_factory: Callable | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.provider_factory = provider_factory
        self.sender_service = SenderService(session, tenant_id)

    def _enforce_sender_limit(self, sender: EmailAccount) -> None:
        # Provider-aware per-sender cap. Redis unavailable => fail open so a
        # resilience blip never hard-blocks legitimate mail.
        key = f"crcrm:sender:{self.tenant_id}:{sender.id}:daily"
        result = RateLimitService(settings.redis_url, fail_open=True).check_limit(
            key, SENDER_DAILY_SEND_LIMIT, SENDER_DAILY_WINDOW_SECONDS
        )
        if not result.allowed:
            raise SenderThrottledError(retry_after=result.retry_after)

    def _list_unsubscribe_headers(self, campaign: Campaign) -> dict[str, str]:
        """One-click List-Unsubscribe headers (guardrails 5, 18, 24).

        Added server-side to every campaign send when a one-click unsubscribe
        URL is configured. The recipient's provider can then honor the request
        without button spam or manual flow.
        """
        unsubscribe_url = (campaign.schedule_config or {}).get("unsubscribe_url")
        if not unsubscribe_url:
            return {}
        return {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }

    def _refresh_sender_credentials(self, sender: EmailAccount) -> None:
        if sender.provider == "GOOGLE":
            # Best-effort; refresh_tokens persists encrypted tokens and handles
            # REAUTH internally.
            self.sender_service.refresh_tokens(sender)

    def send_scheduled(self, scheduled_id: UUID):
        scheduled = self.session.scalar(
            select(ScheduledMessage)
            .where(
                ScheduledMessage.id == scheduled_id,
                ScheduledMessage.tenant_id == self.tenant_id,
            )
            .with_for_update()
        )
        if scheduled is None:
            raise LookupError("Scheduled message not found")
        existing = self.session.scalar(
            select(Message).where(
                Message.tenant_id == self.tenant_id,
                Message.scheduled_message_id == scheduled.id,
            )
        )
        if existing is not None:
            return existing
        if scheduled.status not in {"QUEUED", "DEFERRED", "PROCESSING"}:
            raise RuntimeError("Scheduled message is not eligible for sending")
        if scheduled.status in {"QUEUED", "DEFERRED"}:
            scheduled.status = "PROCESSING"
            self.session.commit()
        campaign = self.session.scalar(
            select(Campaign).where(
                Campaign.id == scheduled.campaign_id,
                Campaign.tenant_id == self.tenant_id,
            )
        )
        recipient_link = self.session.scalar(
            select(CampaignRecipient).where(
                CampaignRecipient.id == scheduled.campaign_recipient_id,
                CampaignRecipient.tenant_id == self.tenant_id,
            )
        )
        if campaign is None or recipient_link is None:
            raise LookupError("Scheduled message references are invalid")
        contact = self.session.scalar(
            select(Contact).where(
                Contact.id == recipient_link.contact_id,
                Contact.tenant_id == self.tenant_id,
            )
        )
        sender = self.sender_service.get(campaign.sender_id)
        self._refresh_sender_credentials(sender)
        result = ComplianceService(self.session, self.tenant_id).check_recipient(
            campaign, sender, contact, recipient_link
        )
        if result.outcome == "BLOCK":
            scheduled.status = "FAILED"
            scheduled.failure_reason = "; ".join(
                check.message for check in result.failures
            )
            scheduled.processed_at = datetime.now(UTC)
            AuditService(self.session, self.tenant_id).record(
                "message_blocked",
                "scheduled_message",
                scheduled.id,
                {"checks": [check.__dict__ for check in result.failures]},
            )
            self.session.commit()
            raise SendBlockedError(result)
        template = (
            self.session.scalar(
                select(TemplateVersion).where(
                    TemplateVersion.id == campaign.template_version_id,
                    TemplateVersion.tenant_id == self.tenant_id,
                )
            )
            if campaign.template_version_id
            else None
        )
        if contact is None or template is None:
            raise LookupError("Recipient or template is unavailable")
        rendered = TemplateService(self.session, self.tenant_id).render(
            template.template_id,
            __import__(
                "app.schemas.templates", fromlist=["TemplatePreviewRequest"]
            ).TemplatePreviewRequest(
                recipient={
                    "first_name": contact.first_name or "",
                    "last_name": contact.last_name or "",
                    "company": contact.company or "",
                    "email": contact.email,
                },
                sender={
                    "sender_name": sender.display_name or sender.email,
                    "sender_company": "",
                    "sender_signature": "",
                },
                custom_values=recipient_link.rendered_data,
            ),
        )
        provider = (
            self.provider_factory(sender)
            if self.provider_factory
            else self.sender_service.provider(sender)
        )
        # Provider-aware rate limiting BEFORE hitting the network: if the
        # sender's daily budget is exhausted, the worker defers with backoff.
        self._enforce_sender_limit(sender)
        provider.connect()
        try:
            provider_result = provider.send(
                ProviderMessage(
                    contact.email,
                    rendered.subject,
                    rendered.html_body,
                    rendered.text_body,
                    sender.reply_to,
                    headers=self._list_unsubscribe_headers(campaign),
                )
            )
        except Exception:
            provider.disconnect()
            raise
        finally:
            provider.disconnect()
        self.sender_service.mark_used(sender)
        AuditService(self.session, self.tenant_id).record(
            "SENDER_SENT",
            "sender",
            sender.id,
            {"email": sender.email, "provider": sender.provider},
        )
        message = Message(
            tenant_id=self.tenant_id,
            scheduled_message_id=scheduled.id,
            campaign_id=campaign.id,
            sender_id=sender.id,
            contact_id=contact.id,
            provider_message_id=provider_result.provider_message_id,
            subject=rendered.subject,
            status="SENT",
        )
        message.events.append(
            MessageEvent(
                tenant_id=self.tenant_id,
                event_type="SENT",
                provider_event_id=provider_result.provider_message_id,
                provider_payload={"provider": provider_result.provider},
                occurred_at=provider_result.accepted_at,
            )
        )
        self.session.add(message)
        scheduled.status = "SENT"
        scheduled.processed_at = provider_result.accepted_at
        stats = dict(campaign.schedule_config.get("statistics", {}))
        stats["sent"] = int(stats.get("sent", 0)) + 1
        campaign.schedule_config = {**campaign.schedule_config, "statistics": stats}
        UsageService(self.session, self.tenant_id).record_event(
            EVENT_MESSAGE_SENT,
            resource_type="message",
            resource_id=message.id,
        )
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            return self.session.scalar(
                select(Message).where(
                    Message.tenant_id == self.tenant_id,
                    Message.scheduled_message_id == scheduled.id,
                )
            )
        return message

    def send_delivery_job(self, job_id: UUID) -> Message | None:
        """Send a claimed ``delivery_jobs`` row through the same compliance and
        provider path as scheduled messages. The job must already be PROCESSING
        (claimed) by the worker. Returns the created Message, or None if the
        campaign was cancelled while the job was in flight.
        """
        job = self.session.scalar(
            select(DeliveryJob)
            .where(
                DeliveryJob.id == job_id,
                DeliveryJob.tenant_id == self.tenant_id,
            )
            .with_for_update()
        )
        if job is None:
            raise LookupError("Delivery job not found")
        if job.status != "PROCESSING":
            raise RuntimeError("Delivery job is not claimed/processing")
        campaign = self.session.scalar(
            select(Campaign).where(
                Campaign.id == job.campaign_id,
                Campaign.tenant_id == self.tenant_id,
            )
        )
        if campaign is None:
            raise LookupError("Campaign for delivery job not found")
        if campaign.status == "CANCELLED":
            job.status = "CANCELLED"
            job.completed_at = datetime.now(UTC)
            self.session.commit()
            return None
        contact = self.session.scalar(
            select(Contact).where(
                Contact.id == job.recipient_id,
                Contact.tenant_id == self.tenant_id,
            )
        )
        if contact is None:
            raise LookupError("Recipient for delivery job not found")
        sender = self.sender_service.get(job.sender_id)
        self._refresh_sender_credentials(sender)
        recipient_link = self.session.scalar(
            select(CampaignRecipient).where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.contact_id == contact.id,
            )
        )
        if recipient_link is None:
            raise LookupError(
                "Campaign recipient link is missing for the delivery job"
            )
        result = ComplianceService(self.session, self.tenant_id).check_recipient(
            campaign, sender, contact, recipient_link
        )
        if result.outcome == "BLOCK":
            job.status = "BLOCKED"
            job.failure_code = "COMPLIANCE_BLOCKED"
            job.last_error = "; ".join(check.message for check in result.failures)
            job.completed_at = datetime.now(UTC)
            AuditService(self.session, self.tenant_id).record(
                "delivery_job_blocked",
                "delivery_job",
                job.id,
                {"checks": [check.__dict__ for check in result.failures]},
            )
            self.session.commit()
            raise SendBlockedError(result)
        template = (
            self.session.scalar(
                select(TemplateVersion).where(
                    TemplateVersion.id == campaign.template_version_id,
                    TemplateVersion.tenant_id == self.tenant_id,
                )
            )
            if campaign.template_version_id
            else None
        )
        if template is None:
            raise LookupError("Template is unavailable")
        rendered = TemplateService(self.session, self.tenant_id).render(
            template.template_id,
            __import__(
                "app.schemas.templates", fromlist=["TemplatePreviewRequest"]
            ).TemplatePreviewRequest(
                recipient={
                    "first_name": contact.first_name or "",
                    "last_name": contact.last_name or "",
                    "company": contact.company or "",
                    "email": contact.email,
                },
                sender={
                    "sender_name": sender.display_name or sender.email,
                    "sender_company": "",
                    "sender_signature": "",
                },
                custom_values=recipient_link.rendered_data if recipient_link else {},
            ),
        )
        if job.sender_account_id is not None:
            return self._send_delivery_job_system_b(
                job, campaign, contact, sender, recipient_link, rendered
            )
        provider = (
            self.provider_factory(sender)
            if self.provider_factory
            else self.sender_service.provider(sender)
        )
        self._enforce_sender_limit(sender)
        provider.connect()
        try:
            provider_result = provider.send(
                ProviderMessage(
                    contact.email,
                    rendered.subject,
                    rendered.html_body,
                    rendered.text_body,
                    sender.reply_to,
                    headers=self._list_unsubscribe_headers(campaign),
                )
            )
        except Exception:
            provider.disconnect()
            raise
        finally:
            provider.disconnect()
        self.sender_service.mark_used(sender)
        message = Message(
            tenant_id=self.tenant_id,
            campaign_id=campaign.id,
            sender_id=sender.id,
            contact_id=contact.id,
            provider_message_id=provider_result.provider_message_id,
            subject=rendered.subject,
            status="SENT",
        )
        message.events.append(
            MessageEvent(
                tenant_id=self.tenant_id,
                event_type="SENT",
                provider_event_id=provider_result.provider_message_id,
                provider_payload={"provider": provider_result.provider},
                occurred_at=provider_result.accepted_at,
            )
        )
        self.session.add(message)
        stats = dict(campaign.schedule_config.get("statistics", {}))
        stats["sent"] = int(stats.get("sent", 0)) + 1
        campaign.schedule_config = {**campaign.schedule_config, "statistics": stats}
        UsageService(self.session, self.tenant_id).record_event(
            EVENT_MESSAGE_SENT,
            resource_type="message",
            resource_id=message.id,
        )
        self.session.commit()
        return message

    def _send_delivery_job_system_b(
        self,
        job: DeliveryJob,
        campaign: Campaign,
        contact: Contact,
        sender: EmailAccount,
        recipient_link: CampaignRecipient,
        rendered: Any,
    ) -> Message:
        """Send a job through a System B ``SenderAccount`` providers.

        Called when ``DeliveryJob.sender_account_id`` is set (campaign sender
        pool). Compliance and template rendering run through the same pipeline
        as the legacy path; quota, telemetry, and health are System B-native.
        """
        account = self.session.scalar(
            select(SenderAccount).where(
                SenderAccount.id == job.sender_account_id,
                SenderAccount.tenant_id == self.tenant_id,
            )
        )
        if account is None:
            raise LookupError("System B sender account not found")
        if (
            account.status != "ACTIVE"
            or not account.campaign_enabled
            or account.health_status in {"FAILED", "REAUTH_REQUIRED"}
        ):
            raise SenderUnavailableError(
                f"Sender health is {account.health_status}; cannot send campaign traffic"
            )
        connection = self.session.scalar(
            select(SenderConnection).where(
                SenderConnection.id == account.connection_id,
                SenderConnection.tenant_id == self.tenant_id,
            )
        )
        if connection is None or connection.status in {"FAILED", "DISABLED", "DISCONNECTED"}:
            raise SenderUnavailableError("Sender connection is not usable")
        if connection.status == "REAUTH_REQUIRED":
            raise SenderUnavailableError("Sender requires reauthentication")

        override = self.session.scalar(
            select(CampaignSender).where(
                CampaignSender.tenant_id == self.tenant_id,
                CampaignSender.campaign_id == campaign.id,
                CampaignSender.sender_id == account.id,
            )
        )
        limit = override.daily_limit if override and override.daily_limit else account.daily_campaign_limit
        SenderQuotaService(RateLimitService(settings.redis_url, fail_open=False)).reserve_campaign(
            account, limit
        )

        provider = get_provider(connection.provider)
        is_google = connection.provider.upper() == "GOOGLE"
        message_id = f"<{uuid4()}@{(connection.email or account.email).split('@')[-1]}>" if is_google else None
        message = EmailMessage(
            from_email=connection.email or account.email,
            to=(contact.email,),
            subject=rendered.subject,
            text_body=rendered.text_body,
            html_body=rendered.html_body,
        )
        headers = self._list_unsubscribe_headers(campaign)
        if message_id is not None:
            headers.setdefault("Message-Id", message_id)
        if headers:
            message = replace(message, headers={**(message.headers or {}), **headers})
        try:
            provider_message_id = provider.send_message(
                ProviderConnectionConfig(
                    connection_type=connection.connection_type,
                    external_account_id=connection.external_account_id,
                    email=connection.email,
                    metadata=dict(connection.connection_metadata or {}),
                    credential_reference=connection.credential_reference,
                    credential_version=connection.credential_version,
                    credential_expires_at=connection.credential_expires_at,
                ),
                message,
            )
        except EmailProviderError as exc:
            SenderHealthService(self.session, self.tenant_id).record_send_outcome(
                account.id, success=False, error_code=exc.code.value
            )
            self._raise_mapped_system_b_failure(exc)
        except Exception as exc:
            SenderHealthService(self.session, self.tenant_id).record_send_outcome(
                account.id, success=False, error_code="PROVIDER_UNAVAILABLE"
            )
            raise SenderUnavailableError("The System B provider could not send the message") from exc

        SenderHealthService(self.session, self.tenant_id).record_send_outcome(
            account.id, success=True, message_id=provider_message_id
        )
        message_row = Message(
            tenant_id=self.tenant_id,
            campaign_id=campaign.id,
            sender_id=sender.id,
            contact_id=contact.id,
            provider_message_id=provider_message_id,
            message_id=message_id.strip("<>") if message_id else None,
            subject=rendered.subject,
            status="SENT",
        )
        message_row.events.append(
            MessageEvent(
                tenant_id=self.tenant_id,
                event_type="SENT",
                provider_event_id=provider_message_id,
                provider_payload={"provider": connection.provider},
                occurred_at=datetime.now(UTC),
            )
        )
        self.session.add(message_row)
        stats = dict(campaign.schedule_config.get("statistics", {}))
        stats["sent"] = int(stats.get("sent", 0)) + 1
        campaign.schedule_config = {**campaign.schedule_config, "statistics": stats}
        UsageService(self.session, self.tenant_id).record_event(
            EVENT_MESSAGE_SENT,
            resource_type="message",
            resource_id=message_row.id,
        )
        self.session.commit()
        return message_row

    def _raise_mapped_system_b_failure(self, exc: EmailProviderError) -> None:
        """Map a normalized provider error onto the worker's retry taxonomy."""
        if exc.code in {
            ProviderErrorCode.RATE_LIMITED,
            ProviderErrorCode.QUOTA_EXCEEDED,
        }:
            raise SenderThrottledError(exc.retry_after or 300, exc.message) from exc
        if exc.code == ProviderErrorCode.PROVIDER_UNAVAILABLE:
            raise TimeoutError(exc.message) from exc
        raise SenderUnavailableError(exc.message) from exc
