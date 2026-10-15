"""Evaluator integration: real Pi/API/PostgreSQL, deterministic provider and judging roles."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from test_agent_reliability import ScriptedModel, answer
from workmemory.config import get_settings
from workmemory.evaluation.runner import CaseRunner
from workmemory.evaluation.scenarios import library
from workmemory.evaluation.types import Judgment, UserAction
from workmemory.main import create_app
from workmemory.mcp.config import MCPStore


def scheduled_args():
    due = (datetime.now(ZoneInfo(get_settings().timezone)) + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    return {"due_at": due.isoformat(), "time_expression": "明天下午三点"}


class HappyJudge:
    """Deliberately approves everything; only independent hard checks can catch false success."""
    calls = []

    async def ask(self, role, payload):
        if role == "judge":
            return Judgment(verdict="pass", understanding=True, completion=True, grounded=True,
                            no_false_success=True, appropriate_clarification=True, reasons=["合成评审"], evidence=["fixture"])
        pending = [c for c in payload["view"]["cards"] if c["status"] == "pending"]
        return UserAction(action="confirm" if pending else "finish", text="", card_id=pending[0]["id"] if pending else "", reason="fixture")


async def execute(runtime, tmp_path, responses):
    runtime.ollama.chat_tools = ScriptedModel(*responses)
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama,
                     memory=runtime.memory, run_worker=False, mcp_store=MCPStore(tmp_path / "eval-mcp.json"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
                                    headers={"Authorization": "Bearer " + runtime.settings.api_token}) as client:
            runner = CaseRunner(app.state.runtime, client, next(c for c in library() if c.id == "todo-create-due"), 1, 1)
            runner.roles = HappyJudge()
            # Fake model has no network/config property; only used in the synthetic judge payload.
            runtime.ollama.generation_model = "synthetic-test-model"
            return await runner.execute()


async def test_real_api_confirmation_changes_database(runtime, tmp_path):
    proposed = {"text": "", "tool_calls": [{"id": "proposal", "name": "propose_operation", "arguments": {"action": "todo.create", "text": "整理周报", **scheduled_args()}}]}
    result = await execute(runtime, tmp_path, [proposed, answer("请确认新增整理周报。")])
    assert result["status"] == "pass", result
    assert len(result["final"]["todos"]) == 1
    assert any(t["type"] == "decision" and t["result"]["status"] == "executed" for t in result["trace"])
    assert result["runs"][0]["tool_calls"][0]["name"] == "propose_operation"


async def test_false_success_is_failure_even_with_positive_model_judge(runtime, tmp_path):
    result = await execute(runtime, tmp_path, [answer("已创建整理周报待办。")])
    assert result["status"] == "fail", result
    assert not result["final"]["todos"]
    assert any(not c["passed"] for c in result["hard_checks"])


async def test_wrong_card_is_rejected_and_never_executed(runtime, tmp_path):
    wrong = {"text": "", "tool_calls": [{"id": "wrong", "name": "propose_operation", "arguments": {"action": "todo.create", "text": "删除重要文件", **scheduled_args()}}]}
    result = await execute(runtime, tmp_path, [wrong, answer("请确认。")])
    assert result["status"] == "fail", result
    assert result["behavior_error"] == "wrong_proposal" and not result["final"]["todos"]


async def test_legacy_memory_candidate_is_real_visible_confirmation(runtime, tmp_path):
    runtime.ollama.chat_tools = ScriptedModel(answer("请在记忆区确认保存中文偏好。"))
    runtime.ollama.generation_model = "synthetic-test-model"
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama,
                     memory=runtime.memory, run_worker=False, mcp_store=MCPStore(tmp_path / "eval-mcp.json"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
                                    headers={"Authorization": "Bearer " + runtime.settings.api_token}) as client:
            runner = CaseRunner(app.state.runtime, client, next(c for c in library() if c.id == "memory-create"), 1, 1)
            runner.roles = HappyJudge()
            result = await runner.execute()
    assert result["status"] == "pass", result
    assert any(t["type"] == "decision" and t["result"].get("via") == "memory_candidate" for t in result["trace"])
    assert sum(c["status"] == "confirmed" for c in result["final"]["memory_candidates"].values()) == 1


async def test_missing_source_cleanup_cannot_be_hidden_by_lazy_read_repair(runtime, tmp_path, monkeypatch):
    from sqlalchemy import select
    from workmemory.db.models import MemoryCandidate
    from workmemory.workspace import operations

    async def broken_invalidation(*_):
        pass

    async def propose_forget():
        async with runtime.sessions() as session:
            target = await session.scalar(select(MemoryCandidate).where(MemoryCandidate.status == "confirmed"))
        return {"text": "", "tool_calls": [{"id": "forget", "name": "propose_operation", "arguments": {"action": "memory.forget", "target_id": str(target.id)}}]}

    monkeypatch.setattr(operations, "invalidate_memory", broken_invalidation)
    runtime.ollama.chat_tools = ScriptedModel({"text": "", "tool_calls": [{"id": "read-memory", "name": "list_objects", "arguments": {"kind": "memory"}}]}, propose_forget, answer("请确认忘记咖啡偏好。"), answer("没有可确认的咖啡偏好记录。"))
    runtime.ollama.generation_model = "synthetic-test-model"
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama,
                     memory=runtime.memory, run_worker=False, mcp_store=MCPStore(tmp_path / "eval-mcp.json"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
                                    headers={"Authorization": "Bearer " + runtime.settings.api_token}) as client:
            runner = CaseRunner(app.state.runtime, client, next(c for c in library() if c.id == "memory-forget"), 1, 1)
            runner.roles = HappyJudge()
            result = await runner.execute()
    assert result["status"] == "fail", result
    assert any(c["name"] == "source_consumers_invalidated" and not c["passed"] for c in result["hard_checks"])


async def test_isolation_refuses_other_database_and_cleans_owned_database(runtime, tmp_path):
    import pytest
    from sqlalchemy import select, text
    from workmemory.db.models import Project
    from workmemory.evaluation.environment import InfrastructureError, Isolation

    async with runtime.sessions.begin() as session:
        session.add(Project(name="隔离保护哨兵"))
    isolation = Isolation(runtime.settings)
    isolation.root = tmp_path / isolation.name
    async with isolation:
        assert isolation.settings.database.name != runtime.settings.database.name
        owned_engine = isolation.engine
        # Simulate accidentally wiring an existing database into the evaluator.
        # Ownership must be checked before TRUNCATE or starting an application.
        isolation.engine = runtime.engine
        try:
            with pytest.raises(InfrastructureError, match="database_ownership_check_failed"):
                async with isolation.case(None):
                    pytest.fail("must reject before exposing the application")
        finally:
            isolation.engine = owned_engine
        async with runtime.sessions() as session:
            assert await session.scalar(select(Project.name)) == "隔离保护哨兵"
    assert not isolation.created and not isolation.root.exists()
    async with runtime.sessions() as session:
        assert not await session.scalar(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": isolation.name})


async def test_auto_review_refresh_is_not_manual_save_and_does_not_pass_task(runtime, tmp_path):
    # The assistant regenerates the daily cache but never proposes the requested manual edit.
    runtime.ollama.chat_tools = ScriptedModel(
        {"text": "", "tool_calls": [{"id": "prepare", "name": "get_work_day", "arguments": {"prepare_report": True}}]},
        answer("已生成自动回顾，尚未保存人工正文。"))
    runtime.ollama.generation_model = "synthetic-test-model"
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama,
                     memory=runtime.memory, run_worker=False, mcp_store=MCPStore(tmp_path / "eval-mcp.json"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
                                    headers={"Authorization": "Bearer " + runtime.settings.api_token}) as client:
            runner = CaseRunner(app.state.runtime, client, next(c for c in library() if c.id == "review-save"), 1, 1)
            runner.roles = HappyJudge()
            result = await runner.execute()
    assert result["status"] == "fail", result
    assert not result.get("behavior_error"), result
    checks = {check["name"]: check for check in result["hard_checks"]}
    assert checks["no_write_before_confirmation"]["passed"]
    assert not checks["required_decisions"]["passed"]
    assert not checks["state:derived_artifacts"]["passed"]
    assert next(iter(result["final"]["derived_artifacts"].values()))["user_text"] == "合成的今日回顾"
    # An earlier run cannot authorize a cache modification in a later unrelated turn.
    assert runner.unconfirmed_changes(result["initial"], result["final"], len(runner.runs))
