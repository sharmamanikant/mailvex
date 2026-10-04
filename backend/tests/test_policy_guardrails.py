from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, SenderAccount, SenderConnection, Tenant, User
from app.services.compliance_profile import ComplianceProfileService
from app.services.compliance_status import (
    BLOCKED,
    COMPLIANT,
    normalize_reason,
)
from app.services.policies import PolicyService
from app.services.safety import HIGH_BOUNCE_RATE, SenderSafetyService


@pytest.fixture()
def policy_session(tmp_path: Any) -> Any:
    engine = create_engine(f"sqlite:///{tmp_path / 'policy-guardrails.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Policy Tenant", slug=f"policy-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        user = User(
            tenant_id=tenant.id,
            email="owner@example.com",
            display_name="Owner",
        )
        session.add(user)
        session.commit()
        yield session, tenant.id, user.id
    engine.dispose()


def test_policy_status_requires_acceptance_and_records_current_version(policy_session: Any) -> None:
    session, tenant_id, user_id = policy_session
    service = PolicyService(session, tenant_id, user_id)

    status = service.status(user_id)
    assert {item["policy_type"] for item in status} == {"acceptable_use", "terms_of_service"}
    assert all(not item["accepted"] for item in status)

    document = service.current_document("terms_of_service")
    acceptance = service.accept("terms_of_service", document.policy_version, "192.0.2.10")

    assert acceptance.policy_version == document.policy_version
    assert service.is_accepted("terms_of_service", user_id)
    assert not service.is_accepted("acceptable_use", user_id)
    assert acceptance.ip_address == "192.0.2.10"


def test_compliance_profile_merges_defaults_and_rejects_unsafe_thresholds(policy_session: Any) -> None:
    session, tenant_id, user_id = policy_session
    service = ComplianceProfileService(session, tenant_id, user_id)

    profile = service.get()
    assert profile.safety_thresholds["window_days"] == 7
    updated = service.update(
        jurisdiction="US-CA",
        require_policy_acceptance=True,
        safety_thresholds={"complaint_rate": 0.02},
    )

    assert updated.jurisdiction == "US-CA"
    assert updated.require_policy_acceptance is True
    assert updated.safety_thresholds["complaint_rate"] == 0.02
    assert updated.safety_thresholds["window_days"] == 7
    assert ComplianceProfileService.unsafe_threshold("complaint_rate", 0.0)
    assert ComplianceProfileService.unsafe_threshold("window_days", 0)
    assert not ComplianceProfileService.unsafe_threshold("complaint_rate", 0.01)


def test_safety_apply_pauses_sender_until_review(policy_session: Any) -> None:
    session, tenant_id, user_id = policy_session
    connection = SenderConnection(
        tenant_id=tenant_id,
        provider="SMTP",
        connection_type="SMTP",
        status="ACTIVE",
    )
    sender = SenderAccount(
        tenant_id=tenant_id,
        connection=connection,
        email="sender@example.com",
        provider="SMTP",
        status="ACTIVE",
    )
    session.add(sender)
    session.flush()

    service = SenderSafetyService(session, tenant_id, user_id)
    service.apply(sender, "REVIEW_REQUIRED", [HIGH_BOUNCE_RATE])

    assert sender.compliance_status == "REVIEW_REQUIRED"
    assert sender.paused_at is not None
    assert sender.resume_guard is True


def test_status_reason_normalization_is_stable() -> None:
    assert normalize_reason([
        "CAMPAIGN_BLOCKED_POLICY_ACCEPTANCE",
        "CAMPAIGN_BLOCKED_POLICY_ACCEPTANCE",
        "CUSTOM_CHECK",
    ]) == ["TENANT_POLICY_NOT_ACCEPTED", "CUSTOM_CHECK"]
    assert BLOCKED != COMPLIANT
