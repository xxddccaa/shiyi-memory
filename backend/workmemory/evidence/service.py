import hashlib
import re
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import jieba
from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from workmemory.db.models import (
    Activity,
    AgentRun,
    ChatMessage,
    ConversationDigest,
    DerivedArtifact,
    Episode,
    EvidenceDependency,
    ExternalSource,
    Feedback,
    MemoryCandidate,
    Observation,
    Operation,
    ProactiveUtterance,
    Project,
    ProjectAlias,
    SearchDocument,
    Todo,
    WorkFact,
    WorkSession,
)
from workmemory.evidence.policy import eligible, eligible_clause, metadata, valid_excerpt_sources
from workmemory.inference.ollama import priority_context
from workmemory.ingestion.dedup import restore
from workmemory.jobs.queue import now
from workmemory.memory.sync import selected_backend
from workmemory.workspace.grouping import DAILY_WORK_LABEL

jieba.setLogLevel(40)
STOP = {
    "的",
    "了",
    "吗",
    "我",
    "是",
    "在",
    "什么",
    "怎么",
    "项目",
    "一下",
    "请",
    "那个",
    "现在",
    "之前",
}


def words(text):
    return list(
        dict.fromkeys(
            w
            for w in jieba.cut_for_search(text.casefold())
            if w.strip() and w not in STOP and re.search(r"\w", w)
        )
    )


def fingerprint(values):
    return hashlib.sha256("|".join(sorted(values)).encode()).hexdigest()


async def index_episode(session, episode, retention_days=30):
    await session.flush()
    await session.execute(delete(SearchDocument).where(SearchDocument.episode_id == episode.id))
    if episode.deleted_at:
        return
    values = []
    if episode.summary and eligible(episode):
        values.append(("summary", episode.id, episode.started_at, None, episode.evidence_expires_at, episode.summary))
    facts = (
        await session.scalars(
            select(WorkFact).where(
                WorkFact.episode_id == episode.id, WorkFact.revision == episode.revision
            )
        )
    ).all()
    for fact in facts if eligible(episode) else []:
        values.append(("fact", fact.id, fact.observed_at, None, episode.evidence_expires_at, fact.text))
    if not episode.corrected:
        rows = (
            await session.execute(
                select(Observation, Activity)
                .join(Activity, Observation.activity_id == Activity.id)
                .where(Observation.episode_id == episode.id, Activity.text.is_not(None))
            )
        ).all()
        observed = set()
        for obs, activity in rows:
            observed.add(activity.id)
            expiry = min(obs.captured_at, activity.captured_at) + timedelta(days=retention_days)
            if expiry > now():
                values.append(
                    (
                        "raw",
                        obs.id,
                        obs.captured_at,
                        obs.app,
                        expiry,
                        restore(activity.text, obs.delta),
                    )
                )
        activities = (
            await session.scalars(
                select(Activity).where(
                    Activity.episode_id == episode.id, Activity.text.is_not(None)
                )
            )
        ).all()
        for activity in activities:
            expiry = activity.captured_at + timedelta(days=retention_days)
            if activity.id not in observed and expiry > now():
                values.append(
                    ("raw", activity.id, activity.captured_at, activity.app, expiry, activity.text)
                )
    for kind, source_id, at, app, expiry, value in values:
        await session.execute(
            insert(SearchDocument).values(
                id=f"{kind}:{source_id}",
                episode_id=episode.id,
                revision=episode.revision,
                kind=kind,
                source_id=source_id,
                captured_at=at,
                app=app,
                expires_at=expiry,
                tokens=func.to_tsvector("simple", " ".join(words(value))),
            )
        )


async def backfill(runtime, limit=50):
    """Idempotent bounded repair, including records created before this migration."""
    async with runtime.sessions.begin() as session:
        marker = (
            select(SearchDocument.id)
            .where(
                SearchDocument.episode_id == Episode.id, SearchDocument.revision == Episode.revision
            )
            .exists()
        )
        rows = (
            await session.scalars(
                select(Episode)
                .where(
                    Episode.deleted_at.is_(None),
                    eligible_clause(),
                    Episode.summary.is_not(None),
                    ~marker,
                )
                .order_by(Episode.started_at, Episode.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for episode in rows:
            await index_episode(session, episode, runtime.settings.retention_days)
        return len(rows)


def excerpt(text, query="", budget=1200):
    if len(text) <= budget:
        return text
    positions = [text.casefold().find(w) for w in words(query) if len(w) > 1]
    first = min((p for p in positions if p >= 0), default=0)
    start = max(0, first - 160)
    return (
        ("…" if start else "")
        + text[start : start + budget]
        + ("…" if start + budget < len(text) else "")
    )


async def resolve(session, snippet_id, retention_days=30, query=""):
    try:
        kind, identifier = snippet_id.split(":", 1)
        source_id = uuid.UUID(identifier)
    except (ValueError, AttributeError):
        return None
    if kind == "external":
        row = await session.get(ExternalSource, source_id)
        if not row or not row.content or row.expires_at <= now():
            return None
        return dict(id=snippet_id, source_kind="external", source_id=str(row.id), revision=1,
                    kind="external", content=row.content, captured_at=row.retrieved_at.isoformat(),
                    expires_at=row.expires_at.isoformat(), episode_id=None, project_id=None,
                    title=row.title, url=row.url)
    if kind in {"todo", "project"}:
        row = await session.get(Todo if kind == "todo" else Project, source_id)
        if (
            not row
            or kind == "todo"
            and row.dismissed
            or kind == "project"
            and row.state in {"merged", "deleted"}
        ):
            return None
        if kind == "todo" and row.episode_id:
            episode = await session.get(Episode, row.episode_id)
            if not episode or episode.deleted_at:
                return None
        content = (
            row.text + ("（已完成）" if row.done else "（未完成）") if kind == "todo" else row.name
        )
        return dict(
            id=snippet_id,
            source_kind=kind,
            source_id=str(row.id),
            revision=row.revision,
            kind=kind,
            content=content,
            # A read timestamp is not evidence of when a task was captured.
            # Manually created todos have no stored creation time in this schema.
            captured_at=(row.created_at.isoformat() if kind == "project" else
                         episode.ended_at.isoformat() if row.episode_id else None),
            expires_at=None,
            episode_id=str(row.episode_id) if kind == "todo" and row.episode_id else None,
            project_id=str(row.project_id) if kind == "todo" and row.project_id else None,
            app=None,
            source_link=None,
            expiry_status="retained",
        )
    if kind == "session":
        row = await session.get(WorkSession, source_id)
        episode = await session.get(Episode, row.episode_id) if row else None
        if not row or not episode or episode.deleted_at or row.revision != episode.revision or (not row.title_confirmed and not await valid_excerpt_sources(session, episode, retention_days)):
            return None
        return dict(id=snippet_id, source_kind="session", source_id=str(row.id), revision=row.edit_revision,
                    kind="session", content=f"{row.title if row.title_confirmed else '工作记录'}（分类：{row.category}）", title=row.title if row.title_confirmed else "", title_needs_review=bool(not row.title_confirmed and row.edit_revision > 1), captured_at=row.started_at.isoformat(),
                    expires_at=episode.evidence_expires_at.isoformat() if not row.title_confirmed and episode.evidence_expires_at else None, episode_id=str(episode.id), project_id=str(episode.project_id) if episode.project_id else None, **metadata(episode))
    if kind == "review":
        row = await session.get(DerivedArtifact, source_id)
        if not row or not await valid_dependencies(session, await dependencies(session, "artifact", row.id), retention_days=retention_days):
            return None
        return dict(id=snippet_id, source_kind="review", source_id=str(row.id), revision=row.edit_revision,
                    kind="review", content=("人工正文需重新核对。" if row.needs_review else row.user_text or "尚未保存人工回顾正文。"),
                    captured_at=row.updated_at.isoformat(), expires_at=None, episode_id=None, project_id=None)
    if kind == "chat":
        message = await session.get(ChatMessage, source_id)
        expiry = message.created_at + timedelta(days=30) if message else now()
        if not message or not message.content or message.status != "ready" or expiry <= now():
            return None
        return dict(
            id=snippet_id,
            source_kind="chat",
            source_id=str(message.id),
            revision=1,
            kind="chat",
            content=message.content,
            captured_at=message.created_at.isoformat(),
            expires_at=expiry.isoformat(),
        )
    if kind == "memory":
        memory = await session.get(MemoryCandidate, source_id)
        if not memory or memory.status != "confirmed" or not memory.text:
            return None
        return dict(
            id=snippet_id,
            source_kind="memory",
            source_id=str(memory.id),
            revision=memory.revision,
            kind="user_confirmed",
            content=memory.text,
            captured_at=memory.created_at.isoformat(),
            confirmed_date=(memory.origin or {}).get("date"),
            expires_at=None,
            expiry_status="retained",
            episode_id=None,
            project_id=(memory.origin or {}).get("project_id"),
            app=None,
            source_link=f"/api/memory/candidates/{memory.id}",
            origin=memory.origin,
        )
    text, app, expiry = None, None, None
    if kind == "fact":
        fact = await session.get(WorkFact, source_id)
        if not fact:
            return None
        episode = await session.get(Episode, fact.episode_id)
        if not episode or fact.revision != episode.revision:
            return None
        if not await valid_excerpt_sources(session, episode, retention_days):
            return None
        text, at, expiry = fact.text, fact.observed_at, episode.evidence_expires_at
    elif kind == "summary":
        episode = await session.get(Episode, source_id)
        if not await valid_excerpt_sources(session, episode, retention_days):
            return None
        text, at, expiry = episode.summary, episode.started_at, episode.evidence_expires_at
    elif kind == "raw":
        observation = await session.get(Observation, source_id)
        activity = (
            await session.get(Activity, observation.activity_id)
            if observation and observation.activity_id
            else None
        )
        if observation is None:
            activity = await session.get(Activity, source_id)
        if not activity or not activity.text:
            return None
        episode = await session.get(Episode, activity.episode_id)
        at, app = (
            (observation.captured_at, observation.app)
            if observation
            else (activity.captured_at, activity.app)
        )
        expiry = min(at, activity.captured_at) + timedelta(days=retention_days)
        if expiry <= now() or episode.corrected:
            return None
        text = restore(activity.text, observation.delta) if observation else activity.text
    else:
        return None
    if not episode or episode.deleted_at or not text:
        return None
    return dict(
        id=snippet_id,
        source_kind="episode",
        source_id=str(episode.id),
        revision=episode.revision,
        kind=kind,
        content=text if kind in {"fact", "summary"} else excerpt(text, query),
        **metadata(episode),
        captured_at=at.isoformat(),
        expires_at=expiry.isoformat() if expiry else None,
        expiry_status="retained" if not expiry else "available",
        episode_id=str(episode.id),
        project_id=str(episode.project_id) if episode.project_id else None,
        app=app,
        source_link=f"/api/episodes/{episode.id}/sources",
    )


def temporal_scope(query, timezone):
    local = datetime.now(ZoneInfo(timezone))
    today = datetime.combine(local.date(), time.min, local.tzinfo)
    if "昨天" in query:
        return today - timedelta(days=1), today
    if "今天" in query or "今日" in query:
        return today, today + timedelta(days=1)
    if "上周" in query:
        end = today - timedelta(days=today.weekday())
        return end - timedelta(days=7), end
    if "本周" in query or "这周" in query:
        return today - timedelta(days=today.weekday()), today + timedelta(days=1)
    return None, None


async def search(
    runtime,
    query,
    *,
    limit=10,
    offset=0,
    project_id=None,
    app=None,
    from_at=None,
    to_at=None,
    include_raw=True,
    unassigned=False,
):
    if project_id and unassigned:
        raise HTTPException(422, "具体项目与日常工作不能同时筛选")
    await backfill(runtime)
    auto_from, auto_to = temporal_scope(query, runtime.settings.timezone)
    from_at, to_at = from_at or auto_from, to_at or auto_to
    warning, hits = None, []
    async with runtime.sessions() as session:
        backend = await selected_backend(session)
        if project_id is None and not unassigned:
            projects = (
                await session.scalars(select(Project).where(Project.state.notin_(["merged", "deleted"])))
            ).all()
            aliases = (await session.scalars(select(ProjectAlias))).all()
            named = {p.id for p in projects if p.name.casefold() in query.casefold()}
            named.update(a.project_id for a in aliases if a.name.casefold() in query.casefold())
            if len(named) == 1:
                project_id = next(iter(named))
            elif len(named) > 1:
                warning = "问题涉及多个项目，请指定项目以缩小范围。"
    token = priority_context.set(0)
    try:
        from workmemory.memory.availability import allowed
        await allowed(runtime.sessions, backend)
        import asyncio
        adapter = await asyncio.wait_for(runtime.memory.get(backend), timeout=5)

        hits = await asyncio.wait_for(adapter.search(query, limit=60), timeout=12)
    except Exception:
        warning = "语义记忆暂不可用，当前使用业务全文检索。"
    finally:
        priority_context.reset(token)
    scope = [Episode.deleted_at.is_(None)]
    if project_id:
        scope.append(Episode.project_id == project_id)
    if unassigned:
        scope.append(Episode.project_id.is_(None))
    if from_at:
        scope.append(Episode.started_at >= from_at)
    if to_at:
        scope.append(Episode.started_at < to_at)
    if app:
        scope.append(
            select(Activity.id)
            .where(Activity.episode_id == Episode.id, Activity.app == app)
            .exists()
        )
    ranks, details = {}, {}
    async with runtime.sessions() as session:
        filters = [
            *scope,
            SearchDocument.revision == Episode.revision,
            or_(SearchDocument.expires_at.is_(None), SearchDocument.expires_at > now()),
        ]
        if not include_raw:
            filters.append(SearchDocument.kind != "raw")
        if app:
            filters.append(or_(SearchDocument.app == app, SearchDocument.app.is_(None)))
        terms = words(query)[:32]
        if terms:
            tsquery = func.to_tsquery(
                "simple", " | ".join("'" + w.replace("'", "''") + "'" for w in terms)
            )
            lexical = (
                await session.scalars(
                    select(SearchDocument.id)
                    .join(Episode)
                    .where(*filters, SearchDocument.tokens.op("@@")(tsquery))
                    .order_by(
                        func.ts_rank_cd(SearchDocument.tokens, tsquery).desc(), SearchDocument.id
                    )
                    .limit(200)
                )
            ).all()
        else:
            lexical = []
        for rank, sid in enumerate(lexical, 1):
            ranks[sid] = 1 / (60 + rank)
            details[sid] = {"lexical_rank": rank}
        ids = [hit.episode_id for hit in hits]
        valid = set(
            (
                await session.scalars(
                    select(Episode.id).where(*scope, Episode.status == "ready", Episode.id.in_(ids))
                )
            ).all()
        )
        for rank, hit in enumerate(hits, 1):
            if hit.episode_id in valid:
                sid = f"summary:{hit.episode_id}"
                ranks[sid] = ranks.get(sid, 0) + 1 / (60 + rank)
                details.setdefault(sid, {})["semantic_rank"] = rank
        # Exact project scope remains useful when the query only names a project.
        if (project_id or unassigned and query.strip() == DAILY_WORK_LABEL) and not ranks:
            ids = (
                await session.scalars(
                    select(Episode.id)
                    .where(*scope, Episode.status == "ready")
                    .order_by(Episode.started_at.desc(), Episode.id)
                    .limit(60)
                )
            ).all()
            for rank, eid in enumerate(ids, 1):
                ranks[f"summary:{eid}"] = 1 / (60 + rank)
        if not app:
            memories = (
                (
                    await session.scalars(
                        select(MemoryCandidate)
                        .where(
                            MemoryCandidate.status == "confirmed",
                            (MemoryCandidate.origin["project_id"].astext == str(project_id)) if project_id else True,
                            MemoryCandidate.origin["project_id"].astext.is_(None) if unassigned else True,
                            or_(
                                *(
                                    MemoryCandidate.text.icontains(w, autoescape=True)
                                    for w in terms[:12]
                                )
                            ),
                        )
                        .order_by(MemoryCandidate.created_at.desc(), MemoryCandidate.id)
                        .limit(20)
                    )
                ).all()
                if terms
                else []
            )
            for rank, memory in enumerate(memories, 1):
                captured_at = datetime.fromisoformat(memory.origin["date"]).replace(tzinfo=ZoneInfo(runtime.settings.timezone)) if memory.origin.get("date") else memory.created_at
                if from_at and captured_at < from_at or to_at and captured_at >= to_at:
                    continue
                ranks[f"memory:{memory.id}"] = 1 / (60 + rank)
        items = []
        for sid in sorted(ranks, key=lambda k: (-ranks[k], k)):
            value = await resolve(session, sid, runtime.settings.retention_days, query)
            if value:
                value["retrieval"] = {**details.get(sid, {}), "rrf": ranks[sid]}
                items.append(value)
        return {
            "items": items[offset : offset + limit],
            "warning": warning,
            "candidate_count": len(items),
            "count_is_exact": False,
            "next_cursor": offset + limit if len(items) > offset + limit else None,
            "scope": {
                "project_id": str(project_id) if project_id else None,
                "unassigned": unassigned,
                "from": from_at.isoformat() if from_at else None,
                "to": to_at.isoformat() if to_at else None,
                "app": app,
            },
        }


async def dependencies(session, kind, identifier):
    return (
        await session.scalars(
            select(EvidenceDependency).where(
                EvidenceDependency.consumer_kind == kind,
                EvidenceDependency.consumer_id == identifier,
            )
        )
    ).all()


async def remember(session, kind, identifier, snippets):
    for s in snippets:
        await session.execute(
            insert(EvidenceDependency)
            .values(
                consumer_kind=kind,
                consumer_id=identifier,
                snippet_id=s["id"],
                source_kind=s["source_kind"],
                source_id=uuid.UUID(s["source_id"]),
                revision=s["revision"],
                expires_at=datetime.fromisoformat(s["expires_at"]) if s.get("expires_at") else None,
            )
            .on_conflict_do_nothing()
        )
        if s["source_kind"] == "session" and s.get("episode_id"):
            source = await resolve(session, "summary:" + s["episode_id"])
            if source:
                await remember(session, kind, identifier, [source])
        elif s["source_kind"] == "review":
            await copy_dependencies(session, "artifact", uuid.UUID(s["source_id"]), kind, identifier)


async def copy_dependencies(session, source_kind, source_id, target_kind, target_id):
    for dep in await dependencies(session, source_kind, source_id):
        await session.execute(
            insert(EvidenceDependency)
            .values(
                consumer_kind=target_kind,
                consumer_id=target_id,
                snippet_id=dep.snippet_id,
                source_kind=dep.source_kind,
                source_id=dep.source_id,
                revision=dep.revision,
                expires_at=dep.expires_at,
            )
            .on_conflict_do_nothing()
        )


async def valid_dependencies(session, deps, *, lock=False, retention_days=30, extra_locks=()):
    # Lock canonical rows in a fixed order through final publication/commit.
    dependency_keys = {(d.source_kind, d.source_id) for d in deps}
    for kind, identifier in sorted(dependency_keys | set(extra_locks), key=str):
        model = {
            "external": ExternalSource,
            "episode": Episode,
            "memory": MemoryCandidate,
            "chat": ChatMessage,
            "todo": Todo,
            "project": Project,
            "session": WorkSession,
            "review": DerivedArtifact,
        }[kind]
        stmt = select(model).where(model.id == identifier)
        if lock:
            stmt = stmt.with_for_update()
        current = await session.scalar(stmt.execution_options(populate_existing=True))
        if current and (kind, identifier) not in dependency_keys:
            continue  # Additional rows are checked by the full target snapshot.
        if (
            not current
            or (kind == "episode" and current.deleted_at)
            or (kind == "memory" and current.status != "confirmed")
            or (kind == "chat" and current.status != "ready")
        ):
            return False
        for dep in deps:
            if dep.source_kind == kind and dep.source_id == identifier:
                if (
                    (current.edit_revision if kind in {"session", "review"} else getattr(current, "revision", 1)) != dep.revision
                    or dep.expires_at
                    and dep.expires_at <= now()
                ):
                    return False
                if not await resolve(session, dep.snippet_id, retention_days):
                    return False
    return True


async def invalidate_consumers(session, deps):
    groups = {}
    for dep in deps:
        groups.setdefault(dep.consumer_kind, set()).add(dep.consumer_id)
    changed = 0
    if groups.get("pi_session"):
        from workmemory.agent.coding import purge_session
        for identifier in groups["pi_session"]:
            await purge_session(session, identifier)
            changed += 1
    updates = [
        (
            "proactive", ProactiveUtterance, ProactiveUtterance.status != "invalidated",
            dict(content=None, context=None, checkpoint=None, citations=[], status="invalidated"),
        ),
        (
            "message",
            ChatMessage,
            ChatMessage.status != "invalidated",
            dict(content=None, citations=[], external_citations=[], context=None, status="invalidated"),
        ),
        (
            "run",
            AgentRun,
            AgentRun.status.in_(["running", "queued"]),
            dict(status="invalidated", error="相关来源已更新，请重新提问。", finished_at=now()),
        ),
        (
            "artifact",
            DerivedArtifact,
            DerivedArtifact.status != "invalidated",
            dict(body=None, status="invalidated", needs_review=True, source_hash=""),
        ),
        (
            "digest",
            ConversationDigest,
            ConversationDigest.status != "invalidated",
            dict(content=None, status="invalidated"),
        ),
        (
            "operation",
            Operation,
            Operation.status == "pending",
            dict(payload=None, preview=None, status="invalidated", revision=Operation.revision + 1),
        ),
    ]
    for kind, model, condition, values in updates:
        if groups.get(kind):
            result = await session.execute(
                update(model).where(model.id.in_(groups[kind]), condition).values(**values)
            )
            changed += result.rowcount
    # External excerpts inherit every private source used to formulate the query.
    # Clear their metadata too: titles and URLs can contain query-derived private text.
    external_ids = set(groups.get("external", set()))
    if groups.get("run"):
        external_ids.update((await session.scalars(select(ExternalSource.id).where(
            ExternalSource.run_id.in_(groups["run"]), ExternalSource.content.is_not(None),
        ))).all())
    if external_ids:
        result = await session.execute(update(ExternalSource).where(ExternalSource.id.in_(external_ids))
            .values(content=None, title="", url=""))
        changed += result.rowcount
    if changed:
        from workmemory.companion.state import emit

        await emit(session, "invalidated")


async def invalidate_episode(session, episode_id):
    await session.execute(
        delete(Feedback).where(Feedback.object_kind == "episode", Feedback.object_id == episode_id)
    )
    deps = (
        await session.scalars(
            select(EvidenceDependency).where(
                EvidenceDependency.source_kind == "episode",
                EvidenceDependency.source_id == episode_id,
            )
        )
    ).all()
    await invalidate_consumers(session, deps)
    await session.execute(delete(SearchDocument).where(SearchDocument.episode_id == episode_id))
    await session.execute(delete(WorkFact).where(WorkFact.episode_id == episode_id))
    # Manual work-session fields survive automatic source refreshes.


async def invalidate_memory(session, memory_id):
    await session.execute(
        delete(Feedback).where(Feedback.object_kind == "memory", Feedback.object_id == memory_id)
    )
    deps = (
        await session.scalars(
            select(EvidenceDependency).where(
                EvidenceDependency.source_kind == "memory",
                EvidenceDependency.source_id == memory_id,
            )
        )
    ).all()
    await invalidate_consumers(session, deps)


async def neighbors(runtime, episode_id):
    async with runtime.sessions() as session:
        episode = await session.get(Episode, episode_id)
        if not episode or episode.deleted_at:
            return {"items": []}
        scope = [Episode.deleted_at.is_(None), Episode.status == "ready", Episode.id != episode_id]
        scope.append(
            Episode.project_id == episode.project_id
            if episode.project_id
            else Episode.device_id == episode.device_id
        )
        before = await session.scalar(
            select(Episode)
            .where(*scope, Episode.started_at < episode.started_at)
            .order_by(Episode.started_at.desc(), Episode.id)
            .limit(1)
        )
        after = await session.scalar(
            select(Episode)
            .where(*scope, Episode.started_at > episode.started_at)
            .order_by(Episode.started_at, Episode.id)
            .limit(1)
        )
        items = []
        for other in (before, after):
            if other:
                value = await resolve(
                    session, f"summary:{other.id}", runtime.settings.retention_days
                )
                if value:
                    value["gap_seconds"] = abs(
                        (other.started_at - episode.started_at).total_seconds()
                    )
                    items.append(value)
        return {
            "items": items,
            "notice": "相邻记录之间可能存在未采集的时间，不能据此计算工作时长。",
        }


async def invalidate_object(session, kind, identifier):
    from workmemory.evidence.archive import clear_archives
    await clear_archives(session, kind, identifier)
    deps = (
        await session.scalars(
            select(EvidenceDependency).where(
                EvidenceDependency.source_kind == kind, EvidenceDependency.source_id == identifier
            )
        )
    ).all()
    await invalidate_consumers(session, deps)
    await session.execute(
        delete(Feedback).where(Feedback.object_kind == kind, Feedback.object_id == identifier)
    )


def canonical_citation(identifier, known):
    """Accept a bare UUID only when it unambiguously identifies already supplied evidence."""
    if identifier in known:
        return identifier
    try:
        bare = str(uuid.UUID(identifier))
    except (ValueError, TypeError, AttributeError):
        return identifier
    matches = [sid for sid in known if sid.rsplit(":", 1)[-1] == bare]
    return matches[0] if len(matches) == 1 else identifier
