import asyncio
import logging

from workmemory.artifacts.drafts import generate as generate_draft
from workmemory.artifacts.service import generate_artifact
from workmemory.collector.status import collector_status
from workmemory.companion import backfill as context_backfill
from workmemory.companion.digest import summarize_conversation
from workmemory.companion.state import preferences
from workmemory.episodes.service import summarize_job
from workmemory.evidence.service import backfill
from workmemory.jobs.queue import claim, finish, heartbeat
from workmemory.memory.sync import sync_job
from workmemory.reminders.service import maintenance

logger = logging.getLogger(__name__)


async def keep_lease(runtime, job):
    while True:
        await asyncio.sleep(25)
        if not await heartbeat(runtime.sessions, job):
            raise RuntimeError("Job lease lost")


async def worker(runtime):
    while True:
        try:
            async with runtime.sessions() as session:
                settings = await preferences(session)
            collector = collector_status(runtime.settings)
            thermal = collector.get("thermal_state", "nominal")
            if thermal in {"serious", "critical"}:
                await asyncio.sleep(5)
                continue
            excluded = []
            if not settings["backfill_enabled"]:
                excluded.append("context")
            if settings["resource_policy"] == "saving" or collector.get("system_low_power"):
                excluded += ["context", "digest"]
            job = await claim(runtime.sessions, excluded)
            if job is None:
                await asyncio.sleep(1)
                continue
            lease = asyncio.create_task(keep_lease(runtime, job))
            action = asyncio.create_task(
                {
                    "summarize": summarize_job,
                    "memory": sync_job,
                    "artifact": generate_artifact,
                    "draft": generate_draft,
                    "digest": summarize_conversation,
                    "context": context_backfill.generate,
                }[job.kind](runtime, job.payload)
            )
            try:
                done, _ = await asyncio.wait([lease, action], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
                await finish(runtime.sessions, job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Job %s (%s) failed: %s", job.id, job.kind, type(exc).__name__)
                await finish(runtime.sessions, job, exc)
            finally:
                lease.cancel()
                action.cancel()
                await asyncio.gather(lease, action, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Worker waiting for database: %s", type(exc).__name__)
            await asyncio.sleep(5)


async def maintenance_loop(runtime):
    while True:
        try:
            await maintenance(runtime)
            count = await backfill(runtime)
            await context_backfill.schedule(runtime)
            await asyncio.sleep(1 if count == 50 else 60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Maintenance waiting: %s", type(exc).__name__)
            await asyncio.sleep(5)
