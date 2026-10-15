"""User-confirmed sources outlive raw expiry, but explicit source edits cascade."""

import uuid
from datetime import date as Date
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from workmemory.db.models import Episode, MemoryCandidate, Operation, Project, WorkFact
from workmemory.evidence.service import fingerprint, invalidate_memory, remember, resolve
from workmemory.jobs.queue import now
from workmemory.workspace.operations import ProposalInput, targets, view


class SourceMemoryInput(BaseModel):
    request_id: uuid.UUID
    expected_revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=2000)
    fact_id: uuid.UUID | None = None
    date: Date | None = None
    project_id: uuid.UUID | None = None


async def propose_memory(session, identifier, body, timezone="Asia/Shanghai"):
    if not body.text.strip():
        raise HTTPException(422, "记忆内容不能为空")
    # Serialise retries before creating an operation; the UUID is scoped to this UI request.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": body.request_id.int % (2**63 - 1)}
    )
    old = await session.scalar(select(Operation).where(Operation.request_id == body.request_id))
    if old:
        if old.origin.get("episode_id") != str(identifier) or old.origin.get(
            "request_hash"
        ) != fingerprint([body.model_dump_json()]):
            raise HTTPException(409, "请求标识已用于另一条来源")
        return view(old)
    episode = await session.scalar(
        select(Episode).where(Episode.id == identifier).with_for_update()
    )
    source = await resolve(session, f"summary:{identifier}")
    if not source or not episode or episode.revision != body.expected_revision:
        raise HTTPException(409, "原文已变化或到期，请重新核对")
    fact = await session.get(WorkFact, body.fact_id) if body.fact_id else None
    if body.fact_id and (
        not fact or fact.episode_id != identifier or fact.revision != episode.revision
    ):
        raise HTTPException(409, "摘录已变化，请重新核对")
    project_id = body.project_id if "project_id" in body.model_fields_set else episode.project_id
    project_source = None
    if project_id:
        project = await session.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if not project or project.state in {"merged", "deleted"}:
            raise HTTPException(404, "项目不存在、已合并或已删除")
        project_source = await resolve(session, f"project:{project_id}")
    origin = {
        "episode_id": str(identifier),
        "revision": episode.revision,
        "date": body.date.isoformat()
        if body.date
        else episode.started_at.astimezone(ZoneInfo(timezone)).date().isoformat(),
        "project_id": str(project_id) if project_id else None,
        "provenance": fact.provenance if fact else {},
        "confirmed_at": now().isoformat(),
        "request_hash": fingerprint([body.model_dump_json()]),
    }
    proposal = ProposalInput(action="memory.create", text=body.text)
    _, _, target_hash = await targets(session, proposal)
    operation = Operation(
        id=uuid.uuid4(),
        action="memory.create",
        payload=proposal.model_dump(mode="json", exclude_unset=True),
        target_hash=target_hash,
        origin=origin,
        request_id=body.request_id,
        preview={
            "label": "编辑并长期留存",
            "after": {"text": body.text},
            "before": [],
            "notice": "确认后长期保存；原文自然到期不删除，主动删除原文会联动删除，纠正原文后须重新核对。",
        },
        expires_at=min(now() + timedelta(hours=24), datetime.fromisoformat(source["expires_at"]))
        if source.get("expires_at")
        else now() + timedelta(hours=24),
    )
    session.add(operation)
    await session.flush()
    await remember(session, "operation", operation.id, [source, *([project_source] if project_source else [])])
    return view(operation)


async def source_changed(session, identifier, deleted=False):
    rows = (
        await session.scalars(
            select(MemoryCandidate)
            .where(
                MemoryCandidate.source_episode_id == identifier,
                MemoryCandidate.status.in_(["confirmed", "needs_review"]),
            )
            .order_by(MemoryCandidate.id)
            .with_for_update()
        )
    ).all()
    for memory in rows:
        await invalidate_memory(session, memory.id)
        memory.revision += 1
        memory.status = "dismissed" if deleted else "needs_review"
        if deleted:
            memory.text = None
            memory.origin = {"episode_id": str(identifier)}


async def revalidate(session, memory):
    episode = await session.get(Episode, memory.source_episode_id)
    if not episode or episode.deleted_at or not await resolve(session, f"summary:{episode.id}"):
        raise HTTPException(409, "原文已删除或不可用，请先核对来源")
    memory.origin = {
        **memory.origin,
        "revision": episode.revision,
        "confirmed_at": now().isoformat(),
        "provenance": {},
    }


async def affected_memories(session, identifier):
    rows = (
        await session.scalars(
            select(MemoryCandidate)
            .where(
                MemoryCandidate.source_episode_id == identifier,
                MemoryCandidate.status.in_(["confirmed", "needs_review"]),
            )
            .order_by(MemoryCandidate.id)
        )
    ).all()
    return [
        {"id": str(row.id), "revision": row.revision, "text": row.text, "status": row.status}
        for row in rows
    ]
