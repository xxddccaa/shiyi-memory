"""Bulk confirmation snapshots and high run limits, with isolated PostgreSQL and actual Pi."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from test_agent import conversation, run_turn, seed
from test_agent_reliability import ScriptedModel, answer
from test_companion import proposal
from workmemory.agent.runtime import AgentManager
from workmemory.artifacts.service import reconcile_todos
from workmemory.db.models import Operation, Todo, TodoTombstone
from workmemory.workspace.operations import decide


async def seed_todos(rt, count=69):
    async with rt.sessions.begin() as session:
        rows = [
            Todo(id=uuid.uuid4(), text=f"合成清单任务{i:03d}", confirmed=True) for i in range(count)
        ]
        session.add_all(rows)
    return [r.id for r in rows]


async def test_bulk_snapshot_69_atomic_concurrent_confirm_and_no_new_targets(runtime):
    tids = await seed_todos(runtime)
    cid = await conversation(runtime)
    item = await proposal(runtime, cid, action="todo.delete_many", all_todos=True)
    assert item["preview"]["count"] == 69 and len(item["preview"]["before"]) == 69
    assert {v["id"] for v in item["preview"]["before"]} == {str(v) for v in tids}
    new_id = (await seed_todos(runtime, 1))[0]
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Todo)) == 70
    results = await asyncio.gather(
        *(decide(runtime, uuid.UUID(item["id"]), 1, True) for _ in range(2))
    )
    assert all(r["status"] == "executed" and r["result"]["count"] == 69 for r in results)
    async with runtime.sessions() as session:
        assert list((await session.scalars(select(Todo.id))).all()) == [new_id]
        assert await session.scalar(select(func.count()).select_from(TodoTombstone)) == 69
        op = await session.get(Operation, uuid.UUID(item["id"]))
        assert op.payload is None and op.preview is None


async def test_bulk_changed_target_invalidates_whole_batch(runtime):
    tids = await seed_todos(runtime, 3)
    item = await proposal(
        runtime, await conversation(runtime), action="todo.delete_many", all_todos=True
    )
    async with runtime.sessions.begin() as session:
        (await session.get(Todo, tids[1])).revision += 1
    result = await decide(runtime, uuid.UUID(item["id"]), 1, True)
    assert result["status"] == "invalidated" and result["preview"] is None
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Todo)) == 3
        assert await session.scalar(select(func.count()).select_from(TodoTombstone)) == 0


async def test_bulk_transaction_failure_rolls_back_every_delete(runtime, monkeypatch):
    await seed_todos(runtime, 3)
    item = await proposal(
        runtime, await conversation(runtime), action="todo.delete_many", all_todos=True
    )
    from workmemory.workspace import service

    original, count = service.remove_todo, 0

    async def fail_second(session, row):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("synthetic transaction failure")
        await original(session, row)

    monkeypatch.setattr(service, "remove_todo", fail_second)
    with pytest.raises(RuntimeError, match="synthetic"):
        await decide(runtime, uuid.UUID(item["id"]), 1, True)
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Todo)) == 3
        assert await session.scalar(select(func.count()).select_from(TodoTombstone)) == 0
        assert (await session.get(Operation, uuid.UUID(item["id"]))).status == "pending"


async def test_bulk_filters_and_reconciliation_respect_deleted_snapshot(runtime):
    from workmemory.db.models import Episode

    eid = await seed(runtime)
    cid = await conversation(runtime)
    from test_workspace_mcp import todo
    await todo(runtime, eid, "合成任务甲")
    tid = await todo(runtime, eid, "合成任务乙")
    async with runtime.sessions.begin() as session:
        episode = await session.get(Episode, eid)
        pid = episode.project_id
        done = await session.get(Todo, tid)
        done.done = True
    standalone = (await seed_todos(runtime, 1))[0]
    item = await proposal(runtime, cid, action="todo.delete_many", project_id=pid, done=False)
    assert item["preview"]["count"] == 1
    assert item["preview"]["before"][0]["text"] == "合成任务甲"
    assert (await decide(runtime, uuid.UUID(item["id"]), 1, True))["status"] == "executed"
    async with runtime.sessions.begin() as session:
        await reconcile_todos(
            session, await session.get(Episode, eid), ["合成任务甲", "合成任务乙"]
        )
        assert await session.get(Todo, standalone)
        assert set(
            (await session.scalars(select(Todo.text).where(Todo.project_id == pid))).all()
        ) == {"合成任务乙"}
    for extra in [
        {},
        {"all_todos": True, "target_ids": [standalone]},
        {"all_todos": True, "done": False},
    ]:
        with pytest.raises(HTTPException):
            await proposal(runtime, cid, action="todo.delete_many", **extra)


async def test_pi_one_bulk_proposal_instead_of_69_individual_calls(runtime):
    await seed_todos(runtime)
    runtime.ollama.chat_tools = ScriptedModel(
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "bulk",
                    "name": "propose_operation",
                    "arguments": {"action": "todo.delete_many", "all_todos": True},
                }
            ],
        },
        answer("已准备69条待办的清单，请在卡片中核对并确认。"),
    )
    runtime.agent = AgentManager(runtime)
    try:
        run = await run_turn(runtime, await conversation(runtime), "把我的待办全部删除")
        assert run.status == "completed" and run.model_calls == 2
        async with runtime.sessions() as session:
            ops = (await session.scalars(select(Operation).where(Operation.run_id == run.id))).all()
            assert len(ops) == 1 and ops[0].preview["count"] == 69 and ops[0].status == "pending"
            assert await session.scalar(select(func.count()).select_from(Todo)) == 69
    finally:
        await runtime.agent.close()


async def test_high_default_allows_80_calls_with_context_window(runtime):
    # Cross every previous model ceiling (6 and 64), as well as old tools/char limits.
    ids = await seed_todos(runtime, 80)
    async with runtime.sessions.begin() as session:
        for i, identifier in enumerate(ids):
            row = await session.get(Todo, identifier)
            row.text = f"合成检查{i:03d}：" + "检查接口状态、日志与验收条件。" * 35
    from workmemory.config.generation import GenerationConfig
    from workmemory.db.models import PiSession
    runtime.ollama.generation = GenerationConfig(provider="openai_compatible", base_url="http://synthetic.invalid/v1",
        model="synthetic", context_window=16384, max_tokens=1024)
    summaries = []
    async def summary(messages, max_tokens):
        summaries.append(messages)
        return {"text": "合成多步检查已完成此前分页，继续检查剩余项。", "tool_calls": []}
    runtime.ollama.agent_summary = summary
    # Each step investigates a different query; unchanged repeated reads now stop as no progress.
    results = [
        {
            "text": "",
            "tool_calls": [
                {"id": f"q{i}", "name": "list_objects", "arguments": {"kind": "todo", "limit": 10, "query": f"合成检查{i:03d}"}}
            ],
        }
        for i in range(80)
    ]
    model = ScriptedModel(*results, answer("合成多步检查已完成。"))
    runtime.ollama.chat_tools = model
    runtime.agent = AgentManager(runtime)
    try:
        from workmemory.agent.runtime import submit_message
        from workmemory.db.models import AgentRun

        sent = await submit_message(runtime, await conversation(runtime), "请完成多步合成检查")
        rid = uuid.UUID(sent["run_id"])
        await asyncio.wait_for(runtime.agent.tasks[rid], 90)
        async with runtime.sessions() as session:
            run = await session.get(AgentRun, rid)
            assert run.status == "completed" and run.model_calls == 81 + len(summaries)
            state = await session.get(PiSession, run.conversation_id)
            assert any(entry["type"] == "compaction" for entry in state.data["entries"])
        assert len(model.calls) == 81
        assert summaries
        assert any("合成多步检查已完成此前分页" in str(messages) for messages, _ in model.calls)
    finally:
        await runtime.agent.close()
