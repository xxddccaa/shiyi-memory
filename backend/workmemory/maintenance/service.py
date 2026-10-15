import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert

from workmemory.collector.status import capture_health, collector_status
from workmemory.companion.state import emit
from workmemory.config import ROOT
from workmemory.db.models import AppState, CaptureIncident, MaintenanceTask
from workmemory.jobs.queue import now
from workmemory.maintenance.backups import BackupFiles

log = logging.getLogger(__name__)


def task_view(row):
    return {
        key: (
            value.isoformat()
            if hasattr(value, "isoformat")
            else str(value)
            if isinstance(value, uuid.UUID)
            else value
        )
        for key in (
            "id",
            "kind",
            "status",
            "result",
            "error",
            "created_at",
            "updated_at",
            "available_at",
        )
        for value in [getattr(row, key)]
    }


async def enqueue(session, kind, identifier, key, automatic=False):
    await session.execute(
        insert(MaintenanceTask)
        .values(
            id=uuid.uuid4(),
            key=key,
            kind=kind,
            payload={"id": identifier, "automatic": automatic},
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=[MaintenanceTask.key])
    )
    row = await session.scalar(select(MaintenanceTask).where(MaintenanceTask.key == key))
    if row.kind != kind or row.payload != {"id": identifier, "automatic": automatic}:
        from fastapi import HTTPException

        raise HTTPException(409, "请求标识已用于另一项维护任务")
    return row


async def schedule(runtime, clock=None):
    clock = clock or now()
    local = clock.astimezone(ZoneInfo(runtime.settings.timezone))
    date = local.date() if local.hour >= 3 else local.date() - timedelta(days=1)
    async with runtime.sessions.begin() as session:
        pref = await session.get(AppState, "backup_policy")
        if pref and not pref.value.get("enabled", True):
            return
        await session.execute(
            update(MaintenanceTask)
            .where(
                MaintenanceTask.kind == "backup",
                MaintenanceTask.payload["automatic"].as_boolean().is_(True),
                MaintenanceTask.status == "pending",
                MaintenanceTask.key != f"backup:{date.isoformat()}",
            )
            .values(status="superseded")
        )
        # One due date, never a loop over missed days.
        await enqueue(
            session,
            "backup",
            f"automatic-{date.isoformat()}.dump",
            f"backup:{date.isoformat()}",
            True,
        )


async def observe_capture(runtime, clock=None):
    clock = clock or now()
    collector = collector_status(runtime.settings)
    async with runtime.sessions.begin() as session:
        receipt = await session.get(AppState, "capture_receipts")
        health = capture_health(collector, receipt.value if receipt else {}, clock)
        reason = (
            health["state"]
            if health["state"] in {"permission_required", "error", "stalled", "offline"}
            else None
        )
        previous = await session.get(AppState, "capture_monitor")
        state = previous.value if previous else {}
        gap = (
            (clock - datetime.fromisoformat(state["seen_at"])).total_seconds()
            if state.get("seen_at")
            else 9999
        )
        current = await session.scalar(
            select(CaptureIncident).where(CaptureIncident.ended_at.is_(None)).with_for_update()
        )
        # An unobserved sleep/offline interval is unknown, not proof of continuing failure.
        if current and (current.reason != reason or gap > 120):
            current.ended_at = current.last_seen_at
            current = None
            await emit(session, "capture")
        grace = getattr(runtime, "maintenance_started", clock)
        if reason == "offline" and (clock - grace).total_seconds() < 120:
            reason = None
        if reason:
            if current is None:
                current = CaptureIncident(
                    reason=reason, started_at=clock, last_seen_at=clock, details={}
                )
                session.add(current)
                await emit(session, "capture")
            current.last_seen_at = clock
            current.details = {
                key: collector[key]
                for key in ("screenshots", "ocr_requests", "spooled_batches", "stage")
                if key in collector
            }
        value = {"seen_at": clock.isoformat(), "state": health["state"]}
        if previous:
            previous.value = value
        else:
            session.add(AppState(key="capture_monitor", value=value))
        await session.execute(
            delete(CaptureIncident).where(CaptureIncident.last_seen_at < clock - timedelta(days=30))
        )


async def run_one(runtime):
    async with runtime.sessions.begin() as session:
        policy = await session.get(AppState, "backup_policy")
        allowed = (
            MaintenanceTask.payload["automatic"].as_boolean().is_not(True)
            if policy and not policy.value.get("enabled", True)
            else True
        )
        row = await session.scalar(
            select(MaintenanceTask)
            .where(
                allowed, MaintenanceTask.status == "pending", MaintenanceTask.available_at <= now()
            )
            .order_by(MaintenanceTask.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not row:
            return False
        if row.payload.get("automatic"):
            policy = await session.get(AppState, "backup_policy")
            if policy and not policy.value.get("enabled", True):
                return False
        row.status, row.updated_at = "running", now()
        identifier, kind, payload = row.id, row.kind, dict(row.payload)
    process = None
    try:
        args = [sys.executable, str(ROOT / "scripts/maintenance_task.py"), kind, payload["id"]]
        if payload.get("automatic"):
            args.append("automatic")
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=ROOT,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(process.communicate(), 660)
        result = json.loads(output)
        if process.returncode or "error" in result:
            raise RuntimeError(result.get("error", "MaintenanceFailed"))
        async with runtime.sessions.begin() as session:
            row = await session.get(MaintenanceTask, identifier)
            row.status, row.result, row.error, row.updated_at = "done", result, None, now()
            await emit(session, "maintenance")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        async with runtime.sessions.begin() as session:
            row = await session.get(MaintenanceTask, identifier)
            row.status = "pending" if payload.get("automatic") else "failed"
            row.error, row.updated_at = type(exc).__name__, now()
            row.available_at = now() + timedelta(hours=1)
            await emit(session, "maintenance")
    finally:
        if process and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
    return True


async def loop(runtime):
    runtime.maintenance_started = now()
    async with runtime.sessions.begin() as session:
        await session.execute(
            update(MaintenanceTask)
            .where(MaintenanceTask.status == "running")
            .values(status="pending", error="Interrupted")
        )
    running = None
    try:
        while True:
            try:
                await observe_capture(runtime)
                await schedule(runtime)
                if running is None or running.done():
                    if running:
                        await running
                    running = asyncio.create_task(run_one(runtime))
            except Exception as exc:
                log.warning("Local maintenance waiting: %s", type(exc).__name__)
            await asyncio.sleep(10)
    finally:
        if running:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def overview(runtime):
    files = await asyncio.to_thread(BackupFiles().list)
    async with runtime.sessions() as session:
        tasks = (
            await session.scalars(
                select(MaintenanceTask).order_by(MaintenanceTask.created_at.desc()).limit(20)
            )
        ).all()
        size = await session.scalar(text("SELECT pg_database_size(current_database())"))
        states = (
            await session.scalars(select(AppState).where(AppState.key.like("backend_health:%")))
        ).all()
        policy = await session.get(AppState, "backup_policy")
    return {
        "backups": files,
        "tasks": [task_view(row) for row in tasks],
        "database_bytes": size,
        "backup_bytes": sum(item["size"] for item in files),
        "backends": {row.key.split(":", 1)[1]: row.value for row in states},
        "policy": policy.value if policy else {"enabled": True, "revision": 0},
        "schedule": "每天03:00；停机后补一次，失败每小时重试。保留最近7个备份日及4个备份周，手动备份独立保留。",
    }
