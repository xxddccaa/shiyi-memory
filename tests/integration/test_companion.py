import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_agent import seed
from workmemory.agent.runtime import AgentManager, submit_message
from workmemory.artifacts.service import generate_artifact, request_artifact
from workmemory.companion.operations import ProposalInput, decide, propose
from workmemory.db.models import (
    ChatMessage,
    Conversation,
    DerivedArtifact,
    Episode,
    Operation,
    Project,
    Todo,
)
from workmemory.episodes.service import mutate_episode
from workmemory.jobs.queue import now


async def conversation(runtime):
    async with runtime.sessions.begin() as session:
        value = Conversation(id=uuid.uuid4(), title="跨端测试")
        session.add(value)
    return value.id


async def proposal(runtime, conversation_id, **data):
    # Explicit authenticated UI fixture, not a model inference of a missing date.
    if data.get("action") == "todo.create" and "due_at" not in data:
        data["due_at"] = now() + timedelta(days=1)
    async with runtime.sessions.begin() as session:
        return await propose(session, conversation_id, ProposalInput(**data))


async def test_confirm_once_reject_expire_and_conflict(runtime):
    identifier = await conversation(runtime)
    item = await proposal(runtime, identifier, action="todo.create", text="下周一补充测试")
    result = await asyncio.gather(
        *(decide(runtime, uuid.UUID(item["id"]), 1, True) for _ in range(2))
    )
    assert all(r["status"] == "executed" for r in result)
    async with runtime.sessions() as session:
        todos = (await session.scalars(select(Todo))).all()
        assert len(todos) == 1 and todos[0].confirmed
        todo_id = todos[0].id
    item = await proposal(runtime, identifier, action="todo.update", target_id=todo_id, done=True)
    async with runtime.sessions.begin() as session:
        todo = await session.get(Todo, todo_id)
        todo.text = "目标已改变"
        todo.revision += 1
    assert (await decide(runtime, uuid.UUID(item["id"]), 1, True))["status"] == "invalidated"
    item = await proposal(runtime, identifier, action="todo.create", text="不会执行")
    assert (await decide(runtime, uuid.UUID(item["id"]), 1, False))["status"] == "rejected"
    item = await proposal(runtime, identifier, action="todo.create", text="过期操作")
    async with runtime.sessions.begin() as session:
        operation = await session.get(Operation, uuid.UUID(item["id"]))
        operation.expires_at = now() - timedelta(seconds=1)
    assert (await decide(runtime, uuid.UUID(item["id"]), 1, True))["status"] == "invalidated"


async def test_source_mutation_invalidates_proposals(runtime):
    episode = await seed(runtime)
    identifier = await conversation(runtime)
    item = await proposal(
        runtime, identifier, action="episode.correct", target_id=episode, text="新的摘要"
    )
    async with runtime.sessions.begin() as session:
        source = await session.get(Episode, episode)
        await mutate_episode(session, source, "已经修订")
    result = await decide(runtime, uuid.UUID(item["id"]), 1, True)
    assert result["status"] == "invalidated" and result["preview"] is None


async def test_message_idempotency(runtime):
    identifier = await conversation(runtime)
    runtime.agent = AgentManager(runtime)
    launched = []
    runtime.agent.launch = launched.append
    request_id = uuid.uuid4()
    first = await submit_message(runtime, identifier, "你好", request_id)
    second = await submit_message(runtime, identifier, "你好", request_id)
    assert first == second and len(launched) == 1
    with pytest.raises(HTTPException):
        await submit_message(runtime, identifier, "不同内容", request_id)
    async with runtime.sessions() as session:
        assert len((await session.scalars(select(ChatMessage))).all()) == 1


async def test_project_merge_reclassifies_and_validates_snapshot(runtime):
    episode = await seed(runtime)
    identifier = await conversation(runtime)
    async with runtime.sessions.begin() as session:
        target = Project(id=uuid.uuid4(), name="统一项目")
        session.add(target)
        source_project = (await session.get(Episode, episode)).project_id
    item = await proposal(
        runtime, identifier, action="project.merge", target_id=target.id, other_id=source_project
    )
    result = await decide(runtime, uuid.UUID(item["id"]), 1, True)
    assert result["status"] == "executed"
    async with runtime.sessions() as session:
        source = await session.get(Episode, episode)
        assert source.project_id == target.id and source.summary
        assert (await session.get(Project, source_project)).state == "merged"


async def test_hierarchical_weekly_full_coverage_and_invalidation(runtime):
    import json

    episode = await seed(runtime)
    async with runtime.sessions.begin() as session:
        source = await session.get(Episode, episode)
        project_id = source.project_id
        # Enough records to exceed both old selection and old content budgets.
        for index in range(85):
            session.add(
                Episode(
                    evidence_state="user_confirmed", corrected=True,                    id=uuid.uuid4(),
                    device_id="synthetic",
                    started_at=source.started_at,
                    ended_at=source.ended_at,
                    project_id=project_id,
                    revision=1,
                    status="ready",
                    summary=f"第{index}条验收已经完成。" + "保留相关测试结果。" * 20,
                )
            )
    calls = []

    async def generated(messages, **kwargs):
        data = json.loads(messages[-1]["content"])
        calls.append(data)
        return json.dumps(
            {
                "progress": [
                    {"text": "本周的验收工作已经整理", "evidence_ids": [data["evidence"][0]["id"]]}
                ]
            }
        )

    runtime.ollama.chat = generated
    item = await request_artifact(runtime, "brief", str(project_id))
    async with runtime.sessions() as session:
        artifact = await session.get(DerivedArtifact, uuid.UUID(item["id"]))
        source_hash = artifact.requested_hash
    await generate_artifact(runtime, {"artifact_id": item["id"], "source_hash": source_hash})
    async with runtime.sessions() as session:
        artifact = await session.get(DerivedArtifact, uuid.UUID(item["id"]))
        assert artifact.status == "ready" and artifact.body["coverage"]["total"] == 86
        assert artifact.body["coverage"]["source_count"] == 86
        assert not artifact.body["coverage"]["truncated"] and len(calls) > 1
    async with runtime.sessions.begin() as session:
        source = await session.get(Episode, episode)
        await mutate_episode(session, source)
    async with runtime.sessions() as session:
        assert (await session.get(DerivedArtifact, uuid.UUID(item["id"]))).body is None


@pytest.fixture
async def companion_client(runtime):
    import httpx
    from workmemory.main import create_app

    app = create_app(
        runtime.settings,
        engine=runtime.engine,
        ollama=runtime.ollama,
        memory=runtime.memory,
        run_worker=False,
        mcp_store=runtime.mcp.store,
    )
    async with app.router.lifespan_context(app):
        # A stable run makes duplicate submissions exercise the HTTP route while avoiding a model.
        app.state.runtime.agent.launch = lambda identifier: None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + runtime.settings.api_token},
        ) as client:
            yield client


async def test_http_idempotency_and_preference_conflict(runtime, companion_client):
    client = companion_client
    cid = (await client.post("/api/conversations", json={})).json()["id"]
    payload = {"content": "你好", "request_id": str(uuid.uuid4())}
    first = await client.post(f"/api/conversations/{cid}/messages", json=payload)
    retry = await client.post(f"/api/conversations/{cid}/messages", json=payload)
    assert first.status_code == retry.status_code == 202
    assert first.json() == retry.json()
    state = (await client.get("/api/client/state")).json()
    change = {"expected_revision": state["revision"], "pet_size": 144}
    assert (await client.patch("/api/client/state", json=change)).status_code == 200
    assert (await client.patch("/api/client/state", json=change)).status_code == 409


async def test_memory_conflict_replacement_invalidates_old_answers(runtime, companion_client):
    from workmemory.db.models import MemoryCandidate
    from workmemory.evidence.service import remember, resolve

    cid = await conversation(runtime)
    async with runtime.sessions.begin() as session:
        message = ChatMessage(
            id=uuid.uuid4(), conversation_id=cid, role="user", content="我的偏好是先写测试"
        )
        answer = ChatMessage(
            id=uuid.uuid4(), conversation_id=cid, role="assistant", content="你喜欢先写测试"
        )
        replacement_message = ChatMessage(
            id=uuid.uuid4(), conversation_id=cid, role="user", content="我的偏好是先写原型"
        )
        session.add_all([message, replacement_message, answer])
        await session.flush()
        old = MemoryCandidate(
            id=uuid.uuid4(),
            conversation_id=cid,
            message_id=message.id,
            text="我的偏好是先写测试",
            status="confirmed",
        )
        new = MemoryCandidate(
            id=uuid.uuid4(),
            conversation_id=cid,
            message_id=replacement_message.id,
            text="我的偏好是先写原型",
            status="pending",
        )
        session.add_all([old, new])
        await session.flush()
        await remember(session, "message", answer.id, [await resolve(session, f"memory:{old.id}")])
    client = companion_client
    items = (await client.get("/api/memory/candidates")).json()["items"]
    candidate = next(i for i in items if i["id"] == str(new.id))
    assert candidate["conflicts"][0]["id"] == str(old.id)
    changed = await client.patch(
        f"/api/memory/candidates/{new.id}",
        json={
            "action": "confirm",
            "expected_revision": 1,
            "replace_id": str(old.id),
            "replace_revision": 1,
        },
    )
    assert changed.status_code == 200, changed.text
    async with runtime.sessions() as session:
        assert (await session.get(MemoryCandidate, old.id)).text is None
        assert (await session.get(ChatMessage, answer.id)).status == "invalidated"
        assert (await session.get(MemoryCandidate, new.id)).status == "confirmed"


async def test_invalidated_history_does_not_emit_forever(runtime, companion_client):
    from sqlalchemy import func
    from workmemory.db.models import ClientEvent
    from workmemory.evidence.service import remember, resolve

    source = await seed(runtime)
    cid = await conversation(runtime)
    async with runtime.sessions.begin() as session:
        answer = ChatMessage(
            id=uuid.uuid4(), conversation_id=cid, role="assistant", content="旧答案"
        )
        session.add(answer)
        await session.flush()
        await remember(session, "message", answer.id, [await resolve(session, f"summary:{source}")])
    async with runtime.sessions.begin() as session:
        await mutate_episode(session, await session.get(Episode, source), "新版")
    async with runtime.sessions() as session:
        before = await session.scalar(select(func.count()).select_from(ClientEvent))
    for _ in range(3):
        response = await companion_client.get(f"/api/conversations/{cid}/messages")
        assert response.status_code == 200 and response.json()["items"][0]["content"] is None
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ClientEvent)) == before


async def test_todo_suggestions_rejected_pair_stays_rejected(runtime, companion_client):
    async with runtime.sessions.begin() as session:
        session.add_all(
            [
                Todo(text="补充发布验收测试", confirmed=True),
                Todo(text="补充发布的验收测试", confirmed=True),
                Todo(text="验收批次 42", confirmed=True),
                Todo(text="验收批次 43", confirmed=True),
            ]
        )
    client = companion_client
    values = (await client.get("/api/todos/merge-suggestions")).json()["items"]
    assert len(values) == 1
    pair = values[0]
    result = await client.post(
        "/api/todos/merge-suggestions/reject",
        json={
            "left_id": pair["left"]["id"],
            "right_id": pair["right"]["id"],
            "left_revision": 1,
            "right_revision": 1,
        },
    )
    assert result.status_code == 200, result.text
    assert (await client.get("/api/todos/merge-suggestions")).json()["items"] == []


async def test_backfill_completion_survives_job_pruning(runtime):
    from sqlalchemy import delete
    from test_pipeline import event
    from workmemory.companion.backfill import generate, schedule
    from workmemory.companion.state import save_preferences
    from workmemory.db.models import Job
    from workmemory.ingestion.service import Batch, ingest

    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=[event()]), runtime.settings)
        episode = await session.scalar(select(Episode))
        episode.summary, episode.status = "旧自动摘要", "ready"
        episode.evidence_state, episode.evidence_policy = "legacy_unverified", 0
        source, revision = episode.id, episode.revision
        await save_preferences(session, {"backfill_enabled": True})
    await schedule(runtime)
    await generate(runtime, {"episode_id": str(source), "revision": revision})
    async with runtime.sessions.begin() as session:
        await session.execute(delete(Job).where(Job.kind == "context"))
    await schedule(runtime)
    async with runtime.sessions() as session:
        assert not (await session.scalars(select(Job).where(Job.kind == "context"))).all()
        episode = await session.get(Episode, source)
        assert episode.evidence_reviewed_revision == episode.revision
        assert episode.context_revision == episode.revision
        assert episode.evidence_state == "excerpted"


async def test_digest_pages_long_history_and_tracks_source_deletion(runtime):
    import json

    from workmemory.companion.digest import summarize_conversation
    from workmemory.db.models import ConversationDigest
    from workmemory.evidence.service import invalidate_object

    cid = await conversation(runtime)
    base = now() - timedelta(days=1)
    async with runtime.sessions.begin() as session:
        rows = [
            ChatMessage(
                id=uuid.uuid4(),
                conversation_id=cid,
                role="user",
                created_at=base + timedelta(seconds=i),
                content=("长消息" * 1500 + "最后的关键内容") if i == 0 else f"历史记录{i}",
            )
            for i in range(125)
        ]
        session.add_all(rows)
    inputs = []

    async def generated(messages, **kwargs):
        data = json.loads(messages[-1]["content"])
        inputs.extend(m["content"] for m in data["messages"])
        return json.dumps({"summary": "已保留用户陈述及其日期"})

    runtime.ollama.chat = generated
    for _ in range(5):
        await summarize_conversation(runtime, {"conversation_id": str(cid)})
    combined = "".join(inputs)
    assert "最后的关键内容" in combined and "历史记录112" in combined
    assert "历史记录124" in combined
    async with runtime.sessions.begin() as session:
        await invalidate_object(session, "chat", rows[0].id)
    async with runtime.sessions() as session:
        digest = await session.scalar(select(ConversationDigest))
        assert digest.status == "invalidated" and digest.content is None


async def test_pi_proposes_but_only_confirmation_executes(runtime):
    import json

    from workmemory.db.models import AgentRun

    cid = await conversation(runtime)

    async def model(messages, tools, **kwargs):
        if tools and tools[0]["name"] == "echo_probe":
            return {
                "text": "",
                "tool_calls": [{"id": "probe", "name": "echo_probe", "arguments": {"value": 17}}],
            }
        results = [m for m in messages if m["role"] == "toolResult"]
        if not results:
            return {
                "text": "",
                "tool_calls": [
                    {
                        "id": "action",
                        "name": "propose_operation",
                        "arguments": {"action": "todo.create", "text": "明天检查验收报告", "due_at": (now()+timedelta(days=1)).isoformat(), "time_expression": "24小时后"},
                    }
                ],
            }
        result = json.loads(results[-1]["content"][0]["text"])
        assert set(result["proposal"]) == {"id", "status", "action", "label"}
        assert result["proposal"]["action"] == "todo.create"
        return {
            "text": json.dumps(
                {
                    "answer": "已准备待办，请确认。",
                    "evidence_ids": [],
                    "operation_status": "pending", "operation_ids": [result["proposal"]["id"]],
                    "memory_status": "not_needed",
                }
            ),
            "tool_calls": [],
        }

    runtime.ollama.chat_tools = model
    runtime.agent = AgentManager(runtime)
    try:
        result = await submit_message(runtime, cid, "帮我创建待办：检查验收报告，24小时后提醒")
        await asyncio.wait_for(runtime.agent.tasks[uuid.UUID(result["run_id"])], 20)
        async with runtime.sessions() as session:
            run = await session.get(AgentRun, uuid.UUID(result["run_id"]))
            assert run.status == "completed", run.error
            assert not (await session.scalars(select(Todo))).all()
            op = await session.scalar(select(Operation))
        assert (await decide(runtime, op.id, 1, True))["status"] == "executed"
        async with runtime.sessions() as session:
            assert len((await session.scalars(select(Todo))).all()) == 1
    finally:
        await runtime.agent.close()


async def test_shared_events_commit_order_rollback_and_resume(runtime):
    from types import SimpleNamespace

    from workmemory.api.companion import client_events
    from workmemory.companion.state import emit
    from workmemory.db.models import ClientEvent

    async with runtime.sessions() as earlier:
        async with earlier.begin():
            await emit(earlier, "began_first")
            async with runtime.sessions.begin() as later:
                await emit(later, "committed_first")
    async with runtime.sessions() as aborted:
        await emit(aborted, "must_not_publish")
        await aborted.rollback()
    async with runtime.sessions() as session:
        rows = (await session.scalars(select(ClientEvent).order_by(ClientEvent.id))).all()
        assert [r.kind for r in rows] == ["committed_first", "began_first"]

    class Request:
        app = SimpleNamespace(state=SimpleNamespace(runtime=runtime))

        async def is_disconnected(self):
            return False

    response = await client_events(Request(), after=rows[0].id)
    packet = await anext(response.body_iterator)
    assert f"id: {rows[1].id}" in packet and "began_first" in packet
    await response.body_iterator.aclose()
    response = await client_events(Request(), after=rows[-1].id + 1000)
    assert "event: reset" in await anext(response.body_iterator)
    await response.body_iterator.aclose()


async def test_optional_resource_and_revision_feedback_cleanup(runtime, companion_client):
    from workmemory.companion.state import save_preferences
    from workmemory.db.models import Feedback, Observation
    from workmemory.ingestion.service import Batch, Event, ingest

    async with runtime.sessions.begin() as session:
        await save_preferences(session, {"resource_capture": True})
        event = Event(
            id=uuid.uuid4(),
            device_id="test-uri",
            captured_at=now(),
            app="com.apple.Safari",
            text="合成来源定位测试",
            method="manual",
            resource_uri="https://user:pass@example.test/path?secret=token#private",
        )
        await ingest(session, Batch(events=[event]), runtime.settings)
    async with runtime.sessions() as session:
        obs = await session.scalar(select(Observation))
        assert obs.capture_meta["resource_uri"] == "https://example.test/path"
        eid = obs.episode_id
        rev = (await session.get(Episode, eid)).revision
    body = {
        "object_kind": "episode",
        "object_id": str(eid),
        "expected_revision": rev,
        "value": "useful",
    }
    assert (await companion_client.post("/api/feedback", json=body)).status_code == 200
    async with runtime.sessions.begin() as session:
        await mutate_episode(session, await session.get(Episode, eid), "人工修正后文本")
    assert (await companion_client.post("/api/feedback", json=body)).status_code == 409
    async with runtime.sessions() as session:
        assert (await session.get(Observation, obs.id)).capture_meta == {}
        assert not (await session.scalars(select(Feedback))).all()


async def test_history_pagination_has_no_overlap(runtime, companion_client):
    cid = await conversation(runtime)
    async with runtime.sessions.begin() as session:
        session.add_all(
            [
                ChatMessage(
                    conversation_id=cid,
                    role="user",
                    content=f"过去的消息{i}",
                    created_at=now() - timedelta(hours=1, seconds=i),
                )
                for i in range(110)
            ]
        )
    first = (await companion_client.get(f"/api/conversations/{cid}/messages")).json()
    second = (
        await companion_client.get(
            f"/api/conversations/{cid}/messages", params={"before": first["next_cursor"]}
        )
    ).json()
    assert len(first["items"]) == 100 and len(second["items"]) == 10
    assert not {m["id"] for m in first["items"]} & {m["id"] for m in second["items"]}
    assert second["next_cursor"] is None


async def test_pi_system_prompt_and_final_round_reaches_real_provider_bridge(runtime):
    import json

    from workmemory.db.models import AgentRun

    runtime.settings.agent.max_model_calls = 6
    source = await seed(runtime)
    cid = await conversation(runtime)
    calls = 0

    async def model(messages, tools, **kwargs):
        nonlocal calls
        if tools and tools[0]["name"] == "echo_probe":
            return {
                "text": "",
                "tool_calls": [{"id": "probe", "name": "echo_probe", "arguments": {"value": 17}}],
            }
        calls += 1
        assert messages[0]["role"] == "system"
        assert (
            "propose_operation" in messages[0]["content"] and "当前时间" in messages[0]["content"]
        )
        if calls < 6:
            assert tools
            return {
                "text": "",
                "tool_calls": [
                    {
                        "id": f"q{calls}",
                        "name": "search_memories" if calls <= 3 else "list_objects",
                        "arguments": {"query": f"蓝鲸验收第{calls}步"} if calls <= 3 else {"kind": "project"},
                    }
                ],
            }
        assert tools == []
        return {
            "text": json.dumps(
                {
                    "answer": "已核对记录。",
                    "evidence_ids": [f"summary:{source}"],
                    "operation_status": "none", "memory_status": "supported",
                }
            ),
            "tool_calls": [],
        }

    runtime.ollama.chat_tools = model
    runtime.agent = AgentManager(runtime)
    try:
        result = await submit_message(runtime, cid, "蓝鲸验收如何？")
        rid = uuid.UUID(result["run_id"])
        await asyncio.wait_for(runtime.agent.tasks[rid], 20)
        async with runtime.sessions() as session:
            assert (await session.get(AgentRun, rid)).status == "completed"
        assert calls == 6
    finally:
        await runtime.agent.close()
