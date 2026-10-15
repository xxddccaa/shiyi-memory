import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from workmemory.db.models import AppState, Episode, MemoryBackend, MemoryReceipt, Project
from workmemory.evidence.policy import valid_excerpt_sources
from workmemory.jobs.queue import enqueue
from workmemory.memory.base import MemoryDocument


async def selected_backend(session):
    state = await session.get(AppState, "memory")
    return state.value["backend"]


async def initialize(session, backend):
    await session.execute(
        insert(AppState).values(key="memory", value={"backend": backend}).on_conflict_do_nothing()
    )
    selected = await selected_backend(session)
    await session.execute(insert(MemoryBackend).values(name=selected).on_conflict_do_nothing())


async def schedule_sync(session, episode, *, all_used=False, force=False):
    names = (
        list((await session.scalars(select(MemoryBackend.name))).all())
        if all_used
        else [await selected_backend(session)]
    )
    for name in names:
        key = f"memory:{name}:{episode.id}:{episode.revision}"
        if force:
            key += f":{uuid.uuid4()}"
        await enqueue(
            session,
            "memory",
            key,
            {
                "episode_id": str(episode.id),
                "backend": name,
                "all_used": all_used,
                "force": force,
            },
            priority=2 if all_used else 20,
        )


async def switch_backend(session, name):
    state = await session.scalar(select(AppState).where(AppState.key == "memory").with_for_update())
    await session.execute(insert(MemoryBackend).values(name=name).on_conflict_do_nothing())
    state.value = {"backend": name}
    health = await session.get(AppState, "backend_health:" + name)
    if health:
        health.value = {**health.value, "retry_at": None}
    await session.flush()
    # Tombstones are replayed too: this also repairs partial external writes after a crash.
    episodes = (
        await session.scalars(
            select(Episode).where(
                (Episode.summary.is_not(None)) | (Episode.deleted_at.is_not(None))
            )
        )
    ).all()
    for episode in episodes:
        receipt = await session.get(MemoryReceipt, (name, episode.id))
        if receipt is None or receipt.revision != episode.revision:
            await enqueue(
                session,
                "memory",
                f"replay:{name}:{episode.id}:{uuid.uuid4()}",
                {
                    "episode_id": str(episode.id),
                    "backend": name,
                    "all_used": bool(episode.deleted_at or episode.corrected),
                },
                priority=15,
            )
    return len(episodes)


class MemoryRegistry:
    def __init__(self, settings, ollama):
        self.settings, self.ollama = settings, ollama
        self.adapters = {}
        self.locks = {name: asyncio.Lock() for name in ("reme", "graphiti")}

    async def get(self, name):
        async with self.locks[name]:
            if name not in self.adapters:
                if name == "reme":
                    from workmemory.memory.reme_adapter import ReMeAdapter

                    adapter = ReMeAdapter(self.settings, self.ollama)
                elif name == "graphiti":
                    from workmemory.memory.graphiti_adapter import GraphitiAdapter

                    adapter = GraphitiAdapter(self.settings, self.ollama)
                else:
                    raise ValueError("Unknown backend")
                try:
                    await adapter.start()
                except BaseException:
                    await adapter.close()
                    raise
                self.adapters[name] = adapter
            return self.adapters[name]

    async def close(self):
        for adapter in self.adapters.values():
            await adapter.close()


async def sync_job(runtime, payload):
    episode_id = uuid.UUID(payload["episode_id"])
    name = payload["backend"]
    async with runtime.sessions() as session:
        episode = await session.get(Episode, episode_id)
        if not episode:
            return
        if not payload.get("all_used") and name != await selected_backend(session):
            return
        receipt = await session.get(MemoryReceipt, (name, episode_id))
        deleted = not await valid_excerpt_sources(session, episode, runtime.settings.retention_days) or not episode.summary
        if receipt and receipt.revision == episode.revision and receipt.deleted == bool(deleted) and not payload.get("force"):
            return
        revision, corrected = episode.revision, episode.corrected
        signature = (revision, episode.evidence_policy, episode.evidence_state, episode.evidence_expires_at, bool(deleted))
        project = await session.get(Project, episode.project_id) if episode.project_id else None
        document = MemoryDocument(
            episode_id,
            revision,
            episode.summary or "",
            episode.started_at,
            project.name if project else None,
        )
    # No business row locks across slow external I/O: deletion can become visible immediately.
    from workmemory.memory.availability import BackendOffline, allowed, connection_error, record
    await allowed(runtime.sessions, name)
    try:
        adapter = await asyncio.wait_for(runtime.memory.get(name), 5)
        if deleted:
            await adapter.delete(episode_id)
            external_ids = []
        else:
            external_ids = await (adapter.correct(document) if corrected else adapter.upsert(document))
    except Exception as exc:
        if connection_error(exc):
            await record(runtime.sessions, name, False, exc)
            raise BackendOffline() from None
        raise
    await record(runtime.sessions, name, True)
    async with runtime.sessions.begin() as session:
        current = await session.scalar(
            select(Episode).where(Episode.id == episode_id).with_for_update()
        )
        if not current:
            return
        if (current.revision, current.evidence_policy, current.evidence_state, current.evidence_expires_at,
                bool(not await valid_excerpt_sources(session, current, runtime.settings.retention_days) or not current.summary)) != signature:
            # An in-flight external write can be stale; never acknowledge it as current.
            # A mutation's durable task (or this repair task) will replace/delete it.
            await schedule_sync(
                session, current, all_used=True, force=True
            )
            return
        await session.execute(
            insert(MemoryReceipt)
            .values(
                backend=name,
                episode_id=episode_id,
                revision=revision,
                external_ids=external_ids,
                deleted=deleted,
            )
            .on_conflict_do_update(
                index_elements=[MemoryReceipt.backend, MemoryReceipt.episode_id],
                set_={"revision": revision, "external_ids": external_ids, "deleted": deleted},
            )
        )
