import uuid

from sqlalchemy import delete, select, update

from workmemory.artifacts.service import remove_todo_source
from workmemory.config import ROOT
from workmemory.db.models import Activity, Episode, Observation, Reminder, WorkSession
from workmemory.evidence.service import index_episode, invalidate_episode
from workmemory.jobs.queue import now
from workmemory.memory.sync import schedule_sync


async def summarize_job(runtime, payload):
    import json

    from workmemory.db.models import WorkFact, WorkSession
    from workmemory.episodes.excerpts import (
        ExcerptSelection,
        selected_blocks,
        source_blocks,
        summary_text,
    )
    from workmemory.evidence.archive import archive_episode
    from workmemory.evidence.policy import POLICY_VERSION

    episode_id = uuid.UUID(payload["episode_id"])
    async with runtime.sessions() as session:
        episode = await session.get(Episode, episode_id)
        if (not episode or episode.deleted_at or episode.corrected or episode.evidence_state == "user_confirmed" or (episode.status == "expired" and episode.evidence_state != "legacy_unverified")
                or episode.revision != payload["revision"]):
            return
        blocks = await source_blocks(session, episode_id, runtime.settings.retention_days)
    selected = []
    if blocks:
        response = await runtime.ollama.chat(
            [{"role": "system", "content": (ROOT / "config/prompts/summarize.txt").read_text()},
             {"role": "user", "content": json.dumps({"blocks": [b.model_view() for b in blocks]}, ensure_ascii=False)}],
            schema=ExcerptSelection.model_json_schema(), priority=payload.get("priority", 10))
        selected = selected_blocks(ExcerptSelection.model_validate_json(response), blocks)
    async with runtime.sessions.begin() as session:
        episode = await session.scalar(select(Episode).where(Episode.id == episode_id).with_for_update())
        if (not episode or episode.deleted_at or episode.corrected or episode.evidence_state == "user_confirmed" or episode.revision != payload["revision"]):
            return
        current = await source_blocks(session, episode_id, runtime.settings.retention_days)
        available = {b.id: b for b in current}
        if any(b.id not in available or b != available[b.id] for b in selected):
            raise ValueError("Excerpt source changed")
        legacy = episode.evidence_state == "legacy_unverified"
        if legacy:
            await archive_episode(session, episode)
            await invalidate_episode(session, episode.id)
            episode.revision += 1
        from datetime import timedelta
        retained = await session.scalar(select(Activity.id).where(Activity.episode_id == episode_id,
            Activity.text.is_not(None), Activity.captured_at > now() - timedelta(days=runtime.settings.retention_days)).limit(1))
        episode.summary = summary_text(selected) if selected else None
        episode.status = "ready"
        episode.evidence_state = "excerpted" if selected else "no_evidence" if retained else "expired"
        episode.evidence_policy = POLICY_VERSION
        episode.evidence_expires_at = min(b.expires_at for b in selected) if selected else None
        episode.context_revision = episode.evidence_reviewed_revision = episode.revision
        await session.execute(delete(WorkFact).where(WorkFact.episode_id == episode.id))
        for block in selected:
            session.add(WorkFact(episode_id=episode.id, revision=episode.revision, kind="observation",
                text=block.text, state="observed", observed_at=block.captured_at,
                provenance=block.provenance(episode.revision)))
        segment = await session.scalar(select(WorkSession).where(WorkSession.episode_id == episode.id))
        if not segment:
            segment = WorkSession(episode_id=episode.id, revision=episode.revision,
                title="屏幕文字记录" if selected else "原始采集记录",
                started_at=episode.started_at, ended_at=episode.ended_at)
            session.add(segment)
        else:
            segment.revision = episode.revision
            if segment.edit_revision <= 1:
                segment.title = "屏幕文字记录" if selected else "原始采集记录"
        await index_episode(session, episode, runtime.settings.retention_days)
        await schedule_sync(session, episode, all_used=legacy or not selected, force=True)


async def mutate_episode(session, episode, correction=None):
    from workmemory.evidence.archive import clear_archives
    from workmemory.evidence.confirmed import source_changed
    await source_changed(session, episode.id, deleted=correction is None)
    await clear_archives(session, "episode", episode.id)
    await invalidate_episode(session, episode.id)
    await remove_todo_source(session, episode.id)
    episode.revision += 1
    # Remove reminders and suggestions that depended on superseded content.
    await session.execute(delete(Reminder).where(Reminder.episode_id == episode.id))
    if correction is None:
        await session.execute(delete(WorkSession).where(WorkSession.episode_id == episode.id))
        episode.evidence_state = "expired"
        episode.deleted_at = now()
        episode.summary = None
        episode.status = "deleted"
        episode.project_hint = None
        episode.project_id = None
        await session.execute(
            update(Activity)
            .where(Activity.episode_id == episode.id)
            .values(text=None, title="", app="deleted")
        )
    else:
        episode.summary = correction
        episode.status = "ready"
        episode.corrected = True
        episode.evidence_state = "user_confirmed"
        episode.evidence_policy = 1
        episode.evidence_expires_at = None
        # Superseded source text must not leak through a source preview or future re-extraction.
        await session.execute(
            update(Activity).where(Activity.episode_id == episode.id).values(text=None, title="")
        )
    await session.execute(
        update(Observation)
        .where(Observation.episode_id == episode.id)
        .values(delta=None, title="", window_id="", activity_id=None, capture_meta={})
    )
    await index_episode(session, episode)
    await schedule_sync(session, episode, all_used=True)
