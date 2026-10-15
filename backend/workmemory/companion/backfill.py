"""Pauseable legacy review from retained originals, never from old summaries."""
import uuid

from sqlalchemy import String, func, select

from workmemory.companion.state import preferences
from workmemory.db.models import Episode, Job
from workmemory.jobs.queue import enqueue


async def schedule(runtime):
    async with runtime.sessions.begin() as session:
        if not (await preferences(session))["backfill_enabled"]:
            return
        scheduled = select(Job.id).where(Job.dedupe_key == "evidence-v1:" + func.cast(Episode.id, String)
            + ":" + func.cast(Episode.revision, String)).exists()
        rows = (await session.scalars(select(Episode).where(
            ~scheduled,
            Episode.evidence_state == "legacy_unverified", Episode.deleted_at.is_(None),
            Episode.evidence_reviewed_revision != Episode.revision)
            .order_by(Episode.started_at.desc(), Episode.id).limit(10))).all()
        for episode in rows:
            await enqueue(session, "context", f"evidence-v1:{episode.id}:{episode.revision}",
                {"episode_id": str(episode.id), "revision": episode.revision}, priority=30)


async def generate(runtime, payload):
    from workmemory.episodes.excerpts import source_blocks
    from workmemory.episodes.service import summarize_job
    identifier = uuid.UUID(payload["episode_id"])
    async with runtime.sessions.begin() as session:
        episode = await session.get(Episode, identifier, with_for_update=True)
        if (not episode or episode.deleted_at or episode.corrected
                or episode.revision != payload["revision"]
                or episode.evidence_reviewed_revision == episode.revision):
            return
        blocks = await source_blocks(session, identifier, runtime.settings.retention_days)
        if not blocks:
            # Retain the legacy body in the human-only archive; do not retry missing originals.
            episode.evidence_reviewed_revision = episode.revision
            return
    await summarize_job(runtime, {**payload, "priority": 30})


async def status(runtime):
    async with runtime.sessions() as session:
        states = dict((await session.execute(select(Job.status, func.count())
            .where(Job.kind == "context").group_by(Job.status))).all())
        counts = dict((await session.execute(select(Episode.evidence_state, func.count())
            .where(Episode.deleted_at.is_(None)).group_by(Episode.evidence_state))).all())
        return {"enabled": (await preferences(session))["backfill_enabled"], "jobs": states,
                "evidence": counts,
                "notice": "仅从尚存原文核对旧记录；缺失原文的旧摘要保留为隔离历史，不交给模型。"}
