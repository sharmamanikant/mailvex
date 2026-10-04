from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


class DelayedQueue(Protocol):
    def enqueue(self, scheduled_id: UUID, due_at: datetime) -> None: ...


@dataclass
class InMemoryDelayedQueue:
    items: deque[tuple[UUID, datetime]]

    def __init__(self) -> None:
        self.items = deque()

    def enqueue(self, scheduled_id: UUID, due_at: datetime) -> None:
        self.items.append((scheduled_id, due_at))


class RedisDelayedQueue:
    def __init__(self, redis_client, key: str = "crcrm:scheduled") -> None:
        self.redis = redis_client
        self.key = key

    def enqueue(self, scheduled_id: UUID, due_at: datetime) -> None:
        self.redis.zadd(self.key, {json.dumps({"scheduled_id": str(scheduled_id)}): due_at.timestamp()})

    def due(self, now: datetime, limit: int = 100) -> list[UUID]:
        values = self.redis.zrangebyscore(self.key, min="-inf", max=now.timestamp(), start=0, num=limit)
        return [UUID(json.loads(value)["scheduled_id"]) for value in values]
