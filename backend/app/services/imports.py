from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import dns.exception
import dns.resolver
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing import EVENT_CONTACT_CREATED, UsageService
from app.models import (
    Contact,
    ContactCustomField,
    ContactFieldDefinition,
    Suppression,
    SuppressionEntry,
)
from app.services.contact_fields import ContactFieldService

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 200
PREVIEW_LIMIT = 10
CHUNK_SIZE = 1000
MX_LOOKUP_TIMEOUT_SECONDS = 1.0
MAX_UNIQUE_DNS_LOOKUPS = 50
ALLOWED_EXTENSIONS = {".csv", ".xlsx"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ROLE_NAMES = {"admin", "administrator", "billing", "contact", "finance", "hello", "info", "jobs", "noreply", "no-reply", "office", "sales", "support", "team"}
DISPOSABLE_DOMAINS = {"10minutemail.com", "guerrillamail.com", "mailinator.com", "tempmail.com", "yopmail.com"}
DEDUP_POLICIES = {"SKIP", "UPDATE", "CREATE_NEW"}
EMAIL_STATUSES = {"VALID", "INVALID", "RISKY", "DISPOSABLE", "ROLE_ACCOUNT", "UNKNOWN"}
BLOCKED_EMAIL_STATUSES = {"INVALID", "DISPOSABLE", "ROLE_ACCOUNT"}

STANDARD_FIELDS = {
    "first_name",
    "last_name",
    "email",
    "phone",
    "company",
    "designation",
    "location",
    "website",
    "industry",
    "source",
    "source_reference",
    "notes",
}

FIELD_MAX_LENGTHS = {
    "first_name": 100,
    "last_name": 100,
    "phone": 50,
    "company": 200,
    "designation": 200,
    "location": 200,
    "website": 500,
    "industry": 150,
    "source": 100,
    "source_reference": 500,
    "notes": 4000,
}

FIELD_ALIASES = {
    "full_name": {"name", "full name", "contact name", "full_name", "contact_name"},
    "first_name": {"first_name", "firstname", "first name", "given name"},
    "last_name": {"last_name", "lastname", "last name", "surname", "family name"},
    "email": {"email", "email address", "e-mail"},
    "phone": {"phone", "phone number", "mobile", "telephone"},
    "company": {"company", "organization", "organisation"},
    "designation": {"designation", "title", "job title", "role"},
    "location": {"location", "city", "address"},
    "website": {"website", "url", "company website"},
    "industry": {"industry", "sector"},
    "source": {"source", "origin"},
    "source_reference": {"source_reference", "source reference", "source id"},
    "notes": {"notes", "comments"},
    "skills": {"skills", "skill"},
    "experience": {"experience", "years experience"},
    "requirement": {"requirement", "requirements"},
    "service": {"service", "product"},
    "service_area": {"service_area", "service area"},
    "budget": {"budget"},
    "technology": {"technology", "technologies"},
    "availability": {"availability"},
    "job_title": {"job_title", "job title"},
}


class ImportValidationError(ValueError):
    pass


@dataclass
class ImportReport:
    total: int = 0
    imported: int = 0
    updated: int = 0
    invalid: int = 0
    duplicate: int = 0
    suppressed: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "imported": self.imported,
            "updated": self.updated,
            "invalid": self.invalid,
            "duplicate": self.duplicate,
            "duplicates": self.duplicate,
            "suppressed": self.suppressed,
            "skipped": self.skipped,
            "errors": self.errors,
        }


@dataclass
class ImportContext:
    mapping: dict[str, str]
    policy: str
    report: ImportReport
    seen_emails: set[str] = field(default_factory=set)
    existing_by_email: dict[str, Contact] = field(default_factory=dict)
    suppressed_emails: set[str] = field(default_factory=set)
    definitions: dict[str, ContactFieldDefinition] = field(default_factory=dict)
    domain_cache: dict[str, str] = field(default_factory=dict)
    dns_lookups_left: int = MAX_UNIQUE_DNS_LOOKUPS


class ImportService:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def validate_file(self, filename: str, content: bytes) -> str:
        raw = filename or ""
        name = Path(raw).name
        if not name or name != raw or name.startswith("."):
            raise ImportValidationError("Invalid file name")
        extension = Path(name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise ImportValidationError("Only CSV and XLSX files are supported")
        if not content:
            raise ImportValidationError("The uploaded file is empty")
        if len(content) > MAX_FILE_BYTES:
            raise ImportValidationError("The uploaded file exceeds the 10 MB limit")
        if extension == ".csv":
            if b"\x00" in content:
                raise ImportValidationError("The uploaded file is not a valid CSV text file")
            try:
                content.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ImportValidationError("The CSV file must be UTF-8 encoded") from exc
        else:
            if not content.startswith(b"PK"):
                raise ImportValidationError("The uploaded file is not a valid XLSX workbook")
            try:
                inflated = sum(info.file_size for info in zipfile.ZipFile(io.BytesIO(content)).infolist())
            except zipfile.BadZipFile as exc:
                raise ImportValidationError("The uploaded file is not a valid XLSX workbook") from exc
            if inflated > MAX_DECOMPRESSED_BYTES:
                raise ImportValidationError("The uploaded XLSX file is too large when expanded")
        return extension

    def headers_and_rows(self, filename: str, content: bytes) -> tuple[list[str], list[dict[str, str]]]:
        extension = self.validate_file(filename, content)
        try:
            if extension == ".csv":
                return self._csv_rows(content)
            return self._xlsx_rows(content)
        except ImportValidationError:
            raise
        except Exception as exc:
            raise ImportValidationError("The file could not be parsed") from exc

    def preview(self, filename: str, content: bytes, limit: int = PREVIEW_LIMIT) -> dict[str, Any]:
        headers, rows = self.headers_and_rows(filename, content)
        custom_keys = list(ContactFieldService(self.session, self.tenant_id).definitions_map().keys())
        return {"headers": headers, "mapping": self.detect_mapping(headers, custom_keys), "rows": rows[:limit], "row_count": len(rows)}

    def prepare(self, headers: list[str], rows: list[dict[str, str]], mapping: dict[str, str] | None = None, policy: str = "SKIP") -> ImportContext:
        if policy not in DEDUP_POLICIES:
            raise ImportValidationError("Invalid duplicate policy")
        if len(rows) > MAX_ROWS:
            raise ImportValidationError("The import exceeds the 100,000 row limit")
        if len(headers) > MAX_COLUMNS:
            raise ImportValidationError("The file has too many columns")
        normalized_mapping = mapping or self.detect_mapping(headers, list(ContactFieldService(self.session, self.tenant_id).definitions_map().keys()))
        email_column = normalized_mapping.get("email")
        if not email_column:
            raise ImportValidationError("An email column is required")
        allowed_fields = STANDARD_FIELDS | {"full_name"}
        definitions = ContactFieldService(self.session, self.tenant_id).definitions_map()
        header_set = set(headers)
        for field_name, column in normalized_mapping.items():
            if field_name not in allowed_fields and field_name not in definitions:
                if definitions:
                    raise ImportValidationError(f"Mapping references an unknown field: {field_name}")
            if column not in header_set:
                raise ImportValidationError(f"Mapping column {column!r} was not found in the file")
        existing = self.session.scalars(select(Contact).where(Contact.tenant_id == self.tenant_id)).all()
        suppressed = self.session.scalars(select(Suppression.email).where(Suppression.tenant_id == self.tenant_id)).all()
        suppressed_modern = self.session.scalars(
            select(SuppressionEntry.email_normalized).where(
                SuppressionEntry.tenant_id == self.tenant_id,
                SuppressionEntry.active.is_(True),
            )
        ).all()
        report = ImportReport(total=len(rows))
        return ImportContext(
            mapping=normalized_mapping,
            policy=policy,
            report=report,
            existing_by_email={contact.email: contact for contact in existing},
            suppressed_emails=(
                {email.lower() for email in suppressed} | {email.lower() for email in suppressed_modern}
            ),
            definitions=definitions,
        )

    def process_rows(self, context: ImportContext, rows: list[dict[str, str]], start_index: int = 0) -> ImportReport:
        for offset, row in enumerate(rows):
            row_number = start_index + offset + 2
            values = {key: str(row.get(column, "") or "").strip() for key, column in context.mapping.items()}
            if not values.get("first_name") and values.get("full_name"):
                name_parts = values["full_name"].split()
                values["first_name"] = name_parts[0]
                values["last_name"] = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
            email = values.get("email", "").lower()
            if not email or not EMAIL_PATTERN.fullmatch(email):
                self._error(context.report, row_number, "invalid", "Invalid email syntax")
                continue
            if email in context.seen_emails:
                self._error(context.report, row_number, "duplicate", "Duplicate email in file")
                continue
            if email in context.suppressed_emails:
                self._error(context.report, row_number, "suppressed", "Recipient is suppressed")
                continue
            status, reason = self.email_status(email, context)
            if status in BLOCKED_EMAIL_STATUSES:
                self._error(context.report, row_number, "invalid", reason)
                continue
            length_error = self._field_length_error(values)
            if length_error:
                self._error(context.report, row_number, "invalid", length_error)
                continue
            custom, validation_error = self._validated_custom_values(context, values)
            if validation_error:
                self._error(context.report, row_number, "invalid", validation_error)
                continue
            existing = context.existing_by_email.get(email)
            if existing is not None:
                if context.policy != "UPDATE":
                    self._error(context.report, row_number, "duplicate", "Contact already exists" if context.policy == "SKIP" else "Contact already exists; cannot create a duplicate")
                    continue
                self._apply_update(existing, values, custom, status)
                context.seen_emails.add(email)
                context.report.updated += 1
                continue
            contact = Contact(
                tenant_id=self.tenant_id,
                email=email,
                first_name=values.get("first_name") or None,
                last_name=values.get("last_name") or None,
                phone=values.get("phone") or None,
                company=values.get("company") or None,
                designation=values.get("designation") or None,
                location=values.get("location") or None,
                website=values.get("website") or None,
                industry=values.get("industry") or None,
                source=values.get("source") or "import",
                source_reference=values.get("source_reference") or None,
                notes=values.get("notes") or None,
                validation_status=status,
                suppression_status="CLEAR",
                consent_status="UNKNOWN",
            )
            contact.custom_fields = [
                ContactCustomField(
                    tenant_id=self.tenant_id,
                    field_key=key,
                    field_value=value,
                )
                for key, value in custom.items()
            ]
            self.session.add(contact)
            UsageService(self.session, self.tenant_id).record_event(
                EVENT_CONTACT_CREATED,
                resource_type="contact",
                metadata={"email": email, "import": True},
            )
            context.seen_emails.add(email)
            context.report.imported += 1
        return context.report

    def import_rows(self, headers: list[str], rows: list[dict[str, str]], mapping: dict[str, str] | None = None, policy: str = "SKIP") -> ImportReport:
        context = self.prepare(headers, rows, mapping, policy)
        self.enforce_contact_headroom(len(rows))
        self.process_rows(context, rows, start_index=0)
        self.session.commit()
        return context.report

    def enforce_contact_headroom(self, projected_new: int) -> None:
        """Refuse an import that cannot fit inside the plan's contact limit.

        ``projected_new`` is an upper bound on how many contacts this batch can
        add. Checks are refused loudly (clear message) rather than trimming
        rows silently.
        """
        if projected_new > 0:
            UsageService(self.session, self.tenant_id).enforce(
                "contacts", quantity=projected_new
            )

    def email_status(self, email: str, context: ImportContext | None = None) -> tuple[str, str]:
        domain = email.rsplit("@", 1)[1].lower()
        local = email.rsplit("@", 1)[0].lower().strip()
        cache: dict[str, str] = {}
        if context is not None:
            cache = context.domain_cache
            domain_status = cache.get(domain)
        else:
            domain_status = None
        if domain_status is None:
            if domain in DISPOSABLE_DOMAINS:
                domain_status = "DISPOSABLE"
            elif context is not None and context.dns_lookups_left <= 0:
                domain_status = "UNKNOWN"
            else:
                try:
                    dns.resolver.resolve(domain, "MX", lifetime=MX_LOOKUP_TIMEOUT_SECONDS)
                    domain_status = "VALID"
                except (dns.exception.DNSException, OSError):
                    domain_status = "UNKNOWN"
                finally:
                    if context is not None and context.dns_lookups_left > 0:
                        context.dns_lookups_left -= 1
            cache[domain] = domain_status
        if domain_status == "DISPOSABLE":
            return "DISPOSABLE", "Disposable email domain"
        if local in ROLE_NAMES:
            return "ROLE_ACCOUNT", "Role-based email address"
        if domain_status == "VALID":
            return "VALID", "Domain has an MX record"
        return "UNKNOWN", "MX lookup could not be confirmed"

    @staticmethod
    def detect_mapping(headers: list[str], custom_keys: tuple[str, ...] | list[str] | set[str] = ()) -> dict[str, str]:
        normalized = {header.strip().lower(): header for header in headers if header and header.strip()}
        mapping: dict[str, str] = {}
        for field_name, aliases in FIELD_ALIASES.items():
            for alias in aliases:
                if alias in normalized:
                    mapping[field_name] = normalized[alias]
                    break
        for key in custom_keys:
            if key.lower() in normalized and key not in mapping:
                mapping[key] = normalized[key.lower()]
        return mapping

    def _validated_custom_values(self, context: ImportContext, values: dict[str, str]) -> tuple[dict[str, str], str | None]:
        custom: dict[str, str] = {}
        for key, value in values.items():
            if key in STANDARD_FIELDS or key == "full_name":
                continue
            if not value:
                continue
            definition = context.definitions.get(key)
            if definition is not None:
                try:
                    value = ContactFieldService.validate_value(definition, value)
                except ValueError as exc:
                    return {}, str(exc)
            custom[key] = value
        return custom, None

    @staticmethod
    def _field_length_error(values: dict[str, str]) -> str | None:
        for key, value in values.items():
            limit = FIELD_MAX_LENGTHS.get(key)
            if limit is not None and value and len(value) > limit:
                return f"Value for {key} exceeds the maximum length of {limit} characters"
        return None

    @staticmethod
    def _apply_update(contact: Contact, values: dict[str, str], custom: dict[str, str], status: str) -> None:
        for key in ("first_name", "last_name", "phone", "company", "designation", "location", "website", "industry", "source", "source_reference", "notes"):
            value = values.get(key)
            if value:
                setattr(contact, key, value)
        if status != "UNKNOWN" and contact.validation_status in {"UNKNOWN", ""}:
            contact.validation_status = status
        existing = {item.field_key: item for item in contact.custom_fields}
        for key, value in custom.items():
            item = existing.get(key)
            if item is not None:
                item.field_value = value
            else:
                contact.custom_fields.append(ContactCustomField(tenant_id=contact.tenant_id, field_key=key, field_value=value))

    @staticmethod
    def _error(report: ImportReport, row_number: int, category: str, message: str) -> None:
        if category == "invalid":
            report.invalid += 1
            report.errors.append({"row": row_number, "category": category, "message": message})
        elif category == "duplicate":
            report.duplicate += 1
        elif category == "suppressed":
            report.suppressed += 1
        else:
            report.skipped += 1
            report.errors.append({"row": row_number, "category": category, "message": message})

    @staticmethod
    def _csv_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        headers = list(reader.fieldnames or [])
        if not headers:
            raise ImportValidationError("The CSV file has no header row")
        return headers, [dict(row) for row in reader]

    @staticmethod
    def _xlsx_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        if sheet is None:
            raise ImportValidationError("The XLSX file has no active worksheet")
        values = list(sheet.values)
        if not values:
            raise ImportValidationError("The XLSX file has no header row")
        headers = [str(value or "").strip() for value in values[0]]
        if not any(headers):
            raise ImportValidationError("The XLSX file has no header row")
        rows = [dict(zip(headers, ("" if value is None else str(value) for value in row), strict=False)) for row in values[1:]]
        return headers, rows