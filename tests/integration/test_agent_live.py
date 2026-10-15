"""Opt-in saved-model checks, sending only synthetic greetings and fixture records."""

import asyncio
import hashlib
import json
import os
import time
import uuid

import pytest
from sqlalchemy import select
from test_agent import conversation, seed
from workmemory.agent.runtime import AgentManager, submit_message
from workmemory.api.agent import run_view
from workmemory.config import ROOT
from workmemory.config.generation import GenerationStore
from workmemory.db.models import AgentRun, ChatMessage, Operation
from workmemory.inference.ollama import Ollama
from workmemory.jobs.queue import now

pytestmark = pytest.mark.skipif(
    os.environ.get("WORKMEMORY_AGENT_LIVE") != "1", reason="Explicit saved-model synthetic check"
)


async def test_saved_model_greetings_and_tool_paths(runtime):
    config_path = ROOT / "config/generation.local.json"
    before = hashlib.sha256(config_path.read_bytes()).hexdigest() if config_path.exists() else None
    store = GenerationStore(runtime.settings.ollama)
    model = Ollama(runtime.settings.ollama, store.current)
    runtime.ollama = model
    runtime.agent = AgentManager(runtime)
    await model.start()
    source = None
    samples = []
    questions = [
        (f"{greeting}-{i + 1}", greeting, "casual")
        for greeting in ("你好", "你好啊", "你是谁", "谢谢")
        for i in range(5)
    ]
    questions += [
        ("model-identity", "你是什么模型呀", "model"),
        ("model-recovery", "你是什么模型呀", "model"),
        ("capabilities", "你能干嘛呀", "general"),
        ("mood", "最近总觉得有点累，想找你说说话", "general"),
        ("we-chat", "我们聊点有趣的吧", "general"),
        ("happy", "今天心情不错", "general"),
        ("knowledge", "数据库事务为什么需要原子性？", "general"),
        ("poem", "帮我写一句关于月亮的诗", "general"),
        ("learning", "我下一步想学Python，你有什么建议？", "general"),
        ("context", "我们来编个故事，主角是一只叫棉花的猫。", "general"),
        ("followup", "刚才故事里的猫叫什么名字？", "context"),
        ("evidence", "蓝鲸验收进展如何？", "evidence"),
        ("after-evidence", "最近有点累，我们聊点轻松的吧。", "general"),
        ("unknown", "我的紫色星云项目海底验收编号是多少？", "unknown"),
        ("mixed", "我昨天紫色星云项目的会议说了什么？顺便解释一下数据库事务的原子性。", "mixed"),
        ("after-missing", "那讲一句关于月亮的诗吧", "general"),
        ("proposal", "请帮我创建一个待办，内容是：检查合成蓝鲸验收清单。", "proposal"),
    ]
    if requested := os.environ.get("WORKMEMORY_AGENT_LIVE_CASES"):
        questions = [item for item in questions if item[0] in requested.split(",")]
    conversations = {}
    try:
        for label, question, kind in questions:
            if source is None and kind in {"evidence", "unknown", "mixed", "proposal"}:
                source = await seed(runtime)
            previous = {
                "followup": "context",
                "after-evidence": "evidence",
                "after-missing": "mixed",
            }.get(label)
            cid = conversations.get(previous) or await conversation(runtime)
            conversations[label] = cid
            if label == "model-recovery":
                # Simulate the old refusal in existing history, without touching real conversations.
                async with runtime.sessions.begin() as session:
                    session.add(
                        ChatMessage(conversation_id=cid, role="user", content="你是什么模型呀")
                    )
                    await session.flush()
                    session.add(
                        ChatMessage(
                            conversation_id=cid,
                            role="assistant",
                            content="现有记录不足以确认，暂时没有可核验的证据。你可以补充项目名或时间范围后再问。",
                        )
                    )
            start = time.monotonic()
            sent = await submit_message(runtime, cid, question)
            rid = uuid.UUID(sent["run_id"])
            await asyncio.wait_for(runtime.agent.tasks[rid], 180)
            async with runtime.sessions() as session:
                run = await session.get(AgentRun, rid)
                value = await run_view(session, run)
                message = await session.scalar(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == cid, ChatMessage.role == "assistant")
                    .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                    .limit(1)
                )
                proposals = (
                    await session.scalars(select(Operation).where(Operation.run_id == rid))
                ).all()
                valid = bool(run.status == "completed" and message and message.content)
                if kind in {"casual", "general", "model", "context"}:
                    valid = (
                        valid
                        and run.model_calls <= 4
                        and not message.citations
                        and not any(
                            text in message.content
                            for text in ("记录不足", "没有可核验", "补充项目名", "记忆不足")
                        )
                    )
                    if kind in {"casual", "model", "context"}:
                        valid = valid and run.search_calls == 0 and run.model_calls <= 2
                    if kind == "model":
                        valid = valid and store.current.model in message.content
                    elif kind == "context":
                        valid = valid and "棉花" in message.content
                elif kind == "mixed":
                    valid = (
                        valid
                        and run.search_calls >= 1
                        and not message.citations
                        and "原子" in message.content
                        and any(
                            text in message.content
                            for text in ("不足", "没有", "未找到", "没找到", "未检索")
                        )
                    )
                elif kind == "evidence":
                    valid = valid and f"summary:{source}" in message.citations
                elif kind == "unknown":
                    valid = (
                        valid
                        and not message.citations
                        and (
                            "不足" in message.content
                            or "没有" in message.content
                            or "未找到" in message.content
                        )
                    )
                elif kind == "proposal":
                    valid = valid and len(proposals) == 1 and proposals[0].status == "pending"
                samples.append(
                    {
                        "case_id": label,
                        "kind": kind,
                        "ok": bool(valid),
                        **value,
                        "seconds": round(time.monotonic() - start, 3),
                        "proposal_count": len(proposals),
                        "synthetic_answer": message.content if message else None,
                        "memory_database_empty": source is None,
                        "citation_count": len(message.citations) if message else 0,
                    }
                )
        report = {
            "recorded_at": now().isoformat(),
            "provider": store.current.provider,
            "model": store.current.model,
            "samples": samples,
            "total": len(samples),
            "passed": sum(s["ok"] for s in samples),
            "general_cases_with_search": [
                s["case_id"] for s in samples if s["kind"] == "general" and s["search_calls"]
            ],
            "scope": "实际 Pi、已保存模型；独立 PostgreSQL、仅合成文本。无真实桌面采集或人工语义评分。",
        }
        (ROOT / ".cache").mkdir(exist_ok=True)
        (ROOT / ".cache/agent-chat-live.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        assert all(s["ok"] for s in samples), [
            (s["case_id"], s["status"], s["error_code"]) for s in samples if not s["ok"]
        ]
    finally:
        await runtime.agent.close()
        await model.close()
        after = (
            hashlib.sha256(config_path.read_bytes()).hexdigest() if config_path.exists() else None
        )
        assert before == after, "Saved model configuration changed during synthetic verification"
