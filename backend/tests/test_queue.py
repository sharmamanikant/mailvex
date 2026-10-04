from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.workers.queue import InMemoryDelayedQueue


def test_in_memory_queue_preserves_delayed_jobs() -> None:
    queue = InMemoryDelayedQueue()
    scheduled_id = uuid4()
    due_at = datetime.now(UTC) + timedelta(minutes=5)
    queue.enqueue(scheduled_id, due_at)
    assert queue.items[0] == (scheduled_id, due_at)
