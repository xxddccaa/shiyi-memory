"""Opt-in reproducible benchmark; uses only the isolated PostgreSQL fixture and synthetic data."""

import asyncio
import json
import os
import platform
import statistics
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import bindparam, func, insert, select, text
from workmemory.config import ROOT
from workmemory.db.models import (
    Activity,
    AgentRun,
    ChatMessage,
    Conversation,
    Episode,
    Project,
    SearchDocument,
)
from workmemory.evidence.service import search, words
from workmemory.ingestion.dedup import significant
from workmemory.jobs.queue import now

pytestmark = pytest.mark.skipif(
    os.environ.get("WORKMEMORY_BENCHMARK") != "1", reason="Explicit 10k synthetic benchmark"
)


def percentile(values, p=0.95):
    return sorted(values)[min(len(values) - 1, int(len(values) * p))]


async def test_companion_acceptance_benchmark(runtime):
    fixture = json.loads((ROOT / "tests/fixtures/companion_acceptance.json").read_text())
    projects = {case["project"]: uuid.uuid4() for case in fixture["known"]}
    cases = {i: {**c, "episode_id": uuid.uuid4()} for i, c in enumerate(fixture["known"])}
    report = {
        "recorded_at": now().isoformat(),
        "hardware": platform.platform(),
        "dataset": {"episodes": 10000, "raw_documents": 10000, "known": 60, "unknown": 15},
        "scope": "业务中文全文路径；语义后端返回空集合，未计嵌入耗时。合成预期未经独立人工复核。",
    }
    started = time.monotonic()
    async with runtime.sessions.begin() as session:
        await session.execute(
            insert(Project), [{"id": value, "name": name} for name, value in projects.items()]
        )
        pids = list(projects.values())
        for offset in range(0, 10000, 250):
            episodes, raw, indexes = [], [], []
            for index in range(offset, offset + 250):
                case = cases.get(index)
                eid = case["episode_id"] if case else uuid.uuid4()
                content = (
                    case["record"] if case else f"常规界面静态浏览样本 {index}，暂无额外业务结论。"
                )
                at = now() - timedelta(days=5 if case else 1, seconds=index)
                pid = projects[case["project"]] if case else pids[index % len(pids)]
                episodes.append(
                    dict(
                        id=eid,
                        device_id="benchmark-synthetic",
                        started_at=at,
                        ended_at=at,
                        project_id=pid,
                        summary=content,
                        status="ready",
                        revision=1,
                    )
                )
                aid = uuid.uuid4()
                if case:
                    case["raw_id"] = aid
                raw.append(
                    dict(
                        id=aid,
                        episode_id=eid,
                        captured_at=at,
                        app="benchmark.synthetic",
                        title="合成",
                        text=content,
                        content_hash=f"{index:064x}",
                        method="manual",
                    )
                )
                for kind, sid in [("summary", eid), ("raw", aid)]:
                    indexes.append(
                        dict(
                            id=f"{kind}:{sid}",
                            episode_id=eid,
                            revision=1,
                            kind=kind,
                            source_id=sid,
                            captured_at=at,
                            app="benchmark.synthetic" if kind == "raw" else None,
                            expires_at=at + timedelta(days=30) if kind == "raw" else None,
                            index_text=" ".join(words(content)),
                        )
                    )
            await session.execute(insert(Episode), episodes)
            await session.execute(insert(Activity), raw)
            await session.execute(
                SearchDocument.__table__.insert().values(
                    tokens=func.to_tsvector("simple", bindparam("index_text"))
                ),
                indexes,
            )
        await session.execute(text("ANALYZE episodes"))
        await session.execute(text("ANALYZE search_documents"))
    report["load_seconds"] = round(time.monotonic() - started, 3)
    cold, warm, ranks, failures = [], [], [], []
    for iteration in range(3):
        for case in cases.values():
            start = time.monotonic()
            result = await search(runtime, case["question"], limit=8)
            (cold if iteration == 0 else warm).append(time.monotonic() - start)
            if iteration == 0:
                rank = next(
                    (
                        i
                        for i, value in enumerate(result["items"], 1)
                        if value["episode_id"] == str(case["episode_id"])
                    ),
                    None,
                )
                ranks.append(1 / rank if rank else 0)
                if not rank:
                    failures.append(case["id"])
    report["retrieval"] = {
        "recall_at_8": (60 - len(failures)) / 60,
        "mrr": statistics.mean(ranks),
        "first_pass_p95_seconds": percentile(cold),
        "warm_p95_seconds": percentile(warm),
        "failed_cases": failures,
        "note": "first_pass 不是清空操作系统/数据库缓存的物理冷启动。",
    }
    changes = [significant(c["before"], c["after"])[0] for c in fixture["critical_changes"]]
    report["critical_changes"] = {"retained": sum(changes), "total": len(changes)}
    if os.environ.get("WORKMEMORY_BENCHMARK_LIVE") == "1":
        from workmemory.agent.runtime import AgentManager, submit_message
        from workmemory.config.generation import GenerationStore
        from workmemory.inference.ollama import Ollama

        store = GenerationStore(runtime.settings.ollama)
        model = Ollama(runtime.settings.ollama, store.current)
        await model.start()
        runtime.ollama = model
        runtime.agent = AgentManager(runtime)
        samples = []
        trace = []
        provider_request = model.request

        async def traced_request(path, body, *args, **kwargs):
            # Diagnostic metadata only: never persist prompt/response bodies or credentials.
            entry = {
                "tools_offered": bool(body.get("tools")),
                "tool_choice": body.get("tool_choice"),
            }
            trace.append(entry)
            try:
                data = await provider_request(path, body, *args, **kwargs)
            except Exception as exc:
                entry["error_type"] = type(exc).__name__
                raise
            choice = (data.get("choices") or [{}])[0]
            message = data.get("message") or choice.get("message") or {}
            entry.update(
                finish_reason=data.get("done_reason") or choice.get("finish_reason"),
                text_characters=len(message.get("content") or ""),
                tool_names=[
                    c.get("function", {}).get("name") for c in message.get("tool_calls") or []
                ],
                completion_tokens=data.get("usage", {}).get("completion_tokens"),
            )
            return data

        model.request = traced_request
        try:
            selected = list(cases.values())[
                : int(os.environ.get("WORKMEMORY_BENCHMARK_ANSWERS", "60"))
            ]
            questions = [
                (
                    c["id"],
                    c["question"],
                    c["expected_text"],
                    [f"summary:{c['episode_id']}", f"raw:{c['raw_id']}"],
                )
                for c in selected
            ]
            questions += [(f"unknown-{i}", q, None, None) for i, q in enumerate(fixture["unknown"])]
            if requested := os.environ.get("WORKMEMORY_BENCHMARK_CASES"):
                identifiers = set(requested.split(","))
                assert identifiers <= {q[0] for q in questions}, "Unknown benchmark case ID"
                questions = [q for q in questions if q[0] in identifiers]
            for label, question, expected, source in questions:
                trace.clear()
                async with runtime.sessions.begin() as session:
                    conversation = Conversation(id=uuid.uuid4(), title="合成验收")
                    session.add(conversation)
                start = time.monotonic()
                result = await submit_message(runtime, conversation.id, question)
                rid = uuid.UUID(result["run_id"])
                await asyncio.wait_for(runtime.agent.tasks[rid], 180)
                async with runtime.sessions() as session:
                    run = await session.get(AgentRun, rid)
                    answer = await session.scalar(
                        select(ChatMessage).where(
                            ChatMessage.conversation_id == conversation.id,
                            ChatMessage.role == "assistant",
                        )
                    )
                    body = answer.content if answer else ""
                    citations = answer.citations if answer else []
                samples.append(
                    {
                        "id": label,
                        "question": question,
                        "status": run.status,
                        "error": run.error,
                        "model_calls": run.model_calls,
                        "search_calls": run.search_calls,
                        "model_trace": list(trace),
                        "seconds": round(time.monotonic() - start, 3),
                        "answer": body,
                        "citations": citations,
                        "expected_text": expected,
                        "expected_source_cited": any(s in source for s in citations)
                        if source
                        else None,
                        "unknown_has_no_citations": not citations if not source else None,
                    }
                )
            report["live"] = {
                "provider": store.current.provider,
                "model": store.current.model,
                "samples": samples,
                "answer_p95_seconds": percentile([s["seconds"] for s in samples]),
                "total": len(samples),
                "failed_cases": [s["id"] for s in samples if s["status"] != "completed"],
                "completed": sum(s["status"] == "completed" for s in samples),
                "known_expected_source_cited": sum(
                    bool(s["expected_source_cited"]) for s in samples if s["expected_text"]
                ),
                "note": "实际 Pi + 所选模型；语义路为空，结果待人工核对语义支持与拒答，不能把无引用当成正确拒答。",
            }
        finally:
            await runtime.agent.close()
            await model.close()
    path = ROOT / ".cache/companion-benchmark.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if "live" in report:
        assert not report["live"]["failed_cases"], report["live"]["failed_cases"]
    assert len(changes) == 50 and all(changes)
    assert report["retrieval"]["recall_at_8"] >= 0.9, report["retrieval"]
