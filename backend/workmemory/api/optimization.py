"""Authenticated local UI operations; no callable model confirmation tools."""

import asyncio
import os
import signal
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from workmemory.artifacts.drafts import DraftInput, request_draft
from workmemory.config import ROOT
from workmemory.db.models import AppState, CaptureIncident, Job, MaintenanceTask
from workmemory.evidence.confirmed import SourceMemoryInput, propose_memory
from workmemory.maintenance.backups import BackupFiles
from workmemory.maintenance.service import enqueue, overview, task_view

router = APIRouter(prefix="/api")


@router.get("/maintenance")
@router.get("/maintenance/backups")
async def maintenance(request: Request):
    return await overview(request.app.state.runtime)


class TaskInput(BaseModel):
    request_id: uuid.UUID


@router.post("/maintenance/backups", status_code=202)
async def backup(body: TaskInput, request: Request):
    async with request.app.state.runtime.sessions.begin() as session:
        row = await enqueue(
            session, "backup", f"manual-{body.request_id}.dump", f"manual:{body.request_id}"
        )
        return task_view(row)


@router.post("/maintenance/backups/{identifier}/verify", status_code=202)
async def verify(identifier: str, body: TaskInput, request: Request):
    try:
        path = BackupFiles().path(identifier)
        if not path.is_file():
            raise ValueError()
    except ValueError:
        raise HTTPException(404, "备份不存在") from None
    async with request.app.state.runtime.sessions.begin() as session:
        return task_view(await enqueue(session, "verify", identifier, f"verify:{body.request_id}"))


@router.get("/maintenance/tasks/{identifier}")
async def task(identifier: uuid.UUID, request: Request):
    async with request.app.state.runtime.sessions() as session:
        row = await session.get(MaintenanceTask, identifier)
        if not row:
            raise HTTPException(404, "维护任务不存在")
        return task_view(row)


class PolicyInput(BaseModel):
    enabled: bool
    expected_revision: int


@router.patch("/maintenance/policy")
async def policy(body: PolicyInput, request: Request):
    async with request.app.state.runtime.sessions.begin() as session:
        from sqlalchemy.dialects.postgresql import insert

        await session.execute(
            insert(AppState)
            .values(key="backup_policy", value={"revision": 0, "enabled": True})
            .on_conflict_do_nothing()
        )
        row = await session.scalar(
            select(AppState).where(AppState.key == "backup_policy").with_for_update()
        )
        if row.value["revision"] != body.expected_revision:
            raise HTTPException(409, "备份设置已更新，请刷新")
        row.value = {"enabled": body.enabled, "revision": body.expected_revision + 1}
        return row.value


@router.post("/maintenance/backends/{name}/retry")
async def retry_backend(name: Literal["reme", "graphiti"], request: Request):
    async with request.app.state.runtime.sessions.begin() as session:
        row = await session.get(AppState, "backend_health:" + name)
        if row:
            row.value = {**row.value, "retry_at": None}
        from workmemory.jobs.queue import now

        await session.execute(
            update(Job)
            .where(
                Job.kind == "memory",
                Job.payload["backend"].astext == name,
                Job.status.in_(["pending", "failed"]),
            )
            .values(status="pending", attempts=0, available_at=now())
        )
    return {"ok": True}


@router.get("/capture/health-events")
async def health_events(request: Request):
    async with request.app.state.runtime.sessions() as session:
        rows = (
            await session.scalars(
                select(CaptureIncident).order_by(CaptureIncident.started_at.desc()).limit(100)
            )
        ).all()
        return {
            "items": [
                {
                    "id": str(row.id),
                    "reason": row.reason,
                    "started_at": row.started_at,
                    "ended_at": row.ended_at,
                    "last_seen_at": row.last_seen_at,
                }
                for row in rows
            ]
        }


class CaptureAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["accessibility", "screen_recording", "locate_app", "reopen", "backup_folder"]


@router.post("/capture/actions")
async def capture_action(body: CaptureAction):
    actions = {
        "accessibility": [
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ],
        "screen_recording": [
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        ],
        "locate_app": ["open", "-R", str(ROOT / "macos/WorkMemory.app")],
        "backup_folder": ["open", str(ROOT / "data/backups")],
        "reopen": [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/reopen_collector.py")],
    }
    process = await asyncio.create_subprocess_exec(
        *actions[body.action],
        cwd=ROOT,
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        code = await asyncio.wait_for(process.wait(), 30)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        raise HTTPException(503, "操作超时，请通过拾忆管理入口检查") from None
    if code:
        raise HTTPException(503, "未能完成操作，请通过拾忆管理入口检查")
    return {"ok": True}


@router.post("/episodes/{identifier}/memory-proposals", status_code=201)
async def source_memory(identifier: uuid.UUID, body: SourceMemoryInput, request: Request):
    async with request.app.state.runtime.sessions.begin() as session:
        return await propose_memory(
            session, identifier, body, request.app.state.runtime.settings.timezone
        )


@router.post("/reviews/{identifier}/draft", status_code=202)
async def report_draft(identifier: uuid.UUID, body: DraftInput, request: Request):
    return await request_draft(request.app.state.runtime, identifier, body)


@router.get("/memory/expiring")
async def expiring_memories(request: Request):
    from datetime import timedelta

    from workmemory.db.models import Episode
    from workmemory.evidence.policy import eligible_clause
    from workmemory.jobs.queue import now

    async with request.app.state.runtime.sessions() as session:
        rows = (
            await session.scalars(
                select(Episode)
                .where(
                    eligible_clause(),
                    Episode.evidence_state == "excerpted",
                    Episode.evidence_expires_at > now(),
                    Episode.evidence_expires_at <= now() + timedelta(days=7),
                )
                .order_by(Episode.evidence_expires_at)
                .limit(100)
            )
        ).all()
        return {
            "items": [
                {"id": str(row.id), "summary": row.summary, "expires_at": row.evidence_expires_at}
                for row in rows
            ]
        }
