from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.services.scheduler import SchedulerService
from app.services.sending import SendBlockedError


class SendingPath(Protocol):
    def send_scheduled(self, scheduled_id: UUID): ...


class ScheduledMessageWorker:
    """Worker orchestration; the sender callable is injected by the sending phase."""

    def __init__(self, scheduler: SchedulerService, sending_service: SendingPath) -> None:
        self.scheduler = scheduler
        self.sending_service = sending_service

    def process(self, scheduled_id: UUID) -> str:
        item = self.scheduler.claim(scheduled_id)
        try:
            self.sending_service.send_scheduled(item.id)
        except SendBlockedError:
            return "FAILED"
        except Exception as error:
            retry_after = getattr(error, "retry_after", None)
            if retry_after is not None:
                self.scheduler.defer(scheduled_id, "Provider throttled delivery", int(retry_after))
                return "DEFERRED"
            self.scheduler.fail(scheduled_id, "Provider delivery failed")
            return "FAILED"
        self.scheduler.mark_sent(scheduled_id)
        return "SENT"
