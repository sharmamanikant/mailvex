"""System B sender-account health (Phase 10Q).

Computes the discrete sender health state from connection status + send
telemetry and persists the result on ``SenderAccount``:

* ``REAUTH_REQUIRED`` — the underlying connection needs a fresh consent/credential.
* ``FAILED`` — the connection/sender is unusable or the most recent send failed.
* ``DEGRADED`` — usable but stale success or an unresolved older failure.
* ``ASSESSING`` — connected but no send telemetry yet.
* ``HEALTHY`` — recent confirmed send success.

Telemetry writers (``record_send_outcome``) are the single funnel through which
the System B send paths (test-send today, warmup + campaign later) keep
``last_success_at`` / ``last_failure_at`` / ``last_error`` current. The evaluator
is deliberately pure so it can run in workers and tests alike.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SenderAccount, SenderConnection
from app.services.audit import AuditService

STALE_SUCCESS_DAYS = 7
FAILURE_WINDOW_MINUTES = 60 * 24


class SenderHealthService:
    """Owns SenderAccount.health_status writes and telemetry recording."""

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    # ------------------------------------------------------------------ #
    # Pure evaluation
    # ------------------------------------------------------------------ #
    @staticmethod
    def evaluate(account: SenderAccount, connection: SenderConnection) -> str:
        now = datetime.now(UTC)

        def _lt(a: datetime | None) -> datetime:
            if a is None:
                return datetime.min.replace(tzinfo=UTC)
            if a.tzinfo is None:
                return a.replace(tzinfo=UTC)
            return a

        if connection.status == "REAUTH_REQUIRED":
            return "REAUTH_REQUIRED"
        if connection.status in ("DISCONNECTED", "FAILED", "DISABLED"):
            return "FAILED"
        if account.status != "ACTIVE":
            return "FAILED"

        last_success = account.last_success_at
        last_failure = account.last_failure_at
        if last_success is None and last_failure is None:
            return "ASSESSING"

        if last_failure is not None and _lt(last_failure) >= _lt(last_success):
            age = (now - _lt(last_failure)).total_seconds()
            if age <= FAILURE_WINDOW_MINUTES * 60:
                return "FAILED"
            return "DEGRADED"

        if last_success is None:
            return "ASSESSING"
        if (now - _lt(last_success)).days > STALE_SUCCESS_DAYS:
            return "DEGRADED"
        return "HEALTHY"

    # ------------------------------------------------------------------ #
    # Telemetry writers
    # ------------------------------------------------------------------ #
    def record_send_outcome(
        self,
        sender_id: UUID,
        *,
        success: bool,
        error_code: str | None = None,
        message_id: str | None = None,
        actor_id: UUID | None = None,
    ) -> SenderAccount:
        """Record one System B send outcome and recompute health."""
        account = self._account(sender_id)
        connection = self.session.scalar(
            select(SenderConnection).where(
                SenderConnection.id == account.connection_id,
                SenderConnection.tenant_id == self.tenant_id,
            )
        )
        if connection is None:
            raise ValueError("Sender connection missing for account")  # pragma: no cover
        now = datetime.now(UTC)
        account.last_used_at = now
        if success:
            account.last_success_at = now
            account.last_error = None
            action = "SENDER_SEND_SUCCEEDED"
            detail: dict[str, object] = {"email": account.email, "provider": account.provider}
            if message_id:
                detail["message_id"] = message_id
        else:
            account.last_failure_at = now
            account.last_error = error_code or "UNKNOWN_ERROR"
            action = "SENDER_SEND_FAILED"
            detail = {
                "email": account.email,
                "provider": account.provider,
                "error_code": account.last_error or "",
            }
        account.health_status = self.evaluate(account, connection)
        AuditService(self.session, self.tenant_id, actor_id).record(
            action,
            "sender_account",
            account.id,
            detail,
        )
        self.session.commit()
        return account

    def refresh(self, sender_id: UUID, *, actor_id: UUID | None = None) -> SenderAccount:
        """Recompute and persist health from current state (health-center refresh)."""
        account = self._account(sender_id)
        connection = self.session.scalar(
            select(SenderConnection).where(
                SenderConnection.id == account.connection_id,
                SenderConnection.tenant_id == self.tenant_id,
            )
        )
        if connection is not None:
            prior = account.health_status
            account.health_status = self.evaluate(account, connection)
            if prior != account.health_status:
                AuditService(self.session, self.tenant_id, actor_id).record(
                    "SENDER_HEALTH_UPDATED",
                    "sender_account",
                    account.id,
                    {"from": prior, "to": account.health_status, "email": account.email},
                )
            self.session.commit()
        return account

    def _account(self, sender_id: UUID) -> SenderAccount:
        account = self.session.scalar(
            select(SenderAccount).where(
                SenderAccount.id == sender_id,
                SenderAccount.tenant_id == self.tenant_id,
            )
        )
        if account is None:
            raise ValueError("Sender account not found")
        return account