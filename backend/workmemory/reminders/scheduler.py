"""Durable due reminders, independent of model and background maintenance queues."""
import asyncio
import logging
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select

from workmemory.companion.state import emit
from workmemory.db.models import ChatMessage, Conversation, Reminder, Todo
from workmemory.evidence.service import invalidate_object, remember, resolve
from workmemory.jobs.queue import now
from workmemory.reminders.schedule import due_occurrences, zone

log = logging.getLogger(__name__)


def view(row, todo=None):
    return {"id": str(row.id), "kind": row.kind, "text": row.text,
            "episode_id": str(row.episode_id) if row.episode_id else None,
            "todo_id": str(row.todo_id) if row.todo_id else None,
            "due_at": row.due_at.isoformat(),
            "planned_at": row.planned_at.isoformat() if row.planned_at else None,
            "revision": row.revision, "status": row.status,
            "needs_display": row.shown_revision < row.revision,
            "missed_count": row.missed_count,
            "notify_pet": bool(todo and todo.notify_pet),
            "conversation_id": str(todo.conversation_id) if todo and todo.conversation_id else None,
            "todo_revision": todo.revision if todo else None}


async def deliver_message(session, reminder, todo):
    conversation = await session.get(Conversation, todo.conversation_id) if todo.conversation_id else None
    if not conversation or conversation.expired:
        conversation = Conversation(id=uuid.uuid4(), title="定时提醒")
        session.add(conversation)
        await session.flush()
        todo.conversation_id = conversation.id
    if reminder.message_id:
        return
    local = reminder.planned_at.astimezone(zone(todo.timezone))
    text = f"到时间啦，该{todo.text}了。\n设置时间：{local:%Y-%m-%d %H:%M}（{todo.timezone}）。"
    if reminder.missed_count:
        text += f"期间共错过 {reminder.missed_count} 次，已合并提醒。"
    reminder.text = text
    message = ChatMessage(id=uuid.uuid4(), conversation_id=conversation.id, role="assistant", content=text)
    session.add(message)
    await session.flush()
    reminder.message_id = message.id
    source = await resolve(session, f"todo:{todo.id}")
    if source:
        await remember(session, "message", message.id, [source])
    conversation.updated_at = now()
    await emit(session, "message", conversation.id)


async def invalidate_todo_state(session, todo):
    # Reminder state is part of the evidence returned with its parent Todo.
    # Keep the schedule version unchanged: existing occurrence controls remain valid.
    await invalidate_object(session, "todo", todo.id)
    todo.revision += 1


async def tick(runtime, clock=None):
    clock = clock or now()
    async with runtime.sessions.begin() as session:
        todos = (await session.scalars(select(Todo).where(
            Todo.origin == "manual", Todo.confirmed.is_(True), Todo.done.is_(False),
            Todo.dismissed.is_(False), Todo.paused.is_(False), Todo.due_at <= clock,
        ).order_by(Todo.id).with_for_update(skip_locked=True))).all()
        for todo in todos:
            latest, count, upcoming = due_occurrences(todo, clock)
            key = f"schedule:{todo.id}:{todo.schedule_revision}:{latest.isoformat()}"
            exists = await session.scalar(select(Reminder.id).where(Reminder.dedupe_key == key))
            # One-shot keeps due_at for display; the persistent occurrence prevents re-creation.
            if exists:
                continue
            previous = (await session.scalars(select(Reminder).where(
                Reminder.todo_id == todo.id, Reminder.status.in_(["pending", "snoozed"])
            ).with_for_update())).all()
            missed = max(0, count - 1) + sum(1 + r.missed_count for r in previous)
            if latest < clock - timedelta(minutes=1):
                missed += 1
            await invalidate_todo_state(session, todo)
            for item in previous:
                item.status, item.dismissed = "superseded", True
            reminder = Reminder(id=uuid.uuid4(), kind="todo", todo_id=todo.id,
                episode_id=todo.episode_id, due_at=latest, planned_at=latest,
                schedule_revision=todo.schedule_revision, dedupe_key=key, text=todo.text,
                missed_count=missed, status="pending", revision=1, shown_revision=0)
            session.add(reminder)
            if upcoming:
                todo.due_at = upcoming
            await deliver_message(session, reminder, todo)
            await emit(session, "reminder", reminder.id)
        # A snoozed delivery is a new presentation of the same occurrence, not a new task.
        snoozed_ids = list((await session.scalars(select(Reminder.todo_id).where(
            Reminder.status == "snoozed", Reminder.due_at <= clock
        ).distinct())).all())
        for identifier in sorted(snoozed_ids, key=str):
            todo = await session.scalar(select(Todo).where(Todo.id == identifier).with_for_update(skip_locked=True))
            if not todo or todo.paused or todo.done or todo.dismissed:
                continue
            for reminder in (await session.scalars(select(Reminder).where(
                Reminder.todo_id == identifier, Reminder.status == "snoozed", Reminder.due_at <= clock,
                Reminder.schedule_revision == todo.schedule_revision,
            ).with_for_update())).all():
                await invalidate_todo_state(session, todo)
                reminder.status = "pending"
                await emit(session, "reminder", reminder.id)


async def act(session, identifier, expected_revision, action, clock=None):
    clock = clock or now()
    # Lock order is always Todo, then Reminder; deletion and the scheduler use the same order.
    todo_id = await session.scalar(select(Reminder.todo_id).where(Reminder.id == identifier))
    todo = await session.scalar(select(Todo).where(Todo.id == todo_id).with_for_update()) if todo_id else None
    reminder = await session.scalar(select(Reminder).where(Reminder.id == identifier).with_for_update().execution_options(populate_existing=True))
    if not reminder:
        raise HTTPException(404, "提醒不存在或已取消")
    if reminder.kind == "todo" and (not todo or todo.paused or todo.dismissed or reminder.schedule_revision != todo.schedule_revision):
        raise HTTPException(409, "事项已经修改，请刷新提醒")
    if action in {"complete", "snooze"} and reminder.kind != "todo":
        raise HTTPException(422, "此提醒不支持该操作")
    # Retrying the exact click succeeds, but cannot change a later instance revision.
    if reminder.revision != expected_revision:
        desired = {"complete": "completed", "snooze": "snoozed", "dismiss": "dismissed"}.get(action)
        if reminder.revision == expected_revision + 1 and reminder.status == desired:
            return view(reminder, todo)
        raise HTTPException(409, "提醒已经变化，请刷新")
    if reminder.status not in {"pending", "snoozed"}:
        if action == "shown" or reminder.status in {"completed", "dismissed"}:
            return view(reminder, todo)
        raise HTTPException(409, "该次提醒已结束")
    if reminder.due_at > clock or reminder.status == "snoozed":
        raise HTTPException(409, "这次提醒尚未到时间")
    if action == "shown":
        if reminder.shown_revision != reminder.revision:
            reminder.shown_revision = reminder.revision
            await emit(session, "reminder", reminder.id)
    else:
        if todo:
            await invalidate_todo_state(session, todo)
        reminder.revision += 1
        if action == "snooze":
            upcoming = todo.due_at if todo.repeat != "once" else None
            reminder.due_at = min(clock + timedelta(minutes=10), upcoming) if upcoming else clock + timedelta(minutes=10)
            reminder.status = "snoozed"
        else:
            reminder.status = "completed" if action == "complete" else "dismissed"
            reminder.dismissed = True
            if action == "complete" and todo.repeat == "once":
                todo.done = True
            if action == "complete":
                from workmemory.companion.feedback import completed
                await completed(session, reminder)
        await emit(session, "reminder", reminder.id)
    return view(reminder, todo)


async def reminder_loop(runtime):
    while True:
        try:
            await tick(runtime)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Reminder scheduler waiting: %s", type(exc).__name__)
        await asyncio.sleep(1)
