import re
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from workmemory.db.models import (
    DerivedArtifact,
    Episode,
    EvidenceDependency,
    Reminder,
    Todo,
    TodoSource,
    WorkFact,
    WorkSession,
)
from workmemory.evidence.policy import eligible_clause, metadata
from workmemory.evidence.service import (
    dependencies,
    fingerprint,
    remember,
    resolve,
    valid_dependencies,
)
from workmemory.jobs.queue import enqueue, now


class ArtifactItem(BaseModel):
    text: str = Field(min_length=1, max_length=1200)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)


class ArtifactBody(BaseModel):
    observations: list[ArtifactItem] = Field(default_factory=list)
    progress: list[ArtifactItem] = Field(default_factory=list, max_length=12)
    decisions: list[ArtifactItem] = Field(default_factory=list, max_length=12)
    blockers: list[ArtifactItem] = Field(default_factory=list, max_length=12)
    next_steps: list[ArtifactItem] = Field(default_factory=list, max_length=12)
    unknowns: list[str] = Field(default_factory=list, max_length=8)


def normalize_todo_text(text):
    normalized = re.sub(r"[\s，。！？、,.!?：:；;]+", "", text.casefold())
    normalized = re.sub(r"^(?:请|需要|记得|接下来要|待办[:：]?)+", "", normalized)
    # A short explicit vocabulary, not fuzzy merging of different dates/numbers/negations.
    normalized = normalized.replace("补上", "补充").replace("增加", "添加")
    return normalized


def todo_text_key(text):
    return fingerprint([normalize_todo_text(text)])


def todo_key(text, project_id):
    return fingerprint([str(project_id or "unclassified"), normalize_todo_text(text)])


async def reconcile_todos(session, episode, texts):
    """Compatibility for old queued jobs: captured plans never create user schedules."""
    return


async def remove_todo_source(session, episode_id):
    todos = (
        await session.scalars(
            select(Todo).where(
                (Todo.episode_id == episode_id)
                | Todo.id.in_(select(TodoSource.todo_id).where(TodoSource.episode_id == episode_id))
            )
        )
    ).all()
    await session.execute(delete(TodoSource).where(TodoSource.episode_id == episode_id))
    for todo in todos:
        from workmemory.evidence.service import invalidate_object

        await invalidate_object(session, "todo", todo.id)
        replacement = await session.scalar(
            select(TodoSource.episode_id)
            .join(Episode)
            .where(
                TodoSource.todo_id == todo.id,
                Episode.deleted_at.is_(None),
                TodoSource.revision == Episode.revision,
            )
            .order_by(Episode.started_at.desc())
            .limit(1)
        )
        if replacement:
            todo.episode_id = replacement
            for reminder in (
                await session.scalars(select(Reminder).where(Reminder.todo_id == todo.id))
            ).all():
                reminder.episode_id = replacement
        else:
            await session.execute(delete(Reminder).where(Reminder.todo_id == todo.id))
            await session.delete(todo)


async def artifact_sources(session, kind, scope, timezone, retention_days=30):
    conditions = [
        eligible_clause(),
        Episode.summary.is_not(None),
    ]
    if kind == "brief":
        conditions.append(Episode.project_id == uuid.UUID(scope))
    else:
        start = datetime.combine(date.fromisoformat(scope), time.min, ZoneInfo(timezone))
        conditions += [
            Episode.started_at >= start,
            Episode.started_at < start + timedelta(days=7 if kind == "weekly" else 1),
        ]
    total = await session.scalar(select(func.count()).select_from(Episode).where(*conditions))
    episodes = (
        await session.scalars(
            select(Episode).where(*conditions).order_by(Episode.started_at.desc(), Episode.id)
        )
    ).all()
    values = []
    for episode in episodes:
        snippet = await resolve(session, f"summary:{episode.id}", retention_days)
        if snippet:
            snippet["content"] = episode.summary
            if snippet.get("evidence_kind") == "screen_text":
                facts = (await session.scalars(select(WorkFact).where(WorkFact.episode_id == episode.id, WorkFact.revision == episode.revision))).all()
                snippet["observations"] = [{"text": f.text, "evidence_ids": [f"summary:{episode.id}"]} for f in facts]
            values.append(snippet)
    source_hash = fingerprint(
        ["artifact-excerpts-v1", kind, scope, *[f"{v['id']}:{v['revision']}" for v in values]]
    )
    return values, source_hash, total


async def request_artifact(runtime, kind, scope, force=False):
    async with runtime.sessions.begin() as session:
        sources, source_hash, total = await artifact_sources(
            session, kind, scope, runtime.settings.timezone, runtime.settings.retention_days
        )
        await session.execute(
            insert(DerivedArtifact)
            .values(id=uuid.uuid4(), kind=kind, scope=scope)
            .on_conflict_do_nothing(index_elements=[DerivedArtifact.kind, DerivedArtifact.scope])
        )
        artifact = await session.scalar(
            select(DerivedArtifact)
            .where(DerivedArtifact.kind == kind, DerivedArtifact.scope == scope)
            .with_for_update()
        )
        deps = await dependencies(session, "artifact", artifact.id)
        if artifact.body and not await valid_dependencies(session, deps, retention_days=runtime.settings.retention_days):
            artifact.body, artifact.status, artifact.needs_review = None, "invalidated", True
        unchanged = artifact.source_hash == source_hash and artifact.status == "ready"
        pending = artifact.requested_hash == source_hash and artifact.status in {
            "pending",
            "running",
        }
        if force or artifact.requested_hash != source_hash:
            artifact.attempts = 0
            artifact.processed_sources = 0
        artifact.total_sources = total
        if not sources:
            artifact.body = ArtifactBody(
                unknowns=["这个范围暂时没有已整理的工作记录。"]
            ).model_dump()
            artifact.source_hash = source_hash
            artifact.requested_hash = source_hash
            artifact.status = "ready"
            await session.execute(
                delete(EvidenceDependency).where(
                    EvidenceDependency.consumer_kind == "artifact",
                    EvidenceDependency.consumer_id == artifact.id,
                )
            )
        elif (
            (force or not unchanged)
            and (force or not pending or not artifact.requested_hash)
            and artifact.attempts < 3
        ):
            artifact.requested_hash = source_hash
            artifact.status = "pending"
            await enqueue(
                session,
                "artifact",
                f"artifact:{artifact.id}:{source_hash}:{artifact.attempts}:{uuid.uuid4() if force else 'auto'}",
                {"artifact_id": str(artifact.id), "source_hash": source_hash},
                priority=5,
            )
        await session.flush()
        return artifact_view(artifact, total=total, source_count=len(sources))


def artifact_view(artifact, **extra):
    return dict(
        id=str(artifact.id),
        kind=artifact.kind,
        scope=artifact.scope,
        status=artifact.status,
        body=artifact.body,
        user_text=artifact.user_text if not artifact.needs_review else None,
        requested_hash=artifact.requested_hash,
        edit_revision=artifact.edit_revision,
        needs_review=artifact.needs_review,
        attempts=artifact.attempts,
        processed_sources=artifact.processed_sources,
        total_sources=artifact.total_sources,
        updated_at=artifact.updated_at.isoformat(),
        **extra,
    )


async def generate_artifact(runtime, payload):
    identifier = uuid.UUID(payload["artifact_id"])
    async with runtime.sessions.begin() as session:
        artifact = await session.scalar(
            select(DerivedArtifact).where(DerivedArtifact.id == identifier).with_for_update()
        )
        if (
            not artifact
            or artifact.requested_hash != payload["source_hash"]
            or artifact.attempts >= 3
        ):
            return
        sources, source_hash, total = await artifact_sources(
            session, artifact.kind, artifact.scope, runtime.settings.timezone, runtime.settings.retention_days
        )
        if source_hash != payload["source_hash"]:
            artifact.status = "stale"
            return
        artifact.attempts += 1
        artifact.status = "running"
        # Invalidated content is already hidden; its obsolete versions cannot poison rebuilding.
        if not artifact.body:
            await session.execute(
                delete(EvidenceDependency).where(
                    EvidenceDependency.consumer_kind == "artifact",
                    EvidenceDependency.consumer_id == identifier,
                )
            )
        # Preserve dependencies of a still-visible older artifact until the replacement commits.
        await remember(session, "artifact", identifier, sources)
        kind = artifact.kind
    try:
        from workmemory.artifacts.hierarchy import build

        async def progress(completed, count):
            async with runtime.sessions.begin() as session:
                current = await session.get(DerivedArtifact, identifier)
                if (
                    not current
                    or current.requested_hash != source_hash
                    or current.status == "invalidated"
                ):
                    raise ValueError("Report invalidated during processing")
                current.processed_sources, current.total_sources = completed, count

        body = await build(runtime, kind, sources, progress)
        body["coverage"] = {"source_count": len(sources), "total": total, "truncated": False}
        async with runtime.sessions.begin() as session:
            # Match canonical versions before taking the artifact lock: same order as deletion.
            deps = await dependencies(session, "artifact", identifier)
            valid = await valid_dependencies(session, deps, lock=True, retention_days=runtime.settings.retention_days)
            artifact = await session.scalar(
                select(DerivedArtifact).where(DerivedArtifact.id == identifier).with_for_update()
            )
            _, current_hash, _ = await artifact_sources(
                session, artifact.kind, artifact.scope, runtime.settings.timezone, runtime.settings.retention_days
            )
            if not valid or current_hash != source_hash or artifact.requested_hash != source_hash:
                artifact.status = "invalidated" if not valid else "stale"
                if not valid:
                    artifact.body = None
                return
            await session.execute(
                delete(EvidenceDependency).where(
                    EvidenceDependency.consumer_kind == "artifact",
                    EvidenceDependency.consumer_id == identifier,
                )
            )
            await remember(session, "artifact", identifier, sources)
            artifact.body, artifact.source_hash = body, source_hash
            artifact.status, artifact.updated_at = "ready", now()
    except Exception:
        async with runtime.sessions.begin() as session:
            artifact = await session.get(DerivedArtifact, identifier)
            if artifact and artifact.requested_hash == source_hash and artifact.status == "running":
                artifact.status = "failed"
        # Retry is persisted and limited by request_artifact, not the generic job's eight retries.


async def work_sessions(runtime, project_id=None, day=None):
    async with runtime.sessions() as session:
        scope = [Episode.deleted_at.is_(None), WorkSession.revision == Episode.revision]
        if project_id:
            scope.append(Episode.project_id == project_id)
        if day:
            start = datetime.combine(day, time.min, ZoneInfo(runtime.settings.timezone))
            scope += [
                WorkSession.started_at >= start,
                WorkSession.started_at < start + timedelta(days=1),
            ]
        rows = (
            await session.execute(
                select(WorkSession, Episode)
                .join(Episode)
                .where(*scope)
                .order_by(WorkSession.started_at, WorkSession.id)
                .limit(200)
            )
        ).all()
        groups = []
        for segment, episode in rows:
            source = {"id": f"summary:{episode.id}", "revision": episode.revision}
            groups.append(
                dict(
                    **metadata(episode),
                    id=str(segment.id),
                    title=segment.title if segment.edit_revision > 1 or episode.evidence_state != "legacy_unverified" else "历史工作段 · 待复核",
                    category=segment.category,
                    revision=segment.edit_revision,
                    source_revision=segment.revision,
                    project_id=str(episode.project_id) if episode.project_id else None,
                    started_at=segment.started_at.isoformat(),
                    ended_at=segment.ended_at.isoformat(),
                    sources=[source],
                )
            )
        return {"items": groups, "notice": "显示记录的观察跨度，不代表持续工作时长。"}
