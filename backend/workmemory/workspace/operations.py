"""Pi proposes typed business changes. Only an authenticated UI confirmation executes them."""

import json
import uuid
from datetime import timedelta
from typing import Literal

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from workmemory.artifacts.service import todo_key
from workmemory.companion.state import emit
from workmemory.db.models import (
    AgentRun,
    ChatMessage,
    Conversation,
    DerivedArtifact,
    Episode,
    MemoryCandidate,
    Operation,
    Project,
    ProjectAlias,
    Reminder,
    Todo,
    TodoSource,
    TodoTombstone,
    WorkSession,
)
from workmemory.evidence.service import (
    copy_dependencies,
    dependencies,
    fingerprint,
    index_episode,
    invalidate_episode,
    invalidate_memory,
    invalidate_object,
    remember,
    resolve,
    valid_dependencies,
)
from workmemory.jobs.queue import now
from workmemory.memory.sync import schedule_sync


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "todo.create",
        "todo.update",
        "todo.delete",
        "todo.delete_many",
        "todo.merge",
        "todo.complete_occurrence",
        "todo.snooze",
        "memory.create",
        "memory.update",
        "memory.forget",
        "project.delete",
        "project.delete_many",
        "project.alias",
        "project.merge",
        "episode.move",
        "episode.correct",
        "episode.delete",
        "session.update",
        "review.save",
    ]
    target_id: uuid.UUID | None = Field(default=None, description="要修改的对象ID；合并时为保留的目标：把A并入B，target_id必须是B。")
    other_id: uuid.UUID | None = Field(default=None, description="仅用于合并：被并入、合并后消失的来源对象A；target_id是保留的B。")
    target_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=1000)
    reminder_id: uuid.UUID | None = Field(default=None, description="完成本次或延后10分钟时必填，从待办reminders读取；仅传action、target_id、reminder_id、reminder_revision。")
    reminder_revision: int | None = Field(default=None, ge=1)
    all_todos: Literal[True] | None = None
    all_projects: Literal[True] | None = None
    text: str | None = Field(default=None, min_length=1, max_length=6000, description="仅在创建或用户要求改正文/标题时传入；复制原始text，不能包含display_text的状态后缀。session.update的text仅用于标题，分类必须单独传category，不要把分类表达拼进标题；review.save用于人工正文。用户提供要保存的正文时逐字保留，包括句末标点；未要求润色不要删改。")
    project_id: uuid.UUID | None = Field(default=None, description="仅在用户明确指定项目时传入；不要从另一条待办推断新待办的项目。")
    done: bool | None = None
    dismissed: bool | None = None
    due_at: AwareDatetime | None = Field(default=None, description="带时区的下一次提醒时间，必须是未来时间。todo.create必须提供；未给具体时间先询问。不能传null取消，暂停用paused=true。")
    time_expression: str | None = Field(default=None, min_length=1, max_length=200, description="创建或改期时，逐字引用用户实际说出的具体时间，如明天下午三点。不能引用自己回复、当前系统时间或工作记录；用户未说具体时间则先追问，不调用提案。")
    timezone: str | None = Field(default=None, description="日程IANA时区；默认应用时区。")
    repeat: Literal["once", "daily", "weekdays", "weekly"] | None = Field(default=None, description="一次性/每天/周一至周五/每周指定星期。创建默认once。")
    weekdays: list[int] | None = Field(default=None, description="仅weekly使用，1=周一，7=周日；首次due_at必须在所选星期内。")
    paused: bool | None = Field(default=None, description="暂停整个日程；恢复用false，若旧时间已过须提供新的未来due_at。")
    notify_pet: bool | None = Field(default=None, description="是否由小猫气泡提醒，创建默认true；确认后启用小猫提醒通道。")
    category: str | None = Field(default=None, min_length=1, max_length=50, description="仅用于session.update，工作段的独立分类字段。用户同时给出标题和分类时，标题写text，分类写category；末尾的‘XX类’通常是在指定分类XX，不是标题后缀。明确给出类别名称时按原文；未请求改分类则省略。")


MODELS = {
    "todo": Todo,
    "memory": MemoryCandidate,
    "project": Project,
    "episode": Episode,
    "session": WorkSession,
    "review": DerivedArtifact,
}
LABELS = {
    "todo.create": "创建待办",
    "todo.update": "修改待办",
    "todo.delete": "删除待办",
    "todo.delete_many": "批量删除待办",
    "todo.merge": "合并待办",
    "todo.complete_occurrence": "完成本次提醒",
    "todo.snooze": "10分钟后再提醒",
    "memory.create": "记住这条信息",
    "memory.update": "修改长期记忆",
    "memory.forget": "忘记这条信息",
    "project.delete": "删除项目",
    "project.delete_many": "批量删除项目",
    "project.alias": "添加项目别名",
    "project.merge": "合并项目",
    "episode.move": "调整项目归属",
    "episode.correct": "纠正记录",
    "episode.delete": "删除记录",
    "session.update": "修改工作段",
    "review.save": "保存回顾编辑",
}
FIELDS = {
    "todo.create": {"text", "project_id", "due_at", "timezone", "repeat", "weekdays", "paused", "notify_pet"},
    "todo.update": {"target_id", "text", "done", "dismissed", "due_at", "timezone", "repeat", "weekdays", "paused", "notify_pet"},
    "todo.merge": {"target_id", "other_id"},
    "todo.complete_occurrence": {"target_id", "reminder_id", "reminder_revision"},
    "todo.snooze": {"target_id", "reminder_id", "reminder_revision"},
    "todo.delete": {"target_id"},
    "todo.delete_many": {"target_ids", "all_todos", "project_id", "done"},
    "memory.create": {"text"},
    "memory.update": {"target_id", "text"},
    "memory.forget": {"target_id"},
    "project.delete": {"target_id"},
    "project.delete_many": {"target_ids", "all_projects"},
    "project.alias": {"target_id", "text"},
    "project.merge": {"target_id", "other_id"},
    "episode.move": {"target_id", "project_id"},
    "episode.correct": {"target_id", "text"},
    "episode.delete": {"target_id"},
    "session.update": {"target_id", "text", "category"},
    "review.save": {"target_id", "text"},
}


def validate(body):
    keys = body.model_fields_set - {"action"}
    unsupported = keys - FIELDS[body.action]
    if unsupported:
        detail = f"{body.action} 不接受字段：{', '.join(sorted(unsupported))}。允许字段：action, {', '.join(sorted(FIELDS[body.action]))}。"
        if body.action in {"todo.complete_occurrence", "todo.snooze"}:
            detail += "请仅传 action、target_id、reminder_id、reminder_revision。延后10分钟由服务在确认时计算，不要传 due_at、timezone、time_expression；完成本次不要传 done。"
        raise HTTPException(422, detail)
    if body.action == "todo.delete_many":
        has_filter = body.project_id is not None or body.done is not None
        if sum([bool(body.target_ids), body.all_todos is True, has_filter]) != 1:
            raise HTTPException(
                422, "请明确指定全部待办、项目/完成状态筛选，或待办ID列表，且不要混用"
            )
    elif body.action == "project.delete_many":
        if sum([bool(body.target_ids), body.all_projects is True]) != 1:
            raise HTTPException(422, "请明确指定全部项目或项目ID列表，且不要混用")
    elif not body.action.endswith(".create") and not body.target_id:
        raise HTTPException(422, "请指定操作对象")
    if (
        body.action
        in {
            "todo.create",
            "memory.create",
            "memory.update",
            "project.alias",
            "episode.correct",
            "review.save",
        }
        and not body.text
    ):
        raise HTTPException(422, "内容不能为空")
    if body.action.endswith(".merge") and (not body.other_id or body.other_id == body.target_id):
        raise HTTPException(422, "请选择两个不同的对象")
    if body.action.startswith("todo") and body.text and len(body.text) > 1000:
        raise HTTPException(422, "待办内容过长")
    if body.action in {"todo.complete_occurrence", "todo.snooze"} and (not body.reminder_id or not body.reminder_revision):
        raise HTTPException(422, "请读取当前提醒并提供提醒ID和版本")
    if body.action == "todo.create":
        from workmemory.config import get_settings
        from workmemory.reminders.schedule import validate_schedule
        validate_schedule(body.model_dump(exclude_unset=True), clock=now(), default_timezone=get_settings().timezone)
    if body.action == "todo.update" and "due_at" in keys and body.due_at is None:
        raise HTTPException(422, "不能清空提醒时间，暂停提醒请使用 paused=true")
    if body.action == "project.alias" and len(body.text) > 200:
        raise HTTPException(422, "项目别名过长")


def snapshot(row):
    keys = (
        "id",
        "name",
        "text",
        "summary",
        "revision",
        "edit_revision",
        "status",
        "state",
        "project_id",
        "episode_id",
        "done",
        "dismissed",
        "confirmed",
        "due_at",
        "timezone", "repeat", "weekdays", "paused", "notify_pet", "schedule_revision",
        "title",
        "category",
        "deleted_at",
        "user_text",
        "source_hash",
        "requested_hash",
        "needs_review",
    )
    return {
        k: str(getattr(row, k))
        if isinstance(getattr(row, k), uuid.UUID)
        else getattr(row, k).isoformat()
        if hasattr(getattr(row, k), "isoformat")
        else getattr(row, k)
        for k in keys
        if hasattr(row, k)
    }


async def targets(session, body, lock=False):
    ids = []
    if body.action == "todo.delete_many":
        if body.target_ids:
            ids.extend(("todo", identifier) for identifier in set(body.target_ids))
        else:
            conditions = [
                Todo.dismissed.is_(False),
                Todo.episode_id.is_(None)
                | Todo.episode_id.in_(select(Episode.id).where(Episode.deleted_at.is_(None))),
            ]
            if body.project_id is not None:
                conditions.append(Todo.project_id == body.project_id)
            if body.done is not None:
                conditions.append(Todo.done == body.done)
            selected = (
                await session.scalars(
                    select(Todo.id).where(*conditions).order_by(Todo.id).limit(1001)
                )
            ).all()
            if len(selected) > 1000:
                raise HTTPException(422, "单张批量操作卡片最多1000条，请按项目或完成状态缩小范围")
            ids.extend(("todo", identifier) for identifier in selected)
        if not ids:
            raise HTTPException(409, "没有匹配的有效待办，无需删除")
    if body.action == "project.delete_many":
        selected = sorted(set(body.target_ids)) if body.target_ids else (await session.scalars(
            select(Project.id).where(Project.state.notin_(["merged", "deleted"])).order_by(Project.id).limit(1001)
        )).all()
        if len(selected) > 1000:
            raise HTTPException(422, "单张批量操作卡片最多1000个项目，请指定项目ID列表缩小范围")
        if not selected:
            raise HTTPException(409, "没有可删除的项目，无需确认")
        ids.extend(("project", identifier) for identifier in selected)
    if body.target_id:
        ids.append((body.action.split(".")[0], body.target_id))
    if body.other_id:
        ids.append((body.action.split(".")[0], body.other_id))
    if body.project_id:
        ids.append(("project", body.project_id))
    rows = {}
    for kind, identifier in sorted(set(ids), key=str):
        model = MODELS[kind]
        stmt = select(model).where(model.id == identifier)
        if lock:
            stmt = stmt.with_for_update()
        row = await session.scalar(stmt.execution_options(populate_existing=True))
        if (
            not row
            or getattr(row, "deleted_at", None)
            or getattr(row, "state", None) in {"merged", "deleted"}
            or body.action == "todo.delete_many"
            and isinstance(row, Todo)
            and row.dismissed
            or isinstance(row, MemoryCandidate)
            and row.status != "confirmed"
        ):
            raise HTTPException(409, "操作对象已失效，请重新选择")
        rows[identifier] = row
    data = [snapshot(rows[k]) for k in sorted(rows, key=str)]
    if body.action in {"project.delete", "project.delete_many"}:
        from workmemory.workspace.project_deletion import impact
        linked, aliases = await impact(session, list(rows.values()))
        for item in data:
            identifier = uuid.UUID(item["id"])
            item.update(linked=linked[identifier], aliases=aliases[identifier],
                        episode_count=len(linked[identifier]["episode"]),
                        todo_count=len(linked[identifier]["todo"]),
                        memory_count=len(linked[identifier]["memory"]))
    if body.action in {"episode.delete", "episode.correct"}:
        from workmemory.evidence.confirmed import affected_memories
        data.append({"confirmed_memories": await affected_memories(session, body.target_id)})
    # Project merge must detect records added or edited after the preview, not only its name.
    if body.action == "project.merge":
        episodes = (
            await session.execute(
                select(Episode.id, Episode.revision)
                .where(
                    Episode.project_id.in_([body.target_id, body.other_id]),
                    Episode.deleted_at.is_(None),
                )
                .order_by(Episode.id)
            )
        ).all()
        data.append({"records": [(str(i), rev) for i, rev in episodes]})
    if body.action in {"todo.complete_occurrence", "todo.snooze"}:
        query = select(Reminder).where(Reminder.id == body.reminder_id)
        reminder = await session.scalar(query.with_for_update() if lock else query)
        todo = rows[body.target_id]
        if not reminder or reminder.todo_id != todo.id or reminder.revision != body.reminder_revision or reminder.status != "pending" or reminder.schedule_revision != todo.schedule_revision or reminder.due_at > now():
            raise HTTPException(409, "本次提醒已经变化，请重新查询")
        data.append({"reminder_id": str(reminder.id), "reminder_revision": reminder.revision, "status": reminder.status})
    return rows, data, fingerprint([json.dumps(data, sort_keys=True, ensure_ascii=False)])


async def propose(session, conversation_id, body, run_id=None):
    if run_id and (body.action == "todo.create" or body.action == "todo.update" and "due_at" in body.model_fields_set):
        from workmemory.reminders.time_source import verify_time_source
        await verify_time_source(session, run_id, body.time_expression)
    # Temporal quotation is input evidence, not a field to mutate on the task.
    body = ProposalInput.model_validate(body.model_dump(exclude_unset=True, exclude={"time_expression"}))
    validate(body)
    conversation = await session.get(Conversation, conversation_id)
    if not conversation or conversation.expired:
        raise HTTPException(404, "会话不存在")
    rows, before, target_hash = await targets(session, body)
    if body.action.startswith("todo."):
        from workmemory.config import get_settings
        from workmemory.reminders.schedule import SCHEDULE_FIELDS, validate_schedule
        if body.action == "todo.create":
            defaults = validate_schedule(body.model_dump(exclude_unset=True), clock=now(), default_timezone=get_settings().timezone)
            body = body.model_copy(update={k: v for k, v in defaults.items() if k != "local_time"})
        elif body.action == "todo.update" and ((body.model_fields_set & (SCHEDULE_FIELDS - {"notify_pet", "paused"})) or body.paused is False or body.done is False):
            validate_schedule(body.model_dump(exclude_unset=True), current=rows[body.target_id], clock=now())
    payload = body.model_dump(mode="json", exclude_unset=True)
    count = sum(isinstance(row, Project if body.action.startswith("project.") else Todo) for row in rows.values())
    if body.action == "todo.delete_many":
        # Freeze selection at proposal time. New matching rows can never silently join it.
        payload = {
            "action": body.action,
            "target_ids": sorted(str(row.id) for row in rows.values() if isinstance(row, Todo)),
        }
        before = [
            snapshot(row)
            for row in sorted(rows.values(), key=lambda row: str(row.id))
            if isinstance(row, Todo)
        ]
        target_hash = fingerprint([json.dumps(before, sort_keys=True, ensure_ascii=False)])
    if body.action == "project.delete_many":
        payload = {"action": body.action, "target_ids": sorted(str(row.id) for row in rows.values())}
    # A model repeating the same proposal in one turn must not create duplicate confirmation cards.
    if run_id:
        duplicate = await session.scalar(
            select(Operation).where(
                Operation.run_id == run_id,
                Operation.payload == payload,
                Operation.status == "pending",
            )
        )
        if duplicate:
            return view(duplicate)
    label = LABELS[body.action]
    if body.action == "todo.delete_many":
        label = f"批量删除 {count} 条待办"
    elif body.action == "project.delete_many":
        label = f"批量删除 {count} 个项目"
    elif body.action in {"project.merge", "todo.merge"}:
        field = "name" if body.action == "project.merge" else "text"
        label = f"将「{getattr(rows[body.other_id], field)}」并入「{getattr(rows[body.target_id], field)}」，保留后者"
    operation = Operation(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        run_id=run_id,
        action=body.action,
        payload=payload,
        target_hash=target_hash,
        status="pending",
        revision=1,
        expires_at=now() + timedelta(hours=24),
        preview={
            "label": label,
            "count": count if body.action in {"todo.delete_many", "project.delete_many"} else None,
            "before": before,
            "after": payload,
            "notice": (
                "确认后删除清单中的项目分类及别名，原始工作记录、待办提醒和确认记忆保留并解除项目归属。关联报告和旧引用会失效；之后新增的项目不受影响。"
                if body.action in {"project.delete", "project.delete_many"}
                else f"确认后永久删除清单内 {count} 条待办、关联提醒和派生引用，不可撤销。含清单内的已完成项，不含已忽略项。原始工作记录保留，阻止旧记录自动重建；之后新增的待办不受影响。"
                if body.action == "todo.delete_many"
                else "确认后永久删除这条待办、提醒和派生引用，不可撤销。原始工作记录保留，并阻止旧记录自动重建这条待办。"
                if body.action == "todo.delete"
                else "确认后执行；来源的删除和纠正会使相关回答、报告及待确认操作失效。"
            ),
        },
    )
    session.add(operation)
    await session.flush()
    if run_id:
        await copy_dependencies(session, "run", run_id, "operation", operation.id)
        run = await session.get(AgentRun, run_id)
        source = await resolve(session, f"chat:{run.message_id}")
        if source:
            await remember(session, "operation", operation.id, [source])
    for row in rows.values():
        sid = (
            f"summary:{row.id}"
            if isinstance(row, Episode)
            else f"memory:{row.id}"
            if isinstance(row, MemoryCandidate)
            else f"todo:{row.id}"
            if isinstance(row, Todo)
            else f"project:{row.id}"
            if isinstance(row, Project)
            else f"session:{row.id}"
            if isinstance(row, WorkSession)
            else f"review:{row.id}"
            if isinstance(row, DerivedArtifact)
            else None
        )
        source = await resolve(session, sid) if sid else None
        if body.action == "todo.delete_many" and isinstance(row, Todo) and not source:
            raise HTTPException(409, "待办来源已变化，请重新生成清单")
        if source:
            await remember(session, "operation", operation.id, [source])
        if isinstance(row, Todo) and row.episode_id:
            if source := await resolve(session, f"summary:{row.episode_id}"):
                await remember(session, "operation", operation.id, [source])
        if isinstance(row, DerivedArtifact):
            await copy_dependencies(session, "artifact", row.id, "operation", operation.id)
    await emit(session, "operation", conversation_id)
    return view(operation)


def view(row):
    expired = row.status == "pending" and row.expires_at <= now()
    return {
        "id": str(row.id),
        "conversation_id": str(row.conversation_id) if row.conversation_id else None,
        "action": row.action,
        "status": "expired" if expired else row.status,
        "revision": row.revision,
        "preview": row.preview if not expired and row.status == "pending" else None,
        "result": row.result if row.status == "executed" else None,
        "expires_at": row.expires_at.isoformat(),
    }


async def move_episode(session, episode, project_id):
    # Reclassifying retains original text and facts while invalidating all old context.
    from workmemory.db.models import WorkFact

    facts = (await session.scalars(select(WorkFact).where(WorkFact.episode_id == episode.id))).all()
    segment = await session.scalar(select(WorkSession).where(WorkSession.episode_id == episode.id))
    fact_data = [(f.kind, f.text, f.state, f.observed_at, f.provenance) for f in facts]
    segment_data = (
        (segment.title, segment.started_at, segment.ended_at, segment.category) if segment else None
    )
    await invalidate_episode(session, episode.id)
    episode.project_id, episode.project_hint = project_id, None
    episode.revision += 1
    for kind, text, state, at, provenance in fact_data:
        session.add(
            WorkFact(
                episode_id=episode.id,
                revision=episode.revision,
                kind=kind,
                text=text,
                state=state,
                observed_at=at,
                provenance={**provenance, "revision": episode.revision} if provenance else {},
            )
        )
    if segment:
        segment.revision = episode.revision
    elif segment_data:
        title, start, end, category = segment_data
        session.add(
            WorkSession(
                episode_id=episode.id,
                revision=episode.revision,
                title=title,
                started_at=start,
                ended_at=end,
                category=category,
            )
        )
    await session.execute(
        update(TodoSource)
        .where(TodoSource.episode_id == episode.id)
        .values(revision=episode.revision)
    )
    for todo in (await session.scalars(select(Todo).where(Todo.episode_id == episode.id))).all():
        await invalidate_object(session, "todo", todo.id)
        todo.project_id, todo.normalized_key = project_id, todo_key(todo.text, project_id)
        todo.revision += 1
    await index_episode(session, episode)
    await schedule_sync(session, episode, all_used=True)


async def execute(session, operation, body, rows):
    row = rows.get(body.target_id)
    action = body.action
    if row and body.action not in {"todo.complete_occurrence", "todo.snooze"} and body.action.split(".")[0] in {"todo", "project", "session", "review"}:
        await invalidate_object(session, body.action.split(".")[0], row.id)
    if body.other_id:
        await invalidate_object(session, body.action.split(".")[0], body.other_id)
    if action == "todo.create":
        from workmemory.reminders.schedule import validate_schedule
        schedule = validate_schedule(body.model_dump(exclude_unset=True), clock=now())
        from workmemory.artifacts.service import todo_text_key

        await session.execute(
            delete(TodoTombstone).where(
                TodoTombstone.text_hash == todo_text_key(body.text),
                TodoTombstone.project_id == body.project_id,
            )
        )
        row = Todo(
            id=uuid.uuid4(),
            text=body.text,
            confirmed=True,
            project_id=body.project_id,
            **schedule, origin="manual", conversation_id=operation.conversation_id,
            normalized_key=todo_key(body.text, body.project_id),
        )
        session.add(row)
        if row.notify_pet:
            from workmemory.companion.state import save_preferences
            await save_preferences(session, {"reminders_enabled": True})
    elif action in {"todo.complete_occurrence", "todo.snooze"}:
        from workmemory.reminders.scheduler import act
        await act(session, body.reminder_id, body.reminder_revision, "snooze" if action == "todo.snooze" else "complete")
    elif action in {"project.delete", "project.delete_many"}:
        from workmemory.workspace.project_deletion import remove_project
        for item in sorted(rows.values(), key=lambda item: str(item.id)):
            await remove_project(session, item)
        await session.flush()
        return {"label": f"批量删除 {len(rows)} 个项目" if action.endswith("delete_many") else "删除项目",
                "count": len(rows), "completed": True}
    elif action == "todo.delete_many":
        from workmemory.workspace.service import remove_todo

        for item in sorted(rows.values(), key=lambda item: str(item.id)):
            await remove_todo(session, item)
        await session.flush()
        return {"label": f"批量删除 {len(rows)} 条待办", "count": len(rows), "completed": True}
    elif action == "todo.delete":
        from workmemory.workspace.service import remove_todo

        await remove_todo(session, row)
    elif action == "todo.update":
        from workmemory.workspace.service import update_todo

        await update_todo(
            session,
            row,
            {
                key: getattr(body, key)
                for key in ("text", "done", "dismissed", "due_at", "timezone", "repeat", "weekdays", "paused", "notify_pet")
                if key in body.model_fields_set
            },
        )
    elif action == "todo.merge":
        other = rows[body.other_id]
        left_episode = await session.get(Episode, row.episode_id) if row.episode_id else None
        right_episode = await session.get(Episode, other.episode_id) if other.episode_id else None
        left_project = row.project_id or (left_episode.project_id if left_episode else None)
        right_project = other.project_id or (right_episode.project_id if right_episode else None)
        if left_project != right_project or any(getattr(row, k) != getattr(other, k) for k in ("due_at", "repeat", "timezone", "weekdays", "paused")):
            raise HTTPException(409, "不同项目或日程的待办不能合并")
        for source in (
            await session.scalars(select(TodoSource).where(TodoSource.todo_id == other.id))
        ).all():
            await session.execute(
                insert(TodoSource)
                .values(todo_id=row.id, episode_id=source.episode_id, revision=source.revision)
                .on_conflict_do_nothing()
            )
        # Keep a dismissed alias to prevent its wording from being extracted again.
        other.dismissed = True
        other.revision += 1
        row.revision += 1
        await session.execute(delete(Reminder).where(Reminder.todo_id == other.id))
    elif action == "memory.create" and operation.origin:
        row = MemoryCandidate(id=uuid.uuid4(), text=body.text, status="confirmed",
            origin=operation.origin, source_episode_id=uuid.UUID(operation.origin["episode_id"]))
        session.add(row)
    elif action == "memory.create":
        message = ChatMessage(
            id=uuid.uuid4(),
            conversation_id=operation.conversation_id,
            role="user",
            content=body.text,
        )
        session.add(message)
        await session.flush()
        row = MemoryCandidate(
            id=uuid.uuid4(),
            conversation_id=operation.conversation_id,
            message_id=message.id,
            text=body.text,
            status="confirmed",
        )
        session.add(row)
    elif action in {"memory.update", "memory.forget"}:
        await invalidate_memory(session, row.id)
        row.text = body.text if action == "memory.update" else None
        row.status = "confirmed" if action == "memory.update" else "dismissed"
        row.revision += 1
    elif action == "project.alias":
        name = body.text.strip()
        existing = await session.get(ProjectAlias, name)
        conflict = await session.scalar(
            select(Project).where(Project.name == name, Project.id != row.id)
        )
        if conflict or existing and existing.project_id != row.id:
            raise HTTPException(409, "这个名称已属于其他项目")
        if not existing:
            session.add(ProjectAlias(name=name, project_id=row.id))
        row.revision += 1
    elif action == "project.merge":
        other = rows[body.other_id]
        await session.execute(
            update(TodoTombstone)
            .where(TodoTombstone.project_id == other.id)
            .values(project_id=row.id)
        )
        for episode in (
            await session.scalars(
                select(Episode)
                .where(Episode.project_id == other.id, Episode.deleted_at.is_(None))
                .order_by(Episode.id)
                .with_for_update()
            )
        ).all():
            await move_episode(session, episode, row.id)
        for todo in (
            await session.scalars(
                select(Todo).where(Todo.project_id == other.id).order_by(Todo.id).with_for_update()
            )
        ).all():
            await invalidate_object(session, "todo", todo.id)
            todo.project_id, todo.revision = row.id, todo.revision + 1
            todo.normalized_key = todo_key(todo.text, row.id)
        await session.execute(
            update(ProjectAlias)
            .where(ProjectAlias.project_id == other.id)
            .values(project_id=row.id)
        )
        await session.execute(
            insert(ProjectAlias)
            .values(name=other.name, project_id=row.id)
            .on_conflict_do_update(index_elements=[ProjectAlias.name], set_={"project_id": row.id})
        )
        other.state, other.revision = "merged", other.revision + 1
        row.revision += 1
    elif action == "episode.move":
        await move_episode(session, row, body.project_id)
    elif action in {"episode.correct", "episode.delete"}:
        from workmemory.episodes.service import mutate_episode

        await mutate_episode(session, row, body.text if action == "episode.correct" else None)
    elif action == "session.update":
        row.edit_revision += 1
        if body.text:
            row.title = body.text[:300]
            row.title_confirmed = True
        if body.category:
            row.category = body.category
    elif action == "review.save":
        row.user_text, row.needs_review = body.text, False
        row.edit_revision += 1
    await session.flush()
    return {"label": LABELS[action], "object_id": str(row.id), "completed": True}


async def decide(runtime, identifier, expected_revision, accept):
    # Source locks before operation locks, same order as invalidation.
    async with runtime.sessions.begin() as session:
        pending = await session.get(Operation, identifier)
        extra_locks = set()
        if pending and pending.action in {"project.delete", "project.delete_many"}:
            from workmemory.workspace.project_deletion import confirmation_locks
            extra_locks = confirmation_locks(pending.preview)
        origin_project_id = (pending.origin or {}).get("project_id") if pending and pending.action == "memory.create" else None
        if origin_project_id:
            extra_locks.add(("project", uuid.UUID(origin_project_id)))
        deps = await dependencies(session, "operation", identifier)
        valid = await valid_dependencies(
            session, deps, lock=True, retention_days=runtime.settings.retention_days, extra_locks=extra_locks
        )
        op = await session.scalar(
            select(Operation).where(Operation.id == identifier).with_for_update().execution_options(populate_existing=True)
        )
        if not op:
            raise HTTPException(404, "操作不存在")
        if op.status != "pending":
            return view(op)
        if op.revision != expected_revision:
            raise HTTPException(409, "操作已更新，请刷新")
        if origin_project_id:
            project = await session.get(Project, uuid.UUID(origin_project_id))
            valid = valid and project is not None and project.state not in {"merged", "deleted"}
        if not valid or op.expires_at <= now():
            op.status, op.payload, op.preview = "invalidated", None, None
        elif not accept:
            op.status, op.payload, op.preview = "rejected", None, None
        else:
            body = ProposalInput.model_validate(op.payload)
            try:
                rows, _, current_hash = await targets(session, body, lock=True)
            except HTTPException:
                rows, current_hash = {}, None
            if current_hash != op.target_hash:
                op.status, op.payload, op.preview = "invalidated", None, None
            else:
                siblings = (await session.scalars(select(Operation).where(
                    Operation.conversation_id == op.conversation_id,
                    Operation.id != op.id, Operation.status == "pending",
                ))).all()
                # Mark first so the action's own invalidation won't erase this execution.
                op.status = "executed"
                op.result = await execute(session, op, body, rows)
                op.payload, op.preview = None, None
                invalidated = [other for other in siblings if other.status == "invalidated"]
                notice = ""
                if invalidated:
                    labels = "、".join(dict.fromkeys(LABELS[other.action] for other in invalidated))
                    notice = f"另有{len(invalidated)}张待确认卡片（{labels}）因本次来源更新而失效，尚未执行。请继续让助手核对并重新提出剩余事项。"
                    op.result = {**op.result, "notice": notice}
                # The previous Pi context is invalidated by an edit. Preserve the new
                # confirmed fact in a receipt with its own current source dependencies.
                # Later edits/deletion must clear this receipt just like any other answer.
                source = None
                if body.action in {"episode.correct", "memory.update", "session.update", "review.save"}:
                    prefix = "summary" if body.action == "episode.correct" else body.action.split(".")[0]
                    source = await resolve(session, f"{prefix}:{body.target_id}", runtime.settings.retention_days)
                if op.conversation_id:
                    receipt = ChatMessage(
                        id=uuid.uuid4(), conversation_id=op.conversation_id, role="assistant",
                        content=f"已执行：{op.result['label']}。" + notice + (
                            f"\n最新记录：{source['content']} [{source['id']}]" if source else ""
                        ),
                        citations=[source["id"]] if source else [],
                    )
                    session.add(receipt)
                    if source:
                        await remember(session, "message", receipt.id, [source])
        op.revision += 1
        await emit(session, "operation", op.conversation_id)
        return view(op)
