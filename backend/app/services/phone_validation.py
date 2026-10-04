"""Phone number signals for the contact validation engine.

Pipeline: normalize -> country -> format -> number type.

The country of a number stored without an international prefix is an
*assumption*, not a fact. The default region is configurable
(``VALIDATION_DEFAULT_PHONE_REGION``) and every verdict records whether the
assumption was applied, so a wrong default is visible rather than silent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberType, number_type

from app.core.config import settings
from app.services import validation_cache

STATUS_VALID = "VALID"
STATUS_INVALID = "INVALID"
STATUS_NOT_PROVIDED = "NOT_PROVIDED"
STATUS_UNKNOWN = "UNKNOWN"

TYPE_MOBILE = "MOBILE"
TYPE_LANDLINE = "LANDLINE"
TYPE_TOLL_FREE = "TOLL_FREE"
TYPE_PREMIUM = "PREMIUM"
TYPE_VOIP = "VOIP"
TYPE_PERSONAL_NUMBER = "PERSONAL_NUMBER"
TYPE_PAGER = "PAGER"
TYPE_UAN = "UAN"
TYPE_VOICEMAIL = "VOICEMAIL"
TYPE_SHARED_COST = "SHARED_COST"
TYPE_UNKNOWN = "UNKNOWN"

_DIGIT_PATTERN = re.compile(r"\d+")

_TYPE_NAMES: dict[int, str] = {
    PhoneNumberType.MOBILE: TYPE_MOBILE,
    PhoneNumberType.FIXED_LINE: TYPE_LANDLINE,
    PhoneNumberType.FIXED_LINE_OR_MOBILE: TYPE_LANDLINE,
    PhoneNumberType.TOLL_FREE: TYPE_TOLL_FREE,
    PhoneNumberType.PREMIUM_RATE: TYPE_PREMIUM,
    PhoneNumberType.VOIP: TYPE_VOIP,
    PhoneNumberType.PERSONAL_NUMBER: TYPE_PERSONAL_NUMBER,
    PhoneNumberType.PAGER: TYPE_PAGER,
    PhoneNumberType.UAN: TYPE_UAN,
    PhoneNumberType.VOICEMAIL: TYPE_VOICEMAIL,
    PhoneNumberType.SHARED_COST: TYPE_SHARED_COST,
}


@dataclass
class PhoneSignals:
    normalized: str | None = None
    status: str = STATUS_NOT_PROVIDED
    phone_type: str = TYPE_UNKNOWN
    region: str | None = None
    country_calling_code: int | None = None
    region_assumed: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "normalized": self.normalized,
            "status": self.status,
            "phone_type": self.phone_type,
            "region": self.region,
            "country_calling_code": self.country_calling_code,
            "region_assumed": self.region_assumed,
            "reasons": list(self.reasons),
        }


def _strip_to_number(raw: str) -> str:
    candidate = raw.strip()
    if candidate.startswith("00"):
        candidate = f"+{candidate[2:]}"
    digits = "".join(char for char in candidate if char.isdigit() or char == "+")
    return digits


def validate_phone(raw: str | None, tenant_id: str) -> PhoneSignals:
    if raw is None or not raw.strip():
        return PhoneSignals(status=STATUS_NOT_PROVIDED)

    candidate = _strip_to_number(raw)
    if not _DIGIT_PATTERN.search(candidate):
        return PhoneSignals(
            status=STATUS_INVALID, reasons=["No digits present in the phone number"]
        )

    cache_key = validation_cache.phone_key(tenant_id, candidate)
    cached = validation_cache.get_json(cache_key)
    if cached is not None:
        return _from_cache(cached)

    default_region = settings.validation_default_phone_region.upper() or None
    region_assumed = not candidate.startswith("+")
    try:
        parsed = phonenumbers.parse(candidate, None if not region_assumed else default_region)
    except NumberParseException as exc:
        return PhoneSignals(
            status=STATUS_INVALID, reasons=[f"Could not parse number: {exc.error_type}"]
        )

    possible = phonenumbers.is_possible_number(parsed)
    valid = phonenumbers.is_valid_number(parsed)
    if not possible:
        return PhoneSignals(
            status=STATUS_INVALID,
            region_assumed=region_assumed,
            reasons=["Number is not a possible length for its country"],
        )
    if not valid:
        return PhoneSignals(
            status=STATUS_UNKNOWN,
            region_assumed=region_assumed,
            reasons=["Number has a possible length but is not an assigned number"],
        )

    signals = PhoneSignals(
        normalized=phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164),
        status=STATUS_VALID,
        phone_type=_TYPE_NAMES.get(number_type(parsed), TYPE_UNKNOWN),
        region=phonenumbers.region_code_for_number(parsed),
        country_calling_code=parsed.country_code,
        region_assumed=region_assumed,
        reasons=["Country inferred from the configured default region"]
        if region_assumed
        else [],
    )
    validation_cache.set_json(
        cache_key, signals.as_dict(), settings.validation_phone_cache_ttl_seconds
    )
    return signals


def _from_cache(cached: dict[str, Any]) -> PhoneSignals:
    return PhoneSignals(
        normalized=cached.get("normalized"),
        status=str(cached.get("status", STATUS_UNKNOWN)),
        phone_type=str(cached.get("phone_type", TYPE_UNKNOWN)),
        region=cached.get("region"),
        country_calling_code=cached.get("country_calling_code"),
        region_assumed=bool(cached.get("region_assumed")),
        reasons=[str(item) for item in cached.get("reasons", [])],
    )
