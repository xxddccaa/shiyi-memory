"""Content-free, commit-ordered notifications and versioned shared preferences."""

from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import delete, event, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from workmemory.companion.proactive_config import ProactiveSettings
from workmemory.db.models import AppState, ClientEvent, Operation
from workmemory.jobs.queue import now

DEFAULTS = {
    "proactive": ProactiveSettings().model_dump(),
    "revision": 0,
    "proactive_revision": 0,
    "recent_conversation_id": None,
    "pet_visible": True,
    "pet_size": 112,
    "pet_skin": "builtin-cat",
    "pet_motion_enabled": True,
    "reminders_enabled": False,
    "hide_fullscreen": True,
    "resource_policy": "standard",
    "resource_capture": False,
    "backfill_enabled": False,
}


async def emit(session, kind="changed", reference=None):
    # Defer event ordering until all domain writes/locks are complete. Holding the event
    # lock during source invalidation would invert lock order in multi-source operations.
    session.info.setdefault("client_events", set()).add(
        (kind, str(reference) if reference else None)
    )


@event.listens_for(Session, "before_commit")
def publish_events(session):
    changes = session.info.pop("client_events", None)
    if changes:
        session.flush()
        session.execute(text("SELECT pg_advisory_xact_lock(738452909)"))
        session.add_all(
            ClientEvent(kind=kind, reference=reference)
            for kind, reference in sorted(changes, key=str)
        )
        session.flush()


@event.listens_for(Session, "after_rollback")
def discard_events(session):
    session.info.pop("client_events", None)


async def preferences(session):
    row = await session.get(AppState, "companion")
    value = {**DEFAULTS, **(row.value if row else {})}
    value["proactive"] = ProactiveSettings.model_validate(value["proactive"]).model_dump()
    return value


async def save_preferences(session, changes, expected_revision=None):
    await session.execute(
        insert(AppState).values(key="companion", value=DEFAULTS).on_conflict_do_nothing()
    )
    row = await session.scalar(
        select(AppState).where(AppState.key == "companion").with_for_update()
    )
    value = {**DEFAULTS, **row.value}
    if expected_revision is not None and expected_revision != value["revision"]:
        raise HTTPException(409, "设置已在另一端更新，请重新加载")
    if any(
        key in changes and changes[key] != value.get(key)
        for key in ("proactive", "pet_visible", "resource_policy")
    ):
        value["proactive_revision"] += 1
    value.update(changes)
    value["revision"] += 1
    row.value = value
    await emit(session, "preferences")
    return value


async def cleanup(session):
    await session.execute(
        delete(ClientEvent).where(ClientEvent.created_at < now() - timedelta(days=7))
    )
    for proposal in (
        await session.scalars(
            select(Operation)
            .where(Operation.status == "pending", Operation.expires_at <= now())
            .with_for_update()
        )
    ).all():
        proposal.status, proposal.payload, proposal.preview = "expired", None, None
        proposal.revision += 1
        await emit(session, "operation", proposal.conversation_id)
