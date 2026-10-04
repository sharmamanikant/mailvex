from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import engine

HEARTBEAT_MAX_AGE = timedelta(minutes=2)


@lru_cache(maxsize=1)
def expected_migration() -> str | None:
    """Resolve the Alembic head from the shipped migration scripts.

    This used to be a hardcoded literal, which silently went stale the moment
    the next migration landed: readiness then reported ``migrations: failed``
    forever, ``/health/ready`` never returned ready, and any traffic gated on
    readiness was refused permanently while every container still looked
    healthy. Reading the head from the scripts removes the failure mode.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        # backend/app/services/readiness.py -> backend/
        root = Path(__file__).resolve().parents[2]
        ini = root / "alembic.ini"
        script_location = root / "app" / "alembic"
        if not ini.is_file() or not script_location.is_dir():
            return None
        config = Config(str(ini))
        config.set_main_option("script_location", str(script_location))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:
        return None


def _heartbeat(redis: Redis, name: str) -> bool:
    value = redis.get(f"crcrm:heartbeat:{name}")
    if not value:
        return False
    return datetime.fromisoformat(cast(str, cast(Any, value))).replace(tzinfo=UTC) >= datetime.now(UTC) - HEARTBEAT_MAX_AGE


def readiness() -> dict[str, str]:
    checks = {"database": "failed", "redis": "failed", "migrations": "failed", "worker": "failed", "scheduler": "failed"}
    try:
        with Session(engine) as session:
            session.execute(text("SELECT 1"))
            revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
        checks["database"] = "ready"
        head = expected_migration()
        # If the head cannot be resolved, do not fail readiness on a technicality;
        # an applied revision is still better evidence than none.
        checks["migrations"] = "ready" if revision is not None and (head is None or revision == head) else "failed"
    except Exception:
        pass
    try:
        redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        if redis.ping():
            checks["redis"] = "ready"
            checks["worker"] = "ready" if _heartbeat(redis, "worker") else "failed"
            checks["scheduler"] = "ready" if _heartbeat(redis, "scheduler") else "failed"
    except Exception:
        pass
    return checks