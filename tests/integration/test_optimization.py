"""Optimization acceptance uses synthetic records in an isolated real PostgreSQL DB."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from test_pipeline import ready
from workmemory.artifacts.drafts import DraftInput, generate, report, request_draft
from workmemory.companion import proactive
from workmemory.companion.feedback import completed, completion_tick, feedback
from workmemory.companion.proactive_config import ProactiveSettings
from workmemory.companion.state import preferences, save_preferences
from workmemory.db.models import (
    AppState,
    CaptureIncident,
    ChatMessage,
    Conversation,
    DerivedArtifact,
    Episode,
    Job,
    MaintenanceTask,
    MemoryCandidate,
    ProactiveUtterance,
    Reminder,
    Todo,
)
from workmemory.episodes.service import mutate_episode
from workmemory.evidence.confirmed import SourceMemoryInput, propose_memory
from workmemory.evidence.service import dependencies, remember, resolve
from workmemory.jobs.queue import claim, enqueue, finish, now
from workmemory.main import create_app
from workmemory.maintenance.service import observe_capture, schedule
from workmemory.memory.availability import BackendOffline, record
from workmemory.memory.sync import sync_job
from workmemory.workspace.operations import decide

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(runtime):
    app = create_app(
        runtime.settings,
        engine=runtime.engine,
        ollama=runtime.ollama,
        memory=runtime.memory,
        run_worker=False,
        mcp_store=runtime.mcp.store,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {runtime.settings.api_token}"},
        ) as client:
            yield client


async def confirm_source(runtime, identifier=None, text="我确认的工作成果"):
    identifier = identifier or await ready(runtime)
    async with runtime.sessions() as session:
        episode = await session.get(Episode, identifier)
        body = SourceMemoryInput(
            request_id=uuid.uuid4(),
            expected_revision=episode.revision,
            text=text,
            date=now().astimezone(ZoneInfo(runtime.settings.timezone)).date(),
        )
    async with runtime.sessions.begin() as session:
        proposal = await propose_memory(session, identifier, body)
    result = await decide(runtime, uuid.UUID(proposal["id"]), proposal["revision"], True)
    assert result["status"] == "executed"
    return identifier, uuid.UUID(result["result"]["object_id"]), body, proposal


async def test_confirmation_is_independent_idempotent_and_source_correction_cascades(
    runtime, client
):
    eid, mid, body, proposal = await confirm_source(runtime)
    async with runtime.sessions.begin() as session:
        assert (await propose_memory(session, eid, body))["id"] == proposal["id"]
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 0
        assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 0
        memory = await session.get(MemoryCandidate, mid)
        assert memory.source_episode_id == eid and memory.status == "confirmed"
        assert memory.conversation_id is None and memory.message_id is None
        artifact = DerivedArtifact(
            id=uuid.uuid4(), kind="test", scope="test", body={"text": "旧内容"}, status="ready"
        )
        session.add(artifact)
        await session.flush()
        await remember(session, "artifact", artifact.id, [await resolve(session, f"memory:{mid}")])
        aid = artifact.id
        await mutate_episode(session, await session.get(Episode, eid), "经本人纠正的新原文")
    async with runtime.sessions() as session:
        value = await session.get(MemoryCandidate, mid)
        assert value.status == "needs_review" and value.text
        assert await resolve(session, f"memory:{mid}") is None
        assert (await session.get(DerivedArtifact, aid)).body is None
        revision = value.revision
    response = await client.patch(
        f"/api/memory/candidates/{mid}",
        json={"action": "confirm", "expected_revision": revision, "text": "重新核对后的结论"},
    )
    assert response.status_code == 200, response.text
    async with runtime.sessions.begin() as session:
        assert (await resolve(session, f"memory:{mid}"))["content"] == "重新核对后的结论"
        await mutate_episode(session, await session.get(Episode, eid))
    async with runtime.sessions() as session:
        value = await session.get(MemoryCandidate, mid)
        assert value.status == "dismissed" and value.text is None


async def test_natural_expiry_retains_confirmation_and_conflicting_request_is_rejected(runtime):
    eid, mid, body, _ = await confirm_source(runtime)
    async with runtime.sessions.begin() as session:
        e = await session.get(Episode, eid)
        e.evidence_expires_at = now() - timedelta(seconds=1)
    from workmemory.evidence.retention import expire_excerpts

    async with runtime.sessions.begin() as session:
        await expire_excerpts(session)
    async with runtime.sessions.begin() as session:
        assert await resolve(session, f"summary:{eid}") is None
        assert await resolve(session, f"memory:{mid}")
        with pytest.raises(HTTPException) as error:
            await propose_memory(session, eid, body.model_copy(update={"text": "改变的内容"}))
        assert error.value.status_code == 409


async def test_delete_preview_rechecks_new_confirmations(runtime, client):
    eid, _, _, _ = await confirm_source(runtime)
    preview = (await client.get(f"/api/episodes/{eid}/delete-preview")).json()
    assert len(preview["memories"]) == 1
    await confirm_source(runtime, eid, "第二条确认内容")
    params = {"expected_revision": preview["revision"], "memory_hash": preview["memory_hash"]}
    assert (await client.delete(f"/api/episodes/{eid}", params=params)).status_code == 409
    fresh = (await client.get(f"/api/episodes/{eid}/delete-preview")).json()
    assert len(fresh["memories"]) == 2
    assert (
        await client.delete(
            f"/api/episodes/{eid}",
            params={"expected_revision": fresh["revision"], "memory_hash": fresh["memory_hash"]},
        )
    ).status_code == 200


async def test_report_reads_no_model_and_drafts_preserve_manual_edit_with_versioned_adoption(
    runtime, client
):
    eid, mid, _, _ = await confirm_source(runtime)
    day = now().astimezone(ZoneInfo(runtime.settings.timezone)).date().isoformat()
    calls = []

    async def model(messages, **kwargs):
        data = json.loads(messages[-1]["content"])
        calls.append(data)
        return json.dumps(
            {"progress": [{"text": "已确认的成果", "evidence_ids": [data["evidence"][0]["id"]]}]}
        )

    runtime.ollama.chat = model
    view = await report(runtime, "review", day)
    assert calls == [] and len(view["materials"]) == 1
    assert view["materials"][0]["id"] == f"memory:{mid}"
    rid = uuid.UUID(view["id"])
    async with runtime.sessions() as session:
        ep = await session.get(Episode, eid)
        bad = DraftInput(
            request_id=uuid.uuid4(),
            expected_revision=0,
            materials=[{"id": f"summary:{eid}", "revision": ep.revision}],
        )
    with pytest.raises(HTTPException):
        await request_draft(runtime, rid, bad)
    body = DraftInput(
        request_id=uuid.uuid4(),
        expected_revision=0,
        materials=[{"id": f"memory:{mid}", "revision": 1}],
    )
    draft = await request_draft(runtime, rid, body)
    assert draft == await request_draft(runtime, rid, body)
    newer = await request_draft(runtime, rid, body.model_copy(update={"request_id": uuid.uuid4()}))
    with pytest.raises(HTTPException):
        await request_draft(runtime, rid, body)
    await generate(runtime, {"id": newer["id"], "hash": newer["requested_hash"], "kind": "review"})
    assert calls
    stale = await client.patch(
        f"/api/reviews/{rid}",
        json={
            "expected_revision": 0,
            "text": "用户采用旧版本",
            "draft_id": draft["id"],
            "draft_hash": draft["requested_hash"],
        },
    )
    assert stale.status_code == 409
    saved = await client.patch(
        f"/api/reviews/{rid}",
        json={
            "expected_revision": 0,
            "text": "人工修改保留",
            "draft_id": newer["id"],
            "draft_hash": newer["requested_hash"],
        },
    )
    assert saved.status_code == 200, saved.text
    async with runtime.sessions() as session:
        assert (await dependencies(session, "artifact", rid))[0].source_id == mid
    await request_draft(
        runtime, rid, body.model_copy(update={"request_id": uuid.uuid4(), "expected_revision": 1})
    )
    assert (await report(runtime, "review", day))["user_text"] == "人工修改保留"
    async with runtime.sessions.begin() as session:
        await mutate_episode(session, await session.get(Episode, eid))
    view = await report(runtime, "review", day)
    assert view["user_text"] is None and view["needs_review"]
    assert (await client.get(f"/api/reviews/{rid}/saved-edit")).json()["text"] == "人工修改保留"


async def test_draft_source_deleted_during_model_never_publishes(runtime):
    eid, mid, _, _ = await confirm_source(runtime)
    view = await report(
        runtime, "review", now().astimezone(ZoneInfo(runtime.settings.timezone)).date().isoformat()
    )
    body = DraftInput(
        request_id=uuid.uuid4(),
        expected_revision=0,
        materials=[{"id": f"memory:{mid}", "revision": 1}],
    )
    draft = await request_draft(runtime, uuid.UUID(view["id"]), body)

    async def model(messages, **kwargs):
        async with runtime.sessions.begin() as session:
            await mutate_episode(session, await session.get(Episode, eid))
        return json.dumps({"progress": [{"text": "不能发布", "evidence_ids": [f"memory:{mid}"]}]})

    runtime.ollama.chat = model
    with pytest.raises(ValueError):
        await generate(
            runtime, {"id": draft["id"], "hash": draft["requested_hash"], "kind": "review"}
        )
    async with runtime.sessions() as session:
        assert (await session.get(DerivedArtifact, uuid.UUID(draft["id"]))).body is None


async def test_offline_backend_cooldown_keeps_cleanup_and_selected_jobs_progressing(runtime):
    eid = await ready(runtime)
    async with runtime.sessions.begin() as session:
        await session.execute(delete(Job))
        for i in range(10):
            await enqueue(
                session,
                "memory",
                f"offline:{i}",
                {"backend": "graphiti", "all_used": True, "episode_id": str(eid)},
            )
    calls = []

    async def offline(*args):
        calls.append(1)
        raise ConnectionError()

    runtime.memory.adapters["graphiti"].upsert = offline
    job = await claim(runtime.sessions)
    with pytest.raises(BackendOffline) as error:
        await sync_job(runtime, job.payload)
    await finish(runtime.sessions, job, error.value)
    assert await claim(runtime.sessions) is None
    async with runtime.sessions.begin() as session:
        await enqueue(session, "memory", "selected", {"backend": "reme", "episode_id": str(eid)})
    selected = await claim(runtime.sessions)
    assert selected.payload["backend"] == "reme"
    await sync_job(runtime, selected.payload)
    await finish(runtime.sessions, selected)
    async with runtime.sessions() as session:
        retry = await session.get(Job, job.id)
        assert retry.attempts == 0 and retry.status == "pending"
        assert (await session.get(AppState, "backend_health:graphiti")).value["retry_at"]
    await record(runtime.sessions, "graphiti", True)
    assert await claim(runtime.sessions)
    assert len(calls) == 1


async def test_daily_backup_coalesces_downtime_and_respects_disable(runtime):
    clock = datetime(2026, 10, 13, 4, tzinfo=ZoneInfo(runtime.settings.timezone))
    await schedule(runtime, clock)
    await schedule(runtime, clock)
    await schedule(runtime, clock + timedelta(days=5))
    async with runtime.sessions.begin() as session:
        rows = (await session.scalars(select(MaintenanceTask))).all()
        assert sorted(row.status for row in rows) == ["pending", "superseded"]
        session.add(AppState(key="backup_policy", value={"enabled": False, "revision": 1}))
    await schedule(runtime, clock + timedelta(days=6))
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(MaintenanceTask)) == 2


async def test_capture_incidents_group_and_do_not_infer_through_sleep(runtime, monkeypatch):
    from workmemory.maintenance import service

    state = {"running": True, "paused": False, "accessibility_granted": False}
    monkeypatch.setattr(service, "collector_status", lambda _: state)
    clock = datetime(2026, 10, 13, tzinfo=UTC)
    await observe_capture(runtime, clock)
    await observe_capture(runtime, clock + timedelta(seconds=10))
    await observe_capture(runtime, clock + timedelta(hours=8))
    state["paused"] = True
    await observe_capture(runtime, clock + timedelta(hours=8, seconds=10))
    async with runtime.sessions() as session:
        rows = (
            await session.scalars(select(CaptureIncident).order_by(CaptureIncident.started_at))
        ).all()
        assert len(rows) == 2 and all(row.ended_at for row in rows)
        assert rows[0].ended_at == clock + timedelta(seconds=10)


async def test_completion_dedupe_quiet_caps_feedback_and_source_invalidation(runtime):
    async with runtime.sessions.begin() as session:
        await save_preferences(
            session,
            {
                "proactive": ProactiveSettings(
                    quiet_start="00:00", quiet_end="00:00", topics=[]
                ).model_dump()
            },
        )
        todo = Todo(
            id=uuid.uuid4(), text="敏感的事项正文", done=True, confirmed=True, origin="manual"
        )
        session.add(todo)
        await session.flush()
        reminder = Reminder(
            id=uuid.uuid4(),
            todo_id=todo.id,
            kind="todo",
            text="敏感提醒",
            due_at=now(),
            status="completed",
            dedupe_key="synthetic-complete",
        )
        session.add(reminder)
        await session.flush()
        await completed(session, reminder)
        await completed(session, reminder)
        bubble = await session.scalar(select(ProactiveUtterance))
        bubble.created_at = now() - timedelta(seconds=5)
        bid, tid = bubble.id, todo.id
    await proactive.heartbeat(runtime, True)
    assert await completion_tick(runtime) == bid
    context, _ = await proactive.prepare(runtime, bid)
    assert "敏感" not in json.dumps(context, ensure_ascii=False) and context["completion"]
    async with runtime.sessions.begin() as session:
        row = await session.get(ProactiveUtterance, bid)
        row.status, row.content = "ready", "好耶，给你一朵小红花～"
    await asyncio.gather(
        proactive.action(runtime, bid, "shown"), proactive.action(runtime, bid, "shown")
    )
    async with runtime.sessions() as session:
        state = (await session.get(AppState, "proactive")).value
        assert state["count"] == 1 and state["completion_count"] == 1
    with pytest.raises(HTTPException):
        await feedback(runtime, bid, "less", 1)  # Completion isn't a topic preference.
    async with runtime.sessions.begin() as session:
        from workmemory.workspace.service import remove_todo

        await remove_todo(session, await session.get(Todo, tid))
    async with runtime.sessions() as session:
        row = await session.get(ProactiveUtterance, bid)
        assert row.content is None and row.status == "invalidated"
    await proactive.mute(runtime, "today")
    async with runtime.sessions() as session:
        prefs = await preferences(session)
        state = (await session.get(AppState, "proactive")).value
        assert (
            await proactive.blocked_reason(session, prefs, state, now(), runtime.settings.timezone)
            == "muted"
        )


async def test_topic_feedback_revisions_and_completion_limits(runtime):
    async with runtime.sessions.begin() as session:
        prefs = await save_preferences(
            session,
            {"proactive": ProactiveSettings(quiet_start="00:00", quiet_end="00:00").model_dump()},
        )
        row = ProactiveUtterance(
            id=uuid.uuid4(),
            trigger="timer",
            topic="break",
            content="休息一下喵",
            shown_at=now(),
            expires_at=now() + timedelta(seconds=10),
            settings_hash=proactive.config_hash(prefs),
            status="shown",
        )
        session.add(row)
        bid = row.id
        revision = prefs["revision"]
    result = await feedback(runtime, bid, "less", revision)
    assert result["proactive"]["topic_preferences"]["break"] == "less"
    with pytest.raises(HTTPException):
        await feedback(runtime, bid, "more", revision)
    await proactive.heartbeat(runtime, True)
    async with runtime.sessions.begin() as session:
        prefs = await preferences(session)
        ledger = await proactive.state_row(session)
        local_day = now().astimezone(ZoneInfo(runtime.settings.timezone)).date().isoformat()
        ledger.value = {**ledger.value, "completion_day": local_day, "completion_count": 3}
        row = ProactiveUtterance(
            id=uuid.uuid4(),
            trigger="completion",
            topic="playful",
            content="已经完成啦",
            status="ready",
            expires_at=now() + timedelta(seconds=50),
            settings_hash=proactive.config_hash(prefs),
            context={"presence_epoch": ledger.value.get("epoch", 0)},
        )
        session.add(row)
        cid = row.id
    with pytest.raises(HTTPException, match="完成鼓励次数"):
        await proactive.action(runtime, cid, "shown")
    async with runtime.sessions.begin() as session:
        ledger = await proactive.state_row(session)
        ledger.value = {
            **ledger.value,
            "completion_count": 1,
            "last_completion_at": now().isoformat(),
        }
    with pytest.raises(HTTPException, match="完成鼓励次数"):
        await proactive.action(runtime, cid, "shown")
    async with runtime.sessions.begin() as session:
        ledger = await proactive.state_row(session)
        ledger.value = {
            **ledger.value,
            "last_completion_at": (now() - timedelta(minutes=31)).isoformat(),
            "day": local_day,
            "count": 30,
        }
    with pytest.raises(HTTPException, match="今天先安静"):
        await proactive.action(runtime, cid, "shown")
    async with runtime.sessions() as session:
        assert (await session.get(ProactiveUtterance, cid)).shown_at is None


async def test_capture_memory_date_uses_application_timezone_and_old_memories_paginate(
    runtime, client
):
    eid = await ready(runtime)
    async with runtime.sessions.begin() as session:
        episode = await session.get(Episode, eid)
        episode.started_at = datetime(2026, 10, 12, 17, tzinfo=UTC)
        body = SourceMemoryInput(
            request_id=uuid.uuid4(), expected_revision=episode.revision, text="日期测试"
        )
        proposal = await propose_memory(session, eid, body, "Asia/Shanghai")
        from workmemory.db.models import Operation

        operation = await session.get(Operation, uuid.UUID(proposal["id"]))
        assert operation.origin["date"] == "2026-10-13"
        for index in range(101):
            session.add(
                MemoryCandidate(
                    text=f"合成确认记忆{index}",
                    status="confirmed",
                    created_at=now() - timedelta(days=40),
                )
            )
    first = (await client.get("/api/memory/candidates?status=confirmed")).json()
    assert len(first["items"]) == 100 and first["next_cursor"] == 100
    second = (await client.get("/api/memory/candidates?status=confirmed&offset=100")).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}
