import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from workmemory.db.models import (
    Activity,
    AgentEvent,
    AgentRun,
    ChatMessage,
    Conversation,
    Episode,
    EvidenceDependency,
    ExternalSource,
    Job,
    MemoryCandidate,
    Observation,
    Reminder,
    SearchDocument,
)
from workmemory.evidence.service import invalidate_consumers
from workmemory.jobs.queue import now


async def maintenance(runtime):
    local = datetime.now(ZoneInfo(runtime.settings.timezone))
    cutoff = now() - timedelta(days=runtime.settings.retention_days)
    async with runtime.sessions.begin() as session:
        from workmemory.companion.state import cleanup
        await cleanup(session)
        expired_deps = (await session.scalars(select(EvidenceDependency).where(
            EvidenceDependency.expires_at <= now()))).all()
        await invalidate_consumers(session, expired_deps)
        await session.execute(delete(SearchDocument).where(SearchDocument.expires_at <= now()))
        chat_cutoff = now() - timedelta(days=30)
        await session.execute(delete(ExternalSource).where(ExternalSource.expires_at <= now()))
        await session.execute(update(ChatMessage).where(ChatMessage.created_at <= chat_cutoff)
                              .values(content=None, citations=[], external_citations=[], context=None, status="expired"))
        await session.execute(update(MemoryCandidate).where(MemoryCandidate.created_at <= chat_cutoff,
            MemoryCandidate.status == "pending").values(text=None, status="dismissed"))
        await session.execute(update(Conversation).where(Conversation.created_at <= chat_cutoff).values(title="历史会话"))
        await session.execute(update(Conversation).where(Conversation.updated_at <= chat_cutoff)
                              .values(title="", expired=True))
        await session.execute(delete(AgentEvent).where(AgentEvent.created_at <= chat_cutoff))
        await session.execute(delete(EvidenceDependency).where(EvidenceDependency.consumer_kind == "run",
            EvidenceDependency.consumer_id.in_(select(AgentRun.id).where(AgentRun.created_at <= chat_cutoff))))
        # Raw retention is a privacy boundary, including unsummarized backlog.
        await session.execute(
            update(Activity).where(Activity.captured_at < cutoff).values(text=None, title="")
        )
        await session.execute(
            update(Observation).where(
                (Observation.captured_at < cutoff) | Observation.activity_id.in_(
                    select(Activity.id).where(Activity.text.is_(None))
                )
            ).values(delta=None, title="", window_id="", activity_id=None, capture_meta={})
        )
        raw_activity = select(Activity.id).where(
            Activity.id == SearchDocument.source_id, Activity.text.is_not(None), Activity.captured_at >= cutoff).exists()
        raw_observation = select(Observation.id).join(Activity, Observation.activity_id == Activity.id).where(
            Observation.id == SearchDocument.source_id, Activity.text.is_not(None),
            Activity.captured_at >= cutoff, Observation.captured_at >= cutoff).exists()
        removed = list((await session.scalars(delete(SearchDocument).where(SearchDocument.kind == "raw",
            ~raw_activity, ~raw_observation).returning(SearchDocument.id))).all())
        if removed:
            invalid = (await session.scalars(select(EvidenceDependency).where(EvidenceDependency.snippet_id.in_(removed)))).all()
            await invalidate_consumers(session, invalid)
        from workmemory.evidence.retention import expire_excerpts
        await expire_excerpts(session, runtime.settings.retention_days, runtime.settings.episode_minutes)
        has_text = (
            select(Activity.id)
            .where(Activity.episode_id == Episode.id, Activity.text.is_not(None))
            .exists()
        )
        await session.execute(
            update(Episode)
            .where(Episode.status == "pending", ~has_text)
            .values(status="expired", evidence_state="expired", project_hint=None, summary=None)
        )
        await session.execute(
            delete(Job).where(Job.status == "done", Job.finished_at < now() - timedelta(days=7))
        )
        if local.hour >= 18:
            start = datetime.combine(local.date(), time.min, ZoneInfo(runtime.settings.timezone))
            exists = await session.scalar(
                select(Episode.id)
                .where(
                    Episode.started_at >= start,
                    Episode.deleted_at.is_(None),
                    Episode.status == "ready",
                )
                .limit(1)
            )
            if exists:
                await session.execute(
                    insert(Reminder)
                    .values(
                        id=uuid.uuid4(),
                        kind="daily",
                        text="今天的工作片段已整理，可以打开每日回顾。",
                        due_at=now(),
                        dedupe_key=f"daily:{local.date()}",
                    )
                    .on_conflict_do_nothing()
                )
