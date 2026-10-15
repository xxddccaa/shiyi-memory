"""Regressions discovered by real daily dialogues, on isolated PostgreSQL/Pi."""

import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from test_agent import agent_runtime as _agent_runtime
from test_agent import conversation, run_turn, seed
from test_agent_reliability import ScriptedModel, answer
from test_companion import companion_client as _companion_client
from test_companion import proposal
from test_workspace_mcp import active_run, todo
from workmemory.agent.protocol import STRUCTURED_REPAIR
from workmemory.db.models import ChatMessage, DerivedArtifact, Episode, Project, Todo, WorkSession
from workmemory.episodes.service import mutate_episode
from workmemory.evidence.service import dependencies, remember, resolve, valid_dependencies
from workmemory.jobs.queue import now
from workmemory.workspace.operations import decide
from workmemory.workspace.service import get_object, list_objects
from workmemory.workspace.tools import execute

agent_runtime = _agent_runtime
companion_client = _companion_client
pytestmark = pytest.mark.integration


def tool(name, **arguments):
    return {"text": "", "tool_calls": [{"id": str(uuid.uuid4()), "name": name, "arguments": arguments}]}


async def test_raw_todo_fields_and_merge_direction_are_unambiguous(runtime):
    eid = await seed(runtime)
    async with runtime.sessions() as session:
        blue = (await session.get(Episode, eid)).project_id
    target = await todo(runtime, pid=blue, text="整理周报")
    async with runtime.sessions.begin() as session:
        (await session.get(Todo, target)).due_at = now() + timedelta(days=1)
        star = Project(id=uuid.uuid4(), name="星河")
        session.add(star)
    moved = await todo(runtime, pid=star.id, text="星河任务")
    async with runtime.sessions() as session:
        value = (await list_objects(session, query="整理周报"))["items"][0]
        assert value["text"] == "整理周报" and value["display_text"] == "整理周报（未完成）"
        assert value["project_name"] == "蓝鲸" and value["due_at"]
        assert (await get_object(session, "todo", target))["text"] == "整理周报"
    cid = await conversation(runtime)
    clear = await proposal(runtime, cid, action="todo.update", target_id=target, paused=True)
    await decide(runtime, uuid.UUID(clear["id"]), 1, True)
    merge = await proposal(runtime, cid, action="project.merge", target_id=blue, other_id=star.id)
    assert merge["preview"]["label"] == "将「星河」并入「蓝鲸」，保留后者"
    await decide(runtime, uuid.UUID(merge["id"]), 1, True)
    async with runtime.sessions() as session:
        row = await session.get(Todo, target)
        assert row.text == "整理周报" and row.due_at is not None and row.paused and not row.done
        assert (await session.get(Todo, moved)).project_id == blue
        assert (await session.get(Project, star.id)).state == "merged"
        assert (await session.get(Project, blue)).state != "merged"


async def test_session_and_review_discovery_mutation_and_source_cleanup(runtime):
    eid = await seed(runtime)
    day = now().astimezone(ZoneInfo(runtime.settings.timezone)).date().isoformat()
    async with runtime.sessions.begin() as session:
        segment = WorkSession(id=uuid.uuid4(), episode_id=eid, revision=1, title="验收", category="开发",
                              started_at=now(), ended_at=now())
        review = DerivedArtifact(id=uuid.uuid4(), kind="daily", scope=day, user_text="验收已通过", status="ready")
        session.add_all([segment, review])
        await session.flush()
        await remember(session, "artifact", review.id, [await resolve(session, f"summary:{eid}")])
    run = await active_run(runtime)
    for kind, identifier in [("session", segment.id), ("review", review.id)]:
        result = await execute(runtime, run.id, "list_objects", {"kind": kind, "day": day})
        assert [r["id"] for r in result["items"]] == [str(identifier)]
        async with runtime.sessions() as session:
            assert not (await list_objects(session, kind, day="2000-01-01"))["items"]
        value = await execute(runtime, run.id, "get_object", {"kind": kind, "identifier": str(identifier),
                                                             "revision": result["items"][0]["revision"]})
        assert value["items"][0]["id"] == f"{kind}:{identifier}"
    async with runtime.sessions.begin() as session:
        deps = await dependencies(session, "run", run.id)
        assert {d.source_kind for d in deps} == {"episode", "session", "review"}
        assert await valid_dependencies(session, deps)
        message = ChatMessage(id=uuid.uuid4(), conversation_id=run.conversation_id, role="assistant", content="旧回顾")
        session.add(message)
        await remember(session, "message", message.id, [await resolve(session, f"review:{review.id}")])
    cid = await conversation(runtime)
    card = await proposal(runtime, cid, action="session.update", target_id=segment.id, text="接口排查", category="调试")
    assert (await decide(runtime, uuid.UUID(card["id"]), 1, True))["status"] == "executed"
    card = await proposal(runtime, cid, action="review.save", target_id=review.id, text="完成接口验收，明日补充测试。")
    assert (await decide(runtime, uuid.UUID(card["id"]), 1, True))["status"] == "executed"
    async with runtime.sessions.begin() as session:
        row = await session.get(WorkSession, segment.id)
        assert (row.title, row.category) == ("接口排查", "调试")
        row = await session.get(DerivedArtifact, review.id)
        assert row.user_text == "完成接口验收，明日补充测试。" and row.edit_revision == 1
        assert (await session.get(ChatMessage, message.id)).content is None
        await mutate_episode(session, await session.get(Episode, eid), None)
    async with runtime.sessions() as session:
        assert await resolve(session, f"session:{segment.id}") is None
        assert await resolve(session, f"review:{review.id}") is None


async def test_nonkeyword_chat_repair_requests_schema_even_with_tools(agent_runtime):
    async def repair():
        assert STRUCTURED_REPAIR.get()
        return answer("番茄工作法用专注与休息交替安排任务。")
    model = ScriptedModel({"text": "bad format", "tool_calls": []}, repair)
    agent_runtime.ollama.chat_tools = model
    result = await run_turn(agent_runtime, await conversation(agent_runtime), "介绍一下番茄工作法")
    assert result.status == "completed" and result.model_calls == 2
    assert model.calls[-1][1]  # Schema repair does not remove still-needed retrieval capability.


async def test_unknown_citation_repairs_by_reading_actual_source(agent_runtime):
    eid = await seed(agent_runtime)
    model = ScriptedModel(answer("验收通过", [f"summary:{uuid.uuid4()}"]),
                          tool("get_evidence", ids=[f"summary:{eid}"]),
                          answer("验收通过", [f"summary:{eid}"]))
    agent_runtime.ollama.chat_tools = model
    result = await run_turn(agent_runtime, await conversation(agent_runtime), "验收结果呢？")
    assert result.status == "completed" and result.model_calls == 3
    async with agent_runtime.sessions() as session:
        messages = (await session.scalars(select(ChatMessage).where(ChatMessage.role == "assistant"))).all()
        assert messages[-1].citations == [f"summary:{eid}"]


async def test_repeated_query_ends_with_clarification_not_budget_failure(agent_runtime):
    responses = [tool("list_objects", kind="todo", query="同名周报") for _ in range(3)]
    model = ScriptedModel(*responses, answer("没有找到这条待办，请补充准确名称。"))
    agent_runtime.ollama.chat_tools = model
    result = await run_turn(agent_runtime, await conversation(agent_runtime), "找同名周报")
    assert result.status == "completed" and result.model_calls == 4
    assert not model.calls[-1][1]


async def test_real_review_ui_and_mcp_share_manual_edit_and_invalidation(runtime, companion_client):
    client = companion_client
    day = "2026-10-08"
    async with runtime.sessions.begin() as session:
        session.add(DerivedArtifact(id=uuid.uuid4(), kind="daily", scope=day,
                                   user_text="兼容旧记录", status="ready"))
    response = await client.get("/api/reviews", params={"day": day})
    assert response.status_code == 200
    report = response.json()
    assert report["kind"] == "review" and report["edit_revision"] == 0
    run = await active_run(runtime)
    found = await execute(runtime, run.id, "list_objects", {"kind": "review", "day": day})
    assert [r["id"] for r in found["items"]] == [report["id"]]
    assert found["total"] == 1
    cid = str(await conversation(runtime))
    card = await client.post(f"/api/conversations/{cid}/operations", json={
        "action": "review.save", "target_id": report["id"], "text": "完成了接口和测试。"
    })
    assert card.status_code == 201
    accepted = await client.post(f"/api/operations/{card.json()['id']}/decision",
                                json={"action": "confirm", "expected_revision": 1})
    assert accepted.status_code == 200 and accepted.json()["status"] == "executed"
    view = (await client.get("/api/reviews", params={"day": day})).json()
    assert view["user_text"] == "完成了接口和测试。" and view["edit_revision"] == 1
    async with runtime.sessions.begin() as session:
        message = ChatMessage(id=uuid.uuid4(), conversation_id=uuid.UUID(cid), role="assistant", content="旧内容")
        session.add(message)
        await remember(session, "message", message.id, [await resolve(session, f"review:{report['id']}")])
    patch = {"text": "补充：测试还剩一项。", "expected_revision": 1}
    edited = await client.patch(f"/api/reviews/{report['id']}", json=patch)
    assert edited.status_code == 200 and edited.json()["edit_revision"] == 2
    async with runtime.sessions() as session:
        assert (await session.get(ChatMessage, message.id)).content is None
        latest = await get_object(session, "review", report["id"], 2)
        assert latest["text"] == patch["text"]
    assert (await client.patch(f"/api/reviews/{report['id']}", json=patch)).status_code == 409


async def test_stable_object_reads_detect_a_four_tool_cycle(runtime):
    from workmemory.agent.output import stalled_tools
    eid = await seed(runtime)
    async with runtime.sessions() as session:
        pid = (await session.get(Episode, eid)).project_id
    tid = await todo(runtime, text="同名任务", pid=pid)
    calls = []
    async with runtime.sessions() as session:
        for _ in range(3):
            for name, args in [("get_object", {"kind": "todo", "identifier": str(tid)}),
                               ("get_object", {"kind": "project", "identifier": str(pid)}),
                               ("list_objects", {"kind": "todo", "query": "同名任务"}),
                               ("list_objects", {"kind": "project", "query": "蓝鲸"})]:
                value = await (get_object(session, **args) if name == "get_object" else list_objects(session, **args))
                calls.append((name, args, value))
        assert stalled_tools(calls)
        assert (await get_object(session, "todo", tid))["captured_at"] is None


async def test_work_session_edit_invalidates_a_read_registered_after_the_edit(runtime):
    from fastapi import HTTPException
    eid = await seed(runtime)
    async with runtime.sessions.begin() as session:
        row = WorkSession(id=uuid.uuid4(), episode_id=eid, revision=1, title="旧标题", category="工作",
                          started_at=now(), ended_at=now())
        session.add(row)
    async with runtime.sessions() as session:
        old = await get_object(session, "session", row.id, 1)
    cid = await conversation(runtime)
    card = await proposal(runtime, cid, action="session.update", target_id=row.id, text="新标题")
    await decide(runtime, uuid.UUID(card["id"]), 1, True)
    async with runtime.sessions.begin() as session:
        # Simulate a read which began before confirmation and registers its dependency later.
        await remember(session, "run", uuid.uuid4(), [old])
        from workmemory.db.models import EvidenceDependency
        stale = (await session.scalars(select(EvidenceDependency).where(
            EvidenceDependency.source_kind == "session", EvidenceDependency.revision == 1))).all()
        assert stale and not await valid_dependencies(session, stale)
        fresh = await get_object(session, "session", row.id, 2)
        assert fresh["text"] == "新标题" and fresh["revision"] == 2
        assert (await session.get(WorkSession, row.id)).revision == 1  # Source remains valid.
        with pytest.raises(HTTPException) as conflict:
            await get_object(session, "session", row.id, 1)
        assert conflict.value.status_code == 409


async def test_confirmation_reports_other_cards_invalidated_by_source_change(runtime):
    from workmemory.db.models import Operation
    tid = await todo(runtime, text="检查接口")
    run = await active_run(runtime)
    await execute(runtime, run.id, "list_objects", {"kind": "todo"})
    first = (await execute(runtime, run.id, "propose_operation", {
        "action": "todo.update", "target_id": str(tid), "done": True
    }))["proposal"]
    second = (await execute(runtime, run.id, "propose_operation", {
        "action": "todo.create", "text": "准备演示", "due_at": (now()+timedelta(days=1)).isoformat(), "time_expression": "24小时后"
    }))["proposal"]
    result = await decide(runtime, uuid.UUID(first["id"]), 1, True)
    assert result["status"] == "executed"
    assert "尚未执行" in result["result"]["notice"]
    assert "创建待办" in result["result"]["notice"]
    async with runtime.sessions() as session:
        assert (await session.get(Operation, uuid.UUID(second["id"]))).status == "invalidated"
        assert not await session.scalar(select(Todo).where(Todo.text == "准备演示"))
        notices = (await session.scalars(select(ChatMessage.content).where(ChatMessage.role == "assistant"))).all()
        assert any("尚未执行" in (text or "") for text in notices)
