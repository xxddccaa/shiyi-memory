"""Final claims about lookup and application operations must match actual tool state."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_agent import agent_runtime as _agent_runtime
from test_agent import conversation, run_turn, seed
from test_agent_reliability import ScriptedModel, answer, search_call
from test_companion import proposal
from workmemory.db.models import ChatMessage, Operation, Todo
from workmemory.jobs.queue import now

agent_runtime = _agent_runtime
pytestmark = pytest.mark.integration


async def test_no_lookup_cannot_be_reported_as_no_memory(agent_runtime):
    rt = agent_runtime
    eid = await seed(rt)
    model = ScriptedModel(
        answer("没有查到你的记录，但可以给些建议。", memory_status="insufficient"),
        search_call(), answer("蓝鲸验收通过，建议先补测试。", [f"summary:{eid}"]),
    )
    rt.ollama.chat_tools = model
    run = await run_turn(rt, await conversation(rt), "我做了什么，给点效率建议")
    assert run.status == "completed" and run.search_calls == 1 and run.model_calls == 3
    assert model.calls[1][1]
    async with rt.sessions() as session:
        replies = (await session.scalars(select(ChatMessage).where(ChatMessage.role == "assistant"))).all()
        assert len(replies) == 1 and "没有查到" not in replies[0].content


async def test_missing_card_claim_repairs_by_creating_actual_pending_card(agent_runtime):
    rt = agent_runtime
    cid = await conversation(rt)

    async def actual_card():
        async with rt.sessions() as session:
            op = await session.scalar(select(Operation).where(Operation.conversation_id == cid))
            return answer("已生成创建待办卡片，请确认。", operation_status="pending", operation_ids=[str(op.id)])

    rt.ollama.chat_tools = ScriptedModel(
        answer("已重新生成卡片，请确认。", operation_status="pending"),
        {"text": "", "tool_calls": [{"id": "create", "name": "propose_operation", "arguments": {
            "action": "todo.create", "text": "准备演示", "due_at": (now()+timedelta(days=1)).isoformat(), "time_expression": "24小时后",
        }}]}, actual_card,
    )
    run = await run_turn(rt, cid, "我没有看到卡片，请创建准备演示待办，24小时后提醒")
    assert run.status == "completed" and run.model_calls == 3
    async with rt.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Operation)) == 1
        assert await session.scalar(select(func.count()).select_from(Todo)) == 0
        assert (await session.scalar(select(Operation))).status == "pending"


@pytest.mark.parametrize("state", ["expired", "rejected", "other_conversation", "not_executed"])
async def test_wrong_operation_status_is_never_published(agent_runtime, state):
    rt = agent_runtime
    cid = await conversation(rt)
    owner = await conversation(rt) if state == "other_conversation" else cid
    card = await proposal(rt, owner, action="todo.create", text="合成待办")
    async with rt.sessions.begin() as session:
        op = await session.get(Operation, uuid.UUID(card["id"]))
        if state == "expired":
            op.expires_at = now() - timedelta(seconds=1)
        elif state == "rejected":
            op.status, op.payload, op.preview = "rejected", None, None
    bad = answer("已生成或执行卡片。", operation_status="executed" if state == "not_executed" else "pending", operation_ids=[card["id"]])
    rt.ollama.chat_tools = ScriptedModel(bad, bad)
    run = await run_turn(rt, cid, "这个待办卡片处理好了吗")
    assert run.status == "failed" and run.model_calls == 2
    async with rt.sessions() as session:
        assert not await session.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
        assert await session.scalar(select(func.count()).select_from(Todo)) == 0
