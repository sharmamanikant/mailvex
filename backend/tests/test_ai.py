from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Tenant
from app.providers import AIGenerationRequest, MockAIProvider
from app.schemas.ai import GenerateEmailRequest
from app.services.ai import AIGenerationNotFoundError, AIService


@pytest.fixture()
def ai_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ai.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="AI Tenant", slug=f"ai-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def request() -> GenerateEmailRequest:
    return GenerateEmailRequest(objective="Discuss supplied Python service", audience="HR managers", context="Use only this context.", service_product="Python development", tone="professional", language="English", cta="Would you like the details?", sender={"sender_name": "Maya"}, recipient={"first_name": "Rajesh", "company": "ABC Technologies"})


def test_mock_provider_uses_supplied_facts(ai_session) -> None:
    session, tenant_id = ai_session
    generation = AIService(session, tenant_id).generate_email(request())
    assert generation.workflow_status == "DRAFT"
    assert generation.generated_output["subject"] == "Python development for ABC Technologies"
    assert "Rajesh" in generation.generated_output["body"]
    assert generation.total_tokens == 200
    assert generation.estimated_cost is not None


def test_generation_is_tenant_scoped(ai_session) -> None:
    session, tenant_id = ai_session
    generation = AIService(session, tenant_id).generate_email(request())
    with pytest.raises(AIGenerationNotFoundError):
        AIService(session, uuid4()).get(generation.id)


def test_mock_provider_contract() -> None:
    result = MockAIProvider().generate_email(AIGenerationRequest("objective", "audience", "context", "service", "professional", "English", "CTA", {"sender_name": "Maya"}, {"first_name": "Rajesh"}))
    assert result.subject
    assert result.body
    assert MockAIProvider().classify_reply("hello")["classification"] == "OTHER"
