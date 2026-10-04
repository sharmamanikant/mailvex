from __future__ import annotations

from typing import Protocol
from uuid import UUID, uuid4

from app.models import Message
from app.providers import SenderUnavailableError
from app.services.delivery_jobs import (
    TRANSIENT_FAILURE_CODES,
    DeliveryJobService,
)
from app.services.sending import SendBlockedError, SenderThrottledError


class JobSendingPath(Protocol):
    def send_delivery_job(self, job_id: UUID) -> Message | None: ...


class DeliveryJobWorker:
    """Orchestrates one delivery job: claim -> send -> outcome.

    The sending service is injected so tests can substitute a fake without
    hitting a provider. ``worker_id`` identifies this worker for lease tracking.
    """

    def __init__(
        self,
        service: DeliveryJobService,
        sending_service: JobSendingPath,
        worker_id: UUID | None = None,
    ) -> None:
        self.service = service
        self.sending_service = sending_service
        self.worker_id = worker_id or uuid4()

    def process(self, job_id: UUID) -> str:
        job = self.service.claim(job_id, self.worker_id)
        if job is None:
            return "SKIPPED"
        try:
            message = self.sending_service.send_delivery_job(job.id)
        except SendBlockedError:
            # send_delivery_job has already transitioned the job to BLOCKED.
            return "BLOCKED"
        except SenderThrottledError as error:
            # Respect the provider limit: reschedule strictly after retry_after.
            self.service.throttle(job.id, error.retry_after)
            return "DEFERRED"
        except Exception as error:
            return self._on_send_error(job.id, error)
        if message is None:
            # Campaign was cancelled while the job was in flight.
            return "CANCELLED"
        self.service.mark_sent(job.id, getattr(message, "provider_message_id", None))
        return "SENT"

    def _on_send_error(self, job_id: UUID, error: Exception) -> str:
        code = self._classify(error)
        detail = f"{type(error).__name__}: {error}"
        if isinstance(error, SenderUnavailableError) or code not in TRANSIENT_FAILURE_CODES:
            self.service.mark_failure(job_id, code, detail, retryable=False)
            return "BLOCKED"
        self.service.mark_failure(job_id, code, detail, retryable=True)
        return "FAILED"

    @staticmethod
    def _classify(error: Exception) -> str:
        if isinstance(error, SenderUnavailableError):
            return "AUTH_FAILED"
        if isinstance(error, (TimeoutError, ConnectionError)):
            return "PROVIDER_TIMEOUT"
        if isinstance(error, OSError):
            return "NETWORK_ERROR"
        return "PROVIDER_ERROR"
