"""Real Ollama pipeline benchmark with synthetic Chinese records and evidence checks."""

import argparse
import asyncio
import json
import statistics
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from workmemory.config import ROOT, get_settings
from workmemory.db import make_engine, make_sessions
from workmemory.db.models import Episode, MemoryReceipt


async def main(iterations=1):
    settings = get_settings()
    engine = make_engine()
    sessions = make_sessions(engine)
    run_id = uuid.uuid4().hex[:8]
    output = ROOT / f"data/exports/benchmark-{run_id}.json"
    report = {
        "run_id": run_id,
        "recorded_at": datetime.now(UTC).isoformat(),
        "iterations": iterations,
        "hardware": {
            "memory_bytes": int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])),
            "chip": subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
            ).strip(),
        },
        "samples": [],
        "errors": [],
    }
    all_ids = []
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{settings.server.port}",
        headers={"Authorization": "Bearer " + settings.api_token},
        timeout=600,
        trust_env=False,
    ) as client:

        async def get(path):
            response = await client.get(path)
            response.raise_for_status()
            return response.json()

        async def post(path, data):
            response = await client.post(path, json=data)
            response.raise_for_status()
            return response.json()

        status = await get("/api/status")
        if not status["ollama"]["models_ready"]:
            raise SystemExit(
                "Models are not ready. Complete scripts/models-pull.sh before benchmarking."
            )
        report["backend"] = status["backend"]
        fixtures = json.loads((ROOT / "tests/fixtures/chinese_work.json").read_text())
        try:
            for iteration in range(iterations):
                prefix = f"验收{run_id}-{iteration}"
                started = time.monotonic()
                events = []
                for index, item in enumerate(fixtures):
                    project = prefix + item["project"]
                    events.append(
                        {
                            "id": str(uuid.uuid4()),
                            "device_id": prefix,
                            "captured_at": (
                                datetime.now(UTC) - timedelta(minutes=30 - index * 5)
                            ).isoformat(),
                            "app": "benchmark.synthetic",
                            "title": "合成中文验收",
                            "text": item["text"].replace(item["project"], project),
                            "method": "manual",
                            "project_hint": project,
                        }
                    )
                await post("/api/ingest", {"events": events})
                ids = []
                for _ in range(900):
                    async with sessions() as session:
                        episodes = (
                            await session.scalars(
                                select(Episode).where(Episode.device_id == prefix)
                            )
                        ).all()
                        ids = [e.id for e in episodes]
                        all_ids.extend(i for i in ids if i not in all_ids)
                        receipts = (
                            await session.scalars(
                                select(MemoryReceipt).where(
                                    MemoryReceipt.backend == status["backend"],
                                    MemoryReceipt.episode_id.in_(ids),
                                )
                            )
                        ).all()
                    if (
                        len(episodes) == len(fixtures)
                        and all(e.status == "ready" for e in episodes)
                        and len(receipts) == len(fixtures)
                        and all(
                            any(r.episode_id == e.id and r.revision == e.revision for r in receipts)
                            for e in episodes
                        )
                    ):
                        break
                    await asyncio.sleep(1)
                else:
                    raise TimeoutError("Pipeline did not finish within 15 minutes")
                pipeline_seconds = time.monotonic() - started
                question_started = time.monotonic()
                answer = await post(
                    "/api/chat",
                    {
                        "question": f"{prefix}星舟项目最新决定什么时候上线？是否已经上线？为什么变更？"
                    },
                )
                assert answer["sources"], "No evidence returned"
                assert all(uuid.UUID(source["id"]) in ids for source in answer["sources"]), (
                    "Evidence from unrelated project"
                )
                assert "周一" in answer["answer"], "Fact change was not reflected in answer"
                assert any(word in answer["answer"] for word in ["未", "还没", "尚未", "没有"]), (
                    "Planned work was reported as completed"
                )
                report["samples"].append(
                    {
                        "pipeline_seconds": round(pipeline_seconds, 3),
                        "seconds_per_episode": round(pipeline_seconds / len(fixtures), 3),
                        "question_seconds": round(time.monotonic() - question_started, 3),
                        "evidence_count": len(answer["sources"]),
                        "facts_changed_correctly": True,
                    }
                )
                print(json.dumps(report["samples"][-1], ensure_ascii=False), flush=True)
            report["mean_seconds_per_episode"] = statistics.mean(
                s["seconds_per_episode"] for s in report["samples"]
            )
            report["keeps_up_with_five_minute_input"] = report["mean_seconds_per_episode"] < 300
            report["inference"] = (await get("/api/status"))["inference"]
            report["memory_pressure"] = subprocess.run(
                ["memory_pressure", "-Q"], capture_output=True, text=True
            ).stdout.strip()
        except Exception as exc:
            report["errors"].append(type(exc).__name__ + ": " + str(exc))
            raise
        finally:
            for episode_id in all_ids:
                response = await client.delete("/api/episodes/" + str(episode_id))
                response.raise_for_status()
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            await engine.dispose()
            print("Benchmark report:", output, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1, choices=range(1, 11))
    args = parser.parse_args()
    asyncio.run(main(args.iterations))
