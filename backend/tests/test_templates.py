from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Contact,
    ContactCustomField,
    EmailAccount,
    SenderProfile,
    Template,
    Tenant,
)
from app.schemas.drafts import (
    DraftApproveRequest,
    DraftGenerateRequest,
    DraftRejectRequest,
    DraftSaveTemplateRequest,
    DraftTransformRequest,
    DraftUpdate,
)
from app.schemas.templates import (
    BUILT_IN_VARIABLES,
    SENDER_VARIABLES,
    TemplateCreate,
    TemplatePreviewRequest,
    TemplateRecipientPreviewRequest,
    TemplateUpdate,
)
from app.services.ai_drafts import AIDraftError, AIDraftNotFoundError, AIDraftService
from app.services.templates import (
    RecipientNotFoundError,
    TemplateError,
    TemplateNotFoundError,
    TemplateService,
)


@pytest.fixture()
def template_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'templates.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Template Tenant", slug=f"templates-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def create_payload() -> TemplateCreate:
    return TemplateCreate(
        name="Intro",
        subject_template="Hello {{first_name}}",
        html_body="<p>Hello {{first_name}} at {{company}}</p>",
        text_body=None,
    )


def _service(session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> TemplateService:
    return TemplateService(session, tenant_id, actor_id)


# ---------------------------------------------------------------------------
# Variable replacement + missing values
# ---------------------------------------------------------------------------


def test_built_in_variable_registry(template_session) -> None:
    assert "first_name" in BUILT_IN_VARIABLES
    assert "email" in BUILT_IN_VARIABLES
    assert "technology" in BUILT_IN_VARIABLES
    assert "availability" in BUILT_IN_VARIABLES
    assert set(SENDER_VARIABLES) <= BUILT_IN_VARIABLES
    assert "sender_email" in BUILT_IN_VARIABLES
    registry = TemplateService.variable_registry()
    assert "email" in registry["recipient"]
    assert "sender_signature" in registry["sender"]


def test_replaces_all_builtin_variables(template_session) -> None:
    session, tenant_id = template_session
    payload = TemplateCreate(
        name="Full",
        subject_template="Hello {{first_name}} {{last_name}} at {{company}}",
        html_body="<p>{{first_name}} {{company}} {{designation}} {{location}} {{industry}} "
        "{{skills}} {{job_title}} {{experience}} {{requirement}} {{service}} "
        "{{service_area}} {{technology}} {{availability}} {{sender_name}} "
        "{{sender_email}} {{sender_company}}</p>",
        text_body=None,
    )
    template = _service(session, tenant_id).create(payload)
    rendered = _service(session, tenant_id).render(
        template.id,
        TemplatePreviewRequest(
            recipient={
                "first_name": "Rajesh",
                "last_name": "Kumar",
                "company": "ABC",
                "designation": "CTO",
                "location": "Pune",
                "industry": "SaaS",
                "skills": "Python",
                "job_title": "CTO",
                "experience": "10 years",
                "requirement": "infrastructure",
                "service": "cloud",
                "service_area": "APAC",
                "technology": "Kubernetes",
                "availability": "Monday",
            },
            sender={
                "sender_name": "Manikant",
                "sender_email": "m@acme.io",
                "sender_company": "Acme",
            },
        ),
    )
    assert rendered.missing_variables == []
    assert rendered.warnings == []
    assert "Rajesh Kumar" in rendered.subject
    assert "Manikant" in rendered.html_body
    assert "m@acme.io" in rendered.html_body


def test_missing_values_are_listed_not_fabricated(template_session) -> None:
    session, tenant_id = template_session
    template = _service(session, tenant_id).create(create_payload())
    rendered = _service(session, tenant_id).render(
        template.id, TemplatePreviewRequest(recipient={"first_name": "Rajesh"})
    )
    assert "company" in rendered.missing_variables
    assert any("Missing" in item for item in rendered.warnings)
    assert rendered.subject == "Hello Rajesh"


def test_missing_fields_are_not_filled_in(template_session) -> None:
    session, tenant_id = template_session
    payload = TemplateCreate(
        name="Hawk",
        subject_template="Dear {{first_name}}, {{company}}",
        html_body="<p>Dear {{first_name}}, {{company}}</p>",
        custom_variables=["service_area"],
    )
    template = _service(session, tenant_id).create(payload)
    rendered = _service(session, tenant_id).render(
        template.id, TemplatePreviewRequest(recipient={"first_name": "Ravi"})
    )
    assert "company" in rendered.missing_variables
    assert "Ravi" in rendered.subject


# ---------------------------------------------------------------------------
# Special characters, escaping, HTML sanitization
# ---------------------------------------------------------------------------


def test_special_characters_roundtrip(template_session) -> None:
    session, tenant_id = template_session
    template = _service(session, tenant_id).create(create_payload())
    rendered = _service(session, tenant_id).render(
        template.id,
        TemplatePreviewRequest(
            recipient={"first_name": "José", "company": "O'Hara & Sons"}
        ),
    )
    assert "José" in rendered.html_body
    assert "&amp;" in rendered.html_body


def test_html_values_are_escaped(template_session) -> None:
    session, tenant_id = template_session
    template = _service(session, tenant_id).create(create_payload())
    rendered = _service(session, tenant_id).render(
        template.id,
        TemplatePreviewRequest(
            recipient={"first_name": "<script>alert(1)</script>", "company": "A&B"}
        ),
    )
    assert "<script>" not in rendered.html_body
    assert "&amp;" in rendered.html_body


def test_script_and_event_handlers_stripped_from_template(template_session) -> None:
    session, tenant_id = template_session
    payload = TemplateCreate(
        name="Danger",
        subject_template="Hi {{first_name}}",
        html_body="<p onclick=\"alert('x')\">Hi</p><script>evil()</script><style>bad{}</style>"
        "<a href=\"javascript:alert(1)\">link</a><img src=\"x\" onerror=\"alert(1)\">",
    )
    template = _service(session, tenant_id).create(payload)
    assert "<script>" not in template.versions[0].html_body
    assert "onclick" not in template.versions[0].html_body
    assert "javascript:" not in template.versions[0].html_body
    assert "style" not in template.versions[0].html_body


def test_javascript_url_filtered_on_render(template_session) -> None:
    session, tenant_id = template_session
    payload = TemplateCreate(
        name="LinkVar",
        subject_template="Hi {{first_name}}",
        html_body='<p>Hi <a href="{{website}}">site</a></p>',
    )
    template = _service(session, tenant_id).create(payload)
    rendered = _service(session, tenant_id).render(
        template.id,
        TemplatePreviewRequest(recipient={"first_name": "Ravi", "website": "javascript:alert(1)"}),
    )
    assert "javascript:" not in rendered.html_body


def test_sanitized_html_is_stored(template_session) -> None:
    session, tenant_id = template_session
    payload = TemplateCreate(
        name="Clean",
        subject_template="Hi",
        html_body="<p>Safe</p><script>bad()</script>",
    )
    template = _service(session, tenant_id).create(payload)
    assert "bad" not in template.versions[0].html_body


def test_variable_values_are_never_executed(template_session) -> None:
    session, tenant_id = template_session
    payload = TemplateCreate(
        name="Injection",
        subject_template="{{first_name}}",
        html_body="<p>{{first_name}}</p>",
    )
    template = _service(session, tenant_id).create(payload)
    code = "{{__import__('os').system('rm -rf')}}"
    rendered = _service(session, tenant_id).render(
        template.id,
        TemplatePreviewRequest(recipient={"first_name": code}),
    )
    assert "<script" not in rendered.html_body
    assert "__import__" in rendered.html_body
    event_value = '<img src=x onerror="alert(1)">'
    with_event = _service(session, tenant_id).render(
        template.id,
        TemplatePreviewRequest(recipient={"first_name": event_value}),
    )
    assert "<img" not in with_event.html_body
    assert "&lt;img" in with_event.html_body
    assert "<script" not in with_event.html_body


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------


def test_history_versions_are_immutable(template_session) -> None:
    session, tenant_id = template_session
    service = _service(session, tenant_id)
    template = service.create(create_payload())
    v1_body = template.versions[0].html_body
    updated = service.update(template.id, TemplateUpdate(html_body="<p>Updated {{first_name}}</p>"))
    versions = sorted(updated.versions, key=lambda version: version.version_number)
    assert len(versions) == 2
    historical = service.get_version(template.id, 1)
    assert historical.html_body == v1_body
    latest = service.get_version(template.id, 2)
    assert "Updated" in latest.html_body
    assert latest.status == "DRAFT"


def test_update_never_mutates_previous_version(template_session) -> None:
    session, tenant_id = template_session
    service = _service(session, tenant_id)
    template = service.create(create_payload())
    first = template.versions[0].html_body
    for _ in range(3):
        template = service.update(template.id, TemplateUpdate(html_body="<p>v {first_name}</p>" * 1))
    versions = sorted(template.versions, key=lambda version: version.version_number)
    assert len(versions) == 4
    assert versions[0].html_body == first
    assert versions[1].subject_template == versions[0].subject_template


def test_every_edit_creates_new_version_and_actor(template_session) -> None:
    session, tenant_id = template_session
    template = _service(session, tenant_id, actor_id=uuid4()).create(create_payload())
    assert template.versions[0].created_by_id is not None
    rendered = _service(session, tenant_id, actor_id=uuid4()).update(
        template.id, TemplateUpdate(subject_template="Hi {{first_name}} {{last_name}}")
    )
    assert len(rendered.versions) == 2


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_tenant_isolation(template_session) -> None:
    session, tenant_id = template_session
    template = _service(session, tenant_id).create(create_payload())
    other = _service(session, uuid4())
    with pytest.raises(TemplateNotFoundError):
        other.get(template.id)
    with pytest.raises(TemplateNotFoundError):
        other.render(template.id, TemplatePreviewRequest())


# ---------------------------------------------------------------------------
# Sender + recipient-specific preview
# ---------------------------------------------------------------------------


def test_sender_values_filled_from_profile(template_session) -> None:
    session, tenant_id = template_session
    template = _service(session, tenant_id).create(
        TemplateCreate(
            name="FromSender",
            subject_template="Hi {{first_name}}",
            html_body="<p>Regards, {{sender_name}} ({{sender_email}}, {{sender_company}})</p>",
        )
    )
    account = EmailAccount(
        tenant_id=tenant_id,
        provider="smtp",
        email="maya@acme.io",
        display_name="Maya Rao",
    )
    session.add(account)
    session.flush()
    session.add(
        SenderProfile(
            tenant_id=tenant_id,
            email_account_id=account.id,
            company="Acme",
            designation="CEO",
            phone="+1 555 0001",
        )
    )
    session.commit()
    rendered = _service(session, tenant_id).render(
        template.id,
        TemplatePreviewRequest(
            recipient={"first_name": "Ravi"}, sender_id=account.id
        ),
    )
    assert rendered.missing_variables == []
    assert "Maya Rao" in rendered.html_body
    assert "maya@acme.io" in rendered.html_body
    assert "Acme" in rendered.html_body


def test_recipient_preview_from_contact(template_session) -> None:
    session, tenant_id = template_session
    template = _service(session, tenant_id).create(
        TemplateCreate(
            name="ContactPreview",
            subject_template="Hi {{first_name}}",
            html_body="<p>Hi {{first_name}} at {{company}}, skills: {{skills}} "
            "availability: {{availability}}</p>",
            custom_variables=["skills", "availability"],
        )
    )
    contact = Contact(
        tenant_id=tenant_id,
        first_name="Gauri",
        last_name="Mishra",
        email="gauri@acme.io",
        company="Acme",
        status="ACTIVE",
    )
    session.add(contact)
    session.flush()
    session.add(
        ContactCustomField(
            tenant_id=tenant_id,
            contact_id=contact.id,
            field_key="skills",
            field_value="Python",
        )
    )
    session.add(
        ContactCustomField(
            tenant_id=tenant_id,
            contact_id=contact.id,
            field_key="availability",
            field_value="Wednesdays",
        )
    )
    session.commit()
    rendered = _service(session, tenant_id).render_for_contact(
        template.id,
        contact.id,
        TemplateRecipientPreviewRequest(),
    )
    assert rendered.missing_variables == []
    assert "Gauri" in rendered.html_body
    assert "Python" in rendered.html_body
    assert "Wednesdays" in rendered.html_body


def test_recipient_preview_missing_contact(template_session) -> None:
    session, tenant_id = template_session
    template = _service(session, tenant_id).create(create_payload())
    with pytest.raises(RecipientNotFoundError):
        _service(session, tenant_id).render_for_contact(
            template.id, uuid4(), TemplateRecipientPreviewRequest()
        )


def test_recipient_preview_supports_full_name(template_session) -> None:
    session, tenant_id = template_session
    template = _service(session, tenant_id).create(
        TemplateCreate(
            name="FullName",
            subject_template="Hi {{full_name}}",
            html_body="<p>Hi {{full_name}}</p>",
        )
    )
    contact = Contact(
        tenant_id=tenant_id,
        first_name="Anand",
        last_name="Kulkarni",
        email="anand@acme.io",
        status="ACTIVE",
    )
    session.add(contact)
    session.commit()
    rendered = _service(session, tenant_id).render_for_contact(
        template.id, contact.id, TemplateRecipientPreviewRequest()
    )
    assert rendered.subject == "Hi Anand Kulkarni"


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


def test_malformed_and_undeclared_variables_are_rejected(template_session) -> None:
    session, tenant_id = template_session
    service = _service(session, tenant_id)
    with pytest.raises(TemplateError, match="Malformed"):
        service.create(TemplateCreate(name="Bad", subject_template="Hi {{first_name", html_body="Body"))
    with pytest.raises(TemplateError, match="declared"):
        service.create(
            TemplateCreate(
                name="Unknown",
                subject_template="Hi {{secret}}",
                html_body="Body",
                custom_variables=["first_name"],
            )
        )
    with pytest.raises(TemplateError, match="declared"):
        service.create(TemplateCreate(name="Unknown2", subject_template="Hi {{secret}}", html_body="Body"))


def test_custom_variables_are_allowed(template_session) -> None:
    session, tenant_id = template_session
    template = _service(session, tenant_id).create(
        TemplateCreate(
            name="Custom",
            subject_template="Hi {{first_name}}",
            html_body="<p>Req: {{requirement}}</p>",
            custom_variables=["requirement"],
        )
    )
    assert "requirement" in template.versions[0].variable_manifest


# ---------------------------------------------------------------------------
# Duplicate / status / list helpers
# ---------------------------------------------------------------------------


def test_update_duplicate_and_tenant_isolation(template_session) -> None:
    session, tenant_id = template_session
    service = _service(session, tenant_id)
    template = service.create(create_payload())
    updated = service.update(template.id, TemplateUpdate(html_body="<p>Updated {{first_name}}</p>"))
    assert len(updated.versions) == 2
    duplicate = service.duplicate(template.id, "Intro Copy")
    assert duplicate.name == "Intro Copy"
    assert service.change_status(template.id, "ACTIVE").status == "ACTIVE"
    with pytest.raises(TemplateNotFoundError):
        TemplateService(session, uuid4()).get(template.id)


def test_template_with_segments_detaches_cleanly(template_session) -> None:
    session, tenant_id = template_session
    service = _service(session, tenant_id)
    template = service.create(create_payload())
    versions, total = service.versions(template.id)
    assert total == 1
    assert versions[0].version_number == 1
    with pytest.raises(TemplateNotFoundError):
        service.get_version(template.id, 99)


# ---------------------------------------------------------------------------
# AIMessageDraft architecture
# ---------------------------------------------------------------------------


@pytest.fixture()
def draft_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'drafts.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Draft Tenant", slug=f"drafts-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def draft_payload() -> DraftGenerateRequest:
    return DraftGenerateRequest(
        objective="Discuss Python services",
        recipient={"first_name": "Rajesh", "company": "ABC"},
        sender={"sender_name": "Manikant"},
        service="Python development",
        cta="Want details?",
    )


def test_draft_generate_approve_save_as_template(draft_session) -> None:
    session, tenant_id = draft_session
    actor_id = uuid4()
    service = AIDraftService(session, tenant_id, actor_id=actor_id)

    draft = service.generate(draft_payload())
    assert draft.generation_status == "GENERATED"
    assert draft.generation_method == "PROVIDER"
    assert draft.provider == "MockAIProvider"
    assert "Rajesh" in draft.generated_body or "ABC" in draft.generated_body
    assert draft.approved_template_id is None

    approved = service.approve(draft.id, DraftApproveRequest())
    assert approved.generation_status == "APPROVED"
    assert approved.approved_by_id == actor_id
    assert approved.approved_at is not None
    assert approved.approved_template_id is None

    saved = service.save_as_template(draft.id, DraftSaveTemplateRequest(template_name="Approved Intro"))
    assert saved.approved_template_id is not None

    created_template = TemplateService(session, tenant_id).get(saved.approved_template_id)
    assert "Rajesh" in created_template.versions[0].html_body


def test_draft_tenant_isolation(draft_session) -> None:
    session, tenant_id = draft_session
    draft = AIDraftService(session, tenant_id).generate(draft_payload())
    with pytest.raises(AIDraftNotFoundError):
        AIDraftService(session, uuid4()).get(draft.id)


def test_draft_reject_and_immutability(draft_session) -> None:
    session, tenant_id = draft_session
    service = AIDraftService(session, tenant_id)
    draft = service.generate(draft_payload())
    rejected = service.reject(draft.id, DraftRejectRequest(note="Too generic"))
    assert rejected.generation_status == "REJECTED"
    assert rejected.review_note == "Too generic"


def test_draft_is_never_sent_directly(draft_session) -> None:
    from sqlalchemy import func
    from sqlalchemy import select as sa_select


    session, tenant_id = draft_session
    draft = AIDraftService(session, tenant_id).generate(draft_payload())
    total_templates = session.scalar(
        sa_select(func.count(Template.id)).where(Template.tenant_id == tenant_id)
    )
    assert total_templates == 0
    assert draft.approved_template_id is None


def test_approve_requires_generated_status(draft_session) -> None:
    session, tenant_id = draft_session
    service = AIDraftService(session, tenant_id)
    draft = service.generate(draft_payload())
    service.reject(draft.id, DraftRejectRequest())
    with pytest.raises(AIDraftError):
        service.approve(draft.id, DraftApproveRequest())
    with pytest.raises(AIDraftError):
        service.save_as_template(draft.id, DraftSaveTemplateRequest(template_name="nope"))


def test_editing_approved_draft_is_immutable(draft_session) -> None:
    session, tenant_id = draft_session
    service = AIDraftService(session, tenant_id)
    draft = service.generate(draft_payload())
    service.approve(draft.id, DraftApproveRequest())
    with pytest.raises(AIDraftError):
        service.update(draft.id, DraftUpdate(subject="changed"))
    with pytest.raises(AIDraftError):
        service.transform(
            draft.id,
            DraftTransformRequest(action="regenerate"),
        )