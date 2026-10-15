import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from workmemory.db.models import Activity, Episode, Job, Observation
from workmemory.episodes.service import mutate_episode
from workmemory.ingestion.dedup import restore
from workmemory.ingestion.service import Batch, Event, ingest
from workmemory.jobs.queue import claim, finish
from workmemory.reminders.service import maintenance

pytestmark = pytest.mark.integration


def event(text, at=None, **fields):
    return Event(
        id=uuid.uuid4(),
        device_id="interactions",
        captured_at=at or datetime.now(UTC) - timedelta(minutes=10),
        app="editor",
        window_id="123",
        title="项目",
        method="paddleocr",
        text=text,
        **fields,
    )


async def test_dedup_reconstruction_critical_change_and_revert(runtime):
    start = (datetime.now(UTC) - timedelta(minutes=10)).replace(second=0, microsecond=0)
    baseline = "固定页面正文。" * 100 + "计划周五上线。末尾文字"
    values = [baseline, baseline[:-1] + "宇", baseline.replace("周五", "周一"), baseline]
    events = [event(text, start + timedelta(seconds=i)) for i, text in enumerate(values)]
    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=events), runtime.settings)
        await ingest(session, Batch(events=list(reversed(events))), runtime.settings)
    async with runtime.sessions() as session:
        records = (
            await session.scalars(select(Observation).order_by(Observation.captured_at))
        ).all()
        assert len(records) == 4
        assert [o.meaningful for o in records] == [True, False, True, True]
        assert records[1].delta and records[1].similarity >= 0.95
        assert len((await session.scalars(select(Activity))).all()) == 2
        for observation, expected in zip(records, values, strict=True):
            full = await session.get(Activity, observation.activity_id)
            assert restore(full.text, observation.delta) == expected
        assert await session.scalar(select(func.count()).select_from(Job)) == 1


async def test_latest_revision_survives_running_job_completion(runtime):
    one = event("项目周五上线")
    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=[one]), runtime.settings)
    running = await claim(runtime.sessions)
    async with runtime.sessions.begin() as session:
        await ingest(
            session,
            Batch(events=[one.model_copy(update={"id": uuid.uuid4(), "text": "项目周一上线"})]),
            runtime.settings,
        )
    await finish(runtime.sessions, running)
    async with runtime.sessions() as session:
        job = await session.get(Job, running.id)
        assert job.status == "pending"
        assert job.payload["revision"] > running.payload["revision"]
    latest = await claim(runtime.sessions)
    await finish(runtime.sessions, running)  # old lease cannot finish the new revision
    async with runtime.sessions() as session:
        assert (await session.get(Job, latest.id)).status == "running"
    await finish(runtime.sessions, latest)


async def test_patches_scrubbed_by_correction_and_retention(runtime):
    text = "固定页面正文。" * 100 + "这里有个人文字"
    first = event(text)
    async with runtime.sessions.begin() as session:
        await ingest(
            session,
            Batch(
                events=[
                    first,
                    first.model_copy(update={"id": uuid.uuid4(), "text": text[:-1] + "宇"}),
                ]
            ),
            runtime.settings,
        )
        episode = await session.scalar(select(Episode))
        await mutate_episode(session, episode, "用户纠正")
    async with runtime.sessions() as session:
        assert all(
            o.delta is None and o.activity_id is None and not o.title
            for o in (await session.scalars(select(Observation))).all()
        )
    other = event(text, datetime.now(UTC) - timedelta(days=5))
    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=[other]), runtime.settings)
    runtime.settings.retention_days = 1
    await maintenance(runtime)
    async with runtime.sessions() as session:
        assert (await session.get(Observation, other.id)).activity_id is None


async def test_different_windows_and_cumulative_edits_not_filtered(runtime):
    start = (datetime.now(UTC) - timedelta(minutes=10)).replace(second=0, microsecond=0)
    base = "abcdefghij" * 50
    events = [event(base, start)]
    for i in range(1, 5):
        events.append(event("X" * (i * 10) + base[i * 10 :], start + timedelta(seconds=i)))
    events.append(events[-1].model_copy(update={"id": uuid.uuid4(), "window_id": "other"}))
    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=events), runtime.settings)
    async with runtime.sessions() as session:
        observations = (
            await session.scalars(select(Observation).order_by(Observation.captured_at))
        ).all()
        assert any(o.meaningful for o in observations[2:5])
        assert any(o.window_id == "other" for o in observations)


async def test_timer_quiet_overnight_does_not_schedule_more_llm_jobs(runtime):
    # Simulated overnight clock: uploads span 8 hours, including five-second samples
    # on either side of every bucket boundary. Each request gets a fresh DB session.
    start = (datetime.now(UTC) - timedelta(hours=12)).replace(minute=0, second=0, microsecond=0)
    base = "固定文档正文。" * 100 + "计划周五上线，金额1000元，待处理。末尾文字"
    first = event(base, start, trigger="timer", phase="snapshot")
    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=[first]), runtime.settings)
    for minutes in range(5, 8 * 60 + 1, 5):
        duplicate = event(
            base, start + timedelta(minutes=minutes), trigger="timer", phase="snapshot"
        )
        noise = duplicate.model_copy(
            update={
                "id": uuid.uuid4(),
                "text": base[:-1] + "宇",
                "captured_at": duplicate.captured_at + timedelta(seconds=5),
            }
        )
        async with runtime.sessions.begin() as session:
            result = await ingest(session, Batch(events=[noise, duplicate]), runtime.settings)
            assert set(result["skipped"]) == {str(duplicate.id), str(noise.id)}
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Episode)) == 1
        assert await session.scalar(select(func.count()).select_from(Observation)) == 1
        assert await session.scalar(select(func.count()).select_from(Job)) == 1
        assert (await session.scalar(select(Episode))).revision == 2
    # A small but critical change after the quiet period must still reach the LLM.
    changed = event(
        base.replace("周五", "周一"), start + timedelta(hours=9), trigger="timer", phase="snapshot"
    )
    async with runtime.sessions.begin() as session:
        result = await ingest(session, Batch(events=[changed]), runtime.settings)
        assert result["accepted"] == [str(changed.id)]
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 2


async def test_timer_cross_bucket_concurrent_upload_and_cumulative_change(runtime):
    start = (datetime.now(UTC) - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    base = "abcdefghij" * 50
    first = event(base, start, trigger="timer", phase="snapshot")
    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=[first]), runtime.settings)

    async def send(value):
        async with runtime.sessions.begin() as session:
            return await ingest(session, Batch(events=[value]), runtime.settings)

    copies = [
        event(base, start + timedelta(minutes=5 + i), trigger="timer", phase="snapshot")
        for i in range(6)
    ]
    results = await asyncio.gather(*(send(value) for value in copies))
    assert all(result["skipped"] for result in results)
    changes = [
        event(
            "X" * size + base[size:],
            start + timedelta(minutes=30 + i * 5),
            trigger="timer",
            phase="snapshot",
        )
        for i, size in enumerate([10, 20, 30])
    ]
    # Sort by time rather than UUID/bucket so cumulative changes use the correct baseline.
    async with runtime.sessions.begin() as session:
        result = await ingest(session, Batch(events=list(reversed(changes))), runtime.settings)
        assert result["skipped"] == [str(value.id) for value in changes[:2]]
        assert result["accepted"] == [str(changes[-1].id)]
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Job)) == 2


async def test_timer_dedup_is_scoped_and_retention_aware(runtime):
    start = (datetime.now(UTC) - timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    first = event("测试项目，计划周一上线", start, trigger="timer", phase="snapshot")
    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=[first]), runtime.settings)
        for fields in [
            {"device_id": "another"},
            {"window_id": "456"},
            {"project_hint": "另一个项目"},
        ]:
            value = first.model_copy(
                update={"id": uuid.uuid4(), "captured_at": start + timedelta(minutes=5), **fields}
            )
            assert (await ingest(session, Batch(events=[value]), runtime.settings))["accepted"]
    runtime.settings.retention_days = 1
    current = first.model_copy(update={"id": uuid.uuid4(), "captured_at": datetime.now(UTC)})
    async with runtime.sessions.begin() as session:
        assert (await ingest(session, Batch(events=[current]), runtime.settings))["accepted"]
