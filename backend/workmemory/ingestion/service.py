import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from workmemory.db.models import Activity, AppState, Episode, Observation, Project
from workmemory.evidence.service import index_episode, invalidate_episode
from workmemory.ingestion.dedup import normalize, patch, restore, significant
from workmemory.jobs.queue import enqueue_summary


class Event(BaseModel):
    id: uuid.UUID
    device_id: str = Field(min_length=1, max_length=100)
    captured_at: AwareDatetime
    app: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=1000)
    text: str = Field(min_length=1, max_length=30000)
    method: Literal["ax", "vision", "paddleocr", "manual"] = "ax"
    project_hint: str | None = Field(default=None, max_length=200)

    resource_uri: str | None = Field(default=None, max_length=2000)
    interaction_id: uuid.UUID | None = None
    trigger: (
        Literal["enter", "click", "app_switch", "window_switch", "visual", "timer", "resume"] | None
    ) = None
    phase: Literal["before", "after", "supplement", "snapshot"] | None = None
    window_id: str = Field(default="", max_length=200)
    event_at: AwareDatetime | None = None
    missing_before: bool = False
    missing_after: bool = False
    merged_count: int = Field(default=0, ge=0, le=1000000)
    dropped_groups: int = Field(default=0, ge=0, le=1000000)

    @field_validator("captured_at", "event_at")
    @classmethod
    def not_future(cls, value):
        if value is not None and value > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("capture timestamp is in the future")
        return value


class Batch(BaseModel):
    events: list[Event] = Field(min_length=1, max_length=100)


def allowed(event, settings):
    if event.method != "manual" and ("拾忆" in event.title or event.app.startswith("local.workmemory.")):
        return False
    return event.app not in settings.collector.excluded_apps and not any(
        re.search(pattern, event.title) for pattern in settings.collector.excluded_title_patterns
    )


def episode_key(event, minutes):
    seconds = minutes * 60
    # Keep both sides of an interaction together when its pre-frame crosses a five-minute boundary.
    anchor = event.event_at if event.interaction_id and event.event_at else event.captured_at
    bucket = datetime.fromtimestamp(int(anchor.timestamp()) // seconds * seconds, UTC)
    key = f"{event.device_id}|{bucket.isoformat()}|{event.project_hint or ''}"
    return uuid.uuid5(uuid.NAMESPACE_URL, key), bucket


async def ingest(session, batch, settings):
    from workmemory.companion.state import preferences
    allow_resources = (await preferences(session))["resource_capture"]
    accepted, skipped = [], []
    counters = {"filtered": 0, "unchanged": 0, "retry": 0, "observations": 0}
    # Serialize a device across bucket boundaries too, including concurrent upload retries.
    # Acquire locks in one order; process each device's batch chronologically afterwards.
    for device in sorted({event.device_id for event in batch.events}):
        lock = int.from_bytes(
            hashlib.sha256(("ingest:" + device).encode()).digest()[:8], signed=True
        )
        await session.execute(select(func.pg_advisory_xact_lock(lock)))
    for event in sorted(
        batch.events,
        key=lambda e: (e.device_id, e.captured_at, str(e.id)),
    ):
        text = normalize(event.text)
        if (
            not text
            or not allowed(event, settings)
            or event.captured_at < datetime.now(UTC) - timedelta(days=settings.retention_days)
        ):
            counters["filtered"] += 1
            skipped.append(str(event.id))
            continue
        episode_id, start = episode_key(event, settings.episode_minutes)
        end = start + timedelta(minutes=settings.episode_minutes)
        existing = await session.get(Episode, episode_id)
        if existing and (existing.deleted_at or existing.corrected):
            counters["filtered"] += 1
            skipped.append(str(event.id))
            continue
        if await session.get(Observation, event.id):
            counters["retry"] += 1
            accepted.append(str(event.id))
            continue
        window = event.window_id or event.title[:200]
        if event.trigger == "timer" and event.method != "manual":
            # A quiet desktop must not create another LLM job at every five-minute boundary.
            # Retained business records make this survive collector/API restarts. Do not
            # create empty timeline episodes solely to record unchanged periodic samples.
            prior = await session.scalar(
                select(Observation)
                .join(Episode, Episode.id == Observation.episode_id)
                .where(
                    Episode.device_id == event.device_id,
                    Episode.project_hint == event.project_hint,
                    Observation.app == event.app,
                    Observation.window_id == window,
                    Observation.title == event.title.replace("\x00", ""),
                    Observation.captured_at <= event.captured_at,
                )
                .order_by(Observation.captured_at.desc(), Observation.id.desc())
                .limit(1)
            )
            if prior and prior.episode_id != episode_id and prior.activity_id:
                source = await session.get(Activity, prior.activity_id)
                prior_episode = await session.get(Episode, prior.episode_id)
                if (
                    source
                    and source.text
                    and source.captured_at
                    >= datetime.now(UTC) - timedelta(days=settings.retention_days)
                    and not prior_episode.deleted_at
                    and not prior_episode.corrected
                    and not any(
                        significant(
                            before,
                            text,
                            settings.collector.similarity_threshold,
                            settings.collector.important_patterns,
                        )[0]
                        for before in (source.text, restore(source.text, prior.delta))
                    )
                ):
                    counters["unchanged"] += 1
                    skipped.append(str(event.id))
                    continue
        await session.execute(
            insert(Episode)
            .values(
                id=episode_id,
                device_id=event.device_id,
                started_at=start,
                ended_at=end,
                project_hint=event.project_hint,
            )
            .on_conflict_do_nothing()
        )
        episode = await session.scalar(
            select(Episode).where(Episode.id == episode_id).with_for_update()
        )
        if episode.deleted_at or episode.corrected:
            # Tombstones and user corrections also win over delayed offline events.
            counters["filtered"] += 1
            skipped.append(str(event.id))
            continue
        if await session.get(Observation, event.id):
            counters["retry"] += 1
            accepted.append(str(event.id))
            continue
        if event.project_hint and not episode.project_id:
            name = event.project_hint.strip()
            if name:
                await session.execute(
                    insert(Project).values(id=uuid.uuid4(), name=name).on_conflict_do_nothing()
                )
                project = await session.scalar(select(Project).where(Project.name == name).with_for_update())
                if project and project.state not in {"merged", "deleted"}:
                    episode.project_id = project.id
                else:
                    episode.project_hint = None
        previous = await session.scalar(
            select(Observation)
            .where(
                Observation.episode_id == episode_id,
                Observation.app == event.app,
                Observation.window_id == window,
                Observation.captured_at <= event.captured_at,
            )
            .order_by(Observation.captured_at.desc(), Observation.id.desc())
            .limit(1)
        )
        baseline = (
            await session.get(Activity, previous.activity_id)
            if previous and previous.activity_id
            else None
        )
        meaningful, score, reason = True, 0.0, "new"
        delta = None
        if baseline and baseline.text and event.method != "manual":
            meaningful, score, reason = significant(
                baseline.text,
                text,
                settings.collector.similarity_threshold,
                settings.collector.important_patterns,
            )
            previous_text = restore(baseline.text, previous.delta)
            prev_changed, _, prev_reason = significant(
                previous_text,
                text,
                settings.collector.similarity_threshold,
                settings.collector.important_patterns,
            )
            if prev_changed:
                meaningful, reason = True, prev_reason
        activity_id = baseline.id if baseline else None
        if meaningful:
            digest = hashlib.sha256(f"{event.app}\n{event.title}\n{text}".encode()).hexdigest()
            result = await session.execute(
                insert(Activity)
                .values(
                    id=event.id,
                    episode_id=episode_id,
                    captured_at=event.captured_at,
                    app=event.app,
                    title=event.title.replace("\x00", ""),
                    text=text,
                    content_hash=digest,
                    method=event.method,
                )
                .on_conflict_do_nothing()
                .returning(Activity.id)
            )
            activity_id = result.scalar_one_or_none()
            if activity_id is None:
                activity_id = await session.scalar(
                    select(Activity.id).where(
                        Activity.episode_id == episode_id,
                        Activity.content_hash == digest,
                    )
                )
                # Same event ID from a different episode is not a new source.
                if activity_id is None:
                    accepted.append(str(event.id))
                    continue
                if baseline is None or (baseline.id == activity_id and previous.delta is None):
                    meaningful, reason = False, "identical"
        elif baseline.text != text:
            delta = patch(baseline.text, text)
        counters["observations"] += 1
        if not meaningful:
            counters["unchanged"] += 1
        session.add(
            Observation(
                id=event.id,
                episode_id=episode_id,
                activity_id=activity_id,
                captured_at=event.captured_at,
                app=event.app,
                title=event.title.replace("\x00", ""),
                window_id=window,
                interaction_id=event.interaction_id,
                trigger=event.trigger,
                phase=event.phase,
                event_at=event.event_at,
                method=event.method,
                delta=delta,
                similarity=score,
                meaningful=meaningful,
                reason=reason,
                capture_meta={
                    "missing_before": event.missing_before,
                    "missing_after": event.missing_after,
                    "merged_count": event.merged_count,
                    "dropped_groups": event.dropped_groups,
                    "resource_uri": sanitize_resource(event.resource_uri, event.app) if allow_resources else None,
                },
            )
        )
        if meaningful:
            await invalidate_episode(session, episode.id)
            episode.revision += 1
            episode.status = "pending"
            episode.evidence_state = "pending"
            episode.evidence_policy = 1
            episode.evidence_expires_at = None
            await enqueue_summary(
                session,
                episode.id,
                episode.revision,
                available_at=datetime.now(UTC) if event.method == "manual" else end,
            )
        await session.flush()
        await index_episode(session, episode, settings.retention_days)
        accepted.append(str(event.id))
    await session.execute(insert(AppState).values(key="capture_receipts", value={}).on_conflict_do_nothing())
    receipt = await session.get(AppState, "capture_receipts", with_for_update=True, populate_existing=True)
    receipt.value = {"received_at": datetime.now(UTC).isoformat(),
        "event_at": max(e.captured_at for e in batch.events).isoformat(),
        "last_batch": counters,
        "totals": {k: receipt.value.get("totals", {}).get(k, 0) + v for k, v in counters.items()}}
    # Both lists acknowledge the batch. Excluded text must also leave the local spool.
    return {"accepted": accepted, "skipped": skipped}


def sanitize_resource(value, app):
    from urllib.parse import urlsplit, urlunsplit
    if not value or app not in {"com.apple.Safari", "com.google.Chrome"}:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        return urlunsplit((parsed.scheme, host + (f":{parsed.port}" if parsed.port else ""), parsed.path, "", ""))
    except ValueError:
        return None
