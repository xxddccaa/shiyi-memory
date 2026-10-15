"""Conservative extraction, lifecycle and human-only history on real PostgreSQL."""
import asyncio
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update
from test_pipeline import event, flush_memory, ready
from workmemory.artifacts.service import generate_artifact, request_artifact
from workmemory.companion.backfill import generate as review
from workmemory.db.models import (
    Activity,
    AppState,
    ChatMessage,
    Conversation,
    DerivedArtifact,
    Episode,
    Job,
    PiSession,
    Project,
    Reminder,
    WorkFact,
    WorkSession,
)
from workmemory.episodes.excerpts import ExcerptSelection, selected_blocks, source_blocks
from workmemory.episodes.service import mutate_episode, summarize_job
from workmemory.evidence.archive import history, initialize_policy
from workmemory.evidence.policy import eligible, valid_excerpt_sources
from workmemory.evidence.service import remember, resolve, search
from workmemory.ingestion.service import Batch, ingest
from workmemory.jobs.queue import now
from workmemory.main import create_app
from workmemory.memory.base import MemoryDocument
from workmemory.memory.sync import sync_job
from workmemory.reminders.service import maintenance

pytestmark = pytest.mark.integration


async def pending(runtime, text, **changes):
    value = event(text=text, method="paddleocr", project_hint=None, **changes)
    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=[value]), runtime.settings)
        episode = await session.scalar(select(Episode))
        return episode.id, episode.revision


@pytest.mark.parametrize("text", [
    "小王：我还没有部署成功。\n我：好的，明天再讨论。",
    "如果验收通过，计划周五发布；现在尚未确认。",
    "示例文档：部署成功。\n这是教程，不是真实运行结果。",
    "草稿：我已经完成项目。\n待核对，请勿发出。",
])
async def test_quotes_never_drop_context_or_invent_project(runtime, text):
    identifier, revision = await pending(runtime, text)
    await summarize_job(runtime, {"episode_id": str(identifier), "revision": revision})
    async with runtime.sessions() as session:
        episode = await session.get(Episode, identifier)
        facts = (await session.scalars(select(WorkFact))).all()
        assert len(facts) == 1 and facts[0].text == text
        assert facts[0].kind == "observation" and facts[0].state == "observed"
        assert "屏幕识别文字" in episode.summary and text in episode.summary
        assert episode.project_id is None
        assert await session.scalar(select(func.count()).select_from(Project)) == 0
        assert await valid_excerpt_sources(session, episode)
        item = await resolve(session, f"fact:{facts[0].id}")
        assert item["evidence_kind"] == "screen_text" and item["expires_at"]


async def test_empty_selection_finishes_without_memory_or_retry(runtime):
    identifier, revision = await pending(runtime, "??? 残缺内容")
    async def empty(*args, **kwargs):
        return json.dumps({"selected_block_ids": []})
    runtime.ollama.chat = empty
    await summarize_job(runtime, {"episode_id": str(identifier), "revision": revision})
    await flush_memory(runtime)
    async with runtime.sessions() as session:
        e = await session.get(Episode, identifier)
        assert e.status == "ready" and e.evidence_state == "no_evidence" and e.summary is None
        assert not await session.scalar(select(WorkFact))
        assert not eligible(e)
        assert await resolve(session, f"summary:{identifier}") is None
    assert not runtime.memory.adapters["reme"].documents


async def test_unknown_block_cannot_write_text(runtime):
    identifier, revision = await pending(runtime, "完整原文")
    async def unknown(*args, **kwargs):
        return json.dumps({"selected_block_ids": ["made-up"]})
    runtime.ollama.chat = unknown
    with pytest.raises(ValueError, match="Unknown excerpt"):
        await summarize_job(runtime, {"episode_id": str(identifier), "revision": revision})
    async with runtime.sessions() as session:
        assert (await session.get(Episode, identifier)).summary is None
        assert await session.scalar(select(WorkFact)) is None


async def test_complete_source_validation_and_context_blocks(runtime):
    text = "甲：尚未批准\n\n" + "需要进一步核对，" * 110 + "\n\n乙：这是草稿，不是最终决定。"
    identifier, revision = await pending(runtime, text)
    async with runtime.sessions() as session:
        blocks = await source_blocks(session, identifier, 30)
        chosen = selected_blocks(ExcerptSelection(selected_block_ids=[blocks[0].id]), blocks)
        assert chosen[0].text == text[chosen[0].start:chosen[0].end]
        assert "尚未批准" in chosen[0].text
    await summarize_job(runtime, {"episode_id": str(identifier), "revision": revision})
    async with runtime.sessions.begin() as session:
        fact = await session.scalar(select(WorkFact))
        fact.text = "已批准"
    async with runtime.sessions() as session:
        assert await resolve(session, f"summary:{identifier}") is None


async def test_expiry_cleans_excerpts_reports_and_all_backends_preserves_manual(runtime):
    identifier = await ready(runtime)
    await flush_memory(runtime)
    runtime.memory.adapters["graphiti"].documents[identifier] = MemoryDocument(identifier, 1, "old", now(), None)
    from workmemory.db.models import MemoryBackend
    async with runtime.sessions.begin() as session:
        session.add(MemoryBackend(name="graphiti"))
        segment = await session.scalar(select(WorkSession))
        segment.title, segment.edit_revision = "人工核对标题", 2
    artifact = await request_artifact(runtime, "review", now().date().isoformat())
    async with runtime.sessions() as session:
        job = await session.scalar(select(Job).where(Job.kind == "artifact"))
    if job:
        await generate_artifact(runtime, job.payload)
    async with runtime.sessions.begin() as session:
        await session.execute(update(Activity).values(captured_at=now() - timedelta(days=35)))
    await maintenance(runtime)
    await flush_memory(runtime)
    async with runtime.sessions() as session:
        e = await session.get(Episode, identifier)
        assert e.evidence_state == "expired" and e.summary is None
        assert not await session.scalar(select(WorkFact))
        assert (await session.scalar(select(WorkSession))).title == "人工核对标题"
        report = await session.get(DerivedArtifact, uuid.UUID(artifact["id"]))
        if job:
            assert report.body is None
    assert all(identifier not in a.documents for a in runtime.memory.adapters.values())


async def test_reports_use_exact_quotes_without_generation(runtime):
    identifier = await ready(runtime, [event(text="某同事：计划下周上线，尚未验收。")])
    async def forbidden(*args, **kwargs):
        raise AssertionError("Screen excerpts must not be paraphrased by report generation")
    runtime.ollama.chat = forbidden
    async with runtime.sessions() as session:
        episode = await session.get(Episode, identifier)
        day = episode.started_at.astimezone().date().isoformat()
    result = await request_artifact(runtime, "review", day)
    async with runtime.sessions() as session:
        job = await session.scalar(select(Job).where(Job.kind == "artifact"))
    await generate_artifact(runtime, job.payload)
    async with runtime.sessions() as session:
        report = await session.get(DerivedArtifact, uuid.UUID(result["id"]))
        assert report.status == "ready"
        assert report.body["observations"][0]["text"] == "某同事：计划下周上线，尚未验收。"
        assert not report.body["progress"] and not report.body["next_steps"]


async def test_migration_quarantine_preserves_display_but_clears_context(runtime):
    identifier = await ready(runtime)
    async with runtime.sessions.begin() as session:
        e = await session.get(Episode, identifier)
        cid = uuid.uuid4()
        session.add(Conversation(id=cid, title="历史测试"))
        await session.flush()
        message = ChatMessage(id=uuid.uuid4(), conversation_id=cid, role="assistant", content="旧自动回答", status="ready")
        session.add(message)
        session.add(PiSession(id=cid, data={"entries": ["旧内容"]}, context={}, status="ready", epoch=0))
        await session.flush()
        source = await resolve(session, f"summary:{identifier}")
        await remember(session, "message", message.id, [source])
        await remember(session, "pi_session", cid, [source])
        e.evidence_state, e.evidence_policy, e.summary = "legacy_unverified", 0, "旧摘要中的不可靠推断"
        e.evidence_reviewed_revision = 0
        session.add(AppState(key="evidence_policy", value={"initialized": False}))
        mid = message.id
    await initialize_policy(runtime)
    await initialize_policy(runtime)
    async with runtime.sessions() as session:
        assert (await session.get(ChatMessage, mid)).content is None
        assert (await session.get(PiSession, cid)).data is None
        assert await resolve(session, f"summary:{identifier}") is None
        archived = await history(session)
        assert len(archived["items"]) == 2
        assert any(r["payload"].get("summary") == "旧摘要中的不可靠推断" for r in archived["items"])
    hits = await search(runtime, "不可靠推断", include_raw=False)
    assert hits["items"] == []
    await review(runtime, {"episode_id": str(identifier), "revision": 2})
    async with runtime.sessions.begin() as session:
        e = await session.get(Episode, identifier)
        assert e.evidence_state == "excerpted"
        assert "不可靠推断" not in e.summary
        assert len((await history(session))["items"]) == 2
        await mutate_episode(session, e)
    async with runtime.sessions() as session:
        assert not (await history(session))["items"]


async def test_missing_original_is_not_regenerated_from_legacy(runtime):
    identifier = await ready(runtime)
    async with runtime.sessions.begin() as session:
        e = await session.get(Episode, identifier)
        e.evidence_state, e.evidence_policy, e.evidence_reviewed_revision = "legacy_unverified", 0, 0
        await session.execute(update(Activity).values(text=None))
        revision = e.revision
    async def forbidden(*args, **kwargs):
        raise AssertionError("Cannot reconstruct originals from old summary")
    runtime.ollama.chat = forbidden
    await review(runtime, {"episode_id": str(identifier), "revision": revision})
    async with runtime.sessions() as session:
        e = await session.get(Episode, identifier)
        assert e.summary and e.evidence_state == "legacy_unverified"
        assert e.evidence_reviewed_revision == revision


async def test_inflight_extraction_cannot_restore_corrected_source(runtime):
    identifier, revision = await pending(runtime, "待核对原文")
    original = runtime.ollama.chat
    async def correcting(*args, **kwargs):
        async with runtime.sessions.begin() as session:
            e = await session.get(Episode, identifier)
            await mutate_episode(session, e, "用户明确纠正")
        return await original(*args, **kwargs)
    runtime.ollama.chat = correcting
    await summarize_job(runtime, {"episode_id": str(identifier), "revision": revision})
    async with runtime.sessions() as session:
        e = await session.get(Episode, identifier)
        assert e.summary == "用户明确纠正" and e.evidence_state == "user_confirmed"
        assert await session.scalar(select(WorkFact)) is None


async def test_inflight_memory_write_cannot_acknowledge_expired_record(runtime):
    identifier = await ready(runtime)
    adapter = runtime.memory.adapters["reme"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = adapter.upsert
    async def blocked(doc):
        entered.set()
        await release.wait()
        return await original(doc)
    adapter.upsert = blocked
    task = asyncio.create_task(sync_job(runtime, {"episode_id": str(identifier), "backend": "reme"}))
    await entered.wait()
    async with runtime.sessions.begin() as session:
        e = await session.get(Episode, identifier)
        e.evidence_expires_at = now() - timedelta(seconds=1)
    await maintenance(runtime)
    release.set()
    await task
    await flush_memory(runtime)
    assert identifier not in adapter.documents


async def test_history_requires_auth_and_sources_expose_provenance(runtime):
    identifier = await ready(runtime)
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama,
        memory=runtime.memory, run_worker=False, mcp_store=runtime.mcp.store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/api/history/legacy")).status_code == 401
            client.headers["Authorization"] = "Bearer " + runtime.settings.api_token
            source = (await client.get(f"/api/episodes/{identifier}/sources")).json()
            assert source["excerpts"][0]["provenance"]["source_id"].startswith("raw:")
            assert source["episode"]["evidence_state"] == "excerpted"
            assert (await client.get("/api/history/legacy")).status_code == 200


async def test_category_only_edit_never_confirms_old_automatic_title(runtime):
    from workmemory.workspace.operations import ProposalInput, decide, propose
    from workmemory.workspace.service import get_object
    await ready(runtime)
    async with runtime.sessions.begin() as session:
        segment = await session.scalar(select(WorkSession))
        segment.title, segment.edit_revision = "旧自动声称：已经完成发布", 2
        sid = segment.id
        conversation = Conversation(title="合成确认测试")
        session.add(conversation)
        await session.flush()
        cid = conversation.id
        item = await get_object(session, "session", sid)
        assert "已经完成发布" not in json.dumps(item, ensure_ascii=False)
        operation = await propose(session, cid, ProposalInput(action="session.update", target_id=sid, category="阅读"))
    await decide(runtime, uuid.UUID(operation["id"]), operation["revision"], True)
    async with runtime.sessions.begin() as session:
        segment = await session.get(WorkSession, sid)
        assert segment.title == "旧自动声称：已经完成发布" and not segment.title_confirmed
        assert "已经完成发布" not in (await resolve(session, f"session:{sid}"))["content"]
        operation = await propose(session, cid, ProposalInput(action="session.update", target_id=sid, text="人工确认的标题"))
    await decide(runtime, uuid.UUID(operation["id"]), operation["revision"], True)
    async with runtime.sessions() as session:
        assert (await session.get(WorkSession, sid)).title_confirmed
        assert (await get_object(session, "session", sid))["text"] == "人工确认的标题"


async def test_legacy_resume_prompts_are_isolated(runtime):
    identifier = await ready(runtime)
    async with runtime.sessions.begin() as session:
        episode = await session.get(Episode, identifier)
        episode.evidence_state = "legacy_unverified"
        reminder = Reminder(kind="resume", episode_id=identifier, text="旧自动结论", due_at=now(), dedupe_key="synthetic-legacy")
        session.add(reminder)
        session.add(AppState(key="evidence_policy", value={"initialized": False}))
        await session.flush()
        rid = reminder.id
    await initialize_policy(runtime)
    async with runtime.sessions() as session:
        reminder = await session.get(Reminder, rid)
        assert reminder.dismissed and "旧自动结论" not in reminder.text
        assert any(r["payload"].get("content") == "旧自动结论" for r in (await history(session))["items"])


async def test_unselectable_retained_original_is_not_labeled_expired(runtime):
    identifier, revision = await pending(runtime, "合成的长段原文" * 600)
    await summarize_job(runtime, {"episode_id": str(identifier), "revision": revision})
    async with runtime.sessions() as session:
        episode = await session.get(Episode, identifier)
        assert episode.evidence_state == "no_evidence" and episode.summary is None


async def test_inactive_backend_cleanup_cannot_starve_original_review(runtime):
    from workmemory.jobs.queue import claim, enqueue, finish
    async with runtime.sessions.begin() as session:
        await enqueue(session, "memory", "inactive-cleanup", {"backend": "graphiti"}, priority=2)
        await enqueue(session, "context", "review-originals", {}, priority=30)
        await enqueue(session, "memory", "current-cleanup", {"backend": "reme"}, priority=2)
    first = await claim(runtime.sessions)
    assert first.dedupe_key == "current-cleanup"
    await finish(runtime.sessions, first)
    second = await claim(runtime.sessions)
    assert second.dedupe_key == "review-originals"
    await finish(runtime.sessions, second)
    async with runtime.sessions.begin() as session:
        state = await session.get(AppState, "memory")
        state.value = {"backend": "graphiti"}
        await enqueue(session, "context", "next-review", {}, priority=30)
    assert (await claim(runtime.sessions)).dedupe_key == "inactive-cleanup"
