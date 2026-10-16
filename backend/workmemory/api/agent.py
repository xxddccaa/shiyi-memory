import asyncio
import json
import uuid
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select, tuple_

from workmemory.agent.errors import AgentFailure
from workmemory.agent.runtime import (
    ACTIVE,
    CHAT_DAYS,
    AgentUnavailable,
    delete_messages,
    retry_blocker,
    retry_run,
    submit_message,
)
from workmemory.artifacts.service import artifact_view, request_artifact, work_sessions
from workmemory.companion.suggestions import memory_conflicts
from workmemory.db.models import (
    AgentEvent,
    AgentRun,
    AgentWorkspace,
    ChatMessage,
    Conversation,
    DerivedArtifact,
    Episode,
    ExternalSource,
    MemoryCandidate,
    Project,
    WorkFact,
)
from workmemory.evidence.policy import eligible_clause, valid_excerpt_sources
from workmemory.evidence.service import (
    dependencies,
    invalidate_consumers,
    invalidate_memory,
    invalidate_object,
    neighbors,
    resolve,
    search,
    valid_dependencies,
)
from workmemory.jobs.queue import now

router = APIRouter(prefix="/api")


def rt(request):
    return request.app.state.runtime


class ConversationInput(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=200)
    workspace_id: uuid.UUID | None = None
    coding_enabled: bool = False
    expected_revision: int | None = None


class ObjectContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["todo", "project", "memory", "episode", "session", "review"]
    id: uuid.UUID
    revision: int = Field(ge=0)


class MessageInput(BaseModel):
    context: ObjectContext | None = None
    request_id: uuid.UUID | None = None
    content: str = Field(min_length=1)


def conversation_view(value):
    return {
        "id": str(value.id),
        "title": value.title
        if value.created_at > now() - timedelta(days=CHAT_DAYS)
        else "历史会话",
        "created_at": value.created_at,
        "updated_at": value.updated_at,
        "workspace_id": str(value.workspace_id) if value.workspace_id else None,
        "coding_enabled": value.coding_enabled,
        "revision": value.revision,
    }


@router.get("/conversations")
async def conversations(request: Request):
    async with rt(request).sessions() as session:
        rows = (
            await session.scalars(
                select(Conversation)
                .where(
                    Conversation.expired.is_(False),
                    Conversation.updated_at > now() - timedelta(days=CHAT_DAYS),
                )
                .order_by(Conversation.updated_at.desc())
                .limit(100)
            )
        ).all()
        return {"items": [conversation_view(c) for c in rows]}


@router.post("/conversations")
async def create_conversation(body: ConversationInput, request: Request):
    async with rt(request).sessions.begin() as session:
        if body.coding_enabled and not body.workspace_id:
            raise HTTPException(422, "开启编程前请选择工作目录")
        if body.workspace_id and not await session.get(AgentWorkspace, body.workspace_id):
            raise HTTPException(404, "工作目录不存在")
        value = Conversation(title=body.title, workspace_id=body.workspace_id, coding_enabled=body.coding_enabled)
        session.add(value)
        await session.flush()
        from workmemory.companion.state import save_preferences

        await save_preferences(session, {"recent_conversation_id": str(value.id)})
        return conversation_view(value)


@router.patch("/conversations/{identifier}")
async def rename_conversation(identifier: uuid.UUID, body: ConversationInput, request: Request):
    async with rt(request).sessions.begin() as session:
        value = await session.scalar(select(Conversation).where(Conversation.id == identifier).with_for_update())
        if not value or value.expired:
            raise HTTPException(404, "会话不存在")
        if body.expected_revision is not None and body.expected_revision != value.revision:
            raise HTTPException(409, "会话设置已更新，请刷新")
        if {"workspace_id", "coding_enabled"} & body.model_fields_set:
            if body.expected_revision is None:
                raise HTTPException(422, "编程设置需要 expected_revision")
            if await session.scalar(select(AgentRun.id).where(AgentRun.conversation_id == identifier, AgentRun.status.in_(ACTIVE))):
                raise HTTPException(409, "运行期间不能修改编程设置")
            workspace_id = body.workspace_id if "workspace_id" in body.model_fields_set else value.workspace_id
            if workspace_id != value.workspace_id and await session.scalar(select(ChatMessage.id).where(ChatMessage.conversation_id == identifier).limit(1)):
                raise HTTPException(409, "更换项目请开启新会话")
            if workspace_id and not await session.get(AgentWorkspace, workspace_id):
                raise HTTPException(404, "工作目录不存在")
            enabled = body.coding_enabled if "coding_enabled" in body.model_fields_set else value.coding_enabled
            if enabled and not workspace_id:
                raise HTTPException(422, "开启编程前请选择工作目录")
            value.workspace_id, value.coding_enabled = workspace_id, enabled
        if "title" in body.model_fields_set:
            value.title = body.title
        value.revision += 1
        return conversation_view(value)


@router.delete("/conversations/{identifier}")
async def remove_conversation(identifier: uuid.UUID, request: Request):
    await delete_messages(rt(request), identifier)
    return {"deleted": True}


@router.delete("/conversations/{identifier}/messages/{message_id}")
async def remove_message(identifier: uuid.UUID, message_id: uuid.UUID, request: Request):
    await delete_messages(rt(request), identifier, message_id)
    return {"deleted": True}


@router.get("/conversations/{identifier}/messages")
async def messages(
    identifier: uuid.UUID, request: Request, before: str | None = Query(None, max_length=100)
):
    page = []
    if before:
        try:
            at, mid = before.split("|", 1)
            moment = datetime.fromisoformat(at)
            if moment.tzinfo is None:
                raise ValueError()
            page = [tuple_(ChatMessage.created_at, ChatMessage.id) < tuple_(moment, uuid.UUID(mid))]
        except ValueError:
            raise HTTPException(422, "消息游标无效") from None
    runtime = rt(request)
    async with runtime.sessions.begin() as session:
        conversation = await session.get(Conversation, identifier)
        if not conversation or conversation.expired:
            raise HTTPException(404, "会话不存在或已到期")
        rows = (
            await session.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == identifier,
                    ChatMessage.created_at > now() - timedelta(days=CHAT_DAYS),
                    *page,
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(100)
            )
        ).all()
        items = []
        for message in reversed(rows):
            deps = await dependencies(session, "message", message.id)
            if not await valid_dependencies(
                session, deps, retention_days=runtime.settings.retention_days
            ):
                await invalidate_consumers(session, deps)
                message.content, message.status, message.citations = None, "invalidated", []
            sources = [
                await resolve(session, sid, runtime.settings.retention_days)
                for sid in message.citations
            ]
            external = []
            if message.status == "ready" and message.external_citations:
                records = (await session.scalars(select(ExternalSource).where(
                    ExternalSource.id.in_([uuid.UUID(v) for v in message.external_citations]),
                    ExternalSource.conversation_id == identifier, ExternalSource.expires_at > now(),
                    ExternalSource.content.is_not(None),
                ))).all()
                external = [{"id": str(v.id), "title": v.title, "url": v.url, "provider": v.provider, "retrieved_at": v.retrieved_at} for v in records]
            items.append(
                {
                    "id": str(message.id),
                    "role": message.role,
                    "content": message.content,
                    "status": message.status,
                    "sources": [s for s in sources if s],
                    "external_sources": external,
                    "context": message.context if message.status == "ready" else None,
                    "created_at": message.created_at,
                }
            )
        run = await session.scalar(
            select(AgentRun)
            .where(AgentRun.conversation_id == identifier)
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        return {
            "items": items,
            "run": await run_view(session, run) if run else None,
            "has_older": len(rows) == 100,
            "next_cursor": f"{rows[-1].created_at.isoformat()}|{rows[-1].id}"
            if len(rows) == 100
            else None,
        }


@router.get("/conversations/{identifier}/requests/{request_id}")
async def message_receipt(identifier: uuid.UUID, request_id: uuid.UUID, request: Request):
    """Reconcile an interrupted client submission without resending its content."""
    async with rt(request).sessions() as session:
        conversation = await session.get(Conversation, identifier)
        if not conversation or conversation.expired or conversation.updated_at <= now() - timedelta(days=CHAT_DAYS):
            raise HTTPException(404, "会话不存在或已到期")
        accepted = await session.scalar(select(ChatMessage.id).where(
            ChatMessage.conversation_id == identifier, ChatMessage.request_id == request_id,
        ))
        return {"accepted": accepted is not None}


@router.post("/conversations/{identifier}/messages", status_code=202)
async def send_message(identifier: uuid.UUID, body: MessageInput, request: Request):
    if not body.content.strip():
        raise HTTPException(422, "消息不能为空")
    return await submit_message(rt(request), identifier, body.content.strip(), body.request_id, body.context.model_dump(mode="json") if body.context else None)


async def run_view(session, run):
    event = await session.scalar(
        select(AgentEvent)
        .where(AgentEvent.run_id == run.id, AgentEvent.kind == run.status)
        .order_by(AgentEvent.id.desc())
        .limit(1)
    )
    fallback = {
        "interrupted": "interrupted",
        "invalidated": "source_invalidated",
        "cancelled": "cancelled",
    }
    diagnostic = AgentFailure.from_data(event.data if event else {})
    code = diagnostic.code
    if code == "unknown":
        code = fallback.get(run.status, "unknown")
    failed = run.status not in {*ACTIVE, "completed"}
    blocker = await retry_blocker(session, run) if failed else None
    return {
        "id": str(run.id),
        "conversation_id": str(run.conversation_id),
        "status": run.status,
        "error": str(AgentFailure(code)) if failed and code == "unknown" else run.error,
        "error_code": code if failed else None,
        "retryable": failed and blocker is None,
        "retry_blocked_reason": blocker if failed else None,
        "model_calls": run.model_calls,
        "search_calls": run.search_calls,
    }


@router.get("/agent/runs/{identifier}")
async def get_run(identifier: uuid.UUID, request: Request):
    async with rt(request).sessions() as session:
        run = await session.get(AgentRun, identifier)
        if not run:
            raise HTTPException(404, "运行不存在")
        return await run_view(session, run)


class RetryInput(BaseModel):
    request_id: uuid.UUID


@router.post("/agent/runs/{identifier}/retry", status_code=202)
async def retry_agent_run(identifier: uuid.UUID, body: RetryInput, request: Request):
    return await retry_run(rt(request), identifier, body.request_id)


@router.post("/agent/runs/{identifier}/cancel")
async def cancel_run(identifier: uuid.UUID, request: Request):
    await rt(request).agent.cancel(identifier)
    return {"cancelled": True}


@router.get("/agent/runs/{identifier}/events")
async def events(identifier: uuid.UUID, request: Request, after: int = Query(0, ge=0)):
    runtime = rt(request)
    async with runtime.sessions() as session:
        if not await session.get(AgentRun, identifier):
            raise HTTPException(404, "运行不存在")
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(422, "事件游标无效") from None

    async def stream():
        nonlocal cursor
        heartbeat = 0
        while not await request.is_disconnected():
            async with runtime.sessions() as session:
                run = await session.get(AgentRun, identifier)
                if not run:
                    return
                rows = (
                    await session.scalars(
                        select(AgentEvent)
                        .where(AgentEvent.run_id == identifier, AgentEvent.id > cursor)
                        .order_by(AgentEvent.id)
                        .limit(100)
                    )
                ).all()
                status = run.status
            for event in rows:
                cursor = event.id
                data = json.dumps({"kind": event.kind, **event.data}, ensure_ascii=False)
                yield f"id: {event.id}\nevent: progress\ndata: {data}\n\n"
            if status not in ACTIVE and len(rows) < 100:
                yield "event: settled\ndata: " + json.dumps({"status": status}) + "\n\n"
                return
            heartbeat += 1
            if heartbeat % 20 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


@router.post("/agent/probe")
async def probe_agent(request: Request):
    manager = rt(request).agent
    manager.probed_revision = None
    try:
        return await manager.probe()
    except (AgentUnavailable, AgentFailure) as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/memory/evidence/search")
async def evidence_search(
    request: Request,
    q: str = Query(min_length=1, max_length=2000),
    project_id: uuid.UUID | None = None,
    app: str | None = Query(None, max_length=200),
    from_at: AwareDatetime | None = Query(None, alias="from"),
    to_at: AwareDatetime | None = Query(None, alias="to"),
    limit: int = Query(10, ge=1, le=30),
    cursor: int = Query(0, ge=0, le=300),
    include_raw: bool = True,
    unassigned: bool = False,
):
    if from_at and to_at and from_at >= to_at:
        raise HTTPException(422, "开始时间必须早于结束时间")
    return await search(
        rt(request),
        q,
        project_id=project_id,
        app=app,
        from_at=from_at,
        to_at=to_at,
        limit=limit,
        offset=cursor,
        include_raw=include_raw,
        unassigned=unassigned,
    )


class ResolveInput(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=30)


@router.post("/memory/evidence/resolve")
async def evidence_resolve(body: ResolveInput, request: Request):
    runtime = rt(request)
    async with runtime.sessions() as session:
        items = [
            await resolve(session, sid, runtime.settings.retention_days)
            if not sid.startswith("chat:")
            else None
            for sid in body.ids
        ]
        return {
            "items": [i for i in items if i],
            "unavailable": [sid for sid, item in zip(body.ids, items) if not item],
        }


@router.get("/episodes/{identifier}/neighbors")
async def episode_neighbors(identifier: uuid.UUID, request: Request):
    return await neighbors(rt(request), identifier)


@router.get("/memory/candidates")
async def candidates(request: Request, conversation_id: uuid.UUID | None = None, status: str | None = Query(None, pattern="^(pending|confirmed|needs_review)$"), offset: int = Query(0, ge=0)):
    async with rt(request).sessions() as session:
        stmt = select(MemoryCandidate).where(
            MemoryCandidate.status.in_(["pending", "confirmed", "needs_review"]),
            (MemoryCandidate.status.in_(["confirmed", "needs_review"]))
            | (MemoryCandidate.created_at > now() - timedelta(days=CHAT_DAYS)),
        )
        if conversation_id:
            stmt = stmt.where(MemoryCandidate.conversation_id == conversation_id)
        if status:
            stmt = stmt.where(MemoryCandidate.status == status)
        rows = (
            await session.scalars(stmt.order_by(MemoryCandidate.created_at.desc(), MemoryCandidate.id).offset(offset).limit(101))
        ).all()
        next_cursor = offset + 100 if len(rows) > 100 else None
        rows = rows[:100]
        confirmed = (
            await session.scalars(
                select(MemoryCandidate)
                .where(MemoryCandidate.status == "confirmed")
                .order_by(MemoryCandidate.created_at.desc())
                .limit(200)
            )
        ).all()
        return {
            "next_cursor": next_cursor,
            "items": [
                {
                    "conflicts": memory_conflicts(c, confirmed),
                    "id": str(c.id),
                    "text": c.text,
                    "status": c.status,
                    "revision": c.revision,
                    "conversation_id": str(c.conversation_id) if c.conversation_id else None,
                    "message_id": str(c.message_id) if c.message_id else None,
                    "origin": c.origin,
                }
                for c in rows
            ]
        }


@router.get("/memory/candidates/{identifier}")
async def get_candidate(identifier: uuid.UUID, request: Request):
    async with rt(request).sessions() as session:
        value = await session.get(MemoryCandidate, identifier)
        if (
            not value
            or value.status == "dismissed"
            or (
                value.status == "pending"
                and value.created_at <= now() - timedelta(days=CHAT_DAYS)
            )
        ):
            raise HTTPException(404, "记忆不存在或已到期")
        return {
            "id": str(value.id),
            "text": value.text,
            "status": value.status,
            "revision": value.revision,
        }


class CandidateChange(BaseModel):
    replace_id: uuid.UUID | None = None
    replace_revision: int | None = Field(None, ge=1)
    action: str = Field(pattern="^(confirm|dismiss)$")
    expected_revision: int = Field(ge=1)
    text: str | None = Field(None, min_length=1, max_length=2000)


@router.patch("/memory/candidates/{identifier}")
async def change_candidate(identifier: uuid.UUID, body: CandidateChange, request: Request):
    async with rt(request).sessions.begin() as session:
        if (
            (body.replace_id is None) != (body.replace_revision is None)
            or body.replace_id == identifier
            or body.replace_id
            and body.action != "confirm"
        ):
            raise HTTPException(422, "替换记忆须指定另一条记忆及其版本，并确认新记忆")
        # Source correction locks Episode before MemoryCandidate. Use the same order.
        source_ids = list((await session.scalars(select(MemoryCandidate.source_episode_id).where(
            MemoryCandidate.id.in_([identifier, *([body.replace_id] if body.replace_id else [])]),
            MemoryCandidate.source_episode_id.is_not(None),
        ))).all())
        if source_ids:
            await session.execute(select(Episode).where(Episode.id.in_(source_ids)).order_by(Episode.id).with_for_update())
        locked = (
            await session.scalars(
                select(MemoryCandidate)
                .where(
                    MemoryCandidate.id.in_(
                        [identifier, *([body.replace_id] if body.replace_id else [])]
                    )
                )
                .order_by(MemoryCandidate.id)
                .with_for_update()
            )
        ).all()
        value = next((c for c in locked if c.id == identifier), None)
        if (
            not value
            or value.status == "dismissed"
            or (
                value.status == "pending"
                and value.created_at <= now() - timedelta(days=CHAT_DAYS)
            )
        ):
            raise HTTPException(404, "记忆候选不存在或已到期")
        if value.revision != body.expected_revision:
            raise HTTPException(409, "候选已更新，请刷新")
        if body.replace_id:
            old = next((c for c in locked if c.id == body.replace_id), None)
            if (
                not old
                or old.status != "confirmed"
                or old.revision != body.replace_revision
                or value.status != "pending"
            ):
                raise HTTPException(409, "待替换的记忆已变化，请刷新后核对")
            await invalidate_memory(session, old.id)
            old.text, old.status = None, "dismissed"
            old.revision += 1
        if value.status == "needs_review" and body.action == "confirm":
            from workmemory.evidence.confirmed import revalidate
            await revalidate(session, value)
        await invalidate_memory(session, value.id)
        value.revision += 1
        value.status = "confirmed" if body.action == "confirm" else "dismissed"
        value.text = (body.text or value.text) if body.action == "confirm" else None
        return {"updated": True, "status": value.status, "revision": value.revision}


@router.get("/work-sessions")
async def sessions(request: Request, project_id: uuid.UUID | None = None, day: date | None = None):
    return await work_sessions(rt(request), project_id, day)


@router.get("/projects/{identifier}/facts")
async def project_facts(identifier: uuid.UUID, request: Request):
    async with rt(request).sessions() as session:
        facts = (
            await session.scalars(
                select(WorkFact)
                .join(Episode)
                .where(
                    Episode.project_id == identifier,
                    eligible_clause(),
                    WorkFact.revision == Episode.revision,
                )
                .order_by(WorkFact.observed_at.desc())
                .limit(100)
            )
        ).all()
        valid = set()
        for episode_id in {f.episode_id for f in facts}:
            episode = await session.get(Episode, episode_id)
            if await valid_excerpt_sources(session, episode, rt(request).settings.retention_days):
                valid.add(episode_id)
        return {
            "items": [
                {
                    "id": str(f.id),
                    "kind": f.kind,
                    "state": f.state,
                    "text": f.text,
                    "evidence_id": f"summary:{f.episode_id}",
                    "revision": f.revision,
                }
                for f in facts if f.episode_id in valid
            ]
        }


@router.get("/projects/{identifier}/brief")
@router.post("/projects/{identifier}/brief")
async def project_brief(identifier: uuid.UUID, request: Request, refresh: bool = False):
    async with rt(request).sessions() as session:
        project = await session.get(Project, identifier)
        if not project or project.state in {"merged", "deleted"}:
            raise HTTPException(404, "项目不存在")
    return await request_artifact(
        rt(request), "brief", str(identifier), request.method == "POST" and refresh
    )


@router.get("/reviews")
@router.post("/reviews")
async def daily_review(
    request: Request,
    day: date | None = None,
    refresh: bool = False,
    period: str = Query("daily", pattern="^(daily|weekly)$"),
):
    runtime = rt(request)
    day = day or datetime.now(ZoneInfo(runtime.settings.timezone)).date()
    if period == "weekly":
        day -= timedelta(days=day.weekday())
    from workmemory.artifacts.drafts import report
    return await report(runtime, "weekly" if period == "weekly" else "review", day.isoformat())


class ReviewEdit(BaseModel):
    draft_id: uuid.UUID | None = None
    draft_hash: str | None = None
    text: str = Field(max_length=20000)
    expected_revision: int = Field(ge=0)


@router.patch("/reviews/{identifier}")
async def edit_review(identifier: uuid.UUID, body: ReviewEdit, request: Request):
    async with rt(request).sessions.begin() as session:
        # Keep the same source-before-target lock order as proposal confirmation.
        parent_deps = await dependencies(session, "artifact", identifier)
        draft_deps = await dependencies(session, "artifact", body.draft_id) if body.draft_id else []
        if body.draft_id and not await valid_dependencies(session, [*parent_deps, *draft_deps], lock=True):
            raise HTTPException(409, "草稿材料已失效")
        if not body.draft_id:
            parent_valid = await valid_dependencies(session, parent_deps, lock=True)
        value = await session.scalar(
            select(DerivedArtifact).where(DerivedArtifact.id == identifier).with_for_update()
        )
        if not value or value.kind not in {"review", "weekly"}:
            raise HTTPException(404, "日报不存在")
        if value.edit_revision != body.expected_revision:
            raise HTTPException(409, "日报已被另一个页面修改，请刷新后重试")
        if body.draft_id:
            from workmemory.evidence.service import copy_dependencies
            draft = await session.scalar(select(DerivedArtifact).where(DerivedArtifact.id == body.draft_id).with_for_update())
            if not draft or draft.scope != str(identifier) or draft.kind != "report_draft" or draft.status != "ready" or draft.requested_hash != body.draft_hash:
                raise HTTPException(409, "草稿已变化，请重新核对")
            from sqlalchemy import delete

            from workmemory.db.models import EvidenceDependency
            await session.execute(delete(EvidenceDependency).where(EvidenceDependency.consumer_kind == "artifact", EvidenceDependency.consumer_id == identifier))
            await copy_dependencies(session, "artifact", draft.id, "artifact", identifier)
        if not body.draft_id and not parent_valid:
            # Explicitly re-confirming a retained manual edit makes it a new statement.
            from sqlalchemy import delete

            from workmemory.db.models import EvidenceDependency
            await session.execute(delete(EvidenceDependency).where(EvidenceDependency.consumer_kind == "artifact", EvidenceDependency.consumer_id == identifier))
        await invalidate_object(session, "review", identifier)
        value.user_text = body.text
        value.edit_revision += 1
        value.needs_review = False
        from workmemory.companion.state import emit
        await emit(session, "changed")
        return artifact_view(value)


@router.get("/reviews/{identifier}/saved-edit")
async def saved_edit(identifier: uuid.UUID, request: Request):
    async with rt(request).sessions() as session:
        value = await session.get(DerivedArtifact, identifier)
        if not value or value.kind not in {"review", "weekly"}:
            raise HTTPException(404, "回顾不存在")
        return {
            "text": value.user_text,
            "needs_review": value.needs_review,
            "revision": value.edit_revision,
            "notice": "这是你保存的人工编辑，可能含已失效的信息；重新确认前不会作为证据使用。",
        }
