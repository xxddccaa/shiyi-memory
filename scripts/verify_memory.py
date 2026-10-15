"""Live ReMe / Graphiti replay, correction and erasure checks on synthetic data."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from workmemory.config import ROOT, get_settings
from workmemory.db import make_engine, make_sessions
from workmemory.db.models import Episode, MemoryReceipt


async def main():
    settings = get_settings()
    engine = make_engine()
    sessions = make_sessions(engine)
    prefix = "记忆切换验收" + uuid.uuid4().hex[:8]
    ids = []
    report = {}
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{settings.server.port}",
        headers={"Authorization": "Bearer " + settings.api_token},
        timeout=600,
        trust_env=False,
    ) as client:

        async def post(path, data=None):
            r = await client.post(path, json=data or {})
            r.raise_for_status()
            return r.json()

        async def get(path):
            r = await client.get(path)
            r.raise_for_status()
            return r.json()

        async def wait_synced(backend, deleted=False):
            for _ in range(1200):
                async with sessions() as session:
                    episodes = (
                        await session.scalars(
                            select(Episode)
                            .where(Episode.device_id == prefix)
                            .order_by(Episode.started_at)
                        )
                    ).all()
                    ids[:] = [e.id for e in episodes]
                    receipts = (
                        await session.scalars(
                            select(MemoryReceipt).where(
                                MemoryReceipt.backend == backend, MemoryReceipt.episode_id.in_(ids)
                            )
                        )
                    ).all()
                if (
                    len(episodes) == 2
                    and len(receipts) == 2
                    and all(
                        any(
                            r.episode_id == e.id
                            and r.revision == e.revision
                            and r.deleted == deleted
                            for r in receipts
                        )
                        for e in episodes
                    )
                ):
                    return
                await asyncio.sleep(1)
            raise TimeoutError(f"{backend} synchronization exceeded 20 minutes")

        before = await get("/api/status")
        if not before["ollama"]["models_ready"]:
            raise SystemExit("Complete the two model downloads before live memory verification.")
        original = before["backend"]
        try:
            await post("/api/config/backend", {"backend": "reme"})
            events = []
            for index, fact in enumerate(
                ["原计划本周五上线。", "计划变更到下周一上线，当前尚未上线。"]
            ):
                events.append(
                    {
                        "id": str(uuid.uuid4()),
                        "device_id": prefix,
                        "captured_at": (
                            datetime.now(UTC) - timedelta(minutes=15 - index * 5)
                        ).isoformat(),
                        "app": "verification.synthetic",
                        "title": prefix,
                        "text": prefix + "项目" + fact,
                        "method": "manual",
                        "project_hint": prefix,
                    }
                )
            await post("/api/ingest", {"events": events})
            await wait_synced("reme")
            for backend in ["graphiti", "reme"]:
                await post("/api/config/backend", {"backend": backend})
                await wait_synced(backend)
                response = await post(
                    "/api/chat", {"question": prefix + "项目最新计划什么时候上线？"}
                )
                assert response["sources"] and "周一" in response["answer"]
                report[backend + "_history_replay"] = "passed"
            async with sessions() as session:
                episode = await session.get(Episode, ids[-1])
            response = await client.patch(
                "/api/episodes/" + str(episode.id),
                json={
                    "summary": prefix + "项目最终改为周二上线，仍未上线。",
                    "expected_revision": episode.revision,
                },
            )
            response.raise_for_status()
            await wait_synced("reme")
            await wait_synced("graphiti")
            report["correction_propagation"] = "passed"
            for episode_id in ids:
                response = await client.delete("/api/episodes/" + str(episode_id))
                response.raise_for_status()
            # Verify immediate filtering before waiting for either physical index cleanup.
            result = await get("/api/memory/search?q=" + prefix)
            assert not any(uuid.UUID(item["id"]) in ids for item in result["items"])
            for backend in ["graphiti", "reme"]:
                await post("/api/config/backend", {"backend": backend})
                await wait_synced(backend, deleted=True)
                result = await get("/api/memory/search?q=" + prefix)
                assert not any(uuid.UUID(item["id"]) in ids for item in result["items"])
            report["delete_and_switch_no_resurrection"] = "passed"
            print(report)
        finally:
            for episode_id in ids:
                response = await client.delete("/api/episodes/" + str(episode_id))
                response.raise_for_status()
            await post("/api/config/backend", {"backend": original})
            import json

            (ROOT / "data/exports/live-memory-verification.json").write_text(
                json.dumps(report, indent=2)
            )
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
