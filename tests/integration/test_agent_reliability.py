"""Actual Pi + isolated PostgreSQL, with deterministic failures at the provider boundary."""

import asyncio
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from test_agent import agent_runtime as _agent_runtime
from test_agent import conversation, run_turn, seed
from workmemory.agent.errors import AgentFailure
from workmemory.agent.runtime import retry_run, submit_message
from workmemory.api.agent import run_view
from workmemory.companion.operations import ProposalInput, propose
from workmemory.db.models import AgentEvent, AgentRun, ChatMessage, Episode, MemoryCandidate
from workmemory.episodes.service import mutate_episode
from workmemory.jobs.queue import now

agent_runtime = _agent_runtime
pytestmark = pytest.mark.integration


class ScriptedModel:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, messages, tools, **kwargs):
        if tools and tools[0]["name"] == "echo_probe":
            return {
                "text": "",
                "tool_calls": [{"id": "probe", "name": "echo_probe", "arguments": {"value": 17}}],
            }
        self.calls.append((messages, tools))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = await result()
        return result


def answer(text="你好，我是拾忆。", ids=None, memory_status=None, operation_status="none", operation_ids=None):
    return {
        "text": json.dumps(
            {
                "answer": text,
                "evidence_ids": ids or [],
                "operation_status": operation_status, "operation_ids": operation_ids or [],
                "memory_status": memory_status or ("supported" if ids else "not_needed"),
            }
        ),
        "tool_calls": [],
    }


def search_call():
    return {
        "text": "",
        "tool_calls": [{"id": "s", "name": "search_memories", "arguments": {"query": "蓝鲸"}}],
    }


async def view(rt, run):
    async with rt.sessions() as session:
        return await run_view(session, run)


@pytest.mark.parametrize("question", ["你好", "你好啊", "你是谁", "谢谢"])
async def test_casual_stays_in_pi_without_tools(agent_runtime, question):
    rt = agent_runtime
    model = ScriptedModel(answer())
    rt.ollama.chat_tools = model
    run = await run_turn(rt, await conversation(rt), question)
    assert run.status == "completed" and run.model_calls == 1 and run.search_calls == 0
    assert rt.agent.bridge.process.pid
    assert model.calls[0][1] == []
    assert model.calls[0][0][0]["role"] == "system"


@pytest.mark.parametrize(
    "first",
    [
        {"text": "", "tool_calls": []},
        {"text": "not json", "tool_calls": []},
        {
            "text": json.dumps({"answer": "旧格式缺少依据声明", "evidence_ids": []}),
            "tool_calls": [],
        },
        AgentFailure("empty_response", repairable=True),
        answer("  "),
    ],
)
async def test_one_structured_repair_with_original_context(agent_runtime, first):
    rt = agent_runtime
    model = ScriptedModel(first, answer())
    rt.ollama.chat_tools = model
    run = await run_turn(rt, await conversation(rt), "你好啊")
    assert run.status == "completed" and run.model_calls == 2
    assert all(not tools for _, tools in model.calls)
    assert model.calls[1][0][:-1] == model.calls[0][0]
    assert "not json" not in json.dumps(model.calls[1])


async def test_work_question_keeps_tools_and_repair_never_replays_search(agent_runtime):
    rt = agent_runtime
    source = await seed(rt)
    model = ScriptedModel(
        search_call(),
        {"text": "bad json", "tool_calls": []},
        answer("蓝鲸验收通过", [f"summary:{source}"]),
    )
    rt.ollama.chat_tools = model
    run = await run_turn(rt, await conversation(rt))
    assert run.status == "completed" and run.model_calls == 3 and run.search_calls == 1
    assert model.calls[0][1] and not model.calls[-1][1]
    assert model.calls[-1][0][:-1] == model.calls[-2][0]


@pytest.mark.parametrize(
    "error,code",
    [
        (AgentFailure("truncated_response", finish_reason="length"), "truncated_response"),
        (AgentFailure("tool_protocol"), "tool_protocol"),
        (httpx.ReadTimeout("secret provider body"), "model_timeout"),
        (httpx.ConnectError("secret connection address"), "model_connection"),
        (
            httpx.HTTPStatusError(
                "secret key",
                request=httpx.Request("POST", "https://synthetic.invalid"),
                response=httpx.Response(401),
            ),
            "model_auth",
        ),
        (
            httpx.HTTPStatusError(
                "secret body",
                request=httpx.Request("POST", "https://synthetic.invalid"),
                response=httpx.Response(503),
            ),
            "model_http",
        ),
    ],
)
async def test_safe_diagnostic_survives_pi_bridge(agent_runtime, error, code):
    rt = agent_runtime
    rt.ollama.chat_tools = ScriptedModel(error)
    run = await run_turn(rt, await conversation(rt), "你好啊")
    data = await view(rt, run)
    assert run.status == "failed" and run.model_calls == 1
    assert data["error_code"] == code and data["retryable"]
    async with rt.sessions() as session:
        events = (
            await session.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id))
        ).all()
        serialized = json.dumps([e.data for e in events])
        assert "secret" not in serialized and "synthetic.invalid" not in serialized
        assert events[-1].data["error_code"] == code


async def test_repair_is_once_and_bounded_by_six_physical_calls(agent_runtime):
    rt = agent_runtime
    rt.settings.agent.max_model_calls = 6
    rt.ollama.chat_tools = ScriptedModel(
        search_call(), search_call(), search_call(),
        *[{"text": "", "tool_calls": [{"id": f"list{i}", "name": "list_workspace", "arguments": {}}]} for i in range(2)],
        {"text": "bad", "tool_calls": []}
    )
    run = await run_turn(rt, await conversation(rt))
    assert run.model_calls == 6 and run.status == "failed"
    assert (await view(rt, run))["error_code"] == "invalid_answer"
    rt.ollama.chat_tools = ScriptedModel(
        {"text": "bad", "tool_calls": []}, {"text": "bad again", "tool_calls": []}
    )
    run = await run_turn(rt, await conversation(rt), "你好")
    assert run.model_calls == 2 and (await view(rt, run))["error_code"] == "invalid_answer"


async def test_source_invalidation_during_repair_drops_answer(agent_runtime):
    rt = agent_runtime
    source = await seed(rt)

    async def invalidate():
        async with rt.sessions.begin() as session:
            await mutate_episode(session, await session.get(Episode, source))
        return answer("旧记录不能发布", [f"summary:{source}"])

    rt.ollama.chat_tools = ScriptedModel(
        search_call(), {"text": "bad", "tool_calls": []}, invalidate
    )
    run = await run_turn(rt, await conversation(rt))
    assert run.status == "invalidated"
    assert not (await view(rt, run))["retryable"]
    async with rt.sessions() as session:
        assert not await session.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))


async def test_concurrent_retry_is_idempotent_without_new_message_or_candidate(agent_runtime):
    rt = agent_runtime
    rt.ollama.chat_tools = ScriptedModel(AgentFailure("model_timeout"))
    cid = await conversation(rt)
    original_request = uuid.uuid4()
    text = "我的工作偏好是先写验收测试。"
    sent = await submit_message(rt, cid, text, original_request)
    await rt.agent.tasks[uuid.UUID(sent["run_id"])]
    launches = []
    rt.agent.launch = launches.append
    retry_id = uuid.uuid4()
    results = await asyncio.gather(
        *(retry_run(rt, uuid.UUID(sent["run_id"]), retry_id) for _ in range(3))
    )
    assert {r["run_id"] for r in results} == {str(retry_id)} and launches == [retry_id]
    repeated = await submit_message(rt, cid, text, original_request)
    assert repeated["run_id"] == str(retry_id)
    async with rt.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 1
        assert await session.scalar(select(func.count()).select_from(MemoryCandidate)) == 1
        assert await session.scalar(select(func.count()).select_from(AgentRun)) == 2
    with pytest.raises(HTTPException, match="409"):
        await retry_run(rt, uuid.UUID(sent["run_id"]), uuid.uuid4())


@pytest.mark.parametrize("blocker", ["expired", "later", "proposal"])
async def test_retry_rejects_old_turns_and_existing_proposals(agent_runtime, blocker):
    rt = agent_runtime
    rt.ollama.chat_tools = ScriptedModel(AgentFailure("model_timeout"))
    cid = await conversation(rt)
    run = await run_turn(rt, cid, "创建合成待办，24小时后提醒")
    async with rt.sessions.begin() as session:
        if blocker == "expired":
            message = await session.get(ChatMessage, run.message_id)
            message.created_at = now() - timedelta(days=31)
        elif blocker == "later":
            session.add(ChatMessage(conversation_id=cid, role="user", content="稍后的问题"))
        else:
            await propose(
                session, cid, ProposalInput(action="todo.create", text="合成待办", due_at=now()+timedelta(days=1), time_expression="24小时后"), run.id
            )
    assert not (await view(rt, run))["retryable"]
    with pytest.raises(HTTPException, match="409"):
        await retry_run(rt, run.id, uuid.uuid4())


async def test_old_generic_error_is_unknown_not_budget(agent_runtime):
    rt = agent_runtime
    cid = await conversation(rt)
    async with rt.sessions.begin() as session:
        message = ChatMessage(id=uuid.uuid4(), conversation_id=cid, role="user", content="你好啊")
        session.add(message)
        await session.flush()
        run = AgentRun(
            conversation_id=cid,
            message_id=message.id,
            status="failed",
            model_calls=1,
            error="Agent 未能完成有效回答，可能已达到预算或模型未按工具协议响应。",
        )
        session.add(run)
        await session.flush()
        data = await run_view(session, run)
        assert data["error_code"] == "unknown" and data["retryable"]
        assert "预算" not in data["error"]


async def test_cancel_during_repair_stops_without_publishing(agent_runtime):
    rt = agent_runtime
    entered, blocked = asyncio.Event(), asyncio.Event()

    async def slow_repair():
        entered.set()
        await blocked.wait()
        return answer()

    rt.ollama.chat_tools = ScriptedModel({"text": "bad", "tool_calls": []}, slow_repair)
    cid = await conversation(rt)
    sent = await submit_message(rt, cid, "你好")
    rid = uuid.UUID(sent["run_id"])
    await asyncio.wait_for(entered.wait(), 10)
    await rt.agent.cancel(rid)
    async with rt.sessions() as session:
        run = await session.get(AgentRun, rid)
        assert run.status == "cancelled" and run.model_calls == 2
        assert not await session.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))


async def test_http_retry_and_history_expose_safe_error_and_latest_run(runtime):
    from workmemory.main import create_app

    runtime.ollama.chat_tools = ScriptedModel(AgentFailure("model_timeout"), answer())
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
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + runtime.settings.api_token},
        ) as client:
            cid = (await client.post("/api/conversations", json={})).json()["id"]
            original_request = str(uuid.uuid4())
            result = await client.post(
                f"/api/conversations/{cid}/messages",
                json={"content": "你好啊", "request_id": original_request},
            )
            rid = uuid.UUID(result.json()["run_id"])
            await app.state.runtime.agent.tasks[rid]
            history = (await client.get(f"/api/conversations/{cid}/messages")).json()
            detail = (await client.get(f"/api/agent/runs/{rid}")).json()
            assert history["run"] == detail
            assert detail["error_code"] == "model_timeout" and detail["retryable"]
            new_id = str(uuid.uuid4())
            responses = await asyncio.gather(
                *(
                    client.post(f"/api/agent/runs/{rid}/retry", json={"request_id": new_id})
                    for _ in range(2)
                )
            )
            assert all(r.status_code == 202 and r.json()["run_id"] == new_id for r in responses)
            task = app.state.runtime.agent.tasks.get(uuid.UUID(new_id))
            if task:
                await task
            latest = (await client.get(f"/api/conversations/{cid}/messages")).json()
            assert len(latest["items"]) == 2 and latest["run"]["status"] == "completed"
            repeat = await client.post(
                f"/api/conversations/{cid}/messages",
                json={"content": "你好啊", "request_id": original_request},
            )
            assert repeat.json()["run_id"] == new_id
            stale = await client.post(
                f"/api/agent/runs/{rid}/retry", json={"request_id": str(uuid.uuid4())}
            )
            assert stale.status_code == 409


@pytest.mark.parametrize(
    "question,reply",
    [
        ("你是什么模型呀", "当前配置的模型是 Synthetic-Chat-27B。"),
        ("你能干嘛呀", "我可以陪你聊天、解释知识，也能帮你查找记忆。"),
        ("最近总觉得有点累，想找你说说话", "愿意聊聊今天让你疲惫的事吗？"),
        ("我们聊点有趣的吧", "要不要聊聊猫为什么喜欢纸箱？"),
        ("今天心情不错", "听起来今天过得不错，发生了什么开心的事？"),
        ("数据库事务为什么需要原子性？", "原子性确保事务要么全部完成，要么全部撤销。"),
        ("帮我写一句关于月亮的诗", "月光落在窗边，替晚归的人留了一盏灯。"),
        ("我下一步想学习Python，给点建议", "可以先练习变量、循环，再写一个小程序。"),
    ],
)
async def test_uncited_general_reply_is_not_replaced_by_memory_fallback(
    agent_runtime, question, reply
):
    rt = agent_runtime
    rt.ollama.generation_model = "Synthetic-Chat-27B"
    model = ScriptedModel(answer(reply))
    rt.ollama.chat_tools = model
    run = await run_turn(rt, await conversation(rt), question)
    assert run.status == "completed" and run.search_calls == 0 and run.model_calls == 1
    async with rt.sessions() as session:
        message = await session.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
        assert message.content == reply and message.citations == []
    assert "Synthetic-Chat-27B" in model.calls[0][0][0]["content"]


async def test_missing_memory_does_not_erase_other_parts_of_mixed_answer(agent_runtime):
    rt = agent_runtime
    reply = "没找到你昨天会议的记录。数据库事务的原子性是指全部成功或全部撤销。"
    rt.ollama.chat_tools = ScriptedModel(search_call(), answer(reply, memory_status="insufficient"))
    run = await run_turn(
        rt, await conversation(rt), "我昨天的会议说了什么？顺便解释数据库事务的原子性。"
    )
    assert run.status == "completed" and run.search_calls == 1
    async with rt.sessions() as session:
        message = await session.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
        assert message.content == reply and message.citations == []


@pytest.mark.parametrize("searched", [False, True])
async def test_claimed_memory_facts_still_require_supplied_citations(agent_runtime, searched):
    rt = agent_runtime
    eid = await seed(rt)
    results = [search_call()] if searched else []
    rt.ollama.chat_tools = ScriptedModel(
        *results, answer("验收通过；也建议分批工作。", memory_status="supported"),
        *([] if searched else [search_call()]),
        answer("记录显示蓝鲸验收通过；也建议分批工作。", [f"summary:{eid}"]),
    )
    run = await run_turn(rt, await conversation(rt), "验收通过了吗？再给点效率建议。")
    assert run.status == "completed" and run.search_calls == 1
    async with rt.sessions() as session:
        message = await session.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
        assert "验收通过" in message.content and "分批工作" in message.content
        assert message.citations == [f"summary:{eid}"]


async def test_general_followup_uses_conversation_and_current_model_configuration(agent_runtime):
    rt = agent_runtime
    cid = await conversation(rt)
    rt.ollama.generation_model = "Synthetic-Old"
    model = ScriptedModel(answer("当前配置是 Synthetic-Old。"))
    rt.ollama.chat_tools = model
    await run_turn(rt, cid, "你是什么模型呀")
    rt.ollama.generation_model = "Synthetic-New"
    model.results.append(answer("刚才我们聊了模型名称，现在配置是 Synthetic-New。"))
    run = await run_turn(rt, cid, "刚才我们聊了什么，你现在还是那个模型吗？")
    assert run.status == "completed" and run.search_calls == 0
    messages = model.calls[-1][0]
    assert (
        "Synthetic-New" in messages[0]["content"] and "Synthetic-Old" not in messages[0]["content"]
    )
    assert any("Synthetic-Old" in json.dumps(m, ensure_ascii=False) for m in messages[1:])
    # Conversation-based replies remain dependent on consumed history even without citations.
    from workmemory.agent.runtime import delete_messages

    await delete_messages(rt, cid)
    async with rt.sessions() as session:
        assert not await session.scalar(select(ChatMessage).where(ChatMessage.content.is_not(None)))
