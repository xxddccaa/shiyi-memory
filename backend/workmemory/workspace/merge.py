"""Resolve explicit merge names and propose the existing confirmed transaction."""

import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from workmemory.db.models import AgentRun, Episode, Project, Todo
from workmemory.evidence.service import dependencies, remember, valid_dependencies
from workmemory.workspace.operations import ProposalInput, propose
from workmemory.workspace.service import get_object


class MergeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["todo", "project"]
    source_name: str = Field(
        min_length=1,
        max_length=1000,
        description="被并入的现有对象完整名称A；如把A并到B，这里填A。名称中的整理/编写等动词也是标题的一部分，不是要执行的工作。",
    )
    target_name: str = Field(
        min_length=1,
        max_length=1000,
        description="合并后保留的现有对象完整名称B。不会改写这个标题。",
    )
    source_project_id: uuid.UUID | None = Field(
        default=None,
        description="同名待办需要区分时，填来源待办的所属项目ID。",
    )
    target_project_id: uuid.UUID | None = Field(
        default=None,
        description="同名待办需要区分时，填保留待办的所属项目ID。",
    )


def error(code, message, **details):
    return {"error": {"code": code, "message": message}, **details}


async def prepare_merge(runtime, run_id, arguments):
    body = MergeInput.model_validate(arguments)
    if body.kind == "project" and (body.source_project_id or body.target_project_id):
        return error("invalid_scope", "项目合并不使用待办的所属项目筛选。")
    model = Todo if body.kind == "todo" else Project
    field = model.text if body.kind == "todo" else model.name
    matches, supplied = {}, []
    async with runtime.sessions.begin() as session:
        for side, name, project_id in [
            ("source", body.source_name, body.source_project_id),
            ("target", body.target_name, body.target_project_id),
        ]:
            statement = select(model).where(field == name)
            if body.kind == "todo":
                statement = statement.where(Todo.dismissed.is_(False))
                if project_id:
                    statement = statement.outerjoin(Episode, Todo.episode_id == Episode.id).where(
                        func.coalesce(Todo.project_id, Episode.project_id) == project_id
                    )
            else:
                statement = statement.where(Project.state.notin_(["merged", "deleted"]))
            rows = (await session.scalars(statement.order_by(model.id).limit(51))).all()
            if len(rows) > 50:
                return error("ambiguous_target", "同名对象过多，请先用所属项目缩小范围。")
            matches[side] = []
            for row in rows:
                try:
                    evidence = await get_object(
                        session, body.kind, row.id, retention_days=runtime.settings.retention_days
                    )
                except HTTPException as exc:
                    if exc.status_code not in {404, 409}:
                        raise
                    continue
                supplied.append(evidence)
                effective_project = None
                if body.kind == "todo":
                    episode = await session.get(Episode, row.episode_id) if row.episode_id else None
                    effective_project = row.project_id or (episode.project_id if episode else None)
                project = (
                    await session.get(Project, effective_project) if effective_project else None
                )
                matches[side].append(
                    {
                        "id": str(row.id),
                        "name": name,
                        "project_name": project.name if project else None,
                        "project_id": str(project.id) if project else None,
                    }
                )
        await remember(session, "run", run_id, supplied)
        if not await valid_dependencies(
            session,
            await dependencies(session, "run", run_id),
            lock=True,
            retention_days=runtime.settings.retention_days,
        ):
            return error("source_invalidated", "对象来源已变化，请重新查询。")
        if any(len(values) != 1 for values in matches.values()):
            return error(
                "merge_target_unresolved",
                "合并需要两条明确的现有对象。未匹配时请查询现有名称；同名时向用户澄清所属项目并结束本轮。尚未生成卡片，不用改标题或创建新对象代替合并。",
                matches=matches,
            )
        source, target = matches["source"][0], matches["target"][0]
        if source["id"] == target["id"]:
            return error("same_target", "这是同一对象，无需合并。请指定另一条现有对象。")
        if body.kind == "todo":
            rows = [await session.get(Todo, uuid.UUID(item["id"])) for item in (source, target)]
            if source["project_id"] != target["project_id"] or rows[0].due_at != rows[1].due_at:
                return error(
                    "incompatible_targets", "不同项目或截止时间的待办不能合并，尚未生成卡片。"
                )
        run = await session.get(AgentRun, run_id)
        if not run or run.status not in {"running", "queued"}:
            raise HTTPException(409, "运行已结束或取消")
        result = await propose(
            session,
            run.conversation_id,
            ProposalInput(
                action=body.kind + ".merge",
                target_id=uuid.UUID(target["id"]),
                other_id=uuid.UUID(source["id"]),
            ),
            run_id,
        )
        return {
            "proposal": {
                "id": result["id"],
                "status": result["status"],
                "action": result["action"],
                "label": (result.get("preview") or {}).get("label"),
            },
            "source": source,
            "target": target,
            "notice": "已提出合并现有对象的卡片，保留target，source被并入；必须等待用户在界面确认，尚未执行。",
        }
