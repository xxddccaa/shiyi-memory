import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from workmemory.artifacts.service import reconcile_todos
from workmemory.db.models import ChatMessage, Conversation, Reminder, Todo
from workmemory.jobs.queue import now
from workmemory.reminders.schedule import due_occurrences, validate_schedule, wall_datetime, zone
from workmemory.reminders.scheduler import act, tick
from workmemory.workspace.operations import ProposalInput, decide, propose
from workmemory.workspace.service import remove_todo, update_todo


async def add(runtime, **overrides):
    due = now() + timedelta(hours=1)
    fields = dict(due_at=due, timezone="Asia/Shanghai", repeat="once", weekdays=[])
    fields.update(overrides)
    clock = min(now(), fields['due_at'] - timedelta(seconds=1))
    schedule = validate_schedule(fields, clock=clock)
    async with runtime.sessions.begin() as session:
        row = Todo(id=uuid.uuid4(), text="合成：写周报", origin="manual", confirmed=True, **schedule)
        session.add(row)
    return row.id


async def test_no_extraction_and_missing_time(runtime):
    await reconcile_todos(None, None, ["明天需要补充测试"])
    async with runtime.sessions.begin() as session:
        c = Conversation(id=uuid.uuid4(), title="合成")
        session.add(c)
        await session.flush()
        with pytest.raises(HTTPException) as error:
            await propose(session, c.id, ProposalInput(action="todo.create", text="写周报"))
        assert error.value.status_code == 422
        assert await session.scalar(select(func.count()).select_from(Todo)) == 0


async def test_due_once_concurrent_tick_and_restart(runtime):
    identifier = await add(runtime)
    async with runtime.sessions() as session:
        due = (await session.get(Todo, identifier)).due_at
    await tick(runtime, due - timedelta(seconds=1))
    async with runtime.sessions() as session:
        assert not (await session.scalars(select(Reminder))).all()
    await asyncio.gather(tick(runtime, due), tick(runtime, due))
    await tick(runtime, due + timedelta(seconds=20))
    async with runtime.sessions() as session:
        reminders = (await session.scalars(select(Reminder))).all()
        assert len(reminders) == 1
        reminder = reminders[0]
        assert reminder.message_id and reminder.shown_revision == 0
        assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 1
    async with runtime.sessions.begin() as session:
        await act(session, reminder.id, 1, "shown", due)
    async with runtime.sessions() as session:
        assert (await session.get(Reminder, reminder.id)).shown_revision == 1


async def test_snooze_is_once_and_idempotent_then_complete(runtime):
    identifier = await add(runtime)
    async with runtime.sessions() as session:
        due = (await session.get(Todo, identifier)).due_at
    await tick(runtime, due)
    async with runtime.sessions.begin() as session:
        reminder = await session.scalar(select(Reminder))
        rid = reminder.id
        result = await act(session, rid, 1, "snooze", due)
        assert result["status"] == "snoozed"
    async with runtime.sessions.begin() as session:
        result = await act(session, rid, 1, "snooze", due)
        assert result["revision"] == 2
    await tick(runtime, due + timedelta(minutes=9))
    async with runtime.sessions() as session:
        assert (await session.get(Reminder, rid)).status == "snoozed"
    await tick(runtime, due + timedelta(minutes=10))
    async with runtime.sessions.begin() as session:
        result = await act(session, rid, 2, "complete", due + timedelta(minutes=10))
        assert result["status"] == "completed"
    await tick(runtime, due + timedelta(hours=1))
    async with runtime.sessions() as session:
        assert (await session.get(Todo, identifier)).done
        assert await session.scalar(select(func.count()).select_from(Reminder)) == 1


async def test_repeat_missed_collapses_and_completion_keeps_schedule(runtime):
    due = datetime(2026, 10, 5, 1, tzinfo=UTC)
    identifier = await add(runtime, due_at=due, repeat="weekdays")
    resumed = datetime(2026, 10, 12, 3, tzinfo=UTC)
    await tick(runtime, resumed)
    async with runtime.sessions.begin() as session:
        todo = await session.get(Todo, identifier)
        reminder = await session.scalar(select(Reminder))
        assert todo.due_at == datetime(2026, 10, 13, 1, tzinfo=UTC)
        assert reminder.planned_at == datetime(2026, 10, 12, 1, tzinfo=UTC)
        assert reminder.missed_count == 6
        await act(session, reminder.id, 1, "complete", resumed)
        assert not todo.done
    await tick(runtime, datetime(2026, 10, 13, 1, tzinfo=UTC))
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Reminder)) == 2


async def test_reschedule_pause_delete_invalidate_old_controls_and_text(runtime):
    identifier = await add(runtime)
    async with runtime.sessions() as session:
        due = (await session.get(Todo, identifier)).due_at
    await tick(runtime, due)
    async with runtime.sessions.begin() as session:
        reminder = await session.scalar(select(Reminder))
        rid = reminder.id
        todo = await session.get(Todo, identifier)
        await update_todo(session, todo, {"due_at": now() + timedelta(days=2)})
    async with runtime.sessions.begin() as session:
        with pytest.raises(HTTPException):
            await act(session, rid, 1, "complete", due)
    async with runtime.sessions.begin() as session:
        todo = await session.get(Todo, identifier)
        await update_todo(session, todo, {"paused": True})
    await tick(runtime, now() + timedelta(days=3))
    async with runtime.sessions.begin() as session:
        assert await session.scalar(select(func.count()).select_from(Reminder)) == 0
        messages = (await session.scalars(select(ChatMessage))).all()
        assert all(m.content is None for m in messages)
        await remove_todo(session, await session.get(Todo, identifier))


async def test_time_card_expired_before_confirmation(runtime):
    async with runtime.sessions.begin() as session:
        c = Conversation(id=uuid.uuid4(), title="合成")
        session.add(c)
        await session.flush()
        item = await propose(session, c.id, ProposalInput(action="todo.create", text="周报", due_at=now()+timedelta(hours=1)))
        from workmemory.db.models import Operation
        op = await session.get(Operation, uuid.UUID(item["id"]))
        op.payload = {**op.payload, "due_at": (now()-timedelta(seconds=1)).isoformat()}
    with pytest.raises(HTTPException):
        await decide(runtime, uuid.UUID(item["id"]), 1, True)
    async with runtime.sessions() as session:
        assert not (await session.scalars(select(Todo))).all()


def test_dst_and_weekday_calendar():
    from datetime import date, time
    tz = zone("America/New_York")
    assert wall_datetime(date(2026,3,8), time(2,30), tz) == datetime(2026,3,8,7,tzinfo=UTC)
    assert wall_datetime(date(2026,11,1), time(1,30), tz) == datetime(2026,11,1,5,30,tzinfo=UTC)
    todo = SimpleNamespace(due_at=datetime(2026,3,7,7,30,tzinfo=UTC), repeat="daily", weekdays=[], timezone="America/New_York", local_time="02:30:00")
    latest, count, following = due_occurrences(todo, datetime(2026,3,8,8,tzinfo=UTC))
    assert count == 2 and latest == datetime(2026,3,8,7,tzinfo=UTC)
    assert following == datetime(2026,3,9,6,30,tzinfo=UTC)


async def test_agent_cannot_invent_time_or_quote_assistant_time(runtime):
    from workmemory.db.models import AgentRun, Operation
    async with runtime.sessions.begin() as session:
        conversation = Conversation(id=uuid.uuid4(), title="合成时间授权")
        session.add(conversation)
        await session.flush()
        message = ChatMessage(id=uuid.uuid4(), conversation_id=conversation.id, role="user", content="提醒我整理周报")
        session.add_all([message, ChatMessage(conversation_id=conversation.id, role="assistant", content="可以十分钟后吗？")])
        await session.flush()
        run = AgentRun(id=uuid.uuid4(), conversation_id=conversation.id, message_id=message.id, status="running")
        session.add(run)
        await session.flush()
        with pytest.raises(HTTPException, match="逐字引用"):
            await propose(session, conversation.id, ProposalInput(action="todo.create", text="周报",
                due_at=now()+timedelta(minutes=10), time_expression="十分钟后"), run.id)
        assert not (await session.scalars(select(Operation))).all()
        message.content = "24小时后提醒我整理周报"
        item = await propose(session, conversation.id, ProposalInput(action="todo.create", text="周报",
            due_at=now()+timedelta(hours=24), time_expression="24小时后"), run.id)
        assert item["status"] == "pending"


async def test_authenticated_form_retries_and_stale_edits(runtime):
    import httpx
    from workmemory.main import create_app
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama,
                     memory=runtime.memory, run_worker=False, mcp_store=runtime.mcp.store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
                                    headers={"Authorization": "Bearer " + runtime.settings.api_token}) as client:
            body = {"request_id":str(uuid.uuid4()), "text":"合成明确设置", "due_at":(now()+timedelta(hours=1)).isoformat(), "timezone":"Asia/Shanghai"}
            first, repeated = await asyncio.gather(*(client.post("/api/todos",json=body) for _ in range(2)))
            assert first.status_code == repeated.status_code == 200
            assert first.json()["id"] == repeated.json()["id"]
            assert (await client.post("/api/todos",json={**body,"text":"不同事项"})).status_code == 409
            identifier = first.json()["id"]
            assert (await client.patch(f"/api/todos/{identifier}",json={"expected_revision":1,"paused":True})).status_code == 200
            assert (await client.patch(f"/api/todos/{identifier}",json={"expected_revision":1,"done":True})).status_code == 409
            assert (await client.patch(f"/api/todos/{identifier}",json={"expected_revision":2,"due_at":None})).status_code == 422


@pytest.mark.parametrize("action", ["complete", "snooze", "dismiss"])
async def test_occurrence_actions_clear_old_evidence_without_changing_schedule(runtime, action):
    from workmemory.db.models import PiSession
    from workmemory.evidence.service import remember, resolve

    identifier = await add(runtime, repeat="daily")
    async with runtime.sessions() as session:
        due = (await session.get(Todo, identifier)).due_at
    await tick(runtime, due)
    async with runtime.sessions.begin() as session:
        todo = await session.get(Todo, identifier)
        reminder = await session.scalar(select(Reminder))
        before = todo.revision
        cid, rid, mid = todo.conversation_id, reminder.id, reminder.message_id
        session.add(PiSession(id=cid, data={"synthetic": "pending occurrence"}))
        await session.flush()
        await remember(session, "pi_session", cid, [await resolve(session, f"todo:{identifier}")])
        await act(session, rid, 1, "shown", due)
        assert todo.revision == before
        assert (await session.get(ChatMessage, mid)).content is not None
        assert (await session.get(PiSession, cid)).data is not None
    async with runtime.sessions.begin() as session:
        await act(session, rid, 1, action, due)
    async with runtime.sessions() as session:
        todo = await session.get(Todo, identifier)
        assert todo.revision == before + 1 and todo.schedule_revision == 1 and not todo.done
        assert (await session.get(ChatMessage, mid)).content is None
        assert (await session.get(PiSession, cid)).data is None
    async with runtime.sessions.begin() as session:
        await act(session, rid, 1, action, due)
        assert (await session.get(Todo, identifier)).revision == before + 1


async def test_snooze_wrong_fields_explain_correction_and_valid_proposal_executes(runtime):
    identifier = await add(runtime, due_at=now()-timedelta(seconds=5), repeat="daily")
    await tick(runtime)
    async with runtime.sessions.begin() as session:
        todo = await session.get(Todo, identifier)
        reminder = await session.scalar(select(Reminder))
        cid, rid = todo.conversation_id, reminder.id
        with pytest.raises(HTTPException) as error:
            await propose(session, cid, ProposalInput(action="todo.snooze", target_id=identifier,
                reminder_id=rid, reminder_revision=1, due_at=now()+timedelta(minutes=10), timezone="Asia/Shanghai"))
        assert "不接受字段：due_at, timezone" in error.value.detail
        assert "请仅传 action、target_id、reminder_id、reminder_revision" in error.value.detail
        card = await propose(session, cid, ProposalInput(action="todo.snooze", target_id=identifier,
            reminder_id=rid, reminder_revision=1))
    result = await decide(runtime, uuid.UUID(card["id"]), card["revision"], True)
    assert result["status"] == "executed"
    async with runtime.sessions() as session:
        assert (await session.get(Reminder, rid)).status == "snoozed"
        assert not (await session.get(Todo, identifier)).done


@pytest.mark.parametrize("changed", [dict(repeat="daily"), dict(timezone="Etc/UTC"), dict(weekdays=[2]), dict(paused=True)])
async def test_merge_suggestions_exclude_different_schedules(runtime, changed):
    from workmemory.companion.suggestions import todo_suggestions

    due = now() + timedelta(days=1)
    async with runtime.sessions.begin() as session:
        session.add_all([Todo(text="补充发布验收测试", confirmed=True, due_at=due),
                         Todo(text="补充发布的验收测试", confirmed=True, due_at=due, **changed)])
    async with runtime.sessions() as session:
        assert (await todo_suggestions(session))["items"] == []
