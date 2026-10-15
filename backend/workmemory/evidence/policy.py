"""Eligibility is distinct from extraction success and from real-world truth."""
from sqlalchemy import and_, or_

from workmemory.db.models import Episode
from workmemory.jobs.queue import now

POLICY_VERSION = 1
LABELS = {
    "pending": "等待原句提炼", "excerpted": "已核对原文 · 屏幕文字",
    "no_evidence": "无可提炼内容", "legacy_unverified": "历史待复核",
    "user_confirmed": "用户确认", "expired": "原文已到期",
}


def eligible(episode):
    if not episode or episode.deleted_at or episode.status != "ready":
        return False
    if episode.evidence_state == "user_confirmed":
        return True
    return bool(episode.evidence_policy == POLICY_VERSION
        and episode.evidence_state == "excerpted"
        and episode.evidence_expires_at and episode.evidence_expires_at > now())


def eligible_clause():
    return and_(Episode.deleted_at.is_(None), Episode.status == "ready", or_(
        Episode.evidence_state == "user_confirmed",
        and_(Episode.evidence_policy == POLICY_VERSION, Episode.evidence_state == "excerpted",
             Episode.evidence_expires_at > now())))


def metadata(episode):
    state = episode.evidence_state
    if state == "excerpted" and (not episode.evidence_expires_at or episode.evidence_expires_at <= now()):
        state = "expired"
    label = "原文缺失或无可核对片段" if state == "legacy_unverified" and episode.evidence_reviewed_revision == episode.revision else LABELS.get(state, state)
    return {"evidence_state": state, "evidence_label": label,
            "evidence_kind": "user_confirmed" if state == "user_confirmed" else "screen_text",
            "evidence_policy": episode.evidence_policy,
            "evidence_expires_at": episode.evidence_expires_at.isoformat() if episode.evidence_expires_at else None}


async def valid_excerpt_sources(session, episode, retention_days=30):
    """Validate against complete restored originals, never a search preview."""
    import hashlib
    import uuid
    from datetime import timedelta

    from sqlalchemy import select

    from workmemory.db.models import Activity, Observation, WorkFact
    from workmemory.ingestion.dedup import restore

    if not eligible(episode):
        return False
    if episode.evidence_state == "user_confirmed":
        return True
    facts = (await session.scalars(select(WorkFact).where(
        WorkFact.episode_id == episode.id, WorkFact.revision == episode.revision))).all()
    if not facts:
        return False
    for fact in facts:
        p = fact.provenance
        try:
            kind, identifier = p["source_id"].split(":", 1)
            if kind != "raw" or p["revision"] != episode.revision or p["policy"] != POLICY_VERSION:
                return False
            identifier = uuid.UUID(identifier)
            obs = await session.get(Observation, identifier)
            activity = await session.get(Activity, obs.activity_id) if obs and obs.activity_id else None
            if obs is None:
                activity = await session.get(Activity, identifier)
            if not activity or not activity.text or activity.episode_id != episode.id:
                return False
            at = min(obs.captured_at, activity.captured_at) if obs else activity.captured_at
            text = restore(activity.text, obs.delta) if obs else activity.text
            if (at + timedelta(days=retention_days) <= now()
                    or hashlib.sha256(text.encode()).hexdigest() != p["source_hash"]
                    or not 0 <= p["start"] < p["end"] <= len(text)
                    or text[p["start"]:p["end"]] != fact.text):
                return False
        except (ValueError, KeyError, TypeError):
            return False
    return True
