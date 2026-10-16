import uuid
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import delete, func, select, update

from workmemory.assistant.service import answer_question, episode_view, retrieve
from workmemory.collector.status import capture_health, collector_status, ocr_status
from workmemory.db.models import (
    Activity,
    AppState,
    Episode,
    Job,
    MemoryBackend,
    MemoryReceipt,
    Observation,
    Project,
    Reminder,
    WorkFact,
)
from workmemory.episodes.service import mutate_episode
from workmemory.evidence.policy import eligible_clause, valid_excerpt_sources
from workmemory.ingestion.dedup import restore
from workmemory.ingestion.service import Batch, ingest
from workmemory.jobs.queue import now
from workmemory.memory.sync import schedule_sync, selected_backend, switch_backend
from workmemory.workspace.grouping import DAILY_WORK_LABEL

router = APIRouter(prefix="/api")


def runtime(request):
    return request.app.state.runtime


async def live_episode(session, episode_id, lock=False):
    stmt = select(Episode).where(Episode.id == episode_id, Episode.deleted_at.is_(None))
    if lock:
        stmt = stmt.with_for_update()
    episode = await session.scalar(stmt)
    if not episode:
        raise HTTPException(404, "片段不存在或已删除")
    return episode


@router.post("/ingest")
async def ingest_events(batch: Batch, request: Request):
    rt = runtime(request)
    async with rt.sessions.begin() as session:
        return await ingest(session, batch, rt.settings)


@router.get("/timeline")
async def timeline(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    project_id: uuid.UUID | None = None,
    day: date | None = None,
    unassigned: bool = False,
):
    rt = runtime(request)
    if project_id and unassigned:
        raise HTTPException(422, "具体项目与日常工作不能同时筛选")
    conditions = [Episode.deleted_at.is_(None)]
    if project_id:
        conditions.append(Episode.project_id == project_id)
    if unassigned:
        conditions.append(Episode.project_id.is_(None))
    if day:
        start = datetime.combine(day, time.min, ZoneInfo(rt.settings.timezone))
        conditions.extend(
            [Episode.started_at >= start, Episode.started_at < start + timedelta(days=1)]
        )
    async with rt.sessions() as session:
        rows = (
            await session.execute(
                select(Episode, Project)
                .outerjoin(Project, Episode.project_id == Project.id)
                .where(*conditions)
                .order_by(Episode.started_at.desc(), Episode.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
        count = await session.scalar(select(func.count()).select_from(Episode).where(*conditions))
    return {"items": [episode_view(e, p) for e, p in rows], "total": count}


@router.get("/episodes/{episode_id}/sources")
async def sources(episode_id: uuid.UUID, request: Request):
    cutoff = now() - timedelta(days=runtime(request).settings.retention_days)
    async with runtime(request).sessions() as session:
        episode = await live_episode(session, episode_id)
        activities = (
            await session.scalars(
                select(Activity)
                .where(Activity.episode_id == episode_id)
                .order_by(Activity.captured_at)
            )
        ).all()
        observations = (
            await session.execute(
                select(Observation, Activity)
                .outerjoin(Activity, Observation.activity_id == Activity.id)
                .where(Observation.episode_id == episode_id)
                .order_by(Observation.captured_at)
            )
        ).all()
        excerpts = []
        if await valid_excerpt_sources(session, episode, runtime(request).settings.retention_days):
            facts = (await session.scalars(select(WorkFact).where(WorkFact.episode_id == episode.id,
                WorkFact.revision == episode.revision))).all()
            excerpts = [{"id": str(f.id), "text": f.text, "provenance": f.provenance} for f in facts]
        return {
            "excerpts": excerpts,
            "observations": [
                {
                    "id": str(o.id),
                    "captured_at": o.captured_at,
                    "app": o.app,
                    "title": o.title if o.captured_at >= cutoff else "",
                    "activity_id": str(o.activity_id) if o.activity_id else None,
                    "interaction_id": str(o.interaction_id) if o.interaction_id else None,
                    "trigger": o.trigger,
                    "phase": o.phase,
                    "event_at": o.event_at,
                    "text": restore(a.text, o.delta) if a and a.text and min(a.captured_at, o.captured_at) >= cutoff else None,
                    "meaningful": o.meaningful,
                    "similarity": o.similarity,
                    "reason": o.reason,
                    "method": o.method,
                    **{k:v for k,v in o.capture_meta.items() if k != "resource_uri"},
                    "resource_uri": o.capture_meta.get("resource_uri") if a and a.text and min(a.captured_at, o.captured_at) >= cutoff else None,
                }
                for o, a in observations
            ],
            "episode": episode_view(episode),
            "activities": [
                {
                    "id": str(a.id),
                    "captured_at": a.captured_at,
                    "app": a.app,
                    "title": a.title if a.captured_at >= cutoff else "",
                    "text": a.text if a.captured_at >= cutoff else None,
                    "method": a.method,
                    "expired": a.text is None or a.captured_at < cutoff,
                }
                for a in activities
            ],
        }


class Correction(BaseModel):
    summary: str = Field(min_length=1, max_length=3000)
    expected_revision: int = Field(ge=1)


@router.patch("/episodes/{episode_id}")
async def correct(episode_id: uuid.UUID, body: Correction, request: Request):
    async with runtime(request).sessions.begin() as session:
        episode = await live_episode(session, episode_id, lock=True)
        if episode.revision != body.expected_revision:
            raise HTTPException(409, "片段已更新，请刷新后重试")
        await mutate_episode(session, episode, body.summary)
        return episode_view(episode)


@router.get("/episodes/{episode_id}/delete-preview")
async def delete_preview(episode_id: uuid.UUID, request: Request):
    from workmemory.evidence.confirmed import affected_memories
    from workmemory.evidence.service import fingerprint
    async with runtime(request).sessions.begin() as session:
        episode = await live_episode(session, episode_id, lock=True)
        memories = await affected_memories(session, episode_id)
        return {"revision": episode.revision, "memories": memories,
                "memory_hash": fingerprint([f"{row['id']}:{row['revision']}" for row in memories])}


@router.delete("/episodes/{episode_id}")
async def remove(episode_id: uuid.UUID, request: Request, expected_revision: int | None = None, memory_hash: str | None = None):
    async with runtime(request).sessions.begin() as session:
        episode = await session.scalar(
            select(Episode).where(Episode.id == episode_id).with_for_update()
        )
        if episode and not episode.deleted_at:
            from workmemory.evidence.confirmed import affected_memories
            from workmemory.evidence.service import fingerprint
            if expected_revision is not None and episode.revision != expected_revision:
                raise HTTPException(409, "原文已变化，请重新预览")
            if memory_hash is not None:
                memories = await affected_memories(session, episode_id)
                if fingerprint([f"{row['id']}:{row['revision']}" for row in memories]) != memory_hash:
                    raise HTTPException(409, "关联长期记忆已变化，请重新预览")
            await mutate_episode(session, episode)
    return {"deleted": True}


@router.post("/episodes/{episode_id}/retry")
async def retry_episode(episode_id: uuid.UUID, request: Request):
    async with runtime(request).sessions.begin() as session:
        episode = await live_episode(session, episode_id, lock=True)
        if episode.status == "ready":
            await schedule_sync(session, episode, force=True)
        else:
            await session.execute(
                update(Job)
                .where(
                    Job.kind == "summarize",
                    Job.payload["episode_id"].astext == str(episode_id),
                    Job.status.in_(["pending", "failed"]),
                )
                .values(status="pending", attempts=0, available_at=now(), last_error=None)
            )
    return {"scheduled": True}


@router.get("/work-groups/daily")
async def daily_work(request: Request):
    async with runtime(request).sessions() as session:
        count, latest = (await session.execute(
            select(func.count(Episode.id), func.max(Episode.started_at)).where(
                Episode.deleted_at.is_(None), Episode.project_id.is_(None)
            )
        )).one()
    return {"name": DAILY_WORK_LABEL, "episodes": count, "last_active": latest}


@router.get("/projects")
async def projects(request: Request):
    async with runtime(request).sessions() as session:
        rows = (
            await session.execute(
                select(Project, func.count(Episode.id), func.max(Episode.started_at))
                .outerjoin(Episode, (Episode.project_id == Project.id) & Episode.deleted_at.is_(None))
                .where(Project.state.notin_(["merged", "deleted"]))
                .group_by(Project.id)
                .order_by(func.max(Episode.started_at).desc())
            )
        ).all()
        return [
            {
                "id": str(p.id),
                "name": p.name,
                "state": p.state,
                "episodes": count,
                "last_active": latest or p.created_at,
            }
            for p, count, latest in rows
        ]


@router.post("/projects/{project_id}/resume")
async def resume_project(project_id: uuid.UUID, request: Request):
    async with runtime(request).sessions.begin() as session:
        episode = await session.scalar(
            select(Episode)
            .where(
                Episode.project_id == project_id,
                eligible_clause(),
            )
            .order_by(Episode.started_at.desc())
            .limit(1)
        )
        if not episode:
            raise HTTPException(404, "项目没有可恢复的记录")
        session.add(
            Reminder(
                id=uuid.uuid4(),
                kind="resume",
                episode_id=episode.id,
                text="这个项目有保留的来源记录，可以打开核对。",
                due_at=now(),
                dedupe_key=f"resume:{uuid.uuid4()}",
            )
        )
    return {"scheduled": True}


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@router.post("/chat")
async def chat(body: Question, request: Request):
    return await answer_question(runtime(request), body.question)


@router.get("/memory/search")
async def memory_search(request: Request, q: str = Query(min_length=1, max_length=2000),
    project_id: uuid.UUID | None = None, app: str | None = None,
    from_at: AwareDatetime | None = Query(None, alias="from"), to_at: AwareDatetime | None = Query(None, alias="to"),
    unassigned: bool = False):
    sources, warning = await retrieve(runtime(request), q, project_id=project_id, app=app,
                                      from_at=from_at, to_at=to_at, unassigned=unassigned)
    return {"items": sources, "warning": warning}


@router.get("/review")
async def review(request: Request, day: date | None = None):
    rt = runtime(request)
    day = day or datetime.now(ZoneInfo(rt.settings.timezone)).date()
    start = datetime.combine(day, time.min, ZoneInfo(rt.settings.timezone))
    async with rt.sessions() as session:
        rows = (
            await session.execute(
                select(Episode, Project)
                .outerjoin(Project, Episode.project_id == Project.id)
                .where(
                    Episode.deleted_at.is_(None),
                    Episode.status == "ready",
                    Episode.started_at >= start,
                    Episode.started_at < start + timedelta(days=1),
                )
                .order_by(Episode.started_at)
            )
        ).all()
    return {"day": day, "items": [episode_view(e, p) for e, p in rows]}


class BackendChange(BaseModel):
    backend: Literal["reme", "graphiti"]


@router.post("/config/backend")
async def change_backend(body: BackendChange, request: Request):
    rt = runtime(request)
    # A failed target initialization does not change the selected backend.
    try:
        await rt.memory.get(body.backend)
    except Exception as exc:
        raise HTTPException(
            503, f"目标记忆后端未就绪：{type(exc).__name__}。Graphiti 需要先启动 Neo4j profile。"
        ) from exc
    async with rt.sessions.begin() as session:
        count = await switch_backend(session, body.backend)
    return {"backend": body.backend, "replay_total": count}


@router.post("/memory/rebuild")
async def rebuild(request: Request):
    async with runtime(request).sessions.begin() as session:
        episodes = (
            await session.scalars(
                select(Episode).where(
                    (Episode.summary.is_not(None)) | (Episode.deleted_at.is_not(None))
                )
            )
        ).all()
        name = await selected_backend(session)
        # Forget acknowledgements, never directly change a framework's storage.
        await session.execute(delete(MemoryReceipt).where(MemoryReceipt.backend == name))
        for episode in episodes:
            await schedule_sync(session, episode, force=True)
    return {"scheduled": len(episodes)}


@router.post("/jobs/retry")
async def retry_failed(request: Request):
    async with runtime(request).sessions.begin() as session:
        result = await session.execute(
            update(Job)
            .where(Job.status == "failed")
            .values(status="pending", attempts=0, available_at=now(), last_error=None)
        )
    return {"scheduled": result.rowcount}


@router.get("/status")
async def status(request: Request):
    rt = runtime(request)
    async with rt.sessions() as session:
        name = await selected_backend(session)
        receipt = await session.get(AppState, "capture_receipts")
        receipt_status = receipt.value if receipt else {}
        latest_observation = await session.scalar(select(func.max(Observation.captured_at)))
        jobs = dict(
            (await session.execute(select(Job.status, func.count()).group_by(Job.status))).all()
        )
        used = list((await session.scalars(select(MemoryBackend.name))).all())
        total = await session.scalar(
            select(func.count())
            .select_from(Episode)
            .where(eligible_clause())
        )
        synced = await session.scalar(
            select(func.count())
            .select_from(MemoryReceipt)
            .join(Episode)
            .where(
                MemoryReceipt.backend == name,
                MemoryReceipt.revision == Episode.revision,
                eligible_clause(),
                MemoryReceipt.deleted.is_(False),
            )
        )
        observation_total = await session.scalar(select(func.count()).select_from(Observation))
        filtered_total = await session.scalar(
            select(func.count()).select_from(Observation).where(Observation.meaningful.is_(False))
        )
        errors = (
            await session.execute(
                select(Job.kind, Job.last_error, func.count())
                .where(Job.last_error.is_not(None), Job.status.in_(["pending", "failed"]))
                .group_by(Job.kind, Job.last_error)
            )
        ).all()
    try:
        model_status = await rt.ollama.health()
    except Exception:
        model_status = {"reachable": False, "models_ready": False}
    return {
        "backend": name,
        "used_backends": used,
        "jobs": jobs,
        "capture_receipts": {**receipt_status, "last_observation_at": latest_observation},
        "capture_statistics": {
            "observations": observation_total,
            "filtered": filtered_total,
            "dedup_ratio": filtered_total / observation_total if observation_total else 0,
        },
        "sync": {"total": total, "synced": synced},
        "ollama": model_status,
        "collector": collector_status(rt.settings),
        "capture_health": capture_health(collector_status(rt.settings), receipt_status),
        "ocr": await ocr_status(rt.settings),
        "inference": {
            "queued": len(rt.ollama.pending),
            "current_model": rt.ollama.current,
            "recent": list(rt.ollama.metrics)[-20:],
        },
        "errors": [{"kind": kind, "error": error, "count": count} for kind, error, count in errors],
        "retention_days": rt.settings.retention_days,
        "cloud_fallback": False,
    }


@router.get("/history/legacy")
async def legacy_history(request: Request, offset: int = Query(0, ge=0),
                         limit: int = Query(50, ge=1, le=100), episode_id: uuid.UUID | None = None):
    from workmemory.evidence.archive import history
    async with runtime(request).sessions() as session:
        return await history(session, offset, limit, episode_id)
