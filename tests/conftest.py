import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from workmemory.config import ROOT, get_settings
from workmemory.db import make_engine, make_sessions
from workmemory.memory.base import MemoryAdapter, MemoryHit
from workmemory.memory.sync import initialize


class FakeOllama:
    def __init__(self):
        self.pending, self.metrics = [], []
        self.current = None
        self.answer_hook = None
        self.embedding_calls = 0

    async def start(self):
        pass

    async def close(self):
        pass

    async def health(self):
        return {"reachable": True, "models_ready": True}

    async def local_models(self):
        return [{"name": "qwen3:8b"}]

    async def embed(self, texts, priority=None):
        self.embedding_calls += 1
        return [[0.01] * 1024 for _ in texts]

    async def chat(self, messages, schema=None, **kwargs):
        if schema.get("title") == "ExcerptSelection":
            data = json.loads(messages[-1]["content"])
            return json.dumps({"selected_block_ids": [b["id"] for b in data["blocks"][:12]]})
        if schema.get("title") == "Answer":
            data = json.loads(messages[-1]["content"])
            if self.answer_hook:
                await self.answer_hook()
            return json.dumps(
                {"answer": data["evidence"][0]["text"], "evidence_ids": [data["evidence"][0]["id"]]}
            )
        return json.dumps(
            {
                "summary": "蓝鲸项目发布时间由周五改为周一，需补充验收测试。",
                "project": "蓝鲸",
                "todos": ["补充验收测试"],
            },
            ensure_ascii=False,
        )


class FakeAdapter(MemoryAdapter):
    def __init__(self, name):
        self.name, self.documents, self.deleted = name, {}, []

    async def start(self):
        pass

    async def close(self):
        pass

    async def upsert(self, document):
        self.documents[document.episode_id] = document
        return [str(document.episode_id)]

    async def delete(self, episode_id):
        self.documents.pop(episode_id, None)
        self.deleted.append(episode_id)

    async def search(self, query, limit=10):
        return [MemoryHit(id) for id in list(self.documents)[:limit]]

    async def status(self):
        return {"ready": True}


class FakeRegistry:
    def __init__(self):
        self.adapters = {name: FakeAdapter(name) for name in ["reme", "graphiti"]}

    async def get(self, name):
        return self.adapters[name]

    async def close(self):
        pass


@pytest.fixture(scope="session")
def database_url():
    """Migrate an isolated real PostgreSQL database; never truncate application data."""
    settings = get_settings()
    name = "workmemory_test_" + uuid.uuid4().hex[:12]
    admin_url = settings.database_url.set(database="postgres")

    async def create():
        engine = make_engine(admin_url)
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        await engine.dispose()

    asyncio.run(create())
    target = settings.database_url.set(database=name)
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = target
    command.upgrade(config, "head")
    yield target

    async def drop():
        engine = make_engine(admin_url)
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await engine.dispose()

    asyncio.run(drop())


@pytest.fixture
async def runtime(database_url, tmp_path):
    engine = make_engine(database_url)
    sessions = make_sessions(engine)
    async with sessions.begin() as session:
        await session.execute(
            text(
                "TRUNCATE capture_incidents,maintenance_tasks,memory_candidates,evidence_archives,todo_tombstones,external_sources,client_events,feedback,conversations,derived_artifacts,evidence_dependencies,reminders,todos,memory_receipts,memory_backends,activities,episodes,projects,jobs,app_state CASCADE"
            )
        )
        await initialize(session, "reme")
    rt = SimpleNamespace(
        engine=engine,
        sessions=sessions,
        settings=get_settings().model_copy(deep=True),
        ollama=FakeOllama(),
        memory=FakeRegistry(),
    )
    from workmemory.mcp.config import MCPStore
    from workmemory.mcp.runtime import MCPManager
    rt.mcp = MCPManager(rt, MCPStore(tmp_path / "mcp.json"))
    yield rt
    await rt.mcp.close()
    await engine.dispose()
