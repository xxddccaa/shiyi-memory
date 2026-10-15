import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update
from workmemory.assistant.service import answer_question, retrieve
from workmemory.db.models import Activity, Episode, Job, MemoryReceipt, Project, Reminder, Todo
from workmemory.episodes.service import mutate_episode, summarize_job
from workmemory.ingestion.service import Batch, Event, ingest
from workmemory.jobs.queue import claim, enqueue, finish, now
from workmemory.main import create_app
from workmemory.memory.sync import schedule_sync, switch_backend, sync_job
from workmemory.reminders.service import maintenance

pytestmark = pytest.mark.integration


def event(**kwargs):
    data = dict(
        id=uuid.uuid4(),
        device_id="synthetic-test",
        captured_at=datetime.now(UTC) - timedelta(minutes=10),
        app="test.editor",
        title="蓝鲸验收",
        text="蓝鲸项目发布时间由周五改为周一，需补充验收测试。",
        method="manual",
        project_hint="蓝鲸",
    )
    data.update(kwargs)
    return Event(**data)


async def ready(rt, events=None):
    async with rt.sessions.begin() as session:
        await ingest(session, Batch(events=events or [event()]), rt.settings)
    async with rt.sessions() as session:
        episode = await session.scalar(select(Episode))
    await summarize_job(rt, {"episode_id": str(episode.id), "revision": episode.revision})
    return episode.id


async def flush_memory(rt):
    async with rt.sessions() as session:
        jobs = (await session.scalars(select(Job).where(Job.kind == "memory"))).all()
    for job in jobs:
        await sync_job(rt, job.payload)


async def test_atomic_write_and_repeated_upload(runtime):
    e = event()
    with pytest.raises(RuntimeError):
        async with runtime.sessions.begin() as session:
            await ingest(session, Batch(events=[e]), runtime.settings)
            raise RuntimeError("simulate abort")
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Activity)) == 0
        assert await session.scalar(select(func.count()).select_from(Job)) == 0

    async def upload(value):
        async with runtime.sessions.begin() as session:
            return await ingest(session, Batch(events=[value]), runtime.settings)

    results = await asyncio.gather(
        upload(e), upload(e), upload(e.model_copy(update={"id": uuid.uuid4()}))
    )
    assert all(result["accepted"] for result in results)
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Activity)) == 1
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


async def test_job_claims_lease_recovery_and_stale_completion(runtime):
    async with runtime.sessions.begin() as session:
        await enqueue(session, "summarize", "test1", {})
        await enqueue(session, "summarize", "test2", {})
    one, two = await asyncio.gather(claim(runtime.sessions), claim(runtime.sessions))
    assert one.id != two.id
    async with runtime.sessions.begin() as session:
        await session.execute(
            update(Job).where(Job.id == one.id).values(lease_until=now() - timedelta(seconds=1))
        )
    recovered = await claim(runtime.sessions)
    assert recovered.id == one.id and recovered.lease_token != one.lease_token
    await finish(runtime.sessions, one)
    async with runtime.sessions() as session:
        assert (await session.get(Job, one.id)).status == "running"
    await finish(runtime.sessions, recovered)
    async with runtime.sessions() as session:
        assert (await session.get(Job, one.id)).status == "done"


async def test_switch_correction_delete_and_offline_replay(runtime):
    first = event()
    second = first.model_copy(update={"id": uuid.uuid4(), "text": "蓝鲸项目必须补充接口测试。"})
    episode_id = await ready(runtime, [first, second])
    await flush_memory(runtime)
    assert episode_id in runtime.memory.adapters["reme"].documents
    async with runtime.sessions.begin() as session:
        await switch_backend(session, "graphiti")
    await flush_memory(runtime)
    assert episode_id in runtime.memory.adapters["graphiti"].documents
    async with runtime.sessions.begin() as session:
        episode = await session.get(Episode, episode_id)
        await mutate_episode(session, episode, "用户纠正：蓝鲸项目发布日期改为周二。")
    # Both backends still have old text here; evidence must already use the correction.
    hits, _ = await retrieve(runtime, "蓝鲸")
    assert "周二" in hits[0]["summary"]
    await flush_memory(runtime)
    assert all("周二" in a.documents[episode_id].text for a in runtime.memory.adapters.values())
    async with runtime.sessions.begin() as session:
        episode = await session.get(Episode, episode_id)
        session.add(
            Reminder(
                kind="resume", episode_id=episode_id, text="old", due_at=now(), dedupe_key="old"
            )
        )
        await session.flush()
        await mutate_episode(session, episode)
    hits, _ = await retrieve(runtime, "蓝鲸")
    assert hits == []
    async with runtime.sessions.begin() as session:
        result = await ingest(session, Batch(events=[first, second]), runtime.settings)
        assert len(result["skipped"]) == 2
        assert await session.scalar(select(func.count()).select_from(Todo)) == 0
        assert await session.scalar(select(func.count()).select_from(Reminder)) == 0
        assert all(t is None for t in (await session.scalars(select(Activity.text))).all())
        await switch_backend(session, "reme")
    await flush_memory(runtime)
    assert all(episode_id not in a.documents for a in runtime.memory.adapters.values())
    async with runtime.sessions() as session:
        receipts = (await session.scalars(select(MemoryReceipt))).all()
        assert len(receipts) == 2 and all(r.deleted for r in receipts)


async def test_deletion_during_answer_invalidates_generated_text(runtime):
    episode_id = await ready(runtime)
    await flush_memory(runtime)

    async def delete_during_inference():
        async with runtime.sessions.begin() as session:
            episode = await session.get(Episode, episode_id)
            await mutate_episode(session, episode)

    runtime.ollama.answer_hook = delete_during_inference
    answer = await answer_question(runtime, "蓝鲸什么时候发布")
    assert answer["sources"] == [] and "更新" in answer["answer"]


async def test_retention_and_only_confirmed_todo_reminders(runtime):
    episode_id = await ready(runtime)
    async with runtime.sessions.begin() as session:
        await session.execute(update(Activity).values(captured_at=now() - timedelta(days=35)))
    async with runtime.sessions.begin() as session:
        assert await session.scalar(select(Todo)) is None
        todo = Todo(text="合成明确设置的提醒", confirmed=False, origin="manual",
                    due_at=now() - timedelta(minutes=1), local_time="15:00:00")
        session.add(todo)
    await maintenance(runtime)
    async with runtime.sessions() as session:
        assert await session.scalar(select(Activity.text)) is None
        assert (await session.get(Episode, episode_id)).summary is None
        assert (
            await session.scalar(
                select(func.count()).select_from(Reminder).where(Reminder.kind == "todo")
            )
            == 0
        )
    async with runtime.sessions.begin() as session:
        todo = await session.scalar(select(Todo))
        todo.confirmed = True
    from workmemory.reminders.scheduler import tick
    await tick(runtime)
    await tick(runtime)
    async with runtime.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Reminder).where(Reminder.kind == "todo")
            )
            == 1
        )


async def test_http_auth_revision_conflict_and_transaction(runtime):
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
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/api/timeline")).status_code == 401
            client.headers["Authorization"] = "Bearer " + runtime.settings.api_token
            e = event()
            assert (
                await client.post("/api/ingest", json={"events": [e.model_dump(mode="json")]})
            ).status_code == 200
            data = (await client.get("/api/timeline")).json()
            episode = data["items"][0]
            conflict = await client.patch(
                "/api/episodes/" + episode["id"], json={"summary": "test", "expected_revision": 999}
            )
            assert conflict.status_code == 409
            assert (
                await client.patch(
                    "/api/episodes/" + episode["id"],
                    json={"summary": "蓝鲸最终上线周二", "expected_revision": episode["revision"]},
                )
            ).status_code == 200
            assert (await client.get("/api/status")).json()["backend"] == "reme"
            assert (await client.delete("/api/episodes/" + episode["id"])).status_code == 200
            assert (
                await client.get("/api/episodes/" + episode["id"] + "/sources")
            ).status_code == 404
            assert (await client.get("/api/timeline")).json()["total"] == 0
            assert (
                await client.post(
                    "/api/ingest",
                    json={"events": [e.model_dump(mode="json")]},
                    headers={"Origin": "https://evil.test"},
                )
            ).status_code == 403


async def test_delete_does_not_wait_for_slow_memory_write(runtime):
    episode_id = await ready(runtime)
    entered, release = asyncio.Event(), asyncio.Event()
    adapter = runtime.memory.adapters["reme"]
    original = adapter.upsert

    async def slow_write(document):
        entered.set()
        await release.wait()
        return await original(document)

    adapter.upsert = slow_write
    task = asyncio.create_task(
        sync_job(runtime, {"episode_id": str(episode_id), "backend": "reme"})
    )
    await entered.wait()

    async def delete_now():
        async with runtime.sessions.begin() as session:
            episode = await session.get(Episode, episode_id)
            await mutate_episode(session, episode)

    await asyncio.wait_for(delete_now(), timeout=2)
    release.set()
    await task
    async with runtime.sessions() as session:
        assert await session.get(MemoryReceipt, ("reme", episode_id)) is None
    adapter.upsert = original
    await flush_memory(runtime)
    assert episode_id not in adapter.documents


async def test_expired_unsummarized_text_is_not_retained(runtime):
    e = event()
    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=[e]), runtime.settings)
        await session.execute(update(Activity).values(captured_at=now() - timedelta(days=31)))
    await maintenance(runtime)
    async with runtime.sessions() as session:
        assert await session.scalar(select(Activity.text)) is None
        assert (await session.scalar(select(Episode))).status == "expired"
    async with runtime.sessions.begin() as session:
        result = await ingest(
            session, Batch(events=[event(captured_at=now() - timedelta(days=31))]), runtime.settings
        )
        assert len(result["skipped"]) == 1


async def test_forced_rebuild_restores_external_data_despite_current_receipt(runtime):
    episode_id = await ready(runtime)
    await flush_memory(runtime)
    adapter = runtime.memory.adapters["reme"]
    adapter.documents.clear()
    # A concurrent completion may restore a current receipt during a rebuild.
    async with runtime.sessions.begin() as session:
        episode = await session.get(Episode, episode_id)
        receipt = await session.get(MemoryReceipt, ("reme", episode_id))
        assert receipt.revision == episode.revision
        await schedule_sync(session, episode, force=True)
    await flush_memory(runtime)
    assert episode_id in adapter.documents


async def test_named_project_evidence_survives_many_newer_unrelated_records(runtime):
    episode_id = await ready(runtime)
    await flush_memory(runtime)
    async with runtime.sessions.begin() as session:
        project = Project(name="山海")
        session.add(project)
        await session.flush()
        for i in range(12):
            session.add(
                Episode(
                    id=uuid.uuid4(),
                    device_id=f"other-{i}",
                    started_at=now(),
                    ended_at=now(),
                    project_id=project.id,
                    summary="山海项目已确认发布计划，本周五上线。",
                    evidence_state="user_confirmed", corrected=True,
                    status="ready",
                )
            )
    evidence, _ = await retrieve(runtime, "蓝鲸项目最新发布计划是什么？", limit=8)
    assert [item["id"] for item in evidence] == [str(episode_id)]
    assert "周一" in evidence[0]["summary"]
