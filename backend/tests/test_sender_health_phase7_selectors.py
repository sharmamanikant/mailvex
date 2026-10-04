"""Phase 7 regression: SendGrid + Zoho DKIM-selector resolution.

These are additive pins for the Phase 7 provider-health extension point. They
assert the exact value the sender-health orchestrator resolves through
``build_provider_adapter`` when a SENDGRID / ZOHO connection is evaluated,
so a future refactor cannot silently route the documented Phase 7 selectors
back into ``NullHealthAdapter`` (UNKNOWN) without a test failure.

Only selector-resolution behavior is tested here - none of these adapters
touch credentials. ``SMTP`` and unknown providers deliberately remain
``None`` (UNKNOWN) rather than risk a false PASS.
"""

from __future__ import annotations

from app.services.sender_health_engine.providers import build_provider_adapter


def _selector(provider: str | None) -> str | None:
    return build_provider_adapter(provider).dkim_selector(None, None)


def test_sendgrid_health_selector_is_documented_s1() -> None:
    """SendGrid signs outbound mail via *selector1/``s1``.

    SendGrid publishes DKIM under ``s1._domainkey.<domain>`` (with ``s2`` as
    the rotation fallback). Asserting the resolved value guards against the
    adapter silently falling through to ``NullHealthAdapter``.
    """
    assert _selector("SENDGRID") == "s1"


def test_zoho_health_selector_is_documented_zoho() -> None:
    """Zoho signs outbound mail with the ``zoho`` selector.

    Zoho publishes DKIM under ``zoho._domainkey.<domain>``. Same guard as
    above - the orchestrator must keep the Phase 7 adapter registered.
    """
    assert _selector("ZOHO") == "zoho"


def test_sendgrid_zoho_resolution_is_case_insensitive() -> None:
    """Provider keys may arrive lower/mixed case from the connection record."""
    assert _selector("sendgrid") == "s1"
    assert _selector("zoho") == "zoho"


def test_unknown_and_smtp_stay_unknown_not_false_pass() -> None:
    """Unwired providers must not fabricate a selector -> UNKNOWN, not PASS."""
    assert _selector("SMTP") is None
    assert _selector("NOT_A_PROVIDER") is None
    assert _selector(None) is None
