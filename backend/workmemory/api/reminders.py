"""Authenticated UI endpoints; these are deliberately not agent execution tools."""
import hashlib
import json
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from workmemory.companion.state import emit, preferences, save_preferences
from workmemory.db.models import AppState, Conversation, Reminder, Todo
from workmemory.jobs.queue import now
from workmemory.reminders.schedule import validate_schedule
from workmemory.reminders.scheduler import act, view
from workmemory.workspace.service import update_todo

router = APIRouter(prefix="/api")


class ScheduleForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    text: str = Field(min_length=1, max_length=1000)
    due_at: AwareDatetime
    timezone: str
    repeat: Literal["once", "daily", "weekdays", "weekly"] = "once"
    weekdays: list[int] = Field(default_factory=list)
    notify_pet: bool = True
    project_id: uuid.UUID | None = None


class ScheduleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    due_at: AwareDatetime | None = None
    timezone: str | None = None
    repeat: Literal["once", "daily", "weekdays", "weekly"] | None = None
    weekdays: list[int] | None = None
    notify_pet: bool | None = None
    paused: bool | None = None
    dismissed: bool | None = None
    done: bool | None = None


class ReminderAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    action: Literal["shown", "complete", "snooze", "dismiss"]


def todo_view(t):
    return {"id": str(t.id), "episode_id": str(t.episode_id) if t.episode_id else None,
            "project_id": str(t.project_id) if t.project_id else None, "revision": t.revision,
            "text": t.text, "confirmed": t.confirmed, "done": t.done, "due_at": t.due_at,
            "dismissed": t.dismissed, "paused": t.paused, "timezone": t.timezone,
            "repeat": t.repeat, "weekdays": t.weekdays, "notify_pet": t.notify_pet,
            "conversation_id": str(t.conversation_id) if t.conversation_id else None}


@router.get("/todos")
async def todos(request: Request):
    async with request.app.state.runtime.sessions() as session:
        rows = (await session.scalars(select(Todo).where(Todo.dismissed.is_(False))
            .order_by(Todo.done, Todo.paused, Todo.due_at.nulls_last(), Todo.id))).all()
        return [todo_view(t) for t in rows]


@router.post("/todos")
async def create(body: ScheduleForm, request: Request):
    rt = request.app.state.runtime
    digest = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    async with rt.sessions.begin() as session:
        # Same form retry is atomic, including conversation and notification creation.
        await session.execute(text("SELECT pg_advisory_xact_lock(738452910)"))
        key = f"schedule-request:{body.request_id}"
        existing = await session.get(AppState, key)
        if existing:
            if existing.value["digest"] != digest:
                raise HTTPException(409, "请求标识已用于其他内容，请重新提交")
            todo = await session.get(Todo, uuid.UUID(existing.value["id"]))
            if not todo:
                raise HTTPException(409, "该次保存的事项已删除，请新建事项")
            return todo_view(todo)
        schedule = validate_schedule(body.model_dump(), clock=now(), default_timezone=rt.settings.timezone)
        from workmemory.db.models import Project
        if body.project_id:
            project = await session.scalar(select(Project).where(Project.id == body.project_id).with_for_update())
            if not project or project.state in {"merged", "deleted"}:
                raise HTTPException(422, "项目不存在、已合并或已删除")
        conversation = Conversation(id=uuid.uuid4(), title="定时提醒")
        session.add(conversation)
        await session.flush()
        todo = Todo(id=uuid.uuid4(), text=body.text.strip(), project_id=body.project_id,
                    origin="manual", confirmed=True, conversation_id=conversation.id, **schedule)
        if not todo.text:
            raise HTTPException(422, "事项内容不能为空")
        session.add(todo)
        # Store only a digest of request content; deletion cannot leave a copy in app_state.
        session.add(AppState(key=key, value={"id": str(todo.id), "digest": digest}))
        if body.notify_pet:
            await save_preferences(session, {"reminders_enabled": True})
        await emit(session, "todo", todo.id)
        await session.flush()
        return todo_view(todo)


@router.patch("/todos/{todo_id}")
async def change(todo_id: uuid.UUID, body: ScheduleChange, request: Request):
    async with request.app.state.runtime.sessions.begin() as session:
        todo = await session.scalar(select(Todo).where(Todo.id == todo_id).with_for_update())
        if not todo or todo.dismissed:
            raise HTTPException(404, "事项不存在")
        if todo.revision != body.expected_revision:
            raise HTTPException(409, "事项已在另一端改变，请刷新")
        await update_todo(session, todo, body.model_dump(exclude_unset=True, exclude={"expected_revision"}))
        return todo_view(todo)


@router.get("/reminders")
async def reminders(request: Request, pet: bool = False):
    async with request.app.state.runtime.sessions() as session:
        if pet and not (await preferences(session))["reminders_enabled"]:
            return []
        query = select(Reminder, Todo).outerjoin(Todo, Reminder.todo_id == Todo.id).where(
            Reminder.dismissed.is_(False), Reminder.status == "pending", Reminder.due_at <= now(),
            (Reminder.kind != "todo") | ((Todo.dismissed.is_(False)) & Todo.done.is_(False) & Todo.paused.is_(False)
                & (Reminder.schedule_revision == Todo.schedule_revision)),
        )
        if pet:
            query = query.where(Todo.notify_pet.is_(True))
        rows = (await session.execute(query.order_by(Reminder.due_at, Reminder.id).limit(100))).all()
        return [view(r, t) for r, t in rows]


@router.post("/reminders/{identifier}/action")
async def action(identifier: uuid.UUID, body: ReminderAction, request: Request):
    async with request.app.state.runtime.sessions.begin() as session:
        return await act(session, identifier, body.expected_revision, body.action)


@router.post("/reminders/{identifier}/dismiss")
async def dismiss(identifier: uuid.UUID, request: Request):
    # Compatibility for non-task daily/resume cards. Task UI must send the exact revision.
    async with request.app.state.runtime.sessions.begin() as session:
        row = await session.get(Reminder, identifier)
        if not row:
            raise HTTPException(404, "提醒不存在")
        if row.kind == "todo":
            raise HTTPException(422, "请使用带提醒版本的操作接口")
        return await act(session, identifier, row.revision, "dismiss")
