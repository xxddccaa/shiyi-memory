import asyncio
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update
from workmemory.agent.runtime import AgentManager, submit_message
from workmemory.artifacts.service import generate_artifact, request_artifact
from workmemory.db.models import (
    AgentRun,
    ChatMessage,
    Conversation,
    DerivedArtifact,
    Episode,
    EvidenceDependency,
    Job,
    MemoryCandidate,
    Project,
)
from workmemory.episodes.service import mutate_episode
from workmemory.evidence.service import index_episode, search
from workmemory.jobs.queue import now
from workmemory.main import create_app
from workmemory.memory.base import MemoryHit
from workmemory.reminders.service import maintenance

pytestmark = pytest.mark.integration


async def seed(runtime, text="蓝鲸验收测试已通过，发布时间改为周一。", days=0):
    async with runtime.sessions.begin() as session:
        project = await session.scalar(select(Project).where(Project.name == "蓝鲸"))
        if not project:
            project = Project(id=uuid.uuid4(), name="蓝鲸")
            session.add(project)
            await session.flush()
        episode = Episode(
            id=uuid.uuid4(),
            device_id="synthetic-agent",
            started_at=now() - timedelta(days=days),
            ended_at=now() - timedelta(days=days),
            project_id=project.id,
            summary=text,
            evidence_state="user_confirmed",
            status="ready",
            revision=1,
        )
        session.add(episode)
        await index_episode(session, episode)
        return episode.id


class ToolModel:
    def __init__(self):
        self.calls = []
        self.hook = None
        self.block = None
        self.started = asyncio.Event()
        self.unsupported = False
        self.loop = False
        self.response_source = None

    async def __call__(self, messages, tools, **kwargs):
        self.calls.append(messages)
        if tools and tools[0]["name"] == "echo_probe":
            return {
                "text": "unsupported" if self.unsupported else "",
                "tool_calls": []
                if self.unsupported
                else [{"id": "probe", "name": "echo_probe", "arguments": {"value": 17}}],
            }
        self.started.set()
        if self.block:
            await self.block.wait()
        results = [m for m in messages if m["role"] == "toolResult"]
        if not results or self.loop:
            return {
                "text": "",
                "tool_calls": [
                    {
                        "id": "search" + str(len(self.calls)),
                        "name": "search_memories",
                        "arguments": {"query": f"蓝鲸验收第{len(self.calls)}步" if self.loop else "蓝鲸验收发布时间"},
                    }
                ],
            }
        if self.hook:
            hook, self.hook = self.hook, None
            await hook()
        data = json.loads(results[-1]["content"][0]["text"])
        sources = data.get("items", [])
        ids = [sources[0]["id"]] if sources else []
        if self.response_source:
            ids = [self.response_source]
        return {
            "text": json.dumps(
                {"answer": "蓝鲸验收已通过。", "evidence_ids": ids, "operation_status": "none", "memory_status": "supported"}
            ),
            "tool_calls": [],
        }


async def conversation(runtime):
    async with runtime.sessions.begin() as session:
        value = Conversation(id=uuid.uuid4())
        session.add(value)
        return value.id


async def run_turn(runtime, identifier, question="蓝鲸验收进展如何？"):
    result = await submit_message(runtime, identifier, question)
    run_id = uuid.UUID(result["run_id"])
    task = runtime.agent.tasks[run_id]
    await asyncio.wait_for(task, 20)
    async with runtime.sessions() as session:
        return await session.get(AgentRun, run_id)


@pytest.fixture
async def agent_runtime(runtime):
    model = ToolModel()
    runtime.ollama.chat_tools = model
    runtime.agent = AgentManager(runtime)
    runtime.tool_model = model
    yield runtime
    await runtime.agent.close()


async def test_real_pi_conversation_tools_persistence_and_user_candidates(agent_runtime):
    rt = agent_runtime
    source = await seed(rt)
    cid = await conversation(rt)
    run = await run_turn(rt, cid)
    assert run.status == "completed", run.error
    assert run.model_calls == 2 and run.search_calls == 1
    assert rt.agent.bridge.process.pid
    async with rt.sessions() as session:
        answer = await session.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
        assert answer.citations == [f"summary:{source}"]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EvidenceDependency)
                .where(
                    EvidenceDependency.consumer_kind == "message",
                    EvidenceDependency.consumer_id == answer.id,
                )
            )
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(MemoryCandidate)) == 0
    second = await run_turn(rt, cid, "我的工作偏好是先完成验收再发布。")
    assert second.status == "completed"
    async with rt.sessions() as session:
        candidate = await session.scalar(select(MemoryCandidate))
        assert candidate.status == "pending" and candidate.text == "我的工作偏好是先完成验收再发布"
        assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 4
    assert any(m["role"] == "assistant" for m in rt.tool_model.calls[-2])


async def test_all_consumed_evidence_invalidates_answer_even_when_not_cited(agent_runtime):
    rt = agent_runtime
    first = await seed(rt)
    second = await seed(rt, "蓝鲸验收决定需要增加备份检查。", days=1)
    rt.tool_model.response_source = f"summary:{first}"

    async def remove():
        async with rt.sessions.begin() as session:
            await mutate_episode(session, await session.get(Episode, second))

    rt.tool_model.hook = remove
    run = await run_turn(rt, await conversation(rt))
    assert run.status == "invalidated"
    async with rt.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ChatMessage).where(ChatMessage.role == "assistant")
            )
            == 0
        )


async def test_history_inherits_evidence_and_confirmed_memory_deletion(agent_runtime):
    rt = agent_runtime
    source = await seed(rt)
    cid = await conversation(rt)
    await run_turn(rt, cid)
    await run_turn(rt, cid, "继续说说蓝鲸的下一步？")
    async with rt.sessions.begin() as session:
        await mutate_episode(session, await session.get(Episode, source), "蓝鲸发布时间尚未确定。")
    async with rt.sessions() as session:
        replies = (
            await session.scalars(select(ChatMessage).where(ChatMessage.role == "assistant"))
        ).all()
        assert len(replies) == 2 and all(m.content is None and not m.citations for m in replies)


async def test_single_active_run_cancellation_and_restart(agent_runtime):
    rt = agent_runtime
    rt.tool_model.block = asyncio.Event()
    cid = await conversation(rt)
    value = await submit_message(rt, cid, "蓝鲸发布进度？")
    await asyncio.wait_for(rt.tool_model.started.wait(), 10)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await submit_message(rt, cid, "第二个客户端重复提问")
    assert exc.value.status_code == 409
    await rt.agent.cancel(uuid.UUID(value["run_id"]))
    async with rt.sessions() as session:
        assert (await session.get(AgentRun, uuid.UUID(value["run_id"]))).status == "cancelled"
        assert (
            await session.scalar(
                select(func.count()).select_from(ChatMessage).where(ChatMessage.role == "assistant")
            )
            == 0
        )
    async with rt.sessions.begin() as session:
        message = await session.scalar(select(ChatMessage))
        orphan = AgentRun(id=uuid.uuid4(), conversation_id=cid, message_id=message.id)
        session.add(orphan)
    await rt.agent.recover()
    async with rt.sessions() as session:
        assert (await session.get(AgentRun, orphan.id)).status == "interrupted"


async def test_unsupported_model_probe_uses_only_synthetic_text(agent_runtime):
    rt = agent_runtime
    rt.tool_model.unsupported = True
    run = await run_turn(rt, await conversation(rt), "我的个人项目是秘密项目")
    assert run.status == "failed" and "工具调用" in run.error
    assert len(rt.tool_model.calls) == 1
    assert "秘密项目" not in json.dumps(rt.tool_model.calls, ensure_ascii=False)


async def test_model_and_search_budgets(agent_runtime):
    rt = agent_runtime
    rt.settings.agent.max_model_calls = 6
    rt.settings.agent.max_memory_searches = 3
    await seed(rt)
    rt.tool_model.loop = True
    run = await run_turn(rt, await conversation(rt))
    assert run.status == "failed"
    assert run.model_calls == 6 and run.search_calls == 3
    from workmemory.api.agent import run_view

    async with rt.sessions() as session:
        assert (await run_view(session, run))["error_code"] == "budget_exhausted"


async def test_retrieval_preserves_rank_and_filters(runtime):
    old = await seed(runtime, "旧记录：蓝鲸数据库回滚方案已确认。", days=12)
    recent = await seed(runtime, "新记录：处理普通页面样式。")

    async def ranked(query, limit):
        return [MemoryHit(old), MemoryHit(recent)]

    runtime.memory.adapters["reme"].search = ranked
    result = await search(runtime, "数据库回滚方案", include_raw=False)
    assert result["items"][0]["episode_id"] == str(old)
    scoped = await search(runtime, "数据库回滚方案", from_at=now() - timedelta(days=1))
    assert all(s["episode_id"] != str(old) for s in scoped["items"])


async def test_artifact_cache_edit_invalidation_and_rebuild(runtime):
    source = await seed(runtime)

    async def artifact_model(messages, **kwargs):
        evidence = json.loads(messages[-1]["content"])["evidence"]
        return json.dumps(
            {"progress": [{"text": "蓝鲸验收已通过", "evidence_ids": [evidence[0]["id"]]}]}
        )

    runtime.ollama.chat = artifact_model
    async with runtime.sessions() as session:
        project = (await session.get(Episode, source)).project_id
    value = await request_artifact(runtime, "brief", str(project))
    async with runtime.sessions() as session:
        job = await session.scalar(select(Job).where(Job.kind == "artifact"))
    await generate_artifact(runtime, job.payload)
    cached = await request_artifact(runtime, "brief", str(project))
    assert cached["status"] == "ready" and cached["id"] == value["id"] and cached["attempts"] == 1
    async with runtime.sessions.begin() as session:
        artifact = await session.get(DerivedArtifact, uuid.UUID(value["id"]))
        artifact.user_text = "我的额外备注"
        await mutate_episode(session, await session.get(Episode, source), "蓝鲸验收尚未通过")
    pending = await request_artifact(runtime, "brief", str(project))
    assert pending["body"] is None and pending["needs_review"] and pending["user_text"] is None
    async with runtime.sessions() as session:
        jobs = (await session.scalars(select(Job).where(Job.kind == "artifact"))).all()
    for job in jobs:
        await generate_artifact(runtime, job.payload)
    final = await request_artifact(runtime, "brief", str(project))
    assert final["status"] == "ready"
    async with runtime.sessions() as session:
        assert (
            await session.get(DerivedArtifact, uuid.UUID(value["id"]))
        ).user_text == "我的额外备注"


async def test_artifact_automatic_attempt_budget(runtime):
    source = await seed(runtime)

    async def broken(*args, **kwargs):
        raise ValueError("synthetic failure")

    runtime.ollama.chat = broken
    async with runtime.sessions() as session:
        project = (await session.get(Episode, source)).project_id
    for _ in range(8):
        value = await request_artifact(runtime, "brief", str(project))
        async with runtime.sessions() as session:
            jobs = (await session.scalars(select(Job).where(Job.kind == "artifact"))).all()
        for job in jobs:
            await generate_artifact(runtime, job.payload)
    assert value["attempts"] == 3
    async with runtime.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "artifact")
            )
            <= 3
        )


async def test_http_candidates_confirm_expiry_and_explicit_delete(runtime):
    runtime.ollama.chat_tools = ToolModel()
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
            response = await client.post(
                f"/api/conversations/{cid}/messages",
                json={"content": "我的日常偏好是先写验收测试。"},
            )
            assert response.status_code == 202
            run_id = uuid.UUID(response.json()["run_id"])
            await asyncio.wait_for(app.state.runtime.agent.tasks[run_id], 20)
            value = (await client.get("/api/memory/candidates")).json()["items"][0]
            confirmed = await client.patch(
                f"/api/memory/candidates/{value['id']}",
                json={"action": "confirm", "expected_revision": 1},
            )
            assert confirmed.status_code == 200
            async with runtime.sessions.begin() as session:
                await session.execute(
                    update(ChatMessage).values(created_at=now() - timedelta(days=31))
                )
                await session.execute(
                    update(Conversation).values(updated_at=now() - timedelta(days=31))
                )
            await maintenance(runtime)
            value = (await client.get("/api/memory/candidates")).json()["items"][0]
            assert value["status"] == "confirmed" and "验收测试" in value["text"]
            await client.delete(f"/api/conversations/{cid}")
            assert (await client.get("/api/memory/candidates")).json()["items"] == []


async def test_canonical_raw_expiry_before_maintenance_and_delta_index(runtime):
    from workmemory.db.models import Activity, Observation, SearchDocument
    from workmemory.evidence.service import remember, resolve
    from workmemory.ingestion.dedup import patch

    eid = await seed(runtime, "蓝鲸测试记录")
    activity_id, observation_id = uuid.uuid4(), uuid.uuid4()
    async with runtime.sessions.begin() as session:
        session.add(
            Activity(
                id=activity_id,
                episode_id=eid,
                captured_at=now(),
                app="test.editor",
                title="验收",
                text="蓝鲸金额100元，等待发布",
                content_hash="b" * 64,
                method="paddleocr",
            )
        )
        await session.flush()
        session.add(
            Observation(
                id=observation_id,
                episode_id=eid,
                activity_id=activity_id,
                captured_at=now(),
                app="test.editor",
                title="验收",
                method="paddleocr",
                delta=patch("蓝鲸金额100元，等待发布", "蓝鲸金额101元，禁止发布"),
            )
        )
        await index_episode(session, await session.get(Episode, eid))
    result = await search(runtime, "禁止发布")
    raw = next(i for i in result["items"] if i["id"] == f"raw:{observation_id}")
    assert "101" in raw["content"] and "禁止发布" in raw["content"]
    cid = await conversation(runtime)
    async with runtime.sessions.begin() as session:
        message = ChatMessage(
            id=uuid.uuid4(), conversation_id=cid, role="assistant", content="原文引用"
        )
        session.add(message)
        await remember(session, "message", message.id, [raw])
        await session.execute(
            update(Activity)
            .where(Activity.id == activity_id)
            .values(captured_at=now() - timedelta(days=31))
        )
        assert await resolve(session, raw["id"]) is None
    await maintenance(runtime)
    async with runtime.sessions() as session:
        assert await resolve(session, f"summary:{eid}") is not None
        # Even when configured retention changes, stale indices cannot resolve back into evidence.
        assert not any(s["id"] == raw["id"] for s in (await search(runtime, "禁止发布"))["items"])
        assert (await session.get(Activity, activity_id)).text is None
        # Maintenance removes raw index tokens for the erased source, not only its original deadline.
        assert (
            await session.scalar(
                select(func.count()).select_from(SearchDocument).where(SearchDocument.kind == "raw")
            )
            == 0
        )


async def test_todo_multi_source_dismissal_and_distinct_amounts(runtime):
    from workmemory.artifacts.service import reconcile_todos
    from workmemory.db.models import Todo, TodoSource

    first = await seed(runtime)
    second = await seed(runtime)
    async with runtime.sessions.begin() as session:
        one, two = await session.get(Episode, first), await session.get(Episode, second)
        # Legacy multi-source data remains subject to source cleanup, even though
        # the extractor can no longer create or merge tasks.
        todo = Todo(id=uuid.uuid4(), text="需要补充验收测试", episode_id=first,
                    project_id=one.project_id, dismissed=True)
        session.add_all([todo, Todo(text="核对金额100元", episode_id=first),
                         Todo(text="核对金额101元", episode_id=second)])
        await session.flush()
        session.add_all([TodoSource(todo_id=todo.id, episode_id=first, revision=one.revision),
                         TodoSource(todo_id=todo.id, episode_id=second, revision=two.revision)])
        await reconcile_todos(session, two, ["请补充验收测试"])
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Todo)) == 3
        assert (
            await session.scalar(
                select(func.count()).select_from(TodoSource).where(TodoSource.todo_id == todo.id)
            )
            == 2
        )
    async with runtime.sessions.begin() as session:
        await mutate_episode(session, await session.get(Episode, first))
    async with runtime.sessions() as session:
        preserved = await session.get(Todo, todo.id)
        assert preserved.dismissed and preserved.episode_id == second


async def test_invalid_citations_never_persist_a_successful_answer(agent_runtime):
    rt = agent_runtime
    await seed(rt)
    rt.tool_model.response_source = "summary:" + str(uuid.uuid4())
    run = await run_turn(rt, await conversation(rt))
    assert run.status == "failed"
    async with rt.sessions() as session:
        assert not (
            await session.scalars(select(ChatMessage).where(ChatMessage.role == "assistant"))
        ).all()


async def test_forgetting_confirmed_memory_invalidates_other_conversations(agent_runtime):
    from workmemory.agent.runtime import delete_messages

    rt = agent_runtime
    origin, consumer = await conversation(rt), await conversation(rt)
    async with rt.sessions.begin() as session:
        message = ChatMessage(
            id=uuid.uuid4(), conversation_id=origin, role="user", content="蓝鲸验收必须先核对备份。"
        )
        session.add(message)
        await session.flush()
        memory = MemoryCandidate(
            id=uuid.uuid4(),
            conversation_id=origin,
            message_id=message.id,
            text=message.content,
            status="confirmed",
        )
        session.add(memory)
    rt.tool_model.response_source = f"memory:{memory.id}"
    run = await run_turn(rt, consumer)
    assert run.status == "completed"
    await delete_messages(rt, origin)
    async with rt.sessions() as session:
        answer = await session.scalar(
            select(ChatMessage).where(
                ChatMessage.conversation_id == consumer, ChatMessage.role == "assistant"
            )
        )
        assert answer.content is None and answer.status == "invalidated"
        assert not await session.get(MemoryCandidate, memory.id)
