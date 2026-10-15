"""Actual Pi, wire adapter and isolated database; long answers retain source invalidation."""

import json
from datetime import timedelta

import httpx
from sqlalchemy import select
from test_agent import conversation, run_turn
from test_bulk_todos import seed_todos
from workmemory.agent.runtime import AgentManager
from workmemory.config.generation import GenerationConfig
from workmemory.db.models import ChatMessage, Todo
from workmemory.inference.ollama import Ollama
from workmemory.jobs.queue import now
from workmemory.workspace.service import remove_todo


async def test_79_todos_long_answer_history_and_all_citations(runtime):
    rt = runtime
    ids = await seed_todos(rt, 79)
    cid = await conversation(rt)
    history = [f"合成历史第{i:02d}条：" + "长上下文记录。" * 150 for i in range(24)]
    async with rt.sessions.begin() as session:
        for i, content in enumerate(history):
            session.add(ChatMessage(conversation_id=cid, role="user", content=content,
                                    created_at=now() - timedelta(minutes=30 - i)))
    seen, items = [], []

    def handler(request):
        body = json.loads(request.content)
        assert body["max_tokens"] == 65536
        if body.get("tools", [{}])[0].get("function", {}).get("name") == "echo_probe":
            msg = {"content": "", "tool_calls": [{"id": "probe", "type": "function", "function": {
                "name": "echo_probe", "arguments": '{"value":17}'}}]}
        else:
            seen.append(body)
            result = [m for m in body["messages"] if m["role"] == "tool"]
            args = {"kind": "todo", "limit": 50}
            if result:
                data = json.loads(result[-1]["content"])
                items.extend(data["items"])
                args["cursor"] = data["next_cursor"]
            if not result or args["cursor"]:
                msg = {"content": "", "tool_calls": [{"id": f"list{len(seen)}", "type": "function", "function": {
                    "name": "list_objects", "arguments": json.dumps(args)}}]}
            else:
                answer = "\n".join(
                    f"{i+1}. {item['text']}：" + "合成任务的详细说明。" * 30 + f" [{item['evidence_id']}]"
                    for i, item in enumerate(items)
                )
                msg = {"content": json.dumps({"answer": answer, "operation_status": "none", "memory_status": "supported",
                    "evidence_ids": [v["evidence_id"] for v in items]}, ensure_ascii=False)}
        return httpx.Response(200, json={"choices": [{"message": msg,
            "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}]})

    model = Ollama(rt.settings.ollama, GenerationConfig(
        provider="openai_compatible", base_url="http://synthetic.test/v1", model="synthetic",
        context_window=1000000, max_tokens=65536,
    ))
    await model.client.aclose()
    model.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt.ollama = model
    rt.agent = AgentManager(rt)
    await model.start()
    try:
        run = await run_turn(rt, cid, "我有什么待办记录")
        assert run.status == "completed", run.error
        assert run.model_calls == 3 and len(items) == 79
        first = json.dumps(seen[0]["messages"], ensure_ascii=False)
        assert all(value in first for value in history)
        async with rt.sessions.begin() as session:
            answer = await session.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
            assert len(answer.content) > 16000
            assert set(answer.citations) == {f"todo:{i}" for i in ids}
            # Raising citation limits must retain the same deletion propagation.
            await remove_todo(session, await session.get(Todo, ids[-1]))
        async with rt.sessions() as session:
            answer = await session.get(ChatMessage, answer.id)
            assert answer.content is None and answer.status == "invalidated"
    finally:
        await rt.agent.close()
        await model.close()


async def test_saved_model_lists_79_synthetic_todos(runtime):
    import asyncio
    import hashlib
    import os
    import time
    import uuid

    import pytest
    from workmemory.agent.runtime import submit_message
    from workmemory.config import ROOT
    from workmemory.config.generation import GenerationStore
    from workmemory.db.models import AgentEvent, AgentRun, Operation

    if os.environ.get("WORKMEMORY_LIST_LIVE") != "1":
        pytest.skip("Explicit synthetic test against the saved provider")
    path = ROOT / "config/generation.local.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    store = GenerationStore(runtime.settings.ollama)
    config = GenerationConfig.model_validate({**store.current.stored(), "max_tokens": 65536,
                                             "context_window": 1000000})
    model = Ollama(runtime.settings.ollama, config)
    runtime.ollama = model
    runtime.agent = AgentManager(runtime)
    await model.start()
    started = time.monotonic()
    try:
        tids = await seed_todos(runtime, 79)
        sent = await submit_message(runtime, await conversation(runtime),
            "我有什么待办记录？请把全部待办逐项列出来，保留每条待办名称和来源。")
        rid = uuid.UUID(sent["run_id"])
        await asyncio.wait_for(runtime.agent.tasks[rid], 240)
        async with runtime.sessions() as session:
            run = await session.get(AgentRun, rid)
            message = await session.scalar(select(ChatMessage).where(ChatMessage.conversation_id == run.conversation_id, ChatMessage.role == "assistant"))
            events = (await session.scalars(select(AgentEvent).where(AgentEvent.run_id == rid))).all()
            operations = (await session.scalars(select(Operation).where(Operation.run_id == rid))).all()
            report = {"at": now().isoformat(), "status": run.status, "model_calls": run.model_calls,
                "error": run.error, "max_output_tokens": config.max_tokens,
                "context_window": config.context_window, "seconds": round(time.monotonic()-started, 2),
                "tools": [e.data.get("tool") for e in events if e.kind == "tool_started"],
                "answer_chars": len(message.content) if message else 0,
                "citation_count": len(message.citations) if message else 0,
                "todo_count": len(tids), "operation_count": len(operations),
                "all_names_returned": bool(message and all(f"合成清单任务{i:03d}" in message.content for i in range(79))),
                "output_tokens": [v["eval_tokens"] for v in model.metrics]}
            (ROOT / ".cache/agent-list-live.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            assert run.status == "completed" and report["all_names_returned"], report
            assert not operations
    finally:
        await runtime.agent.close()
        await model.close()
        assert before == hashlib.sha256(path.read_bytes()).hexdigest()
