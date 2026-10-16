"""Queries and mutations over authoritative business objects, shared by UI and MCP."""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from workmemory.artifacts.service import artifact_sources, todo_text_key
from workmemory.collector.status import collector_status
from workmemory.db.models import (
    Activity,
    DerivedArtifact,
    Episode,
    MemoryCandidate,
    Observation,
    Project,
    Reminder,
    Todo,
    TodoSource,
    TodoTombstone,
    WorkSession,
)
from workmemory.evidence.service import dependencies, invalidate_object, resolve, valid_dependencies
from workmemory.workspace.grouping import DAILY_WORK_LABEL

KINDS = {"todo": Todo, "project": Project, "memory": MemoryCandidate, "episode": Episode, "session": WorkSession, "review": DerivedArtifact}


async def remove_todo(session, row):
    await invalidate_object(session, "todo", row.id)
    ids = [
        str(v)
        for v in (
            await session.scalars(select(TodoSource.episode_id).where(TodoSource.todo_id == row.id))
        ).all()
    ]
    if row.episode_id and str(row.episode_id) not in ids:
        ids.append(str(row.episode_id))
    await session.execute(
        insert(TodoTombstone)
        .values(
            id=row.id,
            project_id=row.project_id,
            text_hash=todo_text_key(row.text),
            source_ids=ids,
        )
        .on_conflict_do_nothing()
    )
    await session.execute(delete(Reminder).where(Reminder.todo_id == row.id))
    await session.delete(row)


async def update_todo(session, row, values):
    from workmemory.artifacts.service import todo_key
    from workmemory.jobs.queue import now
    from workmemory.reminders.schedule import SCHEDULE_FIELDS, validate_schedule
    if any(v is None for v in values.values()):
        raise HTTPException(422, "状态、内容和提醒时间不能为空；暂停请使用 paused=true")
    if "text" in values and (not values["text"].strip() or len(values["text"]) > 1000):
        raise HTTPException(422, "事项内容须为1至1000字")
    if (set(values) & (SCHEDULE_FIELDS - {"notify_pet", "paused"})) or values.get("paused") is False or values.get("done") is False:
        values = {**values, **validate_schedule(values, current=row, clock=now())}
    just_completed = values.get("done") is True and not row.done
    await invalidate_object(session, "todo", row.id)
    for key, value in values.items():
        setattr(row, key, value)
    row.revision += 1
    row.schedule_revision += 1
    row.normalized_key = todo_key(row.text, row.project_id)
    await session.execute(delete(Reminder).where(Reminder.todo_id == row.id))
    if values.get("notify_pet") is True:
        from workmemory.companion.state import save_preferences
        await save_preferences(session, {"reminders_enabled": True})
    if just_completed:
        from types import SimpleNamespace

        from workmemory.companion.feedback import completed
        await completed(session, SimpleNamespace(todo_id=row.id, id=uuid.uuid5(row.id, f"completion:{row.revision}")))
    from workmemory.companion.state import emit
    await emit(session, "todo", row.id)


async def get_object(session, kind, identifier, revision=None, retention_days=30):
    if kind not in KINDS:
        raise HTTPException(422, "不支持的对象类型")
    row = await session.get(KINDS[kind], uuid.UUID(str(identifier)))
    if not row or getattr(row, "deleted_at", None) or getattr(row, "dismissed", False):
        raise HTTPException(404, "对象已删除或不可用")
    if (
        kind == "project"
        and row.state in {"merged", "deleted"}
        or kind == "memory"
        and row.status != "confirmed"
    ):
        raise HTTPException(404, "对象已失效")
    current_revision = row.edit_revision if kind in {"session", "review"} else row.revision
    if revision is not None and current_revision != revision:
        raise HTTPException(409, "对象已更新，请重新选择")
    prefix = "summary" if kind == "episode" else kind
    item = await resolve(session, f"{prefix}:{row.id}", retention_days)
    if not item:
        raise HTTPException(409, "对象来源已失效或尚未整理")
    item["object_id"] = str(row.id)
    if kind == "todo":
        item.update(text=row.text, done=row.done, due_at=row.due_at.isoformat() if row.due_at else None,
                    timezone=row.timezone, repeat=row.repeat, weekdays=row.weekdays, paused=row.paused, notify_pet=row.notify_pet)
        from workmemory.reminders.scheduler import view as reminder_view
        item["reminders"] = [reminder_view(r, row) for r in (await session.scalars(select(Reminder).where(
            Reminder.todo_id == row.id, Reminder.status.in_(["pending", "snoozed"]), Reminder.dismissed.is_(False),
            Reminder.schedule_revision == row.schedule_revision,
        ))).all()]
    if kind == "session":
        item.update(text=item["title"], category=row.category)
    if kind == "review":
        item.update(text=(row.user_text or "") if not row.needs_review else "", scope=row.scope,
                    report_kind=row.kind, needs_review=row.needs_review)
    return item


async def list_objects(
    session,
    kind="todo",
    query="",
    project_id=None,
    done=None,
    cursor=None,
    limit=20,
    retention_days=30,
    day=None,
    timezone="Asia/Shanghai",
):
    if kind not in KINDS:
        raise HTTPException(422, "不支持的对象类型")
    model = KINDS[kind]
    limit = max(1, min(limit, 50))
    conditions = []
    field = (model.name if kind == "project" else model.summary if kind == "episode" else
             model.title if kind == "session" else model.user_text if kind == "review" else model.text)
    if query:
        conditions.append(field.icontains(query, autoescape=True))
    if kind == "todo":
        conditions += [
            Todo.dismissed.is_(False),
            (
                Todo.episode_id.is_(None)
                | Todo.episode_id.in_(select(Episode.id).where(Episode.deleted_at.is_(None)))
            ),
        ]
        if done is not None:
            conditions.append(Todo.done == done)
    elif kind == "memory":
        conditions.append(MemoryCandidate.status == "confirmed")
    elif kind == "project":
        conditions.append(Project.state.notin_(["merged", "deleted"]))
    elif kind == "episode":
        conditions += [Episode.deleted_at.is_(None), Episode.status == "ready"]
    elif kind == "session":
        conditions.append(select(Episode.id).where(Episode.id == WorkSession.episode_id,
            Episode.deleted_at.is_(None), Episode.revision == WorkSession.revision).exists())
    elif kind == "review":
        # UI reports use review; retain compatibility with earlier daily artifacts.
        # Prefer the actual editable UI report when both exist for a date.
        canonical = aliased(DerivedArtifact)
        conditions.append((DerivedArtifact.kind == "review") | (
            (DerivedArtifact.kind == "daily") & ~select(canonical.id).where(
                canonical.kind == "review", canonical.scope == DerivedArtifact.scope
            ).exists()
        ))
    if day:
        try:
            start = datetime.combine(date.fromisoformat(day), time.min, ZoneInfo(timezone))
        except ValueError:
            raise HTTPException(422, "日期无效") from None
        if kind == "review":
            conditions.append(DerivedArtifact.scope == day)
        elif kind in {"session", "episode"}:
            conditions.extend([model.started_at >= start, model.started_at < start + timedelta(days=1)])
        else:
            raise HTTPException(422, "日期筛选仅用于工作段、片段和每日回顾")
    if project_id and kind == "session":
        conditions.append(WorkSession.episode_id.in_(select(Episode.id).where(Episode.project_id == uuid.UUID(str(project_id)))))
    if project_id and kind in {"todo", "episode"}:
        conditions.append(model.project_id == uuid.UUID(str(project_id)))
    total = await session.scalar(select(func.count()).select_from(model).where(*conditions))
    if cursor:
        try:
            conditions.append(model.id > uuid.UUID(cursor))
        except ValueError:
            raise HTTPException(422, "分页游标无效") from None
    rows = (
        await session.scalars(select(model).where(*conditions).order_by(model.id).limit(limit + 1))
    ).all()
    items = []
    for row in rows[:limit]:
        try:
            evidence = await get_object(session, kind, row.id, retention_days=retention_days)
        except HTTPException:
            continue
        project = await session.get(Project, uuid.UUID(evidence["project_id"])) if evidence.get("project_id") else None
        items.append(
            {
                "id": str(row.id),
                "kind": kind,
                "revision": row.edit_revision if kind in {"session", "review"} else row.revision,
                "text": evidence.get("text", evidence["content"]),
                "display_text": evidence["content"],
                "evidence_id": evidence["id"],
                "project_id": evidence.get("project_id"),
                "project_name": project.name if project else DAILY_WORK_LABEL if kind in {"todo", "memory", "episode", "session"} else None,
                **({k: evidence[k] for k in ("due_at", "timezone", "repeat", "weekdays", "paused", "notify_pet", "reminders")} if kind == "todo" else {}),
                **({"category": row.category, "started_at": row.started_at.isoformat()} if kind == "session" else {}),
                **({"day": row.scope, "needs_review": row.needs_review} if kind == "review" else {}),
                "done": getattr(row, "done", None),
                "evidence": evidence,
            }
        )
    names = {}
    for item in items:
        names.setdefault(item["text"], []).append(item["id"])
    ambiguous = [{"text": text, "ids": ids} for text, ids in names.items() if len(ids) > 1]
    return {
        "items": items,
        **({"same_name_matches": ambiguous,
            "notice": "发现同名对象。若用户尚未指定项目/日期/选中对象且只操作一条，请以最终回答询问目标并结束本轮；不要生成多张操作卡片供选择。"} if ambiguous else {}),
        "total": total,
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }


async def work_day(runtime, day=None, cursor=None, limit=20):
    tz = ZoneInfo(runtime.settings.timezone)
    try:
        day = date.fromisoformat(day) if day else datetime.now(tz).date()
        if cursor:
            uuid.UUID(cursor)
    except ValueError:
        raise HTTPException(422, "日期或分页游标无效") from None
    start = datetime.combine(day, time.min, tz)
    end = start + timedelta(days=1)
    async with runtime.sessions() as session:
        criteria = [
            Episode.deleted_at.is_(None),
            Episode.started_at >= start,
            Episode.started_at < end,
        ]
        counts = (
            await session.execute(
                select(Episode.status, func.count()).where(*criteria).group_by(Episode.status)
            )
        ).all()
        raws = await session.scalar(
            select(func.count())
            .select_from(Activity)
            .join(Episode)
            .where(
                Episode.deleted_at.is_(None),
                Episode.corrected.is_(False),
                Activity.text.is_not(None),
                Activity.captured_at
                > datetime.now(tz) - timedelta(days=runtime.settings.retention_days),
                Activity.captured_at >= start,
                Activity.captured_at < end,
            )
        )
        statement = select(Episode).where(*criteria).order_by(Episode.id)
        if cursor:
            statement = statement.where(Episode.id > uuid.UUID(cursor))
        limit = max(1, min(limit, 30))
        rows = (await session.scalars(statement.limit(limit + 1))).all()
        items = []
        for row in rows[:limit]:
            item = await resolve(session, f"summary:{row.id}", runtime.settings.retention_days)
            if not item and not row.corrected:
                raw = await session.scalar(
                    select(Activity)
                    .where(
                        Activity.episode_id == row.id,
                        Activity.text.is_not(None),
                        Activity.captured_at >= start,
                        Activity.captured_at < end,
                        Activity.captured_at
                        > datetime.now(tz) - timedelta(days=runtime.settings.retention_days),
                    )
                    .order_by(Activity.captured_at.desc(), Activity.id)
                    .limit(1)
                )
                observation = await session.scalar(
                    select(Observation)
                    .join(Activity)
                    .where(
                        Observation.episode_id == row.id,
                        Activity.text.is_not(None),
                        Activity.captured_at
                        > datetime.now(tz) - timedelta(days=runtime.settings.retention_days),
                        Observation.captured_at >= start,
                        Observation.captured_at < end,
                        Observation.captured_at
                        > datetime.now(tz) - timedelta(days=runtime.settings.retention_days),
                    )
                    .order_by(Observation.captured_at.desc(), Observation.id.desc())
                    .limit(1)
                )
                # Near-duplicate captures store reversible deltas against a full Activity.
                # Prefer the newest observation, but retain standalone/manual Activities.
                latest = (
                    observation
                    if observation and (not raw or observation.captured_at >= raw.captured_at)
                    else raw
                )
                if latest:
                    item = await resolve(
                        session, f"raw:{latest.id}", runtime.settings.retention_days
                    )
            if item:
                items.append(item)
        sources, source_hash, _ = await artifact_sources(
            session, "daily", day.isoformat(), runtime.settings.timezone, runtime.settings.retention_days
        )
        report = await session.scalar(
            select(DerivedArtifact).where(
                DerivedArtifact.kind == "daily", DerivedArtifact.scope == day.isoformat()
            )
        )
        report_body = None
        if (
            report
            and report.status == "ready"
            and report.source_hash == source_hash
            and await valid_dependencies(
                session, await dependencies(session, "artifact", report.id), retention_days=runtime.settings.retention_days
            )
        ):
            report_body = report.body
        collector = collector_status(runtime.settings)
        return {
            "day": day.isoformat(),
            "timezone": str(tz),
            "raw_count": raws,
            "episode_counts": dict(counts),
            "items": items,
            "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
            "report": report_body,
            "report_sources": sources if report_body else [],
            "report_status": (
                "ready"
                if report_body
                else "stale"
                if report and report.status == "ready"
                else report.status
                if report
                else "not_requested"
            ),
            "collector": {
                k: collector.get(k)
                for k in [
                    "running",
                    "paused",
                    "accessibility_granted",
                    "screen_recording_granted",
                    "last_capture_at",
                ]
            },
            "notice": "未整理记录不等于没有采集；分页未读完时不能声称覆盖整天。报告仅使用已整理的有效来源。",
        }
