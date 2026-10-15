"""Named merge proposals resolve both existing objects without rewriting titles."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_workspace_mcp import active_run, todo
from workmemory.db.models import Operation, Project, Todo
from workmemory.jobs.queue import now
from workmemory.workspace.merge import prepare_merge
from workmemory.workspace.operations import decide
from workmemory.workspace.service import update_todo


@pytest.mark.parametrize("kind", ["todo", "project"])
async def test_named_merge_requires_confirmation_and_retains_the_named_target(runtime, kind):
    async with runtime.sessions.begin() as session:
        source = Project(id=uuid.uuid4(), name="来源工程")
        target = Project(id=uuid.uuid4(), name="保留工程")
        session.add_all([source, target])
    if kind == "todo":
        source_id = await todo(runtime, text="编写部署说明", pid=target.id)
        target_id = await todo(runtime, text="部署说明", pid=target.id)
        names = ("编写部署说明", "部署说明")
    else:
        source_id, target_id = source.id, target.id
        names = (source.name, target.name)
    run = await active_run(runtime)
    result = await prepare_merge(
        runtime, run.id, {"kind": kind, "source_name": names[0], "target_name": names[1]}
    )
    assert result["proposal"]["action"] == kind + ".merge"
    assert result["source"]["id"] == str(source_id) and result["target"]["id"] == str(target_id)
    async with runtime.sessions() as session:
        op = await session.get(Operation, uuid.UUID(result["proposal"]["id"]))
        assert op.status == "pending" and op.payload["target_id"] == str(target_id)
        assert op.payload["other_id"] == str(source_id)
        if kind == "todo":
            assert not (await session.get(Todo, source_id)).dismissed
        else:
            assert (await session.get(Project, source_id)).state == "active"
    assert (await decide(runtime, op.id, op.revision, True))["status"] == "executed"
    async with runtime.sessions() as session:
        if kind == "todo":
            assert (await session.get(Todo, source_id)).dismissed
            assert (await session.get(Todo, target_id)).text == names[1]
        else:
            assert (await session.get(Project, source_id)).state == "merged"
            assert (await session.get(Project, target_id)).state == "active"


async def test_named_merge_missing_ambiguous_and_same_targets_do_not_create_cards(runtime):
    await todo(runtime, text="检查部署")
    await todo(runtime, text="检查部署")
    await todo(runtime, text="部署清单")
    run = await active_run(runtime)
    for source, target in [
        ("检查部署", "部署清单"),
        ("不存在", "部署清单"),
        ("部署清单", "部署清单"),
    ]:
        result = await prepare_merge(
            runtime, run.id, {"kind": "todo", "source_name": source, "target_name": target}
        )
        assert result.get("error") and "proposal" not in result
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Operation)) == 0
        assert await session.scalar(select(func.count()).select_from(Todo)) == 3


async def test_named_merge_changed_source_invalidates_confirmation(runtime):
    source = await todo(runtime, text="检查运行日志")
    target = await todo(runtime, text="运行日志")
    run = await active_run(runtime)
    result = await prepare_merge(
        runtime, run.id, {"kind": "todo", "source_name": "检查运行日志", "target_name": "运行日志"}
    )
    async with runtime.sessions.begin() as session:
        await update_todo(session, await session.get(Todo, source), {"text": "新的日志检查"})
    decision = await decide(runtime, uuid.UUID(result["proposal"]["id"]), 1, True)
    assert decision["status"] == "invalidated"
    async with runtime.sessions() as session:
        assert not (await session.get(Todo, source)).dismissed
        assert (await session.get(Todo, target)).text == "运行日志"


async def test_named_merge_rejects_incompatible_todos_before_creating_a_card(runtime):
    async with runtime.sessions.begin() as session:
        project = Project(id=uuid.uuid4(), name="另一项目")
        session.add(project)
    await todo(runtime, text="检查运行日志")
    await todo(runtime, text="运行日志", pid=project.id)
    run = await active_run(runtime)
    result = await prepare_merge(
        runtime, run.id, {"kind": "todo", "source_name": "检查运行日志", "target_name": "运行日志"}
    )
    assert result["error"]["code"] == "incompatible_targets"
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Operation)) == 0


async def test_named_merge_scopes_same_name_todos_and_rejects_stale_sources(runtime):
    from test_agent import seed
    from workmemory.db.models import Episode
    from workmemory.episodes.service import mutate_episode

    eid = await seed(runtime)
    async with runtime.sessions() as session:
        pid = (await session.get(Episode, eid)).project_id
    source = await todo(runtime, eid=eid, text="检查部署")
    target = await todo(runtime, text="部署清单", pid=pid)
    await todo(runtime, text="检查部署")
    run = await active_run(runtime)
    args = {
        "kind": "todo",
        "source_name": "检查部署",
        "target_name": "部署清单",
        "source_project_id": str(pid),
    }
    result = await prepare_merge(runtime, run.id, args)
    assert result["source"]["id"] == str(source) and result["target"]["id"] == str(target)
    duplicate = await prepare_merge(runtime, run.id, args)
    assert duplicate["proposal"]["id"] == result["proposal"]["id"]
    async with runtime.sessions.begin() as session:
        await mutate_episode(session, await session.get(Episode, eid))
    fresh = await active_run(runtime)
    invalid = await prepare_merge(runtime, fresh.id, args)
    assert invalid["error"]["code"] == "merge_target_unresolved"
    decision = await decide(runtime, uuid.UUID(result["proposal"]["id"]), 1, True)
    assert decision["status"] == "invalidated"


async def test_named_merge_real_pi_mcp_keeps_single_pending_card(runtime):
    from test_agent import conversation, run_turn
    from test_agent_reliability import ScriptedModel, answer
    from workmemory.agent.runtime import AgentManager
    from workmemory.db.models import AgentToolCall

    source = await todo(runtime, text="编写部署说明")
    target = await todo(runtime, text="部署说明")
    runtime.ollama.chat_tools = ScriptedModel(
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "merge",
                    "name": "propose_merge",
                    "arguments": {
                        "kind": "todo",
                        "source_name": "编写部署说明",
                        "target_name": "部署说明",
                    },
                }
            ],
        },
        answer("已准备合并确认卡片，保留部署说明；尚未执行。"),
    )
    runtime.agent = AgentManager(runtime)
    try:
        run = await run_turn(runtime, await conversation(runtime), "把编写部署说明并到部署说明里")
        assert run.status == "completed"
        async with runtime.sessions() as session:
            call = await session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run.id))
            assert (
                call.name == "propose_merge" and call.status == "completed" and not call.side_effect
            )
            op = await session.scalar(select(Operation).where(Operation.run_id == run.id))
            assert op.action == "todo.merge" and op.status == "pending"
            assert op.payload["target_id"] == str(target) and op.payload["other_id"] == str(source)
            assert not (await session.get(Todo, source)).dismissed
        decision = await decide(runtime, op.id, op.revision, False)
        assert decision["status"] == "rejected"
    finally:
        await runtime.agent.close()


@pytest.mark.parametrize("merge_first", [False, True])
async def test_merge_priority_preserves_literal_task_creation_and_remaining_actions(runtime, merge_first):
    from test_agent import conversation, run_turn
    from test_agent_reliability import ScriptedModel, answer
    from workmemory.agent.runtime import AgentManager

    await todo(runtime, text="编写部署说明")
    await todo(runtime, text="部署说明")
    cid = await conversation(runtime)

    async def first():
        names = {tool["name"] for tool in model.calls[-1][1]}
        assert ("propose_operation" not in names) == merge_first
        return {"text": "", "tool_calls": [{"id": "first", "name": "propose_merge" if merge_first else "propose_operation", "arguments": {
            "kind": "todo", "source_name": "编写部署说明", "target_name": "部署说明",
        } if merge_first else {"action": "todo.create", "text": "将日志并到报告里", "due_at": (now()+timedelta(days=1)).isoformat(), "time_expression": "24小时后"}}]}

    async def remaining():
        assert "propose_operation" in {tool["name"] for tool in model.calls[-1][1]}
        return {"text": "", "tool_calls": [{"id": "second", "name": "propose_operation", "arguments": {
            "action": "todo.create", "text": "准备演示", "due_at": (now()+timedelta(days=1)).isoformat(), "time_expression": "24小时后",
        }}]}

    async def finish():
        async with runtime.sessions() as session:
            ids = list(await session.scalars(select(Operation.id).where(Operation.conversation_id == cid)))
        return answer("已准备确认卡片，尚未执行。", operation_status="pending", operation_ids=[str(i) for i in ids])

    model = ScriptedModel(first, *([remaining] if merge_first else []), finish)
    runtime.ollama.chat_tools = model
    runtime.agent = AgentManager(runtime)
    try:
        question = "把编写部署说明并到部署说明里，再新增准备演示待办" if merge_first else "新增待办：将日志并到报告里"
        run = await run_turn(runtime, cid, question + "，新事项24小时后提醒")
        assert run.status == "completed"
        async with runtime.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Operation)) == (2 if merge_first else 1)
            assert await session.scalar(select(func.count()).select_from(Todo)) == 2
    finally:
        await runtime.agent.close()
