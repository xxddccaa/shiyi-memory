"""Business tool execution, independent of Pi and transport."""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from workmemory.artifacts.service import artifact_sources, request_artifact
from workmemory.db.models import AgentRun
from workmemory.evidence.service import (
    dependencies,
    neighbors,
    remember,
    resolve,
    search,
    valid_dependencies,
)
from workmemory.workspace.operations import ProposalInput, propose
from workmemory.workspace.service import get_object, list_objects, work_day


async def execute(runtime, run_id, name, args):
    if name == "propose_merge":
        from workmemory.workspace.merge import prepare_merge
        return await prepare_merge(runtime, run_id, args)
    supplied = []
    if name == "propose_operation":
        async with runtime.sessions.begin() as session:
            run = await session.get(AgentRun, run_id)
            result = await propose(
                session, run.conversation_id, ProposalInput.model_validate(args), run_id
            )
        proposal = {"id": result["id"], "status": result["status"], "action": result["action"], "label": (result.get("preview") or {}).get("label")}
        if (result.get("preview") or {}).get("count") is not None:
            proposal["count"] = result["preview"]["count"]
        return {"proposal": proposal, "notice": "待用户确认，尚未执行。"}
    if name == "search_memories":

        def when(key):
            if not args.get(key):
                return None
            value = datetime.fromisoformat(args[key])
            return (
                value if value.tzinfo else value.replace(tzinfo=ZoneInfo(runtime.settings.timezone))
            )

        data = await search(
            runtime,
            args["query"],
            limit=5,
            project_id=uuid.UUID(args["project_id"]) if args.get("project_id") else None,
            app=args.get("app"),
            from_at=when("from"),
            to_at=when("to"),
        )
        supplied = data["items"]
    elif name == "get_neighbors":
        data = await neighbors(runtime, uuid.UUID(args["episode_id"]))
        supplied = data["items"]
    elif name == "get_work_day":
        if args.get("prepare_report"):
            day = (
                args.get("day")
                or datetime.now(ZoneInfo(runtime.settings.timezone)).date().isoformat()
            )
            await request_artifact(runtime, "daily", day)
        data = await work_day(runtime, args.get("day"), args.get("cursor"), args.get("limit", 20))
        supplied = [*data["items"], *data.pop("report_sources")]
    else:
        async with runtime.sessions() as session:
            if name == "list_workspace":
                data = {}
                for kind, key in [
                    ("project", "projects"),
                    ("todo", "todos"),
                    ("memory", "memories"),
                ]:
                    result = await list_objects(
                        session, kind, limit=12, retention_days=runtime.settings.retention_days
                    )
                    data[key] = []
                    for item in result["items"]:
                        supplied.append(item.pop("evidence"))
                        data[key].append(
                            {**item, "name": item["text"]} if kind == "project" else item
                        )
                    data[key + "_next_cursor"] = result["next_cursor"]
                data["notice"] = "需要更多对象时调用 list_objects，支持搜索与分页。"
            elif name == "list_objects":
                data = await list_objects(
                    session, **args, retention_days=runtime.settings.retention_days, timezone=runtime.settings.timezone
                )
                supplied = [item.pop("evidence") for item in data["items"]]
            elif name == "get_object":
                item = await get_object(
                    session, **args, retention_days=runtime.settings.retention_days
                )
                data = {"items": [item]}
                supplied = [item]
            elif name == "get_project_brief":
                items, _, total = await artifact_sources(
                    session, "brief", args["project_id"], runtime.settings.timezone, runtime.settings.retention_days
                )
                data = {"items": items[:6], "total": total, "notice": "当前有效项目概要。"}
                supplied = data["items"]
            elif name == "get_evidence":
                from workmemory.evidence.service import canonical_citation

                known = {
                    d.snippet_id
                    for d in await dependencies(session, "run", run_id)
                    if d.source_kind not in {"chat", "external"}
                }
                args = {**args, "ids": [canonical_citation(sid, known) for sid in args["ids"]]}
                values = [
                    await resolve(session, sid, runtime.settings.retention_days)
                    if not sid.startswith(("chat:", "external:"))
                    else None
                    for sid in args["ids"][:8]
                ]
                data = {
                    "items": [v for v in values if v],
                    "missing": [sid for sid, item in zip(args["ids"], values) if not item],
                }
                supplied = data["items"]
            else:
                raise HTTPException(404, "未知工具")
    async with runtime.sessions.begin() as session:
        await remember(session, "run", run_id, supplied)
        if not await valid_dependencies(
            session,
            await dependencies(session, "run", run_id),
            lock=True,
            retention_days=runtime.settings.retention_days,
        ):
            raise HTTPException(409, "来源已失效")
    return data
