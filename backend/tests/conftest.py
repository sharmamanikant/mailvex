"""Shared test configuration.

Every value here is set with ``setdefault`` *before* ``app.core.config.Settings``
is first constructed, because ``Settings`` is frozen: anything already resolved
when the module is imported can no longer be overridden by a fixture.

Two groups of variables are handled:

* OAuth client ids/secrets so the sender OAuth tests see usable (non-secret)
  placeholders instead of the developer's shell environment.
* ``APP_ENV``, which selects the test-only behaviour switches. Without it the
  suite inherits whatever the shell reports (``development`` in a normal
  checkout), which leaves ``OAuthStateStore`` pointed at a real Redis and makes
  the mailbox and provider-connection suites fail on any machine that is not
  running Redis. Pinning it here makes the suite hermetic and repeatable.
"""

from __future__ import annotations

import os

# Force the test environment regardless of the ambient shell value. This is a
# hard assignment on purpose: ``setdefault`` would keep a developer's
# ``APP_ENV=production`` and point the suite at production defaults.
os.environ["APP_ENV"] = "test"

# The suite must never reach a real broker, cache or mail transport.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

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
