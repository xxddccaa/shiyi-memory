import asyncio
import json
import uuid
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from workmemory.companion.proactive_config import ProactiveSettings
from workmemory.companion.state import emit, preferences, save_preferences
from workmemory.db.models import (
    AgentRun,
    ClientEvent,
    Conversation,
    Feedback,
    Operation,
    ProjectAlias,
)
from workmemory.evidence.service import dependencies, valid_dependencies
from workmemory.jobs.queue import now
from workmemory.pets.skins import SkinError
from workmemory.workspace.operations import MODELS, ProposalInput, decide, propose, view

router = APIRouter(prefix="/api")


@router.get("/client/state")
async def client_state(request: Request):
    async with request.app.state.runtime.sessions() as session:
        value = await preferences(session)
        value["timezone"] = request.app.state.runtime.settings.timezone
        value["cursor"] = await session.scalar(select(func.max(ClientEvent.id))) or 0
        identifier = value.get("recent_conversation_id")
        value["agent_status"] = None
        if identifier:
            value["agent_status"] = await session.scalar(
                select(AgentRun.status)
                .where(AgentRun.conversation_id == uuid.UUID(identifier))
                .order_by(AgentRun.created_at.desc())
                .limit(1)
            )
        value["pending_operations"] = await session.scalar(
            select(func.count())
            .select_from(Operation)
            .where(Operation.status == "pending", Operation.expires_at > now())
        )
        return value


class PreferencesChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    proactive: ProactiveSettings | None = None
    recent_conversation_id: uuid.UUID | None = None
    pet_visible: bool | None = None
    pet_motion_enabled: bool | None = None
    pet_size: int | None = Field(default=None, ge=48, le=240)
    pet_skin: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    reminders_enabled: bool | None = None
    hide_fullscreen: bool | None = None
    resource_policy: Literal["standard", "saving"] | None = None
    resource_capture: bool | None = None
    backfill_enabled: bool | None = None


@router.patch("/client/state")
async def change_preferences(body: PreferencesChange, request: Request):
    changes = body.model_dump(mode="json", exclude_unset=True, exclude={"expected_revision"})
    if any(v is None and k != "recent_conversation_id" for k, v in changes.items()):
        raise HTTPException(422, "设置不能为空")
    if body.pet_skin and body.pet_skin != "builtin-cat":
        try:
            await request.app.state.runtime.pet_library.preview(body.pet_skin)
        except SkinError as exc:
            raise HTTPException(404, "形象不存在或文件已损坏，请重新导入") from exc
    async with request.app.state.runtime.sessions.begin() as session:
        if body.recent_conversation_id:
            conversation = await session.get(Conversation, body.recent_conversation_id)
            if not conversation or conversation.expired:
                raise HTTPException(404, "会话不存在")
        return await save_preferences(session, changes, body.expected_revision)


@router.get("/client/events")
async def client_events(request: Request, after: int = Query(0, ge=0)):
    rt = request.app.state.runtime

    async def stream():
        cursor = after
        while not await request.is_disconnected():
            async with rt.sessions() as session:
                lower, upper = (
                    await session.execute(
                        select(func.min(ClientEvent.id), func.max(ClientEvent.id))
                    )
                ).one()
                if cursor and ((lower and cursor < lower - 1) or cursor > (upper or 0)):
                    cursor = upper or 0
                    yield f"id: {cursor}\nevent: reset\ndata: {{}}\n\n"
                rows = (
                    await session.scalars(
                        select(ClientEvent)
                        .where(ClientEvent.id > cursor)
                        .order_by(ClientEvent.id)
                        .limit(200)
                    )
                ).all()
            for row in rows:
                cursor = row.id
                data = json.dumps({"kind": row.kind, "reference": row.reference})
                yield f"id: {cursor}\nevent: change\ndata: {data}\n\n"
            if not rows:
                yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


@router.get("/operations")
async def operations(request: Request, conversation_id: uuid.UUID | None = None):
    rt = request.app.state.runtime
    async with rt.sessions.begin() as session:
        query = (
            select(Operation)
            .where(Operation.created_at > now() - timedelta(days=30))
            .order_by(Operation.created_at.desc())
            .limit(100)
        )
        if conversation_id:
            query = query.where(Operation.conversation_id == conversation_id)
        result = []
        for op in (await session.scalars(query)).all():
            if op.status == "pending" and not await valid_dependencies(
                session,
                await dependencies(session, "operation", op.id),
                retention_days=rt.settings.retention_days,
            ):
                op.status, op.payload, op.preview = "invalidated", None, None
                op.revision += 1
            result.append(view(op))
        return {"items": result}


@router.post("/conversations/{identifier}/operations", status_code=201)
async def create_operation(identifier: uuid.UUID, body: ProposalInput, request: Request):
    async with request.app.state.runtime.sessions.begin() as session:
        return await propose(session, identifier, body)


class Decision(BaseModel):
    expected_revision: int = Field(ge=1)
    action: Literal["confirm", "reject"]


@router.post("/operations/{identifier}/decision")
async def decision(identifier: uuid.UUID, body: Decision, request: Request):
    return await decide(
        request.app.state.runtime, identifier, body.expected_revision, body.action == "confirm"
    )


@router.get("/projects/{identifier}/aliases")
async def aliases(identifier: uuid.UUID, request: Request):
    async with request.app.state.runtime.sessions() as session:
        return {
            "items": list(
                (
                    await session.scalars(
                        select(ProjectAlias.name)
                        .where(ProjectAlias.project_id == identifier)
                        .order_by(ProjectAlias.name)
                    )
                ).all()
            )
        }


class FeedbackInput(BaseModel):
    object_kind: Literal["episode", "memory", "review", "project", "todo"]
    object_id: uuid.UUID
    expected_revision: int = Field(ge=0)
    value: Literal["useful", "inaccurate", "wrong_project", "ignored"]


@router.post("/feedback")
async def feedback(body: FeedbackInput, request: Request):
    async with request.app.state.runtime.sessions.begin() as session:
        model = MODELS[body.object_kind]
        row = await session.scalar(
            select(model).where(model.id == body.object_id).with_for_update()
        )
        if (
            not row
            or getattr(row, "deleted_at", None)
            or getattr(row, "status", None) in {"dismissed", "invalidated"}
            or getattr(row, "state", None) in {"merged", "deleted"}
        ):
            raise HTTPException(404, "对象不存在")
        revision = getattr(row, "revision", getattr(row, "edit_revision", 0))
        if revision != body.expected_revision:
            raise HTTPException(409, "内容已更新，请重新反馈")
        await session.execute(
            insert(Feedback)
            .values(
                id=uuid.uuid4(),
                object_kind=body.object_kind,
                object_id=body.object_id,
                revision=revision,
                value=body.value,
            )
            .on_conflict_do_update(
                index_elements=[Feedback.object_kind, Feedback.object_id, Feedback.revision],
                set_={"value": body.value},
            )
        )
        return {"saved": True}


@router.get("/memory/backfill")
async def backfill_status(request: Request):
    from workmemory.companion.backfill import status

    return await status(request.app.state.runtime)


@router.get("/todos/merge-suggestions")
async def merge_suggestions(request: Request):
    from workmemory.companion.suggestions import todo_suggestions

    async with request.app.state.runtime.sessions() as session:
        return await todo_suggestions(session)


class RejectMerge(BaseModel):
    left_id: uuid.UUID
    right_id: uuid.UUID
    left_revision: int = Field(ge=1)
    right_revision: int = Field(ge=1)


@router.post("/todos/merge-suggestions/reject")
async def reject_merge(body: RejectMerge, request: Request):
    from workmemory.db.models import Todo, TodoMergeRejection

    if body.left_id == body.right_id:
        raise HTTPException(422, "请选择两条待办")
    async with request.app.state.runtime.sessions.begin() as session:
        rows = (
            await session.scalars(
                select(Todo)
                .where(Todo.id.in_([body.left_id, body.right_id]))
                .order_by(Todo.id)
                .with_for_update()
            )
        ).all()
        expected = {body.left_id: body.left_revision, body.right_id: body.right_revision}
        if len(rows) != 2 or any(row.revision != expected[row.id] for row in rows):
            raise HTTPException(409, "待办已经变化，请刷新")
        left, right = sorted(expected, key=str)
        await session.execute(
            insert(TodoMergeRejection).values(left_id=left, right_id=right).on_conflict_do_nothing()
        )
        await emit(session, "changed")
        return {"rejected": True}
