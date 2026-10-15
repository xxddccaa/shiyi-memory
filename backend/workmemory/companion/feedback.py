from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select

from workmemory.companion.proactive import (
    blocked_reason,
    clear,
    config_hash,
    next_time,
    stamp,
    state_row,
)
from workmemory.companion.proactive_config import CADENCE, ProactiveSettings
from workmemory.companion.state import preferences, save_preferences
from workmemory.db.models import ProactiveUtterance
from workmemory.evidence.service import dependencies, remember, resolve, valid_dependencies
from workmemory.jobs.queue import now


async def feedback(runtime, identifier, preference, expected_revision):
    async with runtime.sessions.begin() as session:
        row = await session.get(ProactiveUtterance, identifier)
        if (
            not row
            or not row.content
            or row.status == "invalidated"
            or not row.shown_at
            or row.preview
            or row.trigger != "timer"
            or row.topic not in {"playful", "work", "weather", "break"}
            or row.created_at < now() - timedelta(hours=24)
            or not await valid_dependencies(
                session, await dependencies(session, "proactive", identifier)
            )
        ):
            raise HTTPException(409, "这句陪伴已过期或不能反馈")
        prefs = await preferences(session)
        config = ProactiveSettings.model_validate(prefs["proactive"]).model_dump()
        config["topic_preferences"][row.topic] = preference
        return await save_preferences(session, {"proactive": config}, expected_revision)


async def completed(session, reminder):
    prefs = await preferences(session)
    if not (
        prefs["proactive"]["enabled"]
        and ProactiveSettings.model_validate(prefs["proactive"]).completion_enabled
    ):
        return
    source = await resolve(session, f"todo:{reminder.todo_id}")
    if not source:
        return
    import uuid

    if await session.scalar(
        select(ProactiveUtterance.id).where(ProactiveUtterance.trigger_id == reminder.id)
    ):
        return
    row = ProactiveUtterance(
        id=uuid.uuid4(),
        topic="playful",
        trigger="completion",
        trigger_id=reminder.id,
        status="pending",
        settings_hash=config_hash(prefs),
        expires_at=now() + timedelta(seconds=60),
        context={},
    )
    session.add(row)
    await session.flush()
    await remember(session, "proactive", row.id, [source])


async def completion_tick(runtime):
    from zoneinfo import ZoneInfo

    async with runtime.sessions.begin() as session:
        identifier = await session.scalar(
            select(ProactiveUtterance.id)
            .where(
                ProactiveUtterance.trigger == "completion",
                ProactiveUtterance.status == "pending",
                ProactiveUtterance.created_at <= now() - timedelta(seconds=4),
            )
            .order_by(ProactiveUtterance.created_at)
            .limit(1)
        )
        if not identifier:
            return None
        source_valid = await valid_dependencies(
            session, await dependencies(session, "proactive", identifier), lock=True
        )
        row = await session.scalar(
            select(ProactiveUtterance).where(ProactiveUtterance.id == identifier).with_for_update()
        )
        if not row or row.status != "pending":
            return None
        if not source_valid:
            clear(row, "invalidated", "source_invalidated")
            return None
        prefs = await preferences(session)
        config = ProactiveSettings.model_validate(prefs["proactive"])
        ledger = await state_row(session)
        state = dict(ledger.value)
        day = now().astimezone(ZoneInfo(runtime.settings.timezone)).date().isoformat()
        reason = await blocked_reason(session, prefs, state, now(), runtime.settings.timezone)
        if not config.completion_enabled:
            reason = "disabled"
        if row.expires_at <= now():
            reason = "cancelled"
        last = stamp(state.get("last_completion_at"))
        if (state.get("completion_day") == day and state.get("completion_count", 0) >= 3) or (
            last and now() - last < timedelta(minutes=30)
        ):
            reason = "completion_limit"
        if state.get("day") == day and state.get("count", 0) >= CADENCE[config.frequency][2]:
            reason = "daily_limit"
        if (
            runtime.ollama.current
            or runtime.ollama.pending
            or await session.scalar(
                select(ProactiveUtterance.id)
                .where(
                    ProactiveUtterance.status.in_(["running", "ready"]),
                    ProactiveUtterance.expires_at > now(),
                )
                .limit(1)
            )
        ):
            reason = "model_busy"
        if reason:
            clear(row, "skipped", reason)
            return None
        row.status, row.settings_hash = "running", config_hash(prefs)
        row.context = {"presence_epoch": state.get("epoch", 0)}
        state["next_at"] = next_time(config, now()).isoformat()
        ledger.value = state
        return row.id
