from __future__ import annotations

import html as html_lib
import re
from uuid import UUID

import nh3
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Contact,
    EmailAccount,
    Template,
    TemplateVariable,
    TemplateVersion,
)
from app.schemas.templates import (
    BUILT_IN_VARIABLES,
    RECIPIENT_VARIABLES,
    SENDER_VARIABLES,
    RenderedTemplate,
    TemplateCreate,
    TemplatePreviewRequest,
    TemplateRecipientPreviewRequest,
    TemplateUpdate,
)

VARIABLE_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")
_TAG_PATTERN = re.compile(r"<[^>]+>")
_LINE_BREAK_TAGS = re.compile(
    r"(?i)</(p|div|li|tr|h[1-6]|blockquote|pre|section)>|<br\s*/?>"
)

CONTACT_FIELD_MAP: dict[str, str] = {
    "first_name": "first_name",
    "last_name": "last_name",
    "email": "email",
    "phone": "phone",
    "company": "company",
    "designation": "designation",
    "location": "location",
    "website": "website",
    "industry": "industry",
    "source": "source",
    "source_reference": "source_reference",
    "notes": "notes",
}


class TemplateError(ValueError):
    pass


class TemplateNotFoundError(LookupError):
    pass


class RecipientNotFoundError(LookupError):
    pass


class TemplateService:
    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    @staticmethod
    def variables(*parts: str) -> list[str]:
        found: list[str] = []
        for part in parts:
            if part.count("{{") != part.count("}}") or (
                "{{" in part and not VARIABLE_PATTERN.search(part)
            ):
                raise TemplateError("Malformed template variable")
            for name in VARIABLE_PATTERN.findall(part):
                if name not in found:
                    found.append(name)
        return found

    @staticmethod
    def validate_variables(*parts: str, custom: list[str]) -> list[str]:
        names = TemplateService.variables(*parts)
        allowed_custom = set(custom)
        unknown = set(names) - BUILT_IN_VARIABLES - allowed_custom
        if unknown:
            raise TemplateError(f"Variables must be declared: {', '.join(sorted(unknown))}")
        return names

    @staticmethod
    def sanitize_html(content: str) -> str:
        return nh3.clean(content)

    @staticmethod
    def html_to_text(html: str) -> str:
        text = _LINE_BREAK_TAGS.sub("\n", html)
        text = _TAG_PATTERN.sub("", text)
        return html_lib.unescape(text).strip()

    def _template(self, template_id: UUID) -> Template:
        item = self.session.scalar(
            select(Template)
            .options(selectinload(Template.versions))
            .where(Template.id == template_id, Template.tenant_id == self.tenant_id)
        )
        if item is None:
            raise TemplateNotFoundError("Template not found")
        return item

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
        values.update({key: value for key, value in overrides.items() if value is not None})
        return values

    def _recipient_values(self, contact: Contact) -> dict[str, str]:
        values: dict[str, str] = {}
        for variable, attribute in CONTACT_FIELD_MAP.items():
            raw = getattr(contact, attribute)
            if raw is not None:
                values[variable] = str(raw)
        parts = [values.get("first_name"), values.get("last_name")]
        full = " ".join(part for part in parts if part)
        if full:
            values["full_name"] = full
        for value in contact.custom_fields:
            if value.field_value is not None:
                values.setdefault(value.field_key, value.field_value)
        return values

    def create(self, payload: TemplateCreate) -> Template:
        names = self.validate_variables(
            payload.subject_template, payload.html_body, payload.text_body or "",
            custom=payload.custom_variables,
        )
        sanitized = self.sanitize_html(payload.html_body)
        item = Template(
            tenant_id=self.tenant_id,
            name=payload.name,
            description=payload.description,
            created_by_id=self.actor_id,
        )
        version = TemplateVersion(
            tenant_id=self.tenant_id,
            version_number=1,
            subject_template=payload.subject_template,
            html_body=sanitized,
            text_body=payload.text_body,
            status="DRAFT",
            created_by_id=self.actor_id,
            variable_manifest=names,
        )
        version.variables = [
            TemplateVariable(tenant_id=self.tenant_id, name=name, required=name in BUILT_IN_VARIABLES)
            for name in names
        ]
        item.versions = [version]
        self.session.add(item)
        self.session.flush()
        item.current_version_id = version.id
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise TemplateError("A template with this name already exists") from exc
        return self._template(item.id)

    def get(self, template_id: UUID) -> Template:
        return self._template(template_id)

    def list_templates(self) -> tuple[list[Template], int]:
        total = self.session.scalar(select(func.count(Template.id)).where(Template.tenant_id == self.tenant_id)) or 0
        return (
            list(
                self.session.scalars(
                    select(Template)
                    .options(selectinload(Template.versions))
                    .where(Template.tenant_id == self.tenant_id)
                    .order_by(Template.updated_at.desc())
                ).all()
            ),
            total,
        )

    def update(self, template_id: UUID, payload: TemplateUpdate) -> Template:
        item = self._template(template_id)
        current = max(item.versions, key=lambda version: version.version_number)
        values = payload.model_dump(exclude_unset=True)
        subject = values.get("subject_template", current.subject_template)
        html_body = self.sanitize_html(values.get("html_body", current.html_body))
        text_body = values.get("text_body", current.text_body)
        custom = list(values.get("custom_variables", []))
        names = self.validate_variables(subject, html_body, text_body or "", custom=custom)
        if payload.name is not None:
            item.name = payload.name
        if payload.description is not None:
            item.description = payload.description
        version = self._new_version(item, subject, html_body, text_body, names)
        self.session.flush()
        item.current_version_id = version.id
        self.session.commit()
        return self._template(item.id)

    def versions(self, template_id: UUID) -> tuple[list[TemplateVersion], int]:
        item = self._template(template_id)
        ordered = sorted(item.versions, key=lambda version: version.version_number)
        return ordered, len(ordered)

    def get_version(self, template_id: UUID, version_number: int) -> TemplateVersion:
        item = self._template(template_id)
        for version in item.versions:
            if version.version_number == version_number:
                return version
        raise TemplateNotFoundError("Template version not found")

    def duplicate(self, template_id: UUID, name: str) -> Template:
        source = self._template(template_id)
        version = max(source.versions, key=lambda item: item.version_number)
        return self.create(
            TemplateCreate(
                name=name,
                description=source.description,
                subject_template=version.subject_template,
                html_body=version.html_body,
                text_body=version.text_body,
                custom_variables=[name for name in version.variable_manifest if name not in BUILT_IN_VARIABLES],
            )
        )

    def change_status(self, template_id: UUID, status: str) -> Template:
        if status not in {"DRAFT", "ACTIVE", "ARCHIVED"}:
            raise TemplateError("Invalid template lifecycle status")
        item = self._template(template_id)
        item.status = status
        max(item.versions, key=lambda version: version.version_number).status = status
        self.session.commit()
        return item

    def render(self, template_id: UUID, request: TemplatePreviewRequest) -> RenderedTemplate:
        item = self._template(template_id)
        version = max(item.versions, key=lambda value: value.version_number)
        sender = self._sender_values(request.sender_id, request.sender)
        values: dict[str, str] = {
            **request.recipient,
            **sender,
            **request.custom_values,
        }
        return self._render_version(version, values)

    def render_for_contact(
        self,
        template_id: UUID,
        contact_id: UUID,
        request: TemplateRecipientPreviewRequest,
    ) -> RenderedTemplate:
        item = self._template(template_id)
        version = max(item.versions, key=lambda value: value.version_number)
        contact = self.session.scalar(
            select(Contact)
            .options(selectinload(Contact.custom_fields))
            .where(Contact.id == contact_id, Contact.tenant_id == self.tenant_id)
        )
        if contact is None:
            raise RecipientNotFoundError("Recipient contact not found")
        sender = self._sender_values(request.sender_id, request.sender)
        values: dict[str, str] = {
            **self._recipient_values(contact),
            **sender,
            **request.custom_values,
        }
        return self._render_version(version, values)

    def render_template_values(self, version: TemplateVersion, values: dict[str, str]) -> dict[str, str]:
        rendered = self._render_version(version, values)
        return {
            "subject": rendered.subject,
            "html_body": rendered.html_body,
            "text_body": rendered.text_body,
        }

    def _render_version(self, version: TemplateVersion, values: dict[str, str]) -> RenderedTemplate:
        subject_tpl = version.subject_template
        html_tpl = self.sanitize_html(version.html_body)
        text_tpl = version.text_body or self.html_to_text(html_tpl)
        used = self.variables(subject_tpl, html_tpl, text_tpl)
        missing = [name for name in used if not (values.get(name) or "").strip()]
        warnings = [f"Missing {name}" for name in missing]

        def value(name: str) -> str:
            return (values.get(name) or "").strip()

        subject = VARIABLE_PATTERN.sub(
            lambda match: re.sub(r"[\r\n]+", " ", value(match.group(1))).strip(),
            subject_tpl,
        )
        html_body = VARIABLE_PATTERN.sub(
            lambda match: html_lib.escape(value(match.group(1))),
            html_tpl,
        )
        html_body = self.sanitize_html(html_body)
        text_body = VARIABLE_PATTERN.sub(
            lambda match: value(match.group(1)),
            text_tpl,
        )
        return RenderedTemplate(
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            used_variables=used,
            missing_variables=missing,
            warnings=warnings,
        )

    def _new_version(
        self,
        item: Template,
        subject: str,
        html_body: str,
        text_body: str | None,
        names: list[str],
    ) -> TemplateVersion:
        version = TemplateVersion(
            tenant_id=self.tenant_id,
            version_number=max((value.version_number for value in item.versions), default=0) + 1,
            subject_template=subject,
            html_body=html_body,
            text_body=text_body,
            status="DRAFT",
            created_by_id=self.actor_id,
            variable_manifest=names,
        )
        version.variables = [
            TemplateVariable(tenant_id=self.tenant_id, name=name, required=name in BUILT_IN_VARIABLES)
            for name in names
        ]
        item.versions.append(version)
        return version

    @staticmethod
    def variable_registry() -> dict[str, list[str]]:
        return {
            "recipient": list(RECIPIENT_VARIABLES),
            "sender": list(SENDER_VARIABLES),
        }