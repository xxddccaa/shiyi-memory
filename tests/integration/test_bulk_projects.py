"""Project deletion is one frozen, authenticated confirmation over an isolated PG database."""

import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from test_agent import conversation, run_turn, seed
from test_agent_reliability import ScriptedModel, answer
from test_companion import proposal
from test_pipeline import event
from workmemory.agent.runtime import AgentManager
from workmemory.db.models import (
    Activity,
    ChatMessage,
    DerivedArtifact,
    Episode,
    MemoryCandidate,
    Operation,
    Project,
    ProjectAlias,
    Reminder,
    Todo,
)
from workmemory.evidence.service import remember, resolve
from workmemory.ingestion.service import Batch, ingest
from workmemory.jobs.queue import now
from workmemory.main import create_app
from workmemory.workspace.operations import decide
from workmemory.workspace.service import list_objects


async def seed_projects(runtime, count=32, prefix="合成项目"):
    async with runtime.sessions.begin() as session:
        rows = [Project(id=uuid.uuid4(), name=f"{prefix}{i:03d}") for i in range(count)]
        session.add_all(rows)
    return [row.id for row in rows]


async def test_32_projects_one_snapshot_concurrent_confirm_preserves_new_project(runtime):
    ids = await seed_projects(runtime)
    item = await proposal(runtime, await conversation(runtime), action="project.delete_many", all_projects=True)
    assert item["preview"]["count"] == len(item["preview"]["before"]) == 32
    assert {x["id"] for x in item["preview"]["before"]} == set(map(str, ids))
    new_id = (await seed_projects(runtime, 1, "后来新增"))[0]
    results = await asyncio.gather(*(decide(runtime, uuid.UUID(item["id"]), 1, True) for _ in range(2)))
    assert all(r["status"] == "executed" and r["result"]["count"] == 32 for r in results)
    async with runtime.sessions() as session:
        assert list((await session.scalars(select(Project.id).where(Project.state == "active"))).all()) == [new_id]
        assert await resolve(session, f"project:{ids[0]}") is None
        assert (await list_objects(session, "project"))["items"][0]["id"] == str(new_id)
        assert await session.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.role == "assistant")) == 1


@pytest.mark.parametrize("change", ["project", "membership", "alias", "todo", "memory"])
async def test_project_or_impact_change_invalidates_whole_batch(runtime, change):
    ids = await seed_projects(runtime, 2)
    item = await proposal(runtime, await conversation(runtime), action="project.delete_many", all_projects=True)
    async with runtime.sessions.begin() as session:
        if change == "project":
            (await session.get(Project, ids[0])).revision += 1
        elif change == "membership":
            session.add(Episode(id=uuid.uuid4(), device_id="synthetic", project_id=ids[0], started_at=now(), ended_at=now()))
        elif change == "alias":
            session.add(ProjectAlias(name="新增合成别名", project_id=ids[0]))
        elif change == "todo":
            session.add(Todo(text="新增合成提醒", project_id=ids[0]))
        else:
            session.add(MemoryCandidate(text="新增合成记忆", status="confirmed", origin={"project_id": str(ids[0])}))
    result = await decide(runtime, uuid.UUID(item["id"]), 1, True)
    assert result["status"] == "invalidated" and result["preview"] is None
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Project).where(Project.state == "active")) == 2


async def test_project_delete_rolls_back_entire_batch(runtime, monkeypatch):
    await seed_projects(runtime, 3)
    item = await proposal(runtime, await conversation(runtime), action="project.delete_many", all_projects=True)
    from workmemory.workspace import project_deletion
    original, calls = project_deletion.remove_project, 0

    async def fail(session, row):
        nonlocal calls
        calls += 1
        await original(session, row)
        if calls == 2:
            raise RuntimeError("synthetic rollback")

    monkeypatch.setattr(project_deletion, "remove_project", fail)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        await decide(runtime, uuid.UUID(item["id"]), 1, True)
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Project).where(Project.state == "active")) == 3
        assert (await session.get(Operation, uuid.UUID(item["id"]))).status == "pending"


async def test_delete_keeps_records_memories_reminders_and_invalidates_old_context(runtime):
    eid = await seed(runtime)
    cid = await conversation(runtime)
    async with runtime.sessions.begin() as session:
        episode = await session.get(Episode, eid)
        pid = episode.project_id
        source_text = episode.summary
        raw = Activity(id=uuid.uuid4(), episode_id=eid, captured_at=now(), app="synthetic", title="合成原文", text=source_text, content_hash="a" * 64, method="manual")
        memory = MemoryCandidate(id=uuid.uuid4(), status="confirmed", text="本人确认的合成记忆", origin={"project_id": str(pid)})
        todo = Todo(id=uuid.uuid4(), text="合成待办", project_id=pid, confirmed=True, due_at=now() + timedelta(days=1))
        message = ChatMessage(id=uuid.uuid4(), conversation_id=cid, role="assistant", content="旧项目归属回答")
        report = DerivedArtifact(id=uuid.uuid4(), kind="brief", scope=str(pid), body={"progress": []}, user_text="人工项目回顾", status="ready")
        session.add_all([raw, memory, todo, message, report, ProjectAlias(name="合成旧别名", project_id=pid)])
        await session.flush()
        reminder = Reminder(id=uuid.uuid4(), kind="todo", todo_id=todo.id, text="合成提醒", due_at=todo.due_at, dedupe_key="synthetic-reminder")
        session.add(reminder)
        await remember(session, "message", message.id, [await resolve(session, f"project:{pid}")])
    item = await proposal(runtime, cid, action="project.delete", target_id=pid)
    assert item["preview"]["before"][0]["episode_count"] == 1
    assert item["preview"]["before"][0]["todo_count"] == 1
    assert (await decide(runtime, uuid.UUID(item["id"]), 1, True))["status"] == "executed"
    async with runtime.sessions() as session:
        episode = await session.get(Episode, eid)
        assert episode.summary == source_text and episode.project_id is None and not episode.deleted_at
        assert (await session.get(Activity, raw.id)).text == source_text
        assert (await session.get(Todo, todo.id)).project_id is None
        assert (await session.get(Reminder, reminder.id)).todo_id == todo.id
        saved = await session.get(MemoryCandidate, memory.id)
        assert saved.status == "confirmed" and saved.text == "本人确认的合成记忆" and saved.origin["project_id"] is None
        assert (await session.get(ChatMessage, message.id)).content is None
        assert (await session.get(DerivedArtifact, report.id)).body is None
        assert (await session.get(DerivedArtifact, report.id)).user_text == "人工项目回顾"
        assert not await session.get(ProjectAlias, "合成旧别名")
    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=[event(device_id="late-project-import")]), runtime.settings)
    async with runtime.sessions() as session:
        assert (await session.get(Project, pid)).state == "deleted"
        assert not (await session.scalars(select(Episode).where(Episode.project_id == pid))).all()


async def test_project_reject_expiry_empty_and_explicit_selection(runtime):
    with pytest.raises(HTTPException):
        await proposal(runtime, await conversation(runtime), action="project.delete_many", all_projects=True)
    ids = await seed_projects(runtime, 3)
    cid = await conversation(runtime)
    for kwargs in ({}, {"all_projects": True, "target_ids": ids}, {"target_ids": [uuid.uuid4()]}):
        with pytest.raises(HTTPException):
            await proposal(runtime, cid, action="project.delete_many", **kwargs)
    item = await proposal(runtime, cid, action="project.delete_many", target_ids=[ids[0], ids[0]])
    assert item["preview"]["count"] == 1
    assert (await decide(runtime, uuid.UUID(item["id"]), 1, False))["status"] == "rejected"
    item = await proposal(runtime, cid, action="project.delete_many", target_ids=ids[:2])
    async with runtime.sessions.begin() as session:
        (await session.get(Operation, uuid.UUID(item["id"]))).expires_at = now() - timedelta(seconds=1)
    assert (await decide(runtime, uuid.UUID(item["id"]), 1, True))["status"] == "invalidated"
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Project).where(Project.state == "active")) == 3


async def test_pi_prepares_one_project_card_without_individual_calls(runtime):
    await seed_projects(runtime)
    runtime.ollama.chat_tools = ScriptedModel(
        {"text": "", "tool_calls": [{"id": "bulk", "name": "propose_operation", "arguments": {"action": "project.delete_many", "all_projects": True}}]},
        answer("已准备32个项目的清单，请在卡片中核对并确认。"),
    )
    runtime.agent = AgentManager(runtime)
    try:
        run = await run_turn(runtime, await conversation(runtime), "删除我的所有项目")
        assert run.status == "completed" and run.model_calls == 2
        async with runtime.sessions() as session:
            ops = (await session.scalars(select(Operation).where(Operation.run_id == run.id))).all()
            assert len(ops) == 1 and ops[0].preview["count"] == 32 and ops[0].status == "pending"
            assert await session.scalar(select(func.count()).select_from(Project).where(Project.state == "active")) == 32
    finally:
        await runtime.agent.close()


async def test_deleted_project_unavailable_to_api_and_old_page(runtime):
    pid = (await seed_projects(runtime, 1))[0]
    item = await proposal(runtime, await conversation(runtime), action="project.delete", target_id=pid)
    await decide(runtime, uuid.UUID(item["id"]), 1, True)
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama, memory=runtime.memory, run_worker=False, mcp_store=runtime.mcp.store)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Authorization": f"Bearer {runtime.settings.api_token}"}) as client:
        assert (await client.get("/api/projects")).json() == []
        assert (await client.get(f"/api/projects/{pid}/brief")).status_code == 404
        assert (await client.post("/api/feedback", json={"object_kind": "project", "object_id": str(pid), "expected_revision": 2, "value": "useful"})).status_code == 404
        assert (await client.get(f"/api/workspace/objects/project/{pid}")).status_code == 404
        assert (await client.post("/api/todos", json={"request_id": str(uuid.uuid4()), "text": "旧页面合成待办", "project_id": str(pid), "due_at": (now() + timedelta(days=1)).isoformat(), "timezone": "Asia/Shanghai"})).status_code == 422


async def test_legacy_memory_preview_cannot_reassign_a_deleted_project(runtime):
    from sqlalchemy import delete
    from workmemory.db.models import EvidenceDependency
    from workmemory.evidence.confirmed import SourceMemoryInput, propose_memory

    eid = await seed(runtime)
    pid = (await seed_projects(runtime, 1))[0]
    async with runtime.sessions.begin() as session:
        memory = await propose_memory(session, eid, SourceMemoryInput(request_id=uuid.uuid4(), expected_revision=1, text="合成确认内容", project_id=pid))
        # An already-open preview made by the old version had no project dependency.
        await session.execute(delete(EvidenceDependency).where(EvidenceDependency.consumer_id == uuid.UUID(memory["id"]), EvidenceDependency.source_kind == "project"))
    deletion = await proposal(runtime, await conversation(runtime), action="project.delete", target_id=pid)
    assert (await decide(runtime, uuid.UUID(deletion["id"]), 1, True))["status"] == "executed"
    assert (await decide(runtime, uuid.UUID(memory["id"]), 1, True))["status"] == "invalidated"
    async with runtime.sessions() as session:
        assert not (await session.scalars(select(MemoryCandidate))).all()
