"""Opt-in real model + public network, with synthetic business data and temporary MCP config."""

import asyncio
import hashlib
import json
import os
import time
import uuid

import pytest
from sqlalchemy import select
from test_agent import conversation, seed
from test_workspace_mcp import todo
from workmemory.agent.runtime import AgentManager, submit_message
from workmemory.config import ROOT
from workmemory.config.generation import GenerationStore
from workmemory.db.models import AgentEvent, AgentRun, ChatMessage, Operation, Todo
from workmemory.inference.ollama import Ollama
from workmemory.jobs.queue import now
from workmemory.workspace.operations import decide

pytestmark = pytest.mark.skipif(
    os.environ.get("WORKMEMORY_MCP_LIVE") != "1", reason="Explicit synthetic model/network check"
)


async def test_saved_model_business_and_keyless_network(runtime):
    path = ROOT / "config/generation.local.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    store = GenerationStore(runtime.settings.ollama)
    model = Ollama(runtime.settings.ollama, store.current)
    runtime.ollama = model
    runtime.agent = AgentManager(runtime)
    for revision, server in enumerate(runtime.mcp.store.read()[1]):
        runtime.mcp.store.save(revision, server={**server.model_dump(), "enabled": True})
    await model.start()
    await seed(runtime, "合成蓝鲸项目：今天已完成接口联调；计划明天补充集成测试，尚未上线。")
    tid = await todo(runtime, text="合成待办：核对测试清单")
    cases = [
        ("today", "你知道我今天在做什么工作吗", None),
        ("daily", "总结我今日份工作内容", None),
        ("delete", "可以帮我删除这个待办记录吗", {"kind": "todo", "id": str(tid), "revision": 1}),
        ("search", "请联网搜索 Open-Meteo 官方 API 文档，告诉我一个可核验的文档链接。", None),
        ("weather", "帮我查询上海现在的天气，说明城市和数据时间。", None),
    ]
    samples = []
    traces = []
    original = model.chat_tools

    async def traced(messages, tools, **kwargs):
        value = await original(messages, tools, **kwargs)
        traces.append({"reply": value, "available": [t["name"] for t in tools]})
        return value

    model.chat_tools = traced
    try:
        for label, question, context in cases:
            traces.clear()
            cid = await conversation(runtime)
            started = time.monotonic()
            sent = await submit_message(runtime, cid, question, context=context)
            rid = uuid.UUID(sent["run_id"])
            await asyncio.wait_for(runtime.agent.tasks[rid], 240)
            async with runtime.sessions() as session:
                run = await session.get(AgentRun, rid)
                message = await session.scalar(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == cid, ChatMessage.role == "assistant")
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )
                events = (
                    await session.scalars(
                        select(AgentEvent).where(AgentEvent.run_id == rid).order_by(AgentEvent.id)
                    )
                ).all()
                ops = (
                    await session.scalars(select(Operation).where(Operation.run_id == rid))
                ).all()
                tools = [v.data.get("tool") for v in events if v.kind == "tool_started"]
                ok = run.status == "completed" and bool(message and message.content)
                if label in {"today", "daily"}:
                    ok = ok and "get_work_day" in tools and bool(message.citations)
                elif label == "delete":
                    ok = (
                        ok
                        and len(ops) == 1
                        and ops[0].action == "todo.delete"
                        and ops[0].status == "pending"
                        and bool(await session.get(Todo, tid))
                    )
                else:
                    ok = (
                        ok
                        and bool(message.external_citations)
                        and any(
                            (t or "").startswith("mcp_" if label == "search" else "weather__")
                            for t in tools
                        )
                    )
                samples.append(
                    {
                        "case": label,
                        "ok": bool(ok),
                        "status": run.status,
                        "model_calls": run.model_calls,
                        "tools": tools,
                        "seconds": round(time.monotonic() - started, 2),
                        "synthetic_answer": message.content if message else None,
                        "citations": len(message.citations) if message else 0,
                        "external_citations": len(message.external_citations) if message else 0,
                        "synthetic_trace": list(traces),
                        "error_codes": [
                            v.data.get("error_code") for v in events if v.data.get("error_code")
                        ],
                    }
                )
            if label == "delete" and ops and ok:
                assert (await decide(runtime, ops[0].id, ops[0].revision, True))[
                    "status"
                ] == "executed"
        report = {
            "at": now().isoformat(),
            "model": store.current.model,
            "provider": store.current.provider,
            "samples": samples,
            "passed": sum(v["ok"] for v in samples),
            "scope": "真实 Pi、已保存模型和公开网络；独立业务库、合成记录、临时 MCP 配置",
        }
        (ROOT / ".cache/mcp-live.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        assert all(v["ok"] for v in samples), [
            (v["case"], v["status"], v["error_codes"]) for v in samples if not v["ok"]
        ]
    finally:
        await runtime.agent.close()
        await model.close()
        assert before == (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None)
