from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing import EVENT_AI_GENERATION, UsageService
from app.models import AIReplyDraft, Contact, EmailMessage, EmailThread
from app.providers import AIProviderInterface
from app.providers.factory import build_ai_provider
from app.services.ai_safety import (
    decode_warnings,
    detect_prompt_injection,
    detect_unsubscribe,
    encode_warnings,
    intent_is_unsubscribe,
    is_suppressed,
)
from app.services.audit import AuditService
from app.services.senders import SenderService

DRAFT_OPERATIONS = ("PROFESSIONAL", "SHORTEN", "EXPAND", "CHANGE_TONE", "TRANSLATE")


class AIReplyAssistantError(ValueError):
    pass


class AIReplyAssistantNotFoundError(LookupError):
    pass


class AIReplyUnsubscribeError(AIReplyAssistantError):
    """An unsubscribe request requires explicit human confirmation."""


class AIReplyAssistant:
    """Phase 18 AI Reply Assistant.

    The assistant ANALYZES and DRAFTS. It NEVER sends. A reply only leaves the
    system after a human reviews, edits, approves, and explicitly sends it via
    ``InboxReplyService.send``. Inbound email is always treated as untrusted
    content — instructions inside it never override application rules, and no
    AI-generated reply can override suppression rules.
    """

    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        ai_provider: AIProviderInterface | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.ai = ai_provider or build_ai_provider()
        self.audit = AuditService(session, tenant_id, actor_id)
        self.sender_service = SenderService(session, tenant_id)

    # ------------------------------------------------------------ helpers

    def _thread(self, thread_id: UUID) -> EmailThread:
        thread = self.session.scalar(
            select(EmailThread).where(
                EmailThread.id == thread_id,
                EmailThread.tenant_id == self.tenant_id,
            )
        )
        if thread is None:
            raise AIReplyAssistantNotFoundError("Inbox thread not found")
        return thread

    def _draft(self, draft_id: UUID) -> AIReplyDraft:
        draft = self.session.scalar(
            select(AIReplyDraft).where(
                AIReplyDraft.id == draft_id,
                AIReplyDraft.tenant_id == self.tenant_id,
            )
        )
        if draft is None:
            raise AIReplyAssistantNotFoundError("AI reply draft not found")
        return draft

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
        raise AIReplyAssistantError("Cannot determine the reply recipient for this thread")

    def _inbound_text(self, thread: EmailThread) -> str:
        last = self._last_inbound(thread)
        return (last.body_text if last is not None else "") or ""

    # ------------------------------------------------------------- analyze

    def analyze(self, thread_id: UUID) -> dict[str, object]:
        """Classify intent and surface safety signals. Never sends anything."""
        thread = self._thread(thread_id)
        text = self._inbound_text(thread)
        recipient = self._recipient_email(thread)

        injection = detect_prompt_injection(f"{thread.subject or ''}\n{text}")
        classification = self.ai.classify_intent(text, thread.subject or "")
        intent = str(classification.get("intent") or "UNKNOWN").upper()
        confidence = float(classification.get("confidence") or 0.0)
        provider_warnings = [str(w) for w in classification.get("warnings") or []]
        unsubscribe = bool(classification.get("is_unsubscribe")) or detect_unsubscribe(text)

        warnings: list[str] = list(provider_warnings)
        if unsubscribe:
            warnings.insert(0, "Possible unsubscribe request detected.")
        warnings.extend(injection)
        suppressed = is_suppressed(self.session, self.tenant_id, recipient)
        if suppressed:
            warnings.append(
                "Recipient is suppressed; an AI-generated reply can never override this."
            )

        self.audit.record(
            "AI_INTENT_CLASSIFIED",
            "email_thread",
            thread.id,
            {
                "intent": intent,
                "confidence": confidence,
                "unsubscribe": unsubscribe,
                "suppressed": suppressed,
                "warnings": warnings,
            },
        )
        self.session.commit()
        return {
            "thread_id": str(thread.id),
            "intent": intent,
            "confidence": confidence,
            "unsubscribe": unsubscribe,
            "suppressed": suppressed,
            "requires_confirmation": bool(unsubscribe),
            "warnings": warnings,
        }

    # --------------------------------------------------------------- draft

    def draft(self, thread_id: UUID, *, confirm_unsubscribe: bool = False) -> AIReplyDraft:
        thread = self._thread(thread_id)
        text = self._inbound_text(thread)
        subject = "Re: " + (thread.subject or "")
        if subject.startswith("Re: Re: "):
            subject = subject[len("Re: ") :]

        classification = self.ai.classify_intent(text, thread.subject or "")
        intent = str(classification.get("intent") or "UNKNOWN").upper()
        confidence = float(classification.get("confidence") or 0.0)
        unsubscribe = intent_is_unsubscribe(intent) or detect_unsubscribe(text)

        # Unsubscribe: ALWAYS require explicit human confirmation. Until then,
        # we refuse to generate a body so nothing irreversible is committed.
        if unsubscribe and not confirm_unsubscribe:
            raise AIReplyUnsubscribeError(
                "Possible unsubscribe request detected. Confirm you want to "
                "acknowledge it before generating a reply."
            )

        context = {
            "classification": intent,
            "thread_subject": thread.subject or "",
            "contact_email": self._recipient_email(thread),
        }
        body = ""
        error: str | None = None
        try:
            body = self.ai.generate_reply(text, context) or ""
        except Exception as exc:  # pragma: no cover - provider failure path
            error = str(exc)[:500]

        warnings: list[str] = []
        warnings.extend(detect_prompt_injection(f"{thread.subject or ''}\n{text}"))
        if unsubscribe:
            warnings.insert(0, "Possible unsubscribe request detected.")

        draft = AIReplyDraft(
            tenant_id=self.tenant_id,
            thread_id=thread.id,
            created_by_id=self.actor_id,
            subject=subject,
            body=body,
            status="FAILED" if error else "DRAFT",
            provider=self.ai.__class__.__name__,
            operation="DRAFT",
            intent=intent,
            intent_confidence=confidence,
            warnings=encode_warnings(warnings),
        )
        self.session.add(draft)
        self.session.flush()
        self.audit.record(
            "AI_REPLY_GENERATED",
            "email_thread",
            thread.id,
            {
                "draft_id": str(draft.id),
                "operation": "DRAFT",
                "intent": intent,
                "confidence": confidence,
                "unsubscribe": unsubscribe,
                "provider": draft.provider,
                "error": error,
            },
        )
        if error is None:
            UsageService(self.session, self.tenant_id, self.actor_id).meter(
                EVENT_AI_GENERATION,
                resource_type="ai_reply_draft",
                resource_id=draft.id,
                metadata={"operation": "DRAFT"},
            )
        self.session.commit()
        return draft

    # ------------------------------------------- summarize / next action

    def summarize(self, thread_id: UUID) -> AIReplyDraft:
        thread = self._thread(thread_id)
        text = self._inbound_text(thread)
        summary = self.ai.summarize_thread(text, thread.subject or "")
        record = self._assistant_output(thread, "SUMMARIZE", summary)
        return record

    def next_action(self, thread_id: UUID) -> AIReplyDraft:
        thread = self._thread(thread_id)
        text = self._inbound_text(thread)
        classification = self.ai.classify_intent(text, thread.subject or "")
        intent = str(classification.get("intent") or "UNKNOWN").upper()
        action = self.ai.suggest_next_action(text, intent)
        record = self._assistant_output(
            thread, "NEXT_ACTION", action, intent=intent
        )
        return record

    def _assistant_output(
        self,
        thread: EmailThread,
        operation: str,
        text: str,
        *,
        intent: str | None = None,
    ) -> AIReplyDraft:
        record = AIReplyDraft(
            tenant_id=self.tenant_id,
            thread_id=thread.id,
            created_by_id=self.actor_id,
            subject="",
            body="",
            status="DRAFT",
            provider=self.ai.__class__.__name__,
            operation=operation,
            intent=intent,
            summary=text if operation == "SUMMARIZE" else None,
            next_action=text if operation == "NEXT_ACTION" else None,
        )
        self.session.add(record)
        self.session.flush()
        self.audit.record(
            "AI_REPLY_GENERATED",
            "email_thread",
            thread.id,
            {
                "record_id": str(record.id),
                "operation": operation,
                "provider": record.provider,
            },
        )
        UsageService(self.session, self.tenant_id, self.actor_id).meter(
            EVENT_AI_GENERATION,
            resource_type="ai_reply_draft",
            resource_id=record.id,
            metadata={"operation": operation},
        )
        self.session.commit()
        return record

    # --------------------------------------------------------- transforms

    def transform(
        self,
        draft_id: UUID,
        operation: str,
        *,
        tone: str | None = None,
        language: str | None = None,
    ) -> AIReplyDraft:
        operation = operation.upper()
        if operation not in DRAFT_OPERATIONS:
            raise AIReplyAssistantError(f"Unsupported assistant operation '{operation}'")
        source = self._draft(draft_id)
        if not source.body.strip():
            raise AIReplyAssistantError("There is no draft body to transform")

        if operation == "PROFESSIONAL":
            body = self.ai.professionalize(source.body)
        elif operation == "SHORTEN":
            body = self.ai.shorten(source.body)
        elif operation == "EXPAND":
            body = self.ai.expand(source.body)
        elif operation == "CHANGE_TONE":
            body = self.ai.change_draft_tone(source.body, tone or "NEUTRAL")
        else:  # TRANSLATE
            body = self.ai.translate(source.body, language or "en")

        draft = AIReplyDraft(
            tenant_id=self.tenant_id,
            thread_id=source.thread_id,
            created_by_id=self.actor_id,
            subject=source.subject,
            body=body,
            status="DRAFT",
            provider=self.ai.__class__.__name__,
            operation=operation,
            intent=source.intent,
            intent_confidence=source.intent_confidence,
            warnings=source.warnings,
            tone=tone if operation == "CHANGE_TONE" else None,
            language=language if operation == "TRANSLATE" else None,
            source_draft_id=source.id,
        )
        self.session.add(draft)
        self.session.flush()
        self.audit.record(
            "AI_REPLY_GENERATED",
            "ai_reply_draft",
            source.id,
            {
                "result_id": str(draft.id),
                "operation": operation,
                "tone": tone,
                "language": language,
            },
        )
        UsageService(self.session, self.tenant_id, self.actor_id).meter(
            EVENT_AI_GENERATION,
            resource_type="ai_reply_draft",
            resource_id=draft.id,
            metadata={"operation": operation},
        )
        self.session.commit()
        return draft

    # ------------------------------------------------------------- reject

    def reject(self, draft_id: UUID) -> AIReplyDraft:
        draft = self._draft(draft_id)
        if draft.status == "SENT":
            raise AIReplyAssistantError("A sent draft cannot be rejected")
        if draft.status != "REJECTED":
            draft.status = "REJECTED"
            draft.rejected_by_id = self.actor_id
            draft.rejected_at = datetime.now(UTC)
        self.audit.record(
            "AI_REPLY_REJECTED",
            "ai_reply_draft",
            draft.id,
            {"thread_id": str(draft.thread_id)},
        )
        self.session.commit()
        return draft

    # ------------------------------------------------------------ decode

    @staticmethod
    def warnings_of(draft: AIReplyDraft) -> list[str]:
        return decode_warnings(draft.warnings)
