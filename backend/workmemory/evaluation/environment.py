"""Owned PostgreSQL database and ReMe workspace, with real app/worker components."""

import asyncio
import hashlib
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from workmemory.config import ROOT
from workmemory.db import make_engine
from workmemory.db.models import (
    Activity,
    Base,
    ChatMessage,
    Conversation,
    DerivedArtifact,
    Episode,
    Job,
    MemoryCandidate,
    MemoryReceipt,
    Project,
    Todo,
    WorkFact,
    WorkSession,
)
from workmemory.evaluation.oracle import stable_json
from workmemory.evidence.service import index_episode
from workmemory.jobs.queue import claim, finish
from workmemory.main import create_app
from workmemory.mcp.config import MCPStore
from workmemory.memory.sync import schedule_sync

TABLES = ("projects", "todos", "memory_candidates", "episodes", "activities", "observations",
          "work_sessions", "work_facts", "project_aliases", "derived_artifacts", "reminders",
          "todo_sources", "todo_tombstones")


class InfrastructureError(RuntimeError):
    """Messages are controlled codes; upstream errors must not disclose credentials."""


class Isolation:
    def __init__(self, settings):
        self.settings = settings.model_copy(deep=True)
        self.name = "workmemory_eval_" + uuid.uuid4().hex[:16]
        self.settings.database.name = self.name
        self.settings.memory.backend = "reme"
        self.settings.api_token = secrets.token_urlsafe(32)
        self.admin_url = settings.database_url.set(database="postgres")
        self.engine = None
        self.created = False
        self.root = ROOT / ".cache" / self.name

    async def __aenter__(self):
        self.root.mkdir(parents=True, exist_ok=False)
        admin = make_engine(self.admin_url)
        try:
            async with admin.connect() as conn:
                conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.execute(text(f'CREATE DATABASE "{self.name}"'))
                self.created = True
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["database_url"] = self.settings.database_url
            await asyncio.to_thread(command.upgrade, config, "head")
            self.engine = make_engine(self.settings.database_url)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        finally:
            await admin.dispose()

    async def __aexit__(self, *_):
        if self.engine:
            await self.engine.dispose()
        if self.created:
            if not self.name.startswith("workmemory_eval_") or self.settings.database.name != self.name:
                raise InfrastructureError("database_ownership_check_failed")
            admin = make_engine(self.admin_url)
            try:
                async with admin.connect() as conn:
                    conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                    await conn.execute(text(f'DROP DATABASE "{self.name}" WITH (FORCE)'))
                self.created = False
            finally:
                await admin.dispose()
        if self.root.exists():
            shutil.rmtree(self.root)

    @asynccontextmanager
    async def case(self, store):
        # Only tables in this application metadata, only the database we created above.
        async with self.engine.begin() as conn:
            database = await conn.scalar(text("SELECT current_database()"))
            if database != self.name or not self.created:
                raise InfrastructureError("database_ownership_check_failed")
            await conn.execute(text("TRUNCATE " + ",".join('"' + n + '"' for n in Base.metadata.tables) + " CASCADE"))
        settings = self.settings.model_copy(deep=True)
        path = self.root / uuid.uuid4().hex
        path.mkdir()
        settings.memory.reme_workspace = str((path / "reme").relative_to(ROOT))
        app = create_app(settings, engine=self.engine, run_worker=False,
                         generation_store=store, mcp_store=MCPStore(path / "mcp.json"))
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1", headers={"Authorization": "Bearer " + settings.api_token,
                    "Origin": f"http://127.0.0.1:{settings.server.port}"}) as client:
                    yield app.state.runtime, client
        finally:
            shutil.rmtree(path)


async def snapshot(rt):
    state = {}
    async with rt.sessions() as session:
        for name in TABLES:
            table = Base.metadata.tables[name]
            rows = (await session.execute(select(table))).mappings().all()
            state[name] = {}
            for row in rows:
                data = {k: v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, uuid.UUID) else v
                        for k, v in row.items() if k not in {"updated_at", "created_at"}}
                key = str(row.get("id", row.get("name", "|".join(str(row[k.name]) for k in table.primary_key))))
                state[name][key] = data
    return state


async def drain(rt, *, timeout=180):
    """Run the same durable job handlers and leases, with no production maintenance loop."""
    from workmemory.artifacts.service import generate_artifact
    from workmemory.companion.backfill import generate
    from workmemory.companion.digest import summarize_conversation
    from workmemory.episodes.service import summarize_job
    from workmemory.jobs.worker import keep_lease
    from workmemory.memory.sync import sync_job

    handlers = {"summarize": summarize_job, "memory": sync_job, "artifact": generate_artifact,
                "digest": summarize_conversation, "context": generate}
    async with asyncio.timeout(timeout):
        while True:
            async with rt.sessions() as session:
                jobs = (await session.scalars(select(Job).where(Job.status != "done"))).all()
            if not jobs:
                return
            if any(j.status == "failed" for j in jobs):
                raise InfrastructureError("background_job_failed")
            job = await claim(rt.sessions)
            if job is None:
                await asyncio.sleep(.2)
                continue
            lease = asyncio.create_task(keep_lease(rt, job))
            action = asyncio.create_task(handlers[job.kind](rt, job.payload))
            try:
                done, _ = await asyncio.wait([lease, action], return_when=asyncio.FIRST_COMPLETED)
                for completed in done:
                    await completed
                await finish(rt.sessions, job)
            except Exception as exc:
                await finish(rt.sessions, job, exc)
                raise InfrastructureError("background_" + type(exc).__name__) from None
            finally:
                lease.cancel()
                action.cancel()
                await asyncio.gather(lease, action, return_exceptions=True)


async def seed(rt, client, scenario):
    anchor = datetime.now(ZoneInfo(rt.settings.timezone))
    bindings = {"today": anchor.date().isoformat(), "anchor": anchor.isoformat(), "timezone": rt.settings.timezone,
                "next_monday_09": (anchor + timedelta(days=7-anchor.weekday())).replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
                "tomorrow_15": (anchor + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0).isoformat()}
    for key in ("blue", "star", "todo_a", "todo_b", "todo_guard", "episode_a", "session_a", "memory_a", "review_a", "late_todo"):
        bindings[key] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"workmemory-eval:{scenario.id}:{key}"))
    ids = {k: uuid.UUID(v) for k, v in bindings.items() if k not in {"today", "anchor", "tomorrow_15", "timezone", "next_monday_09"}}
    async with rt.sessions.begin() as session:
        session.add_all([Project(id=ids["blue"], name="蓝鲸"), Project(id=ids["star"], name="星河")])
        await session.flush()
        if "projects32" in scenario.seed:
            extra = [uuid.uuid5(uuid.NAMESPACE_URL, f"workmemory-eval:{scenario.id}:project:{n}") for n in range(30)]
            session.add_all(Project(id=identifier, name=f"合成项目{n:03d}") for n, identifier in enumerate(extra))
            bindings["project_ids"] = sorted([bindings["blue"], bindings["star"], *map(str, extra)])
        if "todos" in scenario.seed:
            session.add_all([
                Todo(id=ids["todo_a"], text="整理周报", project_id=ids["blue"], confirmed=True,
                     done="completed" in scenario.seed, due_at=datetime.fromisoformat(bindings["tomorrow_15"]), origin="manual", local_time="15:00:00", timezone=rt.settings.timezone),
                Todo(id=ids["todo_b"], text="整理本周周报" if "duplicate" in scenario.seed else "补充测试", project_id=ids["blue"], confirmed=True, due_at=datetime.fromisoformat(bindings["tomorrow_15"]), origin="manual", local_time="15:00:00", timezone=rt.settings.timezone),
                Todo(id=ids["todo_guard"], text="整理周报" if "ambiguous" in scenario.seed else "核对采购预算", project_id=ids["star"], confirmed=True, due_at=datetime.fromisoformat(bindings["tomorrow_15"]), origin="manual", local_time="15:00:00", timezone=rt.settings.timezone),
            ])
            bindings["blue_todo_ids"] = sorted([bindings["todo_a"], bindings["todo_b"]])
        if scenario.id == "todo-reschedule":
            row = await session.get(Todo, ids["todo_a"])
            row.due_at -= timedelta(hours=1)
            row.local_time = "14:00:00"
        if "due_instance" in scenario.seed:
            from workmemory.db.models import Reminder
            due = anchor - timedelta(minutes=1)
            todo = await session.get(Todo, ids["todo_a"])
            todo.repeat = "daily"
            rid = uuid.uuid5(uuid.NAMESPACE_URL, f"workmemory-eval:{scenario.id}:reminder")
            bindings["reminder_a"] = str(rid)
            session.add(Reminder(id=rid, kind="todo", todo_id=todo.id, text="该整理周报了",
                due_at=due, planned_at=due, schedule_revision=1, status="pending", revision=1,
                shown_revision=0, dedupe_key=f"eval:{scenario.id}:{rid}"))
        if "paused" in scenario.seed:
            (await session.get(Todo, ids["todo_a"])).paused = True
        bulk = 69 if "bulk69" in scenario.seed else 105 if "bulk105" in scenario.seed else 0
        if bulk:
            bindings["bulk_ids"] = sorted(str(uuid.uuid5(uuid.NAMESPACE_URL, f"workmemory-eval:{scenario.id}:bulk:{n}")) for n in range(bulk))
            session.add_all(Todo(id=uuid.UUID(i), text=f"合成清单任务{n:03d}", confirmed=True) for n, i in enumerate(bindings["bulk_ids"]))
        if "memory" in scenario.seed:
            conv, message = uuid.uuid4(), uuid.uuid4()
            session.add(Conversation(id=conv, title="合成历史偏好"))
            await session.flush()
            session.add(ChatMessage(id=message, conversation_id=conv, role="user", content="我喜欢美式咖啡。"))
            await session.flush()
            session.add(MemoryCandidate(id=ids["memory_a"], conversation_id=conv, message_id=message,
                                        text="我喜欢美式咖啡。", status="confirmed"))
        if ("work" in scenario.seed or "yesterday" in scenario.seed) and not scenario.pipeline:
            at = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
            if "yesterday" in scenario.seed:
                at -= timedelta(days=1)
            episode = Episode(evidence_state="user_confirmed", corrected=True, id=ids["episode_a"], device_id="dialogue-eval", started_at=at,
                ended_at=at + timedelta(minutes=5), project_id=ids["blue"], status="ready", revision=1,
                summary="蓝鲸接口验收已通过；接下来需要补充测试。", context_revision=1)
            session.add(episode)
            await session.flush()
            session.add(WorkSession(title_confirmed=True, id=ids["session_a"], episode_id=episode.id, revision=1,
                title="蓝鲸接口验收", started_at=at, ended_at=at + timedelta(minutes=5)))
            session.add(WorkFact(episode_id=episode.id, revision=1, kind="progress", text="蓝鲸接口验收已通过", state="done", observed_at=at))
            await index_episode(session, episode)
            await schedule_sync(session, episode)
        if "review" in scenario.seed:
            session.add(DerivedArtifact(id=ids["review_a"], kind="daily", scope=bindings["today"], status="ready",
                body={"progress": [], "decisions": [], "blockers": [], "next_steps": [], "unknowns": []},
                user_text="合成的今日回顾", edit_revision=1))
    if scenario.pipeline:
        event = {"id": str(uuid.uuid4()), "device_id": "dialogue-eval", "captured_at": (anchor - timedelta(minutes=10)).isoformat(),
                 "app": "synthetic.editor", "title": "合成验收记录", "method": "paddleocr", "trigger": "timer",
                 "text": "蓝鲸项目接口验收已通过。接下来需要补充测试。", "project_hint": "蓝鲸"}
        response = await client.post("/api/ingest", json={"events": [event]})
        response.raise_for_status()
        async with rt.sessions() as session:
            episode = await session.scalar(select(Episode))
            if episode is None:
                raise InfrastructureError("synthetic_ingest_not_accepted")
            bindings["episode_a"] = str(episode.id)
    if not scenario.pending:
        await drain(rt)
    return bindings


async def memory_evidence(rt, bindings):
    """Read-only framework search plus durable receipt state for the judge/oracle."""
    async with rt.sessions() as session:
        rows = (await session.scalars(select(MemoryReceipt))).all()
        episodes = (await session.scalars(select(Episode))).all()
        jobs = (await session.scalars(select(Job))).all()
        activities = (await session.scalars(select(Activity))).all()
    hits = []
    if episodes and any(e.status == "ready" for e in episodes):
        adapter = await rt.memory.get("reme")
        hits = [str(h.episode_id) for h in await adapter.search("蓝鲸接口验收", limit=10)]
    return {"hits": hits, "receipts": [{"episode_id": str(r.episode_id), "revision": r.revision, "deleted": r.deleted} for r in rows],
            "jobs": [{"kind": j.kind, "status": j.status} for j in jobs],
            "activities": [{"episode_id": str(a.episode_id), "text": a.text} for a in activities]}


def fingerprint(value):
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


async def dependent_snapshot(rt, card):
    """Capture consumers before a change so lazy read repair cannot hide missing invalidation."""
    from workmemory.db.models import EvidenceDependency, PiSession

    action = card["action"]
    kind = action.split(".")[0]
    if action.endswith(".create") or kind not in {"todo", "memory", "episode", "project"}:
        return []
    payload = card["preview"]["after"]
    ids = set(payload.get("target_ids", []))
    ids.update(str(payload[k]) for k in ("target_id", "other_id") if payload.get(k))
    async with rt.sessions() as session:
        deps = (await session.scalars(select(EvidenceDependency).where(EvidenceDependency.source_kind == kind,
            EvidenceDependency.source_id.in_([uuid.UUID(i) for i in ids])))).all()
        result = []
        for consumer, identifier in sorted({(d.consumer_kind, d.consumer_id) for d in deps}, key=str):
            item = {"kind": consumer, "id": str(identifier)}
            if consumer == "pi_session":
                row = await session.get(PiSession, identifier)
                item["epoch"] = row.epoch if row else None
            result.append(item)
        return result


async def audit_dependents(rt, consumers, operation_id):
    from workmemory.db.models import (
        AgentRun,
        AgentToolCall,
        AgentToolOutput,
        ConversationDigest,
        ExternalSource,
        Operation,
        PiSession,
    )

    failures = []
    models = {"message": ChatMessage, "run": AgentRun, "artifact": DerivedArtifact,
              "digest": ConversationDigest, "operation": Operation, "pi_session": PiSession,
              "external": ExternalSource}
    async with rt.sessions() as session:
        for item in consumers:
            kind, identifier = item["kind"], uuid.UUID(item["id"])
            if kind not in models or kind == "operation" and item["id"] == operation_id:
                continue
            row = await session.get(models[kind], identifier)
            if row is None:
                continue
            valid = True
            if kind == "message":
                valid = row.content is None and not row.citations and row.status == "invalidated"
            elif kind in {"artifact", "digest"}:
                valid = row.status == "invalidated" and (row.body if kind == "artifact" else row.content) is None
            elif kind == "operation":
                valid = row.status != "pending" and row.preview is None and row.payload is None
            elif kind == "run":
                valid = row.status not in {"queued", "running"}
            elif kind == "pi_session":
                calls = (await session.scalars(select(AgentToolCall).where(AgentToolCall.conversation_id == identifier))).all()
                outputs = await session.scalar(select(AgentToolOutput.call_id).join(AgentToolCall).where(AgentToolCall.conversation_id == identifier).limit(1))
                valid = row.data is None and row.epoch > item["epoch"] and not outputs and all(c.arguments is None and c.result is None for c in calls)
            elif kind == "external":
                valid = row.content is None
            if not valid:
                failures.append({**item, "status": getattr(row, "status", None)})
    return failures
