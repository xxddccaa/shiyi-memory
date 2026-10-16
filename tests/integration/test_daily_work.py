"""Default work grouping uses existing unassigned records, never placeholder projects."""

import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select
from test_pipeline import event
from workmemory.artifacts.drafts import report
from workmemory.db.models import Episode, MemoryCandidate, Project
from workmemory.episodes.service import mutate_episode, summarize_job
from workmemory.evidence.service import search
from workmemory.ingestion.service import Batch, episode_key, ingest
from workmemory.main import create_app
from workmemory.workspace.service import list_objects

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(runtime):
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama,
                     memory=runtime.memory, run_worker=False, mcp_store=runtime.mcp.store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as client:
            yield client


async def seed(runtime):
    first = event(project_hint=None, text="蓝鲸讨论：记录合成校验细节，未指定项目。")
    second = event(project_hint=None, captured_at=first.captured_at - timedelta(minutes=10),
                   text="普通工作：另一段合成校验材料。")
    named = event(text="蓝鲸项目：合成校验材料。")
    same_name = event(project_hint="日常工作", text="用户明确命名的项目：合成校验材料。")
    removed = event(project_hint=None, captured_at=first.captured_at - timedelta(minutes=20),
                    text="即将删除的合成校验材料。")
    events = [first, second, named, same_name, removed]
    async with runtime.sessions.begin() as session:
        await ingest(session, Batch(events=events), runtime.settings)
    ids = [episode_key(e, runtime.settings.episode_minutes)[0] for e in events]
    for identifier in ids:
        async with runtime.sessions() as session:
            row = await session.get(Episode, identifier)
            revision = row.revision
        await summarize_job(runtime, {"episode_id": str(identifier), "revision": revision})
    async with runtime.sessions.begin() as session:
        await mutate_episode(session, await session.get(Episode, ids[-1]))
    return ids, first.captured_at.astimezone(ZoneInfo(runtime.settings.timezone)).date().isoformat()


async def test_default_group_count_pagination_and_project_separation(runtime, client):
    assert (await client.get("/api/work-groups/daily")).status_code == 401
    client.headers["Authorization"] = "Bearer " + runtime.settings.api_token
    assert (await client.get("/api/work-groups/daily")).json() == {
        "name": "日常工作", "episodes": 0, "last_active": None,
    }
    ids, _ = await seed(runtime)
    for _ in range(2):
        group = (await client.get("/api/work-groups/daily")).json()
        assert group["episodes"] == 2 and group["last_active"]
        projects = (await client.get("/api/projects")).json()
        assert {p["name"] for p in projects} == {"蓝鲸", "日常工作"}
    found = []
    for offset in range(2):
        result = (await client.get("/api/timeline", params={
            "unassigned": True, "limit": 1, "offset": offset,
        })).json()
        assert result["total"] == 2
        row = result["items"][0]
        assert row["project"] == "日常工作" and row["project_id"] is None
        found.append(row["id"])
    assert set(found) == {str(i) for i in ids[:2]}
    project_id = projects[0]["id"]
    assert (await client.get("/api/timeline", params={"project_id": project_id, "unassigned": True})).status_code == 422
    named = (await client.get("/api/timeline", params={"project_id": project_id})).json()
    assert named["total"] == 1 and named["items"][0]["project_id"] == project_id
    async with runtime.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == 2


async def test_default_search_scopes_semantic_lexical_and_confirmed_memories(runtime, client):
    ids, day = await seed(runtime)
    async with runtime.sessions.begin() as session:
        named = await session.get(Episode, ids[2])
        default_memory = MemoryCandidate(id=uuid.uuid4(), text="合成校验的确认记录", status="confirmed", origin={"date": day})
        named_memory = MemoryCandidate(id=uuid.uuid4(), text="合成校验的项目记录", status="confirmed",
                                       origin={"date": day, "project_id": str(named.project_id)})
        session.add_all([default_memory, named_memory])
    from test_pipeline import flush_memory
    await flush_memory(runtime)
    # Mentioning an existing project in the query must not override an explicit default scope.
    result = await search(runtime, "蓝鲸合成校验", unassigned=True, limit=30)
    assert result["scope"]["project_id"] is None and result["scope"]["unassigned"]
    assert result["items"]
    assert all(i["project_id"] is None for i in result["items"])
    assert {i["episode_id"] for i in result["items"] if i["episode_id"]} <= {str(i) for i in ids[:2]}
    assert f"memory:{default_memory.id}" in {i["id"] for i in result["items"]}
    assert f"memory:{named_memory.id}" not in {i["id"] for i in result["items"]}
    client.headers["Authorization"] = "Bearer " + runtime.settings.api_token
    for path in ["/api/memory/search", "/api/memory/evidence/search"]:
        response = await client.get(path, params={"q": "合成校验", "unassigned": True})
        assert response.status_code == 200
        assert response.json()["items"]
        assert all(i["project_id"] is None for i in response.json()["items"])
        assert (await client.get(path, params={"q": "校验", "unassigned": True, "project_id": str(named.project_id)})).status_code == 422
    # With no semantic matches, an unrelated query must not return the whole group.
    runtime.memory.adapters["reme"].documents.clear()
    assert not (await search(runtime, "火星钻石", unassigned=True))["items"]


async def test_report_and_object_labels_use_default_without_rewriting_sources(runtime):
    ids, day = await seed(runtime)
    async with runtime.sessions.begin() as session:
        session.add(MemoryCandidate(text="本人确认的日常记录", status="confirmed", origin={"date": day}))
    result = await report(runtime, "daily", day)
    assert any(m["project_name"] == "日常工作" and m["project_id"] is None for m in result["materials"])
    assert {g["project_name"] for g in result["observation_groups"]} == {"日常工作", "蓝鲸"}
    async with runtime.sessions() as session:
        objects = await list_objects(session, "episode")
        default = [o for o in objects["items"] if o["id"] in {str(i) for i in ids[:2]}]
        assert len(default) == 2 and all(o["project_name"] == "日常工作" and o["project_id"] is None for o in default)
        for identifier in ids[:2]:
            row = await session.get(Episode, identifier)
            assert row.project_id is None and row.project_hint is None
            assert "日常工作" not in row.summary
        assert await session.scalar(select(func.count()).select_from(Project)) == 2
