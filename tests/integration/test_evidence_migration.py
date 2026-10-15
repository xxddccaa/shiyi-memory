"""Upgrade actual prior tables containing synthetic history, including independent manual data."""
import asyncio
import uuid
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from workmemory.config import ROOT, get_settings
from workmemory.db import make_engine, make_sessions
from workmemory.db.models import Episode, EvidenceArchive, WorkSession
from workmemory.evidence.archive import initialize_policy
from workmemory.memory.sync import initialize


def test_old_schema_upgrade_preserves_manual_data_and_archives_automatic():
    settings = get_settings()
    name = "workmemory_test_policy_" + uuid.uuid4().hex[:10]
    admin_url = settings.database_url.set(database="postgres")
    target = settings.database_url.set(database=name)
    async def ddl(sql):
        engine = make_engine(admin_url)
        try:
            async with engine.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.execute(text(sql))
        finally:
            await engine.dispose()
    asyncio.run(ddl(f'CREATE DATABASE "{name}"'))
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = target
    automatic, corrected, segment = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        command.upgrade(config, "d91f6_proactive")
        async def seed():
            engine = make_engine(target)
            try:
                async with engine.begin() as c:
                    for identifier, is_corrected in [(automatic, False), (corrected, True)]:
                        await c.execute(text("""INSERT INTO episodes(id,device_id,started_at,ended_at,summary,revision,status,corrected)
                            VALUES(:id,'synthetic',now(),now(),:summary,1,:status,:corrected)"""),
                            {"id": identifier, "summary": "人工正文" if is_corrected else "旧自动推断", "corrected": is_corrected, "status": "ready" if is_corrected else "expired"})
                    await c.execute(text("""INSERT INTO work_sessions(id,episode_id,revision,title,category,started_at,ended_at,edit_revision)
                        VALUES(:id,:episode,1,'人工标题','人工分类',now(),now(),3)"""), {"id": segment, "episode": automatic})
            finally:
                await engine.dispose()
        asyncio.run(seed())
        command.upgrade(config, "head")
        command.check(config)
        async def verify():
            engine = make_engine(target)
            sessions = make_sessions(engine)
            try:
                async with sessions.begin() as session:
                    await initialize(session, "reme")
                runtime = SimpleNamespace(sessions=sessions)
                await initialize_policy(runtime)
                await initialize_policy(runtime)
                async with sessions() as session:
                    old = await session.get(Episode, automatic)
                    manual = await session.get(Episode, corrected)
                    edit = await session.get(WorkSession, segment)
                    assert old.summary == "旧自动推断" and old.evidence_state == "legacy_unverified"
                    assert manual.summary == "人工正文" and manual.evidence_state == "user_confirmed"
                    assert edit.title == "人工标题" and edit.category == "人工分类" and edit.edit_revision == 3
                    archives = (await session.scalars(select(EvidenceArchive))).all()
                    assert len(archives) == 1 and archives[0].payload["summary"] == "旧自动推断"
            finally:
                await engine.dispose()
        asyncio.run(verify())
    finally:
        asyncio.run(ddl(f'DROP DATABASE "{name}" WITH (FORCE)'))
