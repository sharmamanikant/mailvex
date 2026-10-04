"""Phase 5 Sender health engine.

``SenderHealthService`` orchestrates a provider-independent health evaluation
for a Sender: provider connection, mailbox state, domain DNS (SPF/DKIM/DMARC/
MX), sending configuration, and any available sending signals. Outcomes are
normalized, scored deterministically (versioned), and returned with
explanations - never fabricated and never promising inbox placement.
"""

from app.services.sender_health_engine.orchestrator import (
    SenderHealthService,
    build_health_overview_payload,
)
from app.services.sender_health_engine.types import SenderHealthError

__all__ = ["SenderHealthError", "SenderHealthService", "build_health_overview_payload"]