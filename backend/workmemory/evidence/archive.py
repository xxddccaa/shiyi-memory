"""Legacy display data is archived separately from every model-facing interface."""
import json
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert

from workmemory.db.models import (
    AppState,
    ChatMessage,
    DerivedArtifact,
    Episode,
    EvidenceArchive,
    EvidenceDependency,
    ProactiveUtterance,
    Reminder,
    WorkFact,
    WorkSession,
)
from workmemory.jobs.queue import now


def json_value(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


async def snapshot(session, kind, identifier, revision, payload, sources, expires_at=None):
    await session.execute(insert(EvidenceArchive).values(id=uuid.uuid4(), object_kind=kind,
        object_id=identifier, revision=revision, payload=json_value(payload), sources=json_value(sources),
        expires_at=expires_at).on_conflict_do_nothing(index_elements=["object_kind", "object_id", "revision"]))


async def archive_episode(session, episode):
    facts = (await session.scalars(select(WorkFact).where(WorkFact.episode_id == episode.id))).all()
    segment = await session.scalar(select(WorkSession).where(WorkSession.episode_id == episode.id))
    await snapshot(session, "episode", episode.id, episode.revision,
        {"summary": episode.summary, "started_at": episode.started_at,
         "facts": [{"text": f.text, "kind": f.kind, "state": f.state} for f in facts],
         "work_session": {"title": segment.title, "category": segment.category} if segment else None},
        [{"source_kind": "episode", "source_id": str(episode.id)}])


async def clear_archives(session, kind, identifier):
    # Explicit deletion/correction applies to historical copies as well, regardless of version.
    await session.execute(update(EvidenceArchive).where(or_(
        EvidenceArchive.sources.contains([{"source_kind": kind, "source_id": str(identifier)}]),
        (EvidenceArchive.object_kind == kind) & (EvidenceArchive.object_id == identifier)))
        .values(payload=None))


async def initialize_policy(runtime):
    """Run before serving requests. Reentrant, one transaction, no model or external I/O."""
    from workmemory.companion.state import save_preferences
    from workmemory.evidence.service import invalidate_consumers
    from workmemory.memory.sync import schedule_sync

    async with runtime.sessions.begin() as session:
        marker = await session.get(AppState, "evidence_policy", with_for_update=True)
        if not marker or marker.value.get("initialized"):
            return
        episodes = (await session.scalars(select(Episode).where(
            Episode.evidence_state == "legacy_unverified"))).all()
        ids = [e.id for e in episodes]
        for episode in episodes:
            await archive_episode(session, episode)
        deps = (await session.scalars(select(EvidenceDependency).where(
            EvidenceDependency.source_kind == "episode", EvidenceDependency.source_id.in_(ids)))).all()
        groups = {}
        for dep in deps:
            groups.setdefault(dep.consumer_kind, set()).add(dep.consumer_id)
        # Snapshot only visible bodies. Raw Pi/tool contexts are deliberately not archived.
        for kind, model, fields in [
            ("message", ChatMessage, ("content", "conversation_id", "created_at")),
            ("artifact", DerivedArtifact, ("body", "kind", "scope")),
            ("proactive", ProactiveUtterance, ("content", "created_at")),
        ]:
            for identifier in groups.get(kind, set()):
                row = await session.get(model, identifier)
                if not row:
                    continue
                all_deps = (await session.scalars(select(EvidenceDependency).where(
                    EvidenceDependency.consumer_kind == kind, EvidenceDependency.consumer_id == identifier))).all()
                expiries = [d.expires_at for d in all_deps if d.expires_at]
                if kind == "message":
                    from datetime import timedelta
                    expiries.append(row.created_at + timedelta(days=30))
                elif kind == "proactive":
                    expiries.append(row.expires_at)
                refs = [{"source_kind": d.source_kind, "source_id": str(d.source_id)} for d in all_deps]
                if kind == "message":
                    refs += [{"source_kind": "chat", "source_id": str(identifier)},
                             {"source_kind": "conversation", "source_id": str(row.conversation_id)}]
                await snapshot(session, kind, identifier, getattr(row, "edit_revision", 1),
                    {field: getattr(row, field) for field in fields}, refs, min(expiries) if expiries else None)
        # Older resume prompts embedded a freeform automatic summary directly.
        for reminder in (await session.scalars(select(Reminder).where(
            Reminder.kind == "resume", Reminder.episode_id.in_(ids)))).all():
            await snapshot(session, "reminder", reminder.id, reminder.revision,
                {"content": reminder.text}, [{"source_kind": "episode", "source_id": str(reminder.episode_id)}])
            reminder.text = "历史项目提示已隔离，请到历史记录核对。"
            reminder.dismissed, reminder.status = True, "invalidated"
            reminder.revision += 1
        await invalidate_consumers(session, deps)
        for episode in episodes:
            await schedule_sync(session, episode, all_used=True, force=True)
        if episodes:
            # This upgrade implements the user's explicit choice of progressive review.
            await save_preferences(session, {"backfill_enabled": True})
        marker.value = {"initialized": True, "version": 1, "legacy_count": len(episodes)}


async def history(session, offset=0, limit=50, episode_id=None):
    scope = [EvidenceArchive.payload.is_not(None), or_(EvidenceArchive.expires_at.is_(None), EvidenceArchive.expires_at > now())]
    if episode_id:
        scope.append(EvidenceArchive.sources.contains([{"source_kind": "episode", "source_id": str(episode_id)}]))
    rows = (await session.scalars(select(EvidenceArchive).where(*scope)
        .order_by(EvidenceArchive.created_at.desc(), EvidenceArchive.id).offset(offset).limit(limit + 1))).all()
    return {"items": [{"id": str(r.id), "kind": r.object_kind, "payload": r.payload,
                       "created_at": r.created_at.isoformat(), "label": "历史内容 · 未核实，仅供本人查看"}
                      for r in rows[:limit]], "next_offset": offset + limit if len(rows) > limit else None,
            "notice": "这些历史内容不参与检索、报告、自动陪伴或模型上下文；原文缺失时无法复核。"}
