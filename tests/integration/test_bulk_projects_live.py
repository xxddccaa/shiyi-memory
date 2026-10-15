"""Real saved model; only synthetic projects in isolated PostgreSQL, no production mutations."""

import asyncio
import hashlib
import json
import os
import time
import uuid

import pytest
from sqlalchemy import func, select
from test_agent import conversation
from test_bulk_projects import seed_projects
from workmemory.agent.runtime import AgentManager, submit_message
from workmemory.config import ROOT
from workmemory.config.generation import GenerationStore
from workmemory.db.models import AgentEvent, AgentRun, Operation, Project
from workmemory.inference.ollama import Ollama
from workmemory.jobs.queue import now
from workmemory.workspace.operations import decide

pytestmark = pytest.mark.skipif(
    os.environ.get("WORKMEMORY_PROJECTS_LIVE") != "1",
    reason="Explicit saved-model synthetic bulk check",
)


async def test_real_model_prepares_one_card_for_32_projects(runtime):
    path = ROOT / "config/generation.local.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    store = GenerationStore(runtime.settings.ollama)
    model = Ollama(runtime.settings.ollama, store.current)
    runtime.ollama = model
    runtime.agent = AgentManager(runtime)
    await model.start()
    started = time.monotonic()
    try:
        await seed_projects(runtime)
        sent = await submit_message(
            runtime, await conversation(runtime), "请把我的所有项目全部删除"
        )
        rid = uuid.UUID(sent["run_id"])
        await asyncio.wait_for(runtime.agent.tasks[rid], 240)
        async with runtime.sessions() as session:
            run = await session.get(AgentRun, rid)
            operations = (
                await session.scalars(select(Operation).where(Operation.run_id == rid))
            ).all()
            tools = (
                await session.scalars(
                    select(AgentEvent)
                    .where(AgentEvent.run_id == rid, AgentEvent.kind == "tool_started")
                    .order_by(AgentEvent.id)
                )
            ).all()
            report = {
                "at": now().isoformat(),
                "model": store.current.model,
                "run_status": run.status,
                "model_calls": run.model_calls,
                "tools": [e.data.get("tool") for e in tools],
                "cards": len(operations),
                "counts": [op.preview.get("count") if op.preview else None for op in operations],
                "remaining_before_confirmation": await session.scalar(
                    select(func.count()).select_from(Project).where(Project.state == "active")
                ),
                "seconds": round(time.monotonic() - started, 2),
                "scope": "真实保存模型、实际 Pi、独立 PostgreSQL 与32条合成项目，无生产数据删除",
            }
        (ROOT / ".cache/bulk-project-live.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        assert run.status == "completed" and len(operations) == 1, report
        assert operations[0].action == "project.delete_many" and report["counts"] == [32], report
        assert operations[0].status == "pending" and report["remaining_before_confirmation"] == 32
        assert (await decide(runtime, operations[0].id, operations[0].revision, True))["result"][
            "count"
        ] == 32
        async with runtime.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Project).where(Project.state == "active")) == 0
    finally:
        await runtime.agent.close()
        await model.close()
        assert before == (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None)
