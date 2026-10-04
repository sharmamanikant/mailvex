import logging
import sys

sys.path.insert(0, r"C:\Build_AI\CR+CRM\backend")

logging.basicConfig(level=logging.CRITICAL)

from app.models import OutboundMessage
from app.models.entities import OutboundMessage as O2

assert OutboundMessage is O2, "registry export must be the SAME class"
assert OutboundMessage.__tablename__ == "outbound_messages"

cols = {c.name for c in OutboundMessage.__table__.columns}
required = {
    "tenant_id", "sender_id", "provider_connection_id", "correlation_id",
    "from_email", "to_recipients", "subject", "text_body", "html_body",
    "status", "provider", "attempt_count", "error_code", "error_message",
    "created_at", "updated_at",
}
missing = required - cols
assert not missing, f"MISSING columns: {sorted(missing)}"

print("Phase 8 model check OK")
print("  tablename:", OutboundMessage.__tablename__)
print("  primary tenant FK + correlation idempotency present:", {c for c in cols if "correlation" in c or "tenant" in c})
print("  provider + provider_connection_id:", {c for c in cols if "provider" in c})
print("  registry identity: OutboundMessage is O2 ->", OutboundMessage is O2)
