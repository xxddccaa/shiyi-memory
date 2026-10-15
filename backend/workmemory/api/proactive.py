"""Authenticated companion channel. Heartbeats and notifications never contain desktop text."""

import uuid
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from workmemory.companion import proactive
from workmemory.companion.proactive_config import STYLE, ProactiveSettings

router = APIRouter(prefix="/api/client/proactive")


class Presence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    available: bool


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["shown", "close", "attach"]
    conversation_id: uuid.UUID | None = None


@router.post("/presence")
async def presence(body: Presence, request: Request):
    return await proactive.heartbeat(request.app.state.runtime, body.available)


@router.get("")
async def current(request: Request):
    return await proactive.current(request.app.state.runtime)


@router.get("/history")
async def history(request: Request, limit: int = Query(30, ge=1, le=200)):
    from workmemory.companion.proactive_history import history

    return await history(request.app.state.runtime, limit)


@router.get("/defaults")
async def defaults():
    return {"style": STYLE}


class Mute(BaseModel):
    scope: Literal["one_hour", "today"] = "one_hour"


@router.post("/mute")
async def mute(request: Request, body: Mute = Mute()):
    return await proactive.mute(request.app.state.runtime, body.scope)


class FeedbackInput(BaseModel):
    preference: Literal["more", "normal", "less"]
    expected_revision: int = Field(ge=0)


@router.post("/{identifier}/feedback")
async def feedback(identifier: uuid.UUID, body: FeedbackInput, request: Request):
    from workmemory.companion.feedback import feedback
    return await feedback(request.app.state.runtime, identifier, body.preference, body.expected_revision)


@router.post("/preview")
async def preview(body: ProactiveSettings, request: Request):
    return await proactive.preview(request.app.state.runtime, body)


@router.post("/{identifier}/action")
async def action(identifier: uuid.UUID, body: Action, request: Request):
    return await proactive.action(
        request.app.state.runtime, identifier, body.action, body.conversation_id
    )
