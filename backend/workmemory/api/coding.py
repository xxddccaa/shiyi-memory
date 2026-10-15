"""Authenticated UI control plane; never exposed as self-approval tools."""
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from workmemory.agent.coding import local_directory, output_page, purge_session
from workmemory.db.models import (
    AgentEvent,
    AgentRun,
    AgentToolCall,
    AgentWorkspace,
    ChatMessage,
    Conversation,
    PiSession,
)
from workmemory.evidence.service import dependencies, valid_dependencies
from workmemory.jobs.queue import now

router = APIRouter(prefix="/api")


class WorkspaceInput(BaseModel):
    path: str = Field(min_length=1)
    name: str = Field(default="", max_length=200)
    extensions: list[str] = Field(default_factory=list)
    expected_revision: int | None = None


def workspace_view(row):
    return {"id": str(row.id), "name": row.name, "path": row.path, "revision": row.revision, "extensions": row.extensions}


def extension_paths(paths):
    result = []
    for value in paths:
        path = Path(value).expanduser()
        if not path.is_absolute() or not path.is_file() or path.suffix not in {".js", ".mjs", ".ts"}:
            raise HTTPException(422, "代码扩展必须指定可访问的本地 JS／TS 文件绝对路径")
        result.append(str(path.resolve()))
    return result


@router.get("/agent/workspaces")
async def workspaces(request: Request):
    async with request.app.state.runtime.sessions() as session:
        rows = (await session.scalars(select(AgentWorkspace).order_by(AgentWorkspace.name))).all()
        return {"items": [workspace_view(row) for row in rows]}


@router.post("/agent/workspaces", status_code=201)
async def add_workspace(body: WorkspaceInput, request: Request):
    path = local_directory(body.path)
    async with request.app.state.runtime.sessions.begin() as session:
        row = await session.scalar(select(AgentWorkspace).where(AgentWorkspace.path == path))
        if row:
            return workspace_view(row)
        row = AgentWorkspace(path=path, name=body.name or Path(path).name, extensions=extension_paths(body.extensions))
        session.add(row)
        await session.flush()
        return workspace_view(row)


@router.patch("/agent/workspaces/{identifier}")
async def edit_workspace(identifier: uuid.UUID, body: WorkspaceInput, request: Request):
    async with request.app.state.runtime.sessions.begin() as session:
        row = await session.scalar(select(AgentWorkspace).where(AgentWorkspace.id == identifier).with_for_update())
        if not row:
            raise HTTPException(404, "工作目录不存在")
        if row.revision != body.expected_revision:
            raise HTTPException(409, "工作目录设置已更新，请刷新")
        if local_directory(body.path) != row.path:
            raise HTTPException(409, "更换路径请添加新工作目录并开启新会话")
        running = await session.scalar(select(AgentRun.id).join(Conversation, AgentRun.conversation_id == Conversation.id)
            .where(Conversation.workspace_id == identifier, AgentRun.status.in_(["queued", "running"])).limit(1))
        if running:
            raise HTTPException(409, "请等待正在运行的任务结束后修改扩展")
        row.name, row.extensions, row.revision = body.name or Path(row.path).name, extension_paths(body.extensions), row.revision + 1
        return workspace_view(row)


@router.delete("/agent/workspaces/{identifier}")
async def remove_workspace(identifier: uuid.UUID, request: Request):
    async with request.app.state.runtime.sessions.begin() as session:
        row = await session.scalar(select(AgentWorkspace).where(AgentWorkspace.id == identifier).with_for_update())
        if not row:
            raise HTTPException(404, "工作目录不存在")
        conversations = (await session.scalars(select(Conversation).where(Conversation.workspace_id == identifier).with_for_update())).all()
        for conversation in conversations:
            active = await session.scalar(select(AgentRun.id).where(AgentRun.conversation_id == conversation.id, AgentRun.status.in_(["queued", "running"])))
            if active:
                raise HTTPException(409, "请先停止此目录下正在运行的任务")
            conversation.coding_enabled, conversation.workspace_id = False, None
            conversation.revision += 1
        await session.delete(row)
    return {"removed": True}


async def valid_session(session, identifier, retention_days):
    conversation = await session.get(Conversation, identifier)
    if not conversation or conversation.expired:
        raise HTTPException(404, "会话不存在或已到期")
    row = await session.get(PiSession, identifier)
    if row and not await valid_dependencies(session, await dependencies(session, "pi_session", identifier), retention_days=retention_days):
        await purge_session(session, identifier)
    return row


@router.get("/conversations/{identifier}/context")
async def context_status(identifier: uuid.UUID, request: Request):
    rt = request.app.state.runtime
    async with rt.sessions.begin() as session:
        row = await valid_session(session, identifier, rt.settings.retention_days)
        return {"status": row.status if row else "new", **(row.context if row else {})}


class CompactInput(BaseModel):
    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)


@router.post("/conversations/{identifier}/compact", status_code=202)
async def compact(identifier: uuid.UUID, body: CompactInput, request: Request):
    rt = request.app.state.runtime
    async with rt.sessions.begin() as session:
        conversation = await session.scalar(select(Conversation).where(Conversation.id == identifier).with_for_update())
        if not conversation or conversation.expired:
            raise HTTPException(404, "会话不存在")
        existing = await session.get(AgentRun, body.request_id)
        if existing:
            marker = await session.scalar(select(AgentEvent.id).where(AgentEvent.run_id == existing.id, AgentEvent.kind == "compact_requested"))
            if existing.conversation_id != identifier or not marker:
                raise HTTPException(409, "请求标识已被使用")
            return {"run_id": str(existing.id), "status": existing.status}
        if await session.scalar(select(AgentRun.id).where(AgentRun.conversation_id == identifier, AgentRun.status.in_(["queued", "running"]))):
            raise HTTPException(409, "请等待当前任务结束后压缩")
        source = await session.scalar(select(ChatMessage).where(ChatMessage.conversation_id == identifier, ChatMessage.role == "user",
            ChatMessage.status == "ready", ChatMessage.content.is_not(None), ChatMessage.created_at > now() - timedelta(days=30))
            .order_by(ChatMessage.created_at.desc()).limit(1))
        if not source:
            raise HTTPException(409, "还没有可压缩的有效对话")
        run = AgentRun(id=body.request_id, conversation_id=identifier, message_id=source.id)
        session.add(run)
        await session.flush()
        session.add(AgentEvent(run_id=run.id, kind="compact_requested", data={}))
    rt.agent.launch(body.request_id)
    return {"run_id": str(body.request_id), "status": "queued"}


@router.get("/agent/runs/{identifier}/tool-calls")
async def tool_calls(identifier: uuid.UUID, request: Request, offset: int = Query(0, ge=0)):
    rt = request.app.state.runtime
    async with rt.sessions.begin() as session:
        run = await session.get(AgentRun, identifier)
        if not run:
            raise HTTPException(404, "运行不存在")
        await valid_session(session, run.conversation_id, rt.settings.retention_days)
        rows = (await session.scalars(select(AgentToolCall).where(AgentToolCall.run_id == identifier)
            .order_by(AgentToolCall.created_at, AgentToolCall.id).offset(offset).limit(51))).all()
        return {"items": [{"id": str(row.id), "name": row.name, "arguments": row.arguments, "status": row.status,
            "result": row.result, "side_effect": row.side_effect} for row in rows[:50]], "next_offset": offset + 50 if len(rows) > 50 else None}


@router.get("/agent/tool-calls/{identifier}/output")
async def tool_output(identifier: uuid.UUID, request: Request, offset: int = Query(0, ge=0)):
    rt = request.app.state.runtime
    async with rt.sessions.begin() as session:
        call = await session.get(AgentToolCall, identifier)
        if not call:
            raise HTTPException(404, "执行记录不存在")
        await valid_session(session, call.conversation_id, rt.settings.retention_days)
        # Returning a normal empty result allows an invalidation cleanup to commit.
        if call.status == "invalidated":
            return {"text": "", "next_offset": None, "invalidated": True}
        return await output_page(session, identifier, call.conversation_id, offset)
