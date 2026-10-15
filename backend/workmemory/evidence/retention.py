"""Automatic excerpts inherit raw expiry; confirmed independent memories do not."""
from datetime import timedelta

from sqlalchemy import or_, select, update

from workmemory.db.models import Activity, Episode, EvidenceArchive, WorkSession
from workmemory.jobs.queue import now


async def expire_excerpts(session, retention_days=30, episode_minutes=5):
    from workmemory.evidence.policy import valid_excerpt_sources
    from workmemory.evidence.service import invalidate_episode
    from workmemory.memory.sync import schedule_sync
    has_text = select(Activity.id).where(Activity.episode_id == Episode.id, Activity.text.is_not(None)).exists()
    cutoff = now() - timedelta(days=retention_days) + timedelta(minutes=episode_minutes)
    rows = (await session.scalars(select(Episode).where(Episode.evidence_state == "excerpted",
        or_(Episode.evidence_expires_at <= now(), Episode.started_at <= cutoff, ~has_text))
        .with_for_update())).all()
    for episode in rows:
        if await valid_excerpt_sources(session, episode, retention_days):
            continue
        await invalidate_episode(session, episode.id)
        episode.revision += 1
        episode.summary, episode.evidence_state = None, "expired"
        segment = await session.scalar(select(WorkSession).where(WorkSession.episode_id == episode.id))
        if segment:
            segment.revision = episode.revision
            if segment.edit_revision <= 1:
                segment.title = "原文已到期"
        await schedule_sync(session, episode, all_used=True, force=True)
    # Any partial expired fact is excluded too (e.g. an older delta baseline).
    await session.execute(update(EvidenceArchive).where(EvidenceArchive.expires_at <= now()).values(payload=None))
