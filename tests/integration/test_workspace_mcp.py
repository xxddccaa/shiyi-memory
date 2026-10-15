"""Business deletion and real local MCP protocol, always in isolated PostgreSQL."""

import asyncio
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from test_agent import conversation, run_turn, seed
from test_agent_reliability import ScriptedModel, answer
from test_companion import proposal
from workmemory.agent.runtime import AgentManager, submit_message
from workmemory.artifacts.service import reconcile_todos
from workmemory.db.models import (
    Activity,
    AgentRun,
    ChatMessage,
    Episode,
    ExternalSource,
    Observation,
    Operation,
    Project,
    Reminder,
    Todo,
    TodoSource,
    TodoTombstone,
)
from workmemory.episodes.service import mutate_episode
from workmemory.evidence.service import (
    copy_dependencies,
    dependencies,
    remember,
    resolve,
    valid_dependencies,
)
from workmemory.jobs.queue import now
from workmemory.main import create_app
from workmemory.reminders.service import maintenance
from workmemory.workspace.operations import decide
from workmemory.workspace.service import get_object, list_objects, work_day

pytestmark = pytest.mark.integration


async def active_run(rt, cid=None):
    cid = cid or await conversation(rt)
    async with rt.sessions.begin() as session:
        message = ChatMessage(id=uuid.uuid4(), conversation_id=cid, role="user", content="合成查询；如要创建准备演示待办，24小时后提醒")
        session.add(message)
        await session.flush()
        run = AgentRun(
            id=uuid.uuid4(), conversation_id=cid, message_id=message.id, status="running"
        )
        session.add(run)
    return run


async def todo(rt, eid=None, text="删除验收任务", pid=None):
    async with rt.sessions.begin() as session:
        episode = await session.get(Episode, eid) if eid else None
        from workmemory.artifacts.service import todo_key
        project = episode.project_id if episode else pid
        value = Todo(id=uuid.uuid4(), text=text, confirmed=True, project_id=project,
                     episode_id=eid, normalized_key=todo_key(text, project),
                     due_at=now().replace(hour=7, minute=0, second=0, microsecond=0)+timedelta(days=1), origin="legacy" if eid else "manual",
                     local_time="15:00:00")
        session.add(value)
        await session.flush()
        if episode:
            session.add(TodoSource(todo_id=value.id, episode_id=eid, revision=episode.revision))
        return value.id


async def test_delete_clears_derived_text_reminders_and_prevents_resurrection(runtime):
    eid = await seed(runtime)
    tid = await todo(runtime, eid)
    cid = await conversation(runtime)
    async with runtime.sessions.begin() as session:
        value = await session.get(Todo, tid)
        value.confirmed, value.due_at = True, now() - timedelta(minutes=1)
        session.add(
            Reminder(
                kind="todo",
                text=value.text,
                todo_id=tid,
                episode_id=eid,
                due_at=now(),
                dedupe_key="synthetic-delete-reminder",
            )
        )
        message = ChatMessage(
            id=uuid.uuid4(), conversation_id=cid, role="assistant", content=value.text
        )
        session.add(message)
        await remember(session, "message", message.id, [await resolve(session, f"todo:{tid}")])
    item = await proposal(runtime, cid, action="todo.delete", target_id=tid)
    async with runtime.sessions() as session:
        assert await session.get(Todo, tid)  # proposal does not delete
    decisions = await asyncio.gather(
        *(decide(runtime, uuid.UUID(item["id"]), 1, True) for _ in range(2))
    )
    assert all(v["status"] == "executed" and v["preview"] is None for v in decisions)
    async with runtime.sessions.begin() as session:
        assert not await session.get(Todo, tid)
        tombstone = await session.get(TodoTombstone, tid)
        assert tombstone and not hasattr(tombstone, "text") and str(eid) in tombstone.source_ids
        assert not await session.scalar(select(Reminder).where(Reminder.todo_id == tid))
        assert not await session.scalar(select(TodoSource).where(TodoSource.todo_id == tid))
        assert (await session.get(ChatMessage, message.id)).content is None
        operation = await session.get(Operation, uuid.UUID(item["id"]))
        assert operation.payload is None and operation.preview is None
        source = await session.get(Episode, eid)
        assert source.summary  # deleting a todo does not delete the original capture
        await reconcile_todos(session, source, ["删除验收任务"])
        assert not await session.scalar(select(Todo))
    # User can deliberately recreate the same task; legacy extraction remains disabled.
    new = await proposal(
        runtime, cid, action="todo.create", text="删除验收任务", project_id=tombstone.project_id
    )
    assert (await decide(runtime, uuid.UUID(new["id"]), 1, True))["status"] == "executed"
    async with runtime.sessions.begin() as session:
        await reconcile_todos(session, await session.get(Episode, eid), ["删除验收任务"])
        assert await session.scalar(select(func.count()).select_from(Todo)) == 1
        assert not await session.get(TodoTombstone, tid)


async def test_tombstone_survives_project_merge_and_concurrent_reconcile(runtime):
    eid = await seed(runtime)
    tid = await todo(runtime, eid)
    cid = await conversation(runtime)
    deletion = await proposal(runtime, cid, action="todo.delete", target_id=tid)

    async def reconcile():
        async with runtime.sessions.begin() as session:
            await reconcile_todos(session, await session.get(Episode, eid), ["删除验收任务"])

    await asyncio.gather(decide(runtime, uuid.UUID(deletion["id"]), 1, True), reconcile())
    async with runtime.sessions.begin() as session:
        original = (await session.get(Episode, eid)).project_id
        target = Project(id=uuid.uuid4(), name="合并目标")
        session.add(target)
        assert not await session.get(Todo, tid)
    merge = await proposal(
        runtime, cid, action="project.merge", target_id=target.id, other_id=original
    )
    assert (await decide(runtime, uuid.UUID(merge["id"]), 1, True))["status"] == "executed"
    await reconcile()
    async with runtime.sessions() as session:
        assert not await session.scalar(select(Todo))
        assert (await session.get(TodoTombstone, tid)).project_id == target.id


async def test_objects_over_twelve_keyword_pagination_and_stale_selection(runtime):
    tids = [await todo(runtime, text=f"第{i:02d}项部署任务") for i in range(32)]
    async with runtime.sessions() as session:
        first = await list_objects(session, "todo", limit=12)
        second = await list_objects(session, "todo", cursor=first["next_cursor"], limit=12)
        assert first["total"] == 32
        assert not {v["id"] for v in first["items"]} & {v["id"] for v in second["items"]}
        search = await list_objects(session, "todo", query="第31项")
        assert [v["id"] for v in search["items"]] == [str(tids[-1])]
        with pytest.raises(HTTPException) as error:
            await get_object(session, "todo", tids[-1], revision=99)
        assert error.value.status_code == 409
    runtime.agent = AgentManager(runtime)
    launched = []
    runtime.agent.launch = launched.append
    cid, req = await conversation(runtime), uuid.uuid4()
    context = {"kind": "todo", "id": str(tids[-1]), "revision": 1}
    first = await submit_message(runtime, cid, "删除这个", req, context)
    assert first == await submit_message(runtime, cid, "删除这个", req, context)
    with pytest.raises(HTTPException):
        await submit_message(runtime, cid, "删除这个", req, {**context, "id": str(tids[0])})
    assert len(launched) == 1
    await runtime.agent.close()


async def test_day_returns_pending_raw_and_ready_sources_with_timezone(runtime):
    eid = await seed(runtime)
    async with runtime.sessions.begin() as session:
        pending = Episode(
            id=uuid.uuid4(),
            device_id="synthetic",
            started_at=now(),
            ended_at=now(),
            status="pending",
        )
        session.add(pending)
        await session.flush()
        raw = Activity(
            id=uuid.uuid4(),
            episode_id=pending.id,
            content_hash="synthetic-pending",
            captured_at=now(),
            app="test",
            title="",
            text="合成工作原文：正在修改测试用例。",
            method="manual",
        )
        session.add(raw)
    day = await work_day(runtime)
    assert day["timezone"] == "Asia/Shanghai" and day["raw_count"] == 1
    assert day["episode_counts"] == {"ready": 1, "pending": 1}
    assert {v["id"] for v in day["items"]} == {f"summary:{eid}", f"raw:{raw.id}"}
    from workmemory.ingestion.dedup import patch

    revised = "合成工作原文：测试用例已经通过。"
    async with runtime.sessions.begin() as session:
        observed = Observation(
            id=uuid.uuid4(),
            episode_id=pending.id,
            activity_id=raw.id,
            captured_at=raw.captured_at + timedelta(seconds=5),
            app="test",
            method="manual",
            delta=patch(raw.text, revised),
        )
        session.add(observed)
    updated = await work_day(runtime)
    latest = next(v for v in updated["items"] if v["episode_id"] == str(pending.id))
    assert latest["id"] == f"raw:{observed.id}" and latest["content"] == revised
    assert not (await work_day(runtime, "2000-01-01"))["items"]
    with pytest.raises(HTTPException):
        await work_day(runtime, "invalid")
    async with runtime.sessions.begin() as session:
        await mutate_episode(session, await session.get(Episode, pending.id))
    assert (await work_day(runtime))["raw_count"] == 0


async def test_local_real_mcp_scoped_auth_no_self_confirm_and_restart(runtime):
    run = await active_run(runtime)
    tid = await todo(runtime)
    async with runtime.mcp.connection(run_id=run.id) as client:
        tools = (await client.list_tools()).tools
        assert "propose_operation" in {t.name for t in tools}
        assert not {"decide", "confirm", "delete_todo", "execute"} & {t.name for t in tools}
        value = await client.call_tool(
            "get_object", {"kind": "todo", "identifier": str(tid), "revision": 1}
        )
        assert not value.isError and str(tid) in value.content[0].text
        invalid = await client.call_tool("get_object", {"kind": "todo", "identifier": "not-uuid"})
        assert invalid.isError
    await runtime.mcp.close()
    value = await runtime.mcp.call(run.id, "list_objects", {"kind": "todo"})
    assert value["items"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runtime.mcp.asgi), base_url="http://127.0.0.1"
    ) as client:
        assert (await client.post("/api/mcp", json={})).status_code == 401
        assert (
            await client.post(
                "/api/mcp",
                headers={"Authorization": "Bearer " + runtime.settings.api_token},
                json={},
            )
        ).status_code == 401
    async with runtime.sessions.begin() as session:
        (await session.get(AgentRun, run.id)).status = "cancelled"
    with pytest.raises(ValueError):
        await runtime.mcp.call(run.id, "list_workspace", {})


async def test_external_inherits_private_dependencies_and_expiry(runtime):
    eid = await seed(runtime, "私人合成内容不可复制到搜索请求的完整文字，且不能保留旧回答。")
    run = await active_run(runtime)
    async with runtime.sessions.begin() as session:
        await remember(session, "run", run.id, [await resolve(session, f"summary:{eid}")])
    assert not await runtime.mcp.outbound_allowed(
        run.id, {"query": "私人合成内容不可复制到搜索请求的完整文字"}
    )
    assert await runtime.mcp.outbound_allowed(run.id, {"query": "Open-Meteo public docs"})
    result = await runtime.mcp.external_result(
        run.id,
        "exa",
        {"text": "Title: Public docs\nURL: https://example.com/docs\nDocumentation excerpt"},
    )
    xid = uuid.UUID(result["external_sources"][0]["id"])
    async with runtime.sessions.begin() as session:
        message = ChatMessage(
            id=uuid.uuid4(),
            conversation_id=run.conversation_id,
            role="assistant",
            content="合成引用回答",
            external_citations=[str(xid)],
        )
        session.add(message)
        await copy_dependencies(session, "run", run.id, "message", message.id)
        assert await valid_dependencies(session, await dependencies(session, "message", message.id))
        await mutate_episode(session, await session.get(Episode, eid))
    async with runtime.sessions() as session:
        row = await session.get(ExternalSource, xid)
        assert row.content is None and not row.url and not row.title
        assert (await session.get(ChatMessage, message.id)).content is None
        assert not await valid_dependencies(
            session, await dependencies(session, "message", message.id)
        )
    # A public-only source still expires and clears consumers, even if the message is newer.
    run = await active_run(runtime)
    result = await runtime.mcp.external_result(
        run.id, "exa", {"text": "Title: Expiring docs\nhttps://example.com/old\nOld public excerpt"}
    )
    xid = uuid.UUID(result["external_sources"][0]["id"])
    async with runtime.sessions.begin() as session:
        (await session.get(ExternalSource, xid)).expires_at = now() - timedelta(seconds=1)
        # Dependencies keep the captured expiry; validation also checks canonical expiry.
        assert not await valid_dependencies(session, await dependencies(session, "run", run.id))
    await maintenance(runtime)
    async with runtime.sessions() as session:
        assert not await session.get(ExternalSource, xid)


async def test_pi_malformed_first_response_keeps_tools_then_proposes_delete(runtime):
    tid = await todo(runtime)
    model = ScriptedModel(
        {"text": "invalid", "tool_calls": []},
        {
            "text": "",
            "tool_calls": [
                {
                    "id": "p",
                    "name": "propose_operation",
                    "arguments": {"action": "todo.delete", "target_id": str(tid)},
                }
            ],
        },
        answer("已准备删除卡片，请确认。"),
    )
    runtime.ollama.chat_tools = model
    runtime.agent = AgentManager(runtime)
    try:
        cid = await conversation(runtime)
        result = await submit_message(
            runtime, cid, "删除这个", context={"kind": "todo", "id": str(tid), "revision": 1}
        )
        await runtime.agent.tasks[uuid.UUID(result["run_id"])]
        async with runtime.sessions() as session:
            run = await session.get(AgentRun, uuid.UUID(result["run_id"]))
            assert run.status == "completed" and run.model_calls == 3
            assert await session.get(Todo, tid)
            op = await session.scalar(select(Operation).where(Operation.run_id == run.id))
            assert op.status == "pending" and op.action == "todo.delete"
        assert model.calls[1][1]  # format repair retains real tool capability
        assert (await decide(runtime, op.id, 1, True))["status"] == "executed"
    finally:
        await runtime.agent.close()


@pytest.mark.parametrize("forged", [False, True])
async def test_pi_weather_citations_only_accept_actual_run_sources(runtime, monkeypatch, forged):
    from workmemory.mcp import weather

    async def fake_weather(name, args):
        return {
            "city": "上海",
            "weather": {"current": {"temperature_2m": 23, "time": "2026-10-10T12:00"}},
            "title": "上海天气",
            "source_url": "https://api.open-meteo.com/v1/forecast",
        }

    monkeypatch.setattr(weather, "call", fake_weather)
    server = runtime.mcp.store.public()["servers"][1]
    server.pop("has_token")
    server.pop("env_keys")
    server["enabled"] = True
    runtime.mcp.store.save(0, server=server)

    async def model(messages, tools, **kwargs):
        if tools and tools[0]["name"] == "echo_probe":
            return {
                "tool_calls": [{"id": "probe", "name": "echo_probe", "arguments": {"value": 17}}]
            }
        results = [m for m in messages if m["role"] == "toolResult"]
        if not results:
            return {
                "tool_calls": [
                    {
                        "id": "weather",
                        "name": "weather__get_weather",
                        "arguments": {"city": "上海", "latitude": 31.2, "longitude": 121.5},
                    }
                ]
            }
        data = json.loads(results[-1]["content"][0]["text"])
        ids = [str(uuid.uuid4())] if forged else [v["id"] for v in data["external_sources"]]
        return {
            "text": json.dumps(
                {
                    "answer": "上海当前气温 23°C。",
                    "evidence_ids": [],
                    "operation_status": "none", "memory_status": "not_needed",
                    "external_source_ids": ids,
                }
            )
        }

    runtime.ollama.chat_tools = model
    runtime.agent = AgentManager(runtime)
    try:
        run = await run_turn(runtime, await conversation(runtime), "上海天气如何？")
        assert run.status == ("failed" if forged else "completed")
        async with runtime.sessions() as session:
            messages = (
                await session.scalars(select(ChatMessage).where(ChatMessage.role == "assistant"))
            ).all()
            assert len(messages) == (0 if forged else 1)
            if messages:
                assert len(messages[0].external_citations) == 1 and not messages[0].citations
    finally:
        await runtime.agent.close()


async def test_http_mcp_config_revision_secrets_and_auth(runtime):
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
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer " + runtime.settings.api_token},
        ) as client:
            state = (await client.get("/api/mcp/servers")).json()
            assert all(not s["enabled"] for s in state["servers"])
            body = {
                "expected_revision": state["revision"],
                "server": {
                    "id": "custom",
                    "label": "只读测试",
                    "url": "https://example.test/mcp",
                    "token": "secret-test-token",
                    "env": {"EXAMPLE_SECRET": "secret-env"},
                },
            }
            response = await client.put("/api/mcp/servers", json=body)
            assert (
                response.status_code == 200
                and "secret-test-token" not in response.text
                and "secret-env" not in response.text
            )
            assert (await client.put("/api/mcp/servers", json=body)).status_code == 409
            invalid = await client.put(
                "/api/mcp/servers",
                json={
                    "expected_revision": 1,
                    "server": {**body["server"], "url": "https://user:secret@example.test/mcp"},
                },
            )
            assert invalid.status_code == 422 and "secret" not in invalid.text
            assert (await client.post("/api/mcp", json={})).status_code == 401
            assert (await client.post("/api/mcp/", json={})).status_code == 401
            assert (
                await client.get("/api/mcp/servers", headers={"Authorization": "Bearer bad"})
            ).status_code == 401


async def test_selected_object_changed_before_execution_is_source_invalidated(runtime):
    tid = await todo(runtime)
    runtime.agent = AgentManager(runtime)
    runtime.agent.launch = lambda run_id: None
    try:
        cid = await conversation(runtime)
        response = await submit_message(
            runtime, cid, "删除这个", context={"kind": "todo", "id": str(tid), "revision": 1}
        )
        async with runtime.sessions.begin() as session:
            (await session.get(Todo, tid)).revision += 1
        run_id = uuid.UUID(response["run_id"])
        await runtime.agent._execute(run_id)
        async with runtime.sessions() as session:
            result = await session.get(AgentRun, run_id)
            assert result.status == "invalidated" and result.model_calls == 0
            assert await session.get(Todo, tid)
    finally:
        await runtime.agent.close()
