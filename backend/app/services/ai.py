from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing import EVENT_AI_GENERATION, UsageService
from app.models import AIGeneration
from app.providers import AIGenerationRequest, AIProviderInterface
from app.providers.mock_ai import MockAIProvider
from app.schemas.ai import GenerateEmailRequest


class AIGenerationNotFoundError(LookupError):
    pass


class AIService:
    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None, provider: AIProviderInterface | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.provider = provider or MockAIProvider()

    def generate_email(self, payload: GenerateEmailRequest) -> AIGeneration:
        request = AIGenerationRequest(objective=payload.objective, audience=payload.audience, context=payload.context, service_product=payload.service_product, tone=payload.tone, language=payload.language, cta=payload.cta, sender=payload.sender, recipient={**payload.recipient, **payload.custom_values}, instruction=payload.instruction)
        result = self.provider.generate_email(request)
        generation = AIGeneration(tenant_id=self.tenant_id, created_by_id=self.actor_id, provider=self.provider.__class__.__name__, model=result.model, request_summary={"objective": payload.objective, "audience": payload.audience, "tone": payload.tone, "language": payload.language, "instruction": payload.instruction, "recipient_fields": sorted(payload.recipient)}, generated_output={"subject": result.subject, "body": result.body, "cta": result.cta, "personalization_suggestions": result.personalization_suggestions, "follow_up": result.follow_up}, validation_status="PENDING", workflow_status="DRAFT", prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens, total_tokens=result.prompt_tokens + result.completion_tokens, estimated_cost=Decimal(str(result.estimated_cost)), cost_currency="USD")
        self.session.add(generation)
        UsageService(self.session, self.tenant_id, self.actor_id).meter(
            EVENT_AI_GENERATION,
            resource_type="ai_generation",
            metadata={"tokens": result.prompt_tokens + result.completion_tokens},
        )
        self.session.commit()
        return generation

    def get(self, generation_id: UUID) -> AIGeneration:
        generation = self.session.scalar(select(AIGeneration).where(AIGeneration.id == generation_id, AIGeneration.tenant_id == self.tenant_id))
        if generation is None:
            raise AIGenerationNotFoundError("AI generation not found")
        return generation

    def transform(self, generation_id: UUID, instruction: str, tone: str | None = None, language: str | None = None) -> AIGeneration:
        generation = self.get(generation_id)
        summary = dict(generation.request_summary)
        summary["instruction"] = instruction
        if tone:
            summary["tone"] = tone
        if language:
            summary["language"] = language
        payload = GenerateEmailRequest(objective=str(summary.get("objective", "")), audience=str(summary.get("audience", "")), context="", service_product="", tone=str(summary.get("tone", "professional")), language=str(summary.get("language", "English")), cta=str(generation.generated_output.get("cta", "")), recipient={key: "" for key in generation.request_summary.get("recipient_fields", [])}, instruction=instruction)
        return self.generate_email(payload)
