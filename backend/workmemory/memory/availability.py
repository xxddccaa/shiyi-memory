"""Persist a single retry gate per backend, without consuming cleanup obligations."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from workmemory.db.models import AppState
from workmemory.jobs.queue import now

DELAYS = (60, 300, 900, 3600)


class BackendOffline(Exception):
    pass


def connection_error(error):
    return isinstance(error, (OSError, TimeoutError)) or type(error).__name__ in {
        "ServiceUnavailable",
        "SessionExpired",
        "AuthError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ConnectionResetError",
        "BackendOffline",
    }


async def allowed(sessions, name):
    async with sessions() as session:
        row = await session.get(AppState, "backend_health:" + name)
        if (
            row
            and row.value.get("retry_at")
            and datetime.fromisoformat(row.value["retry_at"]) > now()
        ):
            raise BackendOffline()


async def record(sessions, name, success, error=None):
    async with sessions.begin() as session:
        key = "backend_health:" + name
        await session.execute(insert(AppState).values(key=key, value={}).on_conflict_do_nothing())
        row = await session.scalar(select(AppState).where(AppState.key == key).with_for_update())
        failures = 0 if success else row.value.get("failures", 0) + 1
        row.value = {
            "ready": success,
            "failures": failures,
            "checked_at": now().isoformat(),
            "retry_at": None
            if success
            else (now() + timedelta(seconds=DELAYS[min(failures - 1, 3)])).isoformat(),
            "error": type(error).__name__ if error else None,
        }
