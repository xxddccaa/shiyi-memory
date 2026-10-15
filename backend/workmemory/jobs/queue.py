import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, and_, case, cast, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from workmemory.db.models import AppState, Job

LEASE_SECONDS = 90
MAX_ATTEMPTS = 8


def now():
    return datetime.now(UTC)


async def enqueue(session, kind, key, payload, priority=10, available_at=None):
    await session.execute(
        insert(Job)
        .values(
            id=uuid.uuid4(),
            kind=kind,
            dedupe_key=key,
            payload=payload,
            priority=priority,
            available_at=available_at or now(),
        )
        .on_conflict_do_nothing(index_elements=[Job.dedupe_key])
    )


async def enqueue_summary(session, episode_id, revision, available_at):
    statement = insert(Job).values(
        id=uuid.uuid4(),
        kind="summarize",
        dedupe_key=f"summarize:{episode_id}",
        payload={"episode_id": str(episode_id), "revision": revision},
        priority=10,
        available_at=available_at,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[Job.dedupe_key],
            set_={
                "payload": statement.excluded.payload,
                "status": case((Job.status == "running", "running"), else_="pending"),
                "attempts": case((Job.status == "running", Job.attempts), else_=0),
                "available_at": available_at,
                "finished_at": None,
                "last_error": None,
            },
        )
    )


async def claim(sessions, excluded=()):
    async with sessions.begin() as session:
        selected = select(AppState.value["backend"].astext).where(AppState.key == "memory").scalar_subquery()
        # Cleanup remains durable for every used backend. An inactive optional
        # service must not put its entire retry backlog ahead of current work.
        priority = case((and_(Job.kind == "memory", Job.payload["backend"].astext != selected),
                         func.greatest(Job.priority, 40)), else_=Job.priority)
        job = await session.scalar(
            select(Job)
            .where(
                Job.kind.not_in(excluded),
                ~exists(select(AppState.key).where(
                    AppState.key == "backend_health:" + Job.payload["backend"].astext,
                    cast(AppState.value["retry_at"].astext, DateTime(timezone=True)) > func.now(),
                )) ,
                or_(
                    and_(Job.status == "pending", Job.available_at <= func.now()),
                    and_(Job.status == "running", Job.lease_until < func.now()),
                )
            )
            .order_by(priority, Job.available_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return None
        job.status = "running"
        job.attempts += 1
        job.lease_token = uuid.uuid4()
        job.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
        return job


async def heartbeat(sessions, job):
    async with sessions.begin() as session:
        result = await session.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == "running", Job.lease_token == job.lease_token)
            .values(lease_until=now() + timedelta(seconds=LEASE_SECONDS))
        )
        return result.rowcount == 1


async def finish(sessions, job, error=None):
    async with sessions.begin() as session:
        current = await session.scalar(
            select(Job)
            .where(
                Job.id == job.id,
                Job.status == "running",
                Job.lease_token == job.lease_token,
            )
            .with_for_update()
        )
        if current is None:
            return
        if job.kind == "summarize" and current.payload != job.payload:
            current.status = "pending"
            current.attempts = 0
            current.last_error = None
            current.lease_until = current.lease_token = None
            return
        values = dict(lease_until=None, lease_token=None)
        if error and type(error).__name__ == "BackendOffline":
            values.update(status="pending", attempts=max(0, job.attempts - 1), last_error="BackendOffline", available_at=now())
        elif error:
            # Error class only: upstream exception strings may contain captured personal text.
            values.update(
                status="failed" if job.attempts >= MAX_ATTEMPTS else "pending",
                last_error=type(error).__name__,
                available_at=now() + timedelta(seconds=min(300, 2**job.attempts)),
            )
        else:
            values.update(status="done", finished_at=now(), last_error=None)
        await session.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == "running", Job.lease_token == job.lease_token)
            .values(**values)
        )
