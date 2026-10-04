from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.billing import EVENT_AI_GENERATION, UsageService
from app.core.config import Settings, settings
from app.models import (
    AIMessageDraft,
    Contact,
    EmailAccount,
    Template,
    UsageRecord,
)
from app.providers import AIGenerationRequest, AIGenerationResult, AIProviderInterface
from app.providers.mock_ai import MockAIProvider
from app.schemas.drafts import (
    DraftApproveRequest,
    DraftGenerateRequest,
    DraftRejectRequest,
    DraftSaveTemplateRequest,
    DraftTransformRequest,
    DraftUpdate,
    DraftUsageResponse,
)
from app.schemas.templates import BUILT_IN_VARIABLES, TemplateCreate
from app.services.audit import AuditService
from app.services.templates import CONTACT_FIELD_MAP, TemplateService

COST_METRIC = "ai_generation_cost_usd"
COUNT_METRIC = "ai_generation_count"


class AIDraftError(ValueError):
    pass


class AIDraftNotFoundError(LookupError):
    pass


class AIDraftCostLimitError(AIDraftError):
    pass


@dataclass(frozen=True)
class AICostLimits:
    monthly_budget_usd: Decimal
    monthly_generation_limit: int

    @classmethod
    def from_settings(cls, cfg: Settings) -> AICostLimits:
        return cls(
            monthly_budget_usd=Decimal(str(cfg.ai_monthly_budget_usd)),
            monthly_generation_limit=cfg.ai_monthly_generation_limit,
        )


_MODEL_FOR_PROVIDER: dict[type[AIProviderInterface], str] = {
    MockAIProvider: "mock-v1",
}


class AIDraftService:
    """Production AI Message Studio workflow.

    Objective -> recipient context -> AI generation -> draft -> human review ->
    approved content -> (campaign). Drafts are never sent directly. The only
    exit paths from a draft are approval (for campaign use) and, separately,
    save-as-template (for reuse as a versioned Template).
    """

    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        provider: AIProviderInterface | None = None,
        cost_limits: AICostLimits | None = None,
        request_timeout_ms: int | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.provider = provider or MockAIProvider()
        self.cost_limits = cost_limits or AICostLimits.from_settings(settings)
        self.request_timeout_ms = request_timeout_ms
        self.audit_service = AuditService(session, tenant_id, actor_id)

    # ------------------------------------------------------------------ read

    def _get(self, draft_id: UUID) -> AIMessageDraft:
        draft = self.session.scalar(
            select(AIMessageDraft).where(
                AIMessageDraft.id == draft_id,
                AIMessageDraft.tenant_id == self.tenant_id,
            )
        )
        if draft is None:
            raise AIDraftNotFoundError("AI message draft not found")
        return draft

    def get(self, draft_id: UUID) -> AIMessageDraft:
        return self._get(draft_id)

    def list_drafts(self, page: int, page_size: int) -> tuple[list[AIMessageDraft], int]:
        total = (
            self.session.scalar(
                select(func.count(AIMessageDraft.id)).where(
                    AIMessageDraft.tenant_id == self.tenant_id
                )
            )
            or 0
        )
        items = list(
            self.session.scalars(
                select(AIMessageDraft)
                .where(AIMessageDraft.tenant_id == self.tenant_id)
                .order_by(AIMessageDraft.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
        )
        return items, total

    # ------------------------------------------------------------------ create

    def generate(self, payload: DraftGenerateRequest) -> AIMessageDraft:
        self._enforce_cost_limits()
        sender = self._sender_values(payload.sender_id, payload.sender)
        recipient = self._recipient_values(payload.contact_id, payload.recipient, payload.custom_values)
        context = self._input_context(payload, sender, recipient)
        draft = AIMessageDraft(
            tenant_id=self.tenant_id,
            created_by_id=self.actor_id,
            contact_id=payload.contact_id,
            template_id=payload.template_id,
            objective=payload.objective,
            input_context=context,
            generated_subject="",
            generated_body="",
            generation_status="DRAFT",
            generation_type=payload.generation_type,
            tone=payload.tone,
            language=payload.language,
            desired_length=payload.desired_length,
            cta=payload.cta,
            provider=self.provider.__class__.__name__,
            generation_method="TEMPLATE_RENDER" if payload.template_id is not None else "PROVIDER",
        )
        self.session.add(draft)
        self.session.flush()
        self._audit("AI_GENERATION_CREATED", draft, {"action": "generate"})

        if payload.template_id is not None:
            self._generate_from_template(draft, payload, sender, recipient)
        else:
            UsageService(self.session, self.tenant_id, self.actor_id).meter(
                EVENT_AI_GENERATION,
                resource_type="ai_message_draft",
                resource_id=draft.id,
                metadata={"operation": "generate"},
            )
            request = self._request(draft, payload, sender, recipient, instruction="generate")
            self._run_provider(draft, self.provider.generate_email, request)
            self._finalize_status(draft)
        self.session.commit()
        return draft

    def transform(self, draft_id: UUID, payload: DraftTransformRequest) -> AIMessageDraft:
        draft = self._get(draft_id)
        if draft.generation_status == "APPROVED":
            raise AIDraftError("An approved draft is immutable; duplicate it to revise")
        if payload.action == "change_tone" and not payload.tone:
            raise AIDraftError("change_tone requires a tone")
        if payload.action == "translate" and not payload.language:
            raise AIDraftError("translate requires a language")
        self._enforce_cost_limits()

        ctx = dict(draft.input_context)
        sender = dict(ctx.get("sender") or {})
        recipient = dict(ctx.get("recipient") or {})
        desired_length = draft.desired_length
        if payload.action == "shorten":
            desired_length = "SHORT"
        elif payload.action == "expand":
            desired_length = "LONG"
        request = AIGenerationRequest(
            objective=draft.objective,
            audience=str(ctx.get("audience") or ""),
            context=str(ctx.get("context") or ""),
            service_product="",
            tone=payload.tone or draft.tone,
            language=payload.language or draft.language,
            cta=str(ctx.get("cta") or ""),
            sender=sender,
            recipient=recipient,
            instruction=payload.action,
            generation_type=draft.generation_type,
            desired_length=desired_length,
            product=str(ctx.get("product") or ""),
            service=str(ctx.get("service") or ""),
        )
        operation = {
            "regenerate": self.provider.generate_email,
            "improve": self.provider.improve_email,
            "change_tone": self.provider.change_tone,
            "translate": self.provider.translate_email,
            "shorten": self.provider.generate_email,
            "expand": self.provider.generate_email,
        }[payload.action]
        UsageService(self.session, self.tenant_id, self.actor_id).meter(
            EVENT_AI_GENERATION,
            resource_type="ai_message_draft",
            resource_id=draft.id,
            metadata={"operation": payload.action},
        )
        self._run_provider(draft, operation, request)
        self._finalize_status(draft)
        self._audit("AI_GENERATION_REGENERATED", draft, {"action": payload.action, "tone": request.tone, "language": request.language})
        self.session.commit()
        return draft

    # --------------------------------------------------------------- review

    def update(self, draft_id: UUID, payload: DraftUpdate) -> AIMessageDraft:
        draft = self._get(draft_id)
        if draft.generation_status == "APPROVED":
            raise AIDraftError("An approved draft is immutable; duplicate it to revise")
        values = payload.model_dump(exclude_unset=True)
        if "subject" in values:
            draft.generated_subject = str(values["subject"])
        if "body" in values:
            draft.generated_body = str(values["body"])
        draft.generation_status = "REVIEW_REQUIRED" if draft.warnings else "GENERATED"
        self.session.commit()
        return draft

    def approve(self, draft_id: UUID, payload: DraftApproveRequest) -> AIMessageDraft:
        draft = self._get(draft_id)
        if draft.generation_status not in {"GENERATED", "REVIEW_REQUIRED"}:
            raise AIDraftError("Only a generated draft can be approved")
        if not (draft.generated_subject.strip() and draft.generated_body.strip()):
            raise AIDraftError("Cannot approve a draft with empty content")
        draft.generation_status = "APPROVED"
        draft.approved_by_id = self.actor_id
        draft.approved_at = datetime.now(UTC)
        draft.reviewed_by_id = self.actor_id
        draft.reviewed_at = datetime.now(UTC)
        draft.review_note = payload.note
        self._audit("AI_GENERATION_APPROVED", draft, {"review_note": payload.note})
        self.session.commit()
        return draft

    def reject(self, draft_id: UUID, payload: DraftRejectRequest) -> AIMessageDraft:
        draft = self._get(draft_id)
        if draft.generation_status not in {"GENERATED", "REVIEW_REQUIRED"}:
            raise AIDraftError("Only a generated draft can be rejected")
        draft.generation_status = "REJECTED"
        draft.reviewed_by_id = self.actor_id
        draft.reviewed_at = datetime.now(UTC)
        draft.review_note = payload.note
        self._audit("AI_GENERATION_REJECTED", draft, {"review_note": payload.note})
        self.session.commit()
        return draft

    def save_as_template(self, draft_id: UUID, payload: DraftSaveTemplateRequest) -> AIMessageDraft:
        draft = self._get(draft_id)
        if draft.generation_status != "APPROVED":
            raise AIDraftError("Approval is required before saving as a template")
        template = TemplateService(self.session, self.tenant_id, self.actor_id).create(
            self._template_create(payload.template_name, draft)
        )
        draft.approved_template_id = template.id
        self.session.commit()
        return draft

    # ------------------------------------------------------------------ cost

    def usage(self) -> DraftUsageResponse:
        period = self._period_start()
        cost = self._usage_total(COST_METRIC, period)
        count = self._usage_total(COUNT_METRIC, period)
        return DraftUsageResponse(
            provider=self.provider.__class__.__name__,
            model=_MODEL_FOR_PROVIDER.get(type(self.provider)),
            monthly_generations=int(count),
            monthly_cost_usd=Decimal(str(cost)),
            monthly_budget_usd=self.cost_limits.monthly_budget_usd,
            monthly_generation_limit=self.cost_limits.monthly_generation_limit,
        )

    def _period_start(self) -> datetime:
        return datetime.now(UTC).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )

    def _usage_total(self, metric: str, period: datetime) -> Decimal:
        total = self.session.scalar(
            select(func.coalesce(func.sum(UsageRecord.quantity), 0)).where(
                UsageRecord.tenant_id == self.tenant_id,
                UsageRecord.metric == metric,
                UsageRecord.period_start == period,
            )
        )
        return Decimal(str(total or 0))

    def _increment_usage(self, metric: str, quantity: Decimal, period: datetime) -> None:
        record = self.session.scalar(
            select(UsageRecord).where(
                UsageRecord.tenant_id == self.tenant_id,
                UsageRecord.metric == metric,
                UsageRecord.period_start == period,
            )
        )
        if record is None:
            self.session.add(
                UsageRecord(
                    tenant_id=self.tenant_id,
                    metric=metric,
                    period_start=period,
                    quantity=quantity,
                    source="ai_message_studio",
                )
            )
        else:
            record.quantity = record.quantity + quantity

    def _enforce_cost_limits(self) -> None:
        period = self._period_start()
        cost = self._usage_total(COST_METRIC, period)
        count = self._usage_total(COUNT_METRIC, period)
        if self.cost_limits.monthly_budget_usd > 0 and cost >= self.cost_limits.monthly_budget_usd:
            raise AIDraftCostLimitError(
                f"Monthly AI budget reached (${self.cost_limits.monthly_budget_usd:g})"
            )
        if self.cost_limits.monthly_generation_limit > 0 and count >= Decimal(self.cost_limits.monthly_generation_limit):
            raise AIDraftCostLimitError(
                f"Monthly AI generation limit reached ({self.cost_limits.monthly_generation_limit})"
            )

    # -------------------------------------------------------------- internal

    def _run_provider(
        self,
        draft: AIMessageDraft,
        operation: Callable[[AIGenerationRequest], AIGenerationResult],
        request: AIGenerationRequest,
    ) -> None:
        started = time.monotonic()
        result: AIGenerationResult | None
        error: str | None
        try:
            generation = operation(request)
            result = generation
            error = None
        except AIDraftError:
            raise
        except Exception as exc:
            result = None
            error = str(exc)
        duration_ms = int((time.monotonic() - started) * 1000)
        draft.request_duration_ms = duration_ms
        if error is not None:
            draft.generation_status = "FAILED"
            draft.error_message = error[:1000]
            return
        if result is None or not (result.subject or "").strip() or not (result.body or "").strip():
            draft.generation_status = "FAILED"
            draft.error_message = "AI provider returned an empty or malformed response"
            return
        if self.request_timeout_ms is not None and duration_ms > self.request_timeout_ms:
            draft.generation_status = "FAILED"
            draft.error_message = f"AI request exceeded timeout of {self.request_timeout_ms} ms"
            return
        draft.generated_subject = result.subject
        draft.generated_body = result.body
        draft.cta = result.cta or request.cta
        draft.provider = self.provider.__class__.__name__
        draft.model = result.model
        draft.input_tokens = result.prompt_tokens
        draft.output_tokens = result.completion_tokens
        draft.total_tokens = result.prompt_tokens + result.completion_tokens
        draft.estimated_cost = Decimal(str(result.estimated_cost))
        draft.cost_currency = "USD"
        draft.error_message = None
        draft.warnings = list(result.warnings or [])
        if draft.estimated_cost is not None:
            self._increment_usage(COST_METRIC, draft.estimated_cost, self._period_start())
            self._increment_usage(COUNT_METRIC, Decimal("1"), self._period_start())

    def _finalize_status(self, draft: AIMessageDraft) -> None:
        if draft.generation_status != "FAILED":
            draft.generation_status = "REVIEW_REQUIRED" if draft.warnings else "GENERATED"

    def _generate_from_template(
        self,
        draft: AIMessageDraft,
        payload: DraftGenerateRequest,
        sender: dict[str, str],
        recipient: dict[str, str],
    ) -> None:
        template = self.session.scalar(
            select(Template)
            .options(selectinload(Template.versions))
            .where(Template.id == payload.template_id, Template.tenant_id == self.tenant_id)
        )
        if template is None:
            draft.generation_status = "FAILED"
            draft.error_message = "Source template not found"
            return
        version = max(template.versions, key=lambda item: item.version_number)
        values = {**recipient, **sender}
        rendered = TemplateService(self.session, self.tenant_id).render_template_values(
            version, values
        )
        draft.generated_subject = rendered["subject"]
        draft.generated_body = rendered["text_body"]
        draft.provider = "TEMPLATE_RENDER"
        draft.model = None
        draft.error_message = None
        draft.warnings = [
            f"Missing {name}"
            for name in TemplateService.variables(
                version.subject_template, version.html_body, rendered["text_body"]
            )
            if not (values.get(name) or "").strip()
        ]
        self._finalize_status(draft)

    def _request(
        self,
        draft: AIMessageDraft,
        payload: DraftGenerateRequest,
        sender: dict[str, str],
        recipient: dict[str, str],
        instruction: str,
    ) -> AIGenerationRequest:
        return AIGenerationRequest(
            objective=payload.objective,
            audience=payload.audience,
            context=payload.context,
            service_product="",
            tone=draft.tone,
            language=draft.language,
            cta=payload.cta,
            sender=sender,
            recipient=recipient,
            instruction=instruction,
            generation_type=draft.generation_type,
            desired_length=draft.desired_length,
            product=payload.product,
            service=payload.service,
        )

    @staticmethod
    def _input_context(
        payload: DraftGenerateRequest,
        sender: dict[str, str],
        recipient: dict[str, str],
    ) -> dict[str, object]:
        return {
            "audience": payload.audience,
            "context": payload.context,
            "service": payload.service,
            "product": payload.product,
            "cta": payload.cta,
            "generation_type": payload.generation_type,
            "desired_length": payload.desired_length,
            "sender": sender,
            "recipient": recipient,
        }

    def _recipient_values(
        self,
        contact_id: UUID | None,
        overrides: dict[str, str],
        custom_values: dict[str, str],
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        if contact_id is not None:
            contact = self.session.scalar(
                select(Contact)
                .options(selectinload(Contact.custom_fields))
                .where(Contact.id == contact_id, Contact.tenant_id == self.tenant_id)
            )
            if contact is None:
                raise AIDraftError("Recipient contact not found")
            for variable, attribute in CONTACT_FIELD_MAP.items():
                raw = getattr(contact, attribute)
                if raw is not None:
                    values[variable] = str(raw)
            parts = [values.get("first_name"), values.get("last_name")]
            full = " ".join(part for part in parts if part)
            if full:
                values["full_name"] = full
            for field in contact.custom_fields:
                if field.field_value is not None:
                    values.setdefault(field.field_key, field.field_value)
        values.update({key: value for key, value in overrides.items() if value})
        values.update({key: value for key, value in custom_values.items() if value})
        return values

    def _sender_values(self, sender_id: UUID | None, overrides: dict[str, str]) -> dict[str, str]:
        values: dict[str, str] = {}
        if sender_id is not None:
            account = self.session.scalar(
                select(EmailAccount)
                .options(selectinload(EmailAccount.profile))
                .where(EmailAccount.id == sender_id, EmailAccount.tenant_id == self.tenant_id)
            )
            if account is not None:
                values["sender_name"] = (account.display_name or "").strip()
                values["sender_email"] = (account.email or "").strip()
                profile = account.profile
                if profile is not None:
                    values["sender_company"] = (profile.company or "").strip()
                    values["sender_designation"] = (profile.designation or "").strip()
                    values["sender_phone"] = (profile.phone or "").strip()
                    values["sender_signature"] = (profile.signature or "").strip()
        values.update({key: value for key, value in overrides.items() if value})
        return values

    @staticmethod
    def _body_to_html(body: str) -> str:
        paragraphs = [f"<p>{line.strip()}</p>" for line in str(body).splitlines() if line.strip()]
        return "".join(paragraphs) if paragraphs else f"<p>{body}</p>"

    @staticmethod
    def _template_create(name: str, draft: AIMessageDraft) -> TemplateCreate:
        html_body = AIDraftService._body_to_html(draft.generated_body)
        used = TemplateService.variables(draft.generated_subject, html_body, draft.generated_body or "")
        custom = [item for item in used if item not in BUILT_IN_VARIABLES]
        return TemplateCreate(
            name=name,
            description=f"Approved from AI draft {draft.id}",
            subject_template=draft.generated_subject,
            html_body=html_body,
            text_body=draft.generated_body,
            custom_variables=custom,
        )

    def _audit(self, action: str, draft: AIMessageDraft, extra: dict[str, object] | None = None) -> None:
        self.audit_service.record(
            action,
            "ai_message_draft",
            draft.id,
            {
                "draft_id": str(draft.id),
                "generation_type": draft.generation_type,
                "tone": draft.tone,
                "language": draft.language,
                "desired_length": draft.desired_length,
                "provider": draft.provider,
                "model": draft.model,
                "tokens": draft.total_tokens,
                "estimated_cost": float(draft.estimated_cost or 0),
                "status": draft.generation_status,
                **(extra or {}),
            },
        )