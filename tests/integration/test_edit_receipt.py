"""Confirmed edits remain usable after old Pi context is invalidated, with fresh dependencies."""

import pytest
from sqlalchemy import select
from test_agent import agent_runtime as _agent_runtime
from test_agent import conversation, run_turn, seed
from test_agent_reliability import ScriptedModel, answer
from workmemory.db.models import ChatMessage, Episode, Operation, PiSession
from workmemory.episodes.service import mutate_episode
from workmemory.evidence.service import dependencies, valid_dependencies
from workmemory.workspace.operations import decide

agent_runtime = _agent_runtime
pytestmark = pytest.mark.integration


async def test_edit_receipt_carries_latest_fact_into_pi_and_expires_with_source(agent_runtime):
    rt = agent_runtime
    eid = await seed(rt)
    sid = f"summary:{eid}"
    cid = await conversation(rt)
    rt.ollama.chat_tools = ScriptedModel(
        {"text": "", "tool_calls": [{"id": "read", "name": "get_evidence", "arguments": {"ids": [sid]}}]},
        {"text": "", "tool_calls": [{"id": "correct", "name": "propose_operation", "arguments": {
            "action": "episode.correct", "target_id": str(eid), "text": "蓝鲸接口验收未通过，需要修复超时。",
        }}]},
        answer("已生成更正卡片，确认后生效。"),
    )
    run = await run_turn(rt, cid, "更正蓝鲸接口验收记录为未通过，需要修复超时。")
    assert run.status == "completed"
    async with rt.sessions() as session:
        op = await session.scalar(select(Operation).where(Operation.run_id == run.id))
    assert (await decide(rt, op.id, op.revision, True))["status"] == "executed"
    async with rt.sessions() as session:
        receipt = await session.scalar(select(ChatMessage).where(
            ChatMessage.conversation_id == cid, ChatMessage.role == "assistant",
            ChatMessage.status == "ready",
        ).order_by(ChatMessage.created_at.desc()).limit(1))
        assert "未通过，需要修复超时" in receipt.content and receipt.citations == [sid]
        deps = await dependencies(session, "message", receipt.id)
        assert any(d.source_id == eid and d.revision == 2 for d in deps)
        assert await valid_dependencies(session, deps)
        assert (await session.get(PiSession, cid)).data is None
    model = ScriptedModel(answer("蓝鲸接口验收未通过，需要修复超时。", [sid]))
    rt.ollama.chat_tools = model
    followup = await run_turn(rt, cid, "蓝鲸接口验收到底过了吗？")
    assert followup.status == "completed"
    assert any("最新记录：蓝鲸接口验收未通过" in str(m["content"]) for m in model.calls[0][0])
    async with rt.sessions.begin() as session:
        await mutate_episode(session, await session.get(Episode, eid))
    async with rt.sessions() as session:
        cleared = await session.get(ChatMessage, receipt.id)
        assert cleared.status == "invalidated" and cleared.content is None and cleared.citations == []
        assert (await session.get(PiSession, cid)).data is None
        responses = (await session.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == cid, ChatMessage.role == "assistant",
        ))).all()
        assert all(m.content is None for m in responses)
