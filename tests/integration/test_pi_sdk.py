"""Real Pi SDK, temporary native projects and isolated PostgreSQL."""
import asyncio
import json
import uuid

import httpx
import pytest
from sqlalchemy import func, select
from test_agent import agent_runtime as _agent_runtime
from test_agent import conversation, run_turn, seed
from test_agent_reliability import ScriptedModel, answer, search_call
from workmemory.agent.coding import CodingRun, load_session
from workmemory.agent.errors import AgentFailure
from workmemory.agent.runtime import delete_messages, retry_blocker, submit_message
from workmemory.config.generation import GenerationConfig
from workmemory.db.models import (
    AgentEvent,
    AgentRun,
    AgentToolCall,
    AgentToolOutput,
    AgentWorkspace,
    ChatMessage,
    Conversation,
    Episode,
    PiSession,
)
from workmemory.episodes.service import mutate_episode
from workmemory.main import create_app

agent_runtime = _agent_runtime
pytestmark = pytest.mark.integration


async def coding_conversation(rt, path):
    async with rt.sessions.begin() as session:
        workspace = AgentWorkspace(id=uuid.uuid4(), path=str(path), name="合成项目")
        session.add(workspace)
        await session.flush()
        value = Conversation(id=uuid.uuid4(), workspace_id=workspace.id, coding_enabled=True)
        session.add(value)
        return value.id


def call(name, args, identifier):
    return {"text": "", "tool_calls": [{"id": identifier, "name": name, "arguments": args}]}


async def test_native_file_edit_test_output_and_restart(agent_runtime, tmp_path):
    rt = agent_runtime
    (tmp_path / "value.txt").write_text("old\n")
    cid = await coding_conversation(rt, tmp_path)
    rt.ollama.chat_tools = ScriptedModel(
        call("read", {"path": "value.txt"}, "read1"),
        call("edit", {"path": "value.txt", "oldText": "old", "newText": "new"}, "edit1"),
        call("bash", {"command": "test \"$(cat value.txt)\" = new && printf 'synthetic-test-passed'"}, "bash1"),
        answer("已修改文件，测试通过。"),
    )
    run = await run_turn(rt, cid, "修改合成项目并执行测试")
    assert run.status == "completed", run.error
    assert (tmp_path / "value.txt").read_text() == "new\n"
    async with rt.sessions() as session:
        checkpoint = await session.get(PiSession, cid)
        assert checkpoint.data["entries"]
        calls = (await session.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id).order_by(AgentToolCall.created_at))).all()
        assert [row.name for row in calls] == ["read", "edit", "bash"]
        assert all(row.status == "completed" for row in calls)
        assert "+new" in calls[1].result["change"]["diff"]
        assert calls[2].result["details"]["exit_code"] == 0
        assert "synthetic-test-passed" in (await session.scalar(select(AgentToolOutput.content).where(AgentToolOutput.call_id == calls[2].id)))
    await rt.agent.bridge.close()
    rt.ollama.chat_tools = ScriptedModel(answer("之前的文件修改和测试记录仍在。"))
    again = await run_turn(rt, cid, "继续说说刚才的结果")
    assert again.status == "completed", again.error
    assert "synthetic-test-passed" in json.dumps(rt.ollama.chat_tools.calls[0][0])
    await delete_messages(rt, cid)
    assert (tmp_path / "value.txt").read_text() == "new\n"
    async with rt.sessions() as session:
        assert await session.get(PiSession, cid) is None
        assert await session.scalar(select(func.count()).select_from(AgentToolOutput)) == 0


async def test_full_sdk_auto_and_manual_compaction_persist(agent_runtime):
    rt = agent_runtime
    rt.ollama.generation = GenerationConfig(provider="openai_compatible", base_url="http://synthetic.invalid/v1", model="synthetic", context_window=4096, max_tokens=512)
    cid = await conversation(rt)
    async with rt.sessions.begin() as session:
        for i in range(12):
            session.add(ChatMessage(conversation_id=cid, role="user", content=f"synthetic-anchor {i}: " + "旧任务合成数据。" * 500))
    summaries = []
    async def summary(messages, max_tokens):
        summaries.append(messages)
        assert max_tokens == 512
        return {"text": "保留 synthetic-anchor 的目标、已经执行的操作和下一步。", "tool_calls": [], "usage": {"input": 200, "output": 30, "totalTokens": 230}, "usage_estimated": False}
    rt.ollama.agent_summary = summary
    rt.ollama.chat_tools = ScriptedModel(answer("继续合成任务。"))
    run = await run_turn(rt, cid, "继续任务")
    assert run.status == "completed", run.error
    assert summaries
    async with rt.sessions() as session:
        state = await session.get(PiSession, cid)
        assert any(e["type"] == "compaction" for e in state.data["entries"])
        events = (await session.scalars(select(AgentEvent.kind).where(AgentEvent.run_id == run.id))).all()
        assert "compaction_start" in events and "compaction_end" in events
    # Manual compression uses the same lifecycle without adding a fabricated chat question.
    async with rt.sessions.begin() as session:
        manual = AgentRun(id=uuid.uuid4(), conversation_id=cid, message_id=run.message_id)
        session.add(manual)
        await session.flush()
        session.add(AgentEvent(run_id=manual.id, kind="compact_requested", data={}))
        manual_id = manual.id
    rt.agent.launch(manual_id)
    await asyncio.wait_for(rt.agent.tasks[manual_id], 20)
    async with rt.sessions() as session:
        result = await session.get(AgentRun, manual_id)
        assert result.status == "completed", result.error
        assert await session.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.conversation_id == cid)) == 14


async def test_source_invalidation_purges_pi_tree_and_native_output(agent_runtime, tmp_path):
    rt = agent_runtime
    source = await seed(rt)
    cid = await coding_conversation(rt, tmp_path)
    rt.ollama.chat_tools = ScriptedModel(search_call(), call("bash", {"command": "printf synthetic-private-derived"}, "bash1"), answer("已完成", [f"summary:{source}"]))
    run = await run_turn(rt, cid)
    assert run.status == "completed", run.error
    async with rt.sessions.begin() as session:
        await mutate_episode(session, await session.get(Episode, source), "合成来源已纠正")
    async with rt.sessions() as session:
        checkpoint = await session.get(PiSession, cid)
        assert checkpoint.data is None and checkpoint.epoch > 0
        calls = (await session.scalars(select(AgentToolCall).where(AgentToolCall.conversation_id == cid))).all()
        assert all(row.arguments is None and row.result is None for row in calls)
        assert await session.scalar(select(func.count()).select_from(AgentToolOutput)) == 0


async def test_cancel_native_command_does_not_replay_write(agent_runtime, tmp_path):
    rt = agent_runtime
    cid = await coding_conversation(rt, tmp_path)
    rt.ollama.chat_tools = ScriptedModel(call("bash", {"command": "printf started > marker; sleep 30; printf too-late > late"}, "bash1"))
    value = await submit_message(rt, cid, "执行合成命令")
    run_id = uuid.UUID(value["run_id"])
    for _ in range(100):
        if (tmp_path / "marker").exists():
            break
        await asyncio.sleep(.05)
    assert (tmp_path / "marker").exists()
    await rt.agent.cancel(run_id)
    assert not (tmp_path / "late").exists()
    async with rt.sessions.begin() as session:
        run = await session.get(AgentRun, run_id)
        assert run.status == "cancelled"
        run.status = "interrupted"
        assert "避免重复执行" in await retry_blocker(session, run)
        tool = await session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run_id))
        assert tool.status == "uncertain"
    rt.ollama.chat_tools = ScriptedModel(answer("上次命令已中断，下一步先核对实际文件状态。"))
    again = await run_turn(rt, cid, "说明上次执行状态")
    assert again.status == "completed", again.error
    restored = rt.ollama.chat_tools.calls[0][0]
    assert any(m.get("toolCallId") == "bash1" and m.get("isError") for m in restored)
    assert not (tmp_path / "late").exists()


async def test_stale_checkpoint_cannot_restore_invalidated_body(agent_runtime):
    rt = agent_runtime
    cid = await conversation(rt)
    rt.ollama.chat_tools = ScriptedModel(answer("合成回答"))
    completed = await run_turn(rt, cid, "你好")
    async with rt.sessions.begin() as session:
        run = await session.get(AgentRun, completed.id)
        run.status = "running"
        pi, workspace = await load_session(session, await session.get(Conversation, cid), run, rt.settings.retention_days)
        before = pi["data"]
        from workmemory.agent.coding import purge_session
        await purge_session(session, cid)
    owner = CodingRun(rt, completed.id, cid, pi["epoch"], workspace)
    with pytest.raises(AgentFailure, match="相关记忆"):
        await owner.handle({"type": "checkpoint", "data": before, "epoch": pi["epoch"], "context": {}})
    async with rt.sessions() as session:
        assert (await session.get(PiSession, cid)).data is None


async def test_coding_control_plane_requires_revision_and_new_project_conversation(runtime, tmp_path):
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama, memory=runtime.memory, run_worker=False, mcp_store=runtime.mcp.store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"Authorization": "Bearer " + runtime.settings.api_token}) as client:
            w = await client.post("/api/agent/workspaces", json={"path": str(tmp_path)})
            assert w.status_code == 201
            workspace = w.json()
            c = (await client.post("/api/conversations", json={"workspace_id": workspace["id"], "coding_enabled": True})).json()
            assert c["coding_enabled"] and c["workspace_id"] == workspace["id"]
            stale = await client.patch(f'/api/conversations/{c["id"]}', json={"coding_enabled": False, "expected_revision": 999})
            assert stale.status_code == 409
            changed = await client.patch(f'/api/conversations/{c["id"]}', json={"coding_enabled": False, "expected_revision": c["revision"]})
            assert changed.status_code == 200
            missing = await client.post("/api/agent/workspaces", json={"path": str(tmp_path / "missing")})
            assert missing.status_code == 422
            await client.delete(f'/api/agent/workspaces/{workspace["id"]}')
            assert tmp_path.is_dir()


async def test_saved_provider_codes_synthetic_project(runtime, tmp_path):
    import hashlib
    import os
    import time

    from workmemory.agent.runtime import AgentManager
    from workmemory.config import ROOT
    from workmemory.config.generation import GenerationStore
    from workmemory.inference.ollama import Ollama

    if os.environ.get("WORKMEMORY_CODING_LIVE") != "1":
        pytest.skip("Explicit synthetic coding task against the saved model")
    config_path = ROOT / "config/generation.local.json"
    before = hashlib.sha256(config_path.read_bytes()).hexdigest()
    model = Ollama(runtime.settings.ollama, GenerationStore(runtime.settings.ollama).current)
    runtime.ollama = model
    runtime.agent = AgentManager(runtime)
    (tmp_path / "maths.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_maths.py").write_text("from maths import add\nassert add(2, 3) == 5\nprint('synthetic-pi-test-passed')\n")
    cid = await coding_conversation(runtime, tmp_path)
    started = time.monotonic()
    await model.start()
    try:
        value = await submit_message(runtime, cid,
            "这是临时合成项目。请读取 maths.py 和 test_maths.py，修复 add 函数，再用 bash 执行 python3 test_maths.py 验证。完成后用中文说明修改和测试结果。只操作当前项目。")
        run_id = uuid.UUID(value["run_id"])
        await asyncio.wait_for(runtime.agent.tasks[run_id], 240)
        async with runtime.sessions() as session:
            run = await session.get(AgentRun, run_id)
            calls = (await session.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run_id).order_by(AgentToolCall.created_at))).all()
            state = await session.get(PiSession, cid)
            report = {"status": run.status, "error": run.error, "seconds": round(time.monotonic() - started, 2),
                "tools": [{"name": c.name, "status": c.status} for c in calls], "model_calls": run.model_calls,
                "context": state.context, "model": model.generation.model, "context_window": model.generation.context_window,
                "max_tokens": model.generation.max_tokens}
            (ROOT / ".cache/pi-sdk-live.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            assert run.status == "completed", report
            assert any(c.name == "bash" and c.status == "completed" for c in calls), report
        check = await asyncio.create_subprocess_exec("python3", "test_maths.py", cwd=tmp_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await check.communicate()
        assert check.returncode == 0 and b"synthetic-pi-test-passed" in stdout
    finally:
        await runtime.agent.close()
        await model.close()
        assert before == hashlib.sha256(config_path.read_bytes()).hexdigest()


async def test_source_change_stops_silent_native_command(agent_runtime, tmp_path):
    rt = agent_runtime
    source = await seed(rt)
    cid = await coding_conversation(rt, tmp_path)
    rt.ollama.chat_tools = ScriptedModel(search_call(), call("bash", {"command": "printf started > marker; sleep 30; printf stale > late"}, "silent"))
    sent = await submit_message(rt, cid, "读取记忆后运行合成命令")
    rid = uuid.UUID(sent["run_id"])
    task = rt.agent.tasks[rid]
    for _ in range(200):
        if (tmp_path / "marker").exists():
            break
        await asyncio.sleep(.05)
    assert (tmp_path / "marker").exists()
    async with rt.sessions.begin() as session:
        await mutate_episode(session, await session.get(Episode, source), "合成来源已纠正")
    await asyncio.wait_for(task, 10)
    async with rt.sessions() as session:
        assert (await session.get(PiSession, cid)).data is None
        assert await session.scalar(select(func.count()).select_from(AgentToolOutput)) == 0
    assert not (tmp_path / "late").exists()


async def test_worker_death_stops_native_command(agent_runtime, tmp_path):
    rt = agent_runtime
    cid = await coding_conversation(rt, tmp_path)
    rt.ollama.chat_tools = ScriptedModel(call("bash", {"command": "printf started > marker; sleep 30; printf stale > late"}, "crash"))
    sent = await submit_message(rt, cid, "执行合成命令")
    rid = uuid.UUID(sent["run_id"])
    task = rt.agent.tasks[rid]
    for _ in range(200):
        if (tmp_path / "marker").exists():
            break
        await asyncio.sleep(.05)
    assert (tmp_path / "marker").exists()
    rt.agent.bridge.process.kill()
    await asyncio.wait_for(task, 10)
    async with rt.sessions() as session:
        call_row = await session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == rid))
        assert call_row.status == "uncertain"
        assert (await session.get(AgentRun, rid)).status == "failed"
    assert not (tmp_path / "late").exists()
