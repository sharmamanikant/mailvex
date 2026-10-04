"""Shared test configuration.

Sets OAuth client env vars before ``app.core.config.Settings`` is first
constructed so the frozen settings expose usable (but non-secret) Microsoft 365
client identifiers to the sender OAuth tests.
"""

from __future__ import annotations

import os

os.environ.setdefault("MICROSOFT_CLIENT_ID", "microsoft-test-client-id")
os.environ.setdefault("MICROSOFT_CLIENT_SECRET", "microsoft-test-client-secret")
os.environ.setdefault("MICROSOFT_TENANT_ID", "common")
os.environ.setdefault("MICROSOFT_AUTHORITY", "https://login.microsoftonline.com")
os.environ.setdefault(
    "MICROSOFT_SENDER_REDIRECT_URI",
    "http://localhost:8000/api/v1/senders/microsoft/oauth/callback",
)
os.environ.setdefault(
    "MICROSOFT_WORKSPACE_REDIRECT_URI",
    "http://localhost:8000/api/v1/provider-connections/microsoft/callback",
)
os.environ.setdefault("GOOGLE_CLIENT_ID", "google-test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "google-test-client-secret")
os.environ.setdefault("ZOHO_CLIENT_ID", "zoho-test-client-id")
os.environ.setdefault("ZOHO_CLIENT_SECRET", "zoho-test-client-secret")
os.environ.setdefault("ZOHO_AUTHORITY", "https://accounts.zoho.com")
os.environ.setdefault(
    "ZOHO_SENDER_REDIRECT_URI",
    "http://localhost:8000/api/v1/senders/zoho/oauth/callback",
)
