"""Authenticated UI management; MCP tools cannot invoke these confirmation/configuration APIs."""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from workmemory.config.generation import agent_limits
from workmemory.mcp.config import ServerConfig
from workmemory.workspace.operations import LABELS
from workmemory.workspace.service import get_object, list_objects, work_day

router = APIRouter(prefix="/api")


@router.get("/workspace/objects")
async def objects(
    request: Request,
    kind: Literal["todo", "project", "memory", "episode", "session", "review"] = "todo",
    q: str = Query("", max_length=300),
    project_id: uuid.UUID | None = None,
    done: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    day: str | None = None,
):
    rt = request.app.state.runtime
    async with rt.sessions() as session:
        value = await list_objects(
            session, kind, q, project_id, done, cursor, limit, rt.settings.retention_days, day, rt.settings.timezone
        )
        for item in value["items"]:
            item.pop("evidence", None)
        return value


@router.get("/workspace/objects/{kind}/{identifier}")
async def object_view(request: Request, kind: str, identifier: uuid.UUID):
    rt = request.app.state.runtime
    async with rt.sessions() as session:
        return await get_object(
            session, kind, identifier, retention_days=rt.settings.retention_days
        )


@router.get("/workspace/day")
async def day_view(request: Request, day: str | None = None, cursor: str | None = None):
    value = await work_day(request.app.state.runtime, day, cursor)
    value.pop("report_sources", None)
    return value


@router.get("/agent/capabilities")
async def capabilities(request: Request):
    manager = request.app.state.runtime.mcp
    tools = await manager.catalog()
    return {
        "tools": [{"name": t["name"], "description": t["description"]} for t in tools],
        "operations": LABELS,
        "services": manager.status,
        "requires_confirmation": True,
        "agent": "pi-coding-agent",
        "coding": {"available": True, "requires_workspace": True, "execution": "native", "confirmation": "autonomous", "sandboxed": False},
        "compaction": {"automatic": True, "manual": True},
        "budget": agent_limits(
            request.app.state.runtime.settings,
            getattr(request.app.state.runtime.ollama, "generation", None),
        ).public(),
    }


@router.get("/mcp/servers")
async def settings(request: Request):
    manager = request.app.state.runtime.mcp
    return {**manager.store.public(), "status": manager.status}


class SaveServer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    server: dict[str, Any]


@router.put("/mcp/servers")
async def save_server(request: Request, body: SaveServer):
    manager = request.app.state.runtime.mcp
    result = manager.store.save(body.expected_revision, server=body.server)
    manager.cache.clear()
    manager.status.clear()
    return result


@router.delete("/mcp/servers/{identifier}")
async def remove_server(request: Request, identifier: str, expected_revision: int = Query(ge=0)):
    manager = request.app.state.runtime.mcp
    result = manager.store.save(expected_revision, remove=identifier)
    manager.cache.clear()
    manager.status.pop(identifier, None)
    return result


class TestServer(BaseModel):
    server: dict[str, Any]


@router.post("/mcp/servers/test")
async def test_server(request: Request, body: TestServer):
    manager = request.app.state.runtime.mcp
    raw = dict(body.server)
    _, servers, _ = manager.store.read()
    existing = next((v for v in servers if v.id == raw.get("id")), None)
    if existing and all(
        raw.get(k, getattr(existing, k)) == getattr(existing, k)
        for k in ["transport", "url", "command", "args"]
    ):
        raw.setdefault("token", existing.token)
        raw.setdefault("env", existing.env)
    try:
        server = ServerConfig.model_validate(raw)
    except ValueError:
        raise HTTPException(422, "MCP 配置无效，请检查地址或程序路径") from None
    return await manager.test(server)


class CityInput(BaseModel):
    expected_revision: int = Field(ge=0)
    city: str | None = Field(default=None, max_length=100)


@router.put("/mcp/city")
async def city(request: Request, body: CityInput):
    return request.app.state.runtime.mcp.store.save(
        body.expected_revision, city=body.city, set_city=True
    )
