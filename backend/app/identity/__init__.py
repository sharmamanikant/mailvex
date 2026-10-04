"""External identity subsystem (System A — Clerk).

Verifies Clerk session tokens, maps verified identities to application
users/tenants, and exposes the Clerk-only fast dependency. The legacy
password-based auth path is preserved separately in ``app.security``.
"""