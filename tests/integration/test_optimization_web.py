"""Real browser/API/PG; only synthetic report generation, no live maintenance jobs."""

import asyncio
import json
import os
import socket

import pytest
import uvicorn
from sqlalchemy import select
from test_pipeline import event, ready
from workmemory.artifacts.drafts import generate
from workmemory.config import ROOT
from workmemory.db.models import Job
from workmemory.main import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("WORKMEMORY_BROWSER_TEST") != "1", reason="Explicit browser integration"
)


async def test_optimization_browser(runtime, monkeypatch, tmp_path):
    from workmemory.maintenance import service
    from workmemory.maintenance.backups import BackupFiles

    monkeypatch.setattr(service, "BackupFiles", lambda: BackupFiles(tmp_path))
    await ready(runtime, [event(text="合成项目资料，仅为屏幕上出现的文字。" * 80 + "长摘录结尾保留。")])

    async def model(messages, **kwargs):
        data = json.loads(messages[-1]["content"])
        return json.dumps(
            {
                "progress": [
                    {"text": "已确认的合成验收", "evidence_ids": [data["evidence"][0]["id"]]}
                ]
            }
        )

    runtime.ollama.chat = model
    app = create_app(
        runtime.settings,
        engine=runtime.engine,
        ollama=runtime.ollama,
        memory=runtime.memory,
        run_worker=False,
        mcp_store=runtime.mcp.store,
    )

    async def drafts():
        while True:
            async with runtime.sessions.begin() as session:
                row = await session.scalar(
                    select(Job).where(Job.kind == "draft", Job.status == "pending").limit(1)
                )
                payload = dict(row.payload) if row else None
                if row:
                    row.status = "done"
            if payload:
                await generate(runtime, payload)
            await asyncio.sleep(0.05)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    runtime.settings.server.port = port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    worker = asyncio.create_task(drafts())
    process = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        process = await asyncio.create_subprocess_exec(
            "bash",
            "scripts/npm.sh",
            "run",
            "test:e2e",
            "--",
            "optimization.spec.ts",
            cwd=ROOT,
            env={
                **os.environ,
                "WORKMEMORY_ENDPOINT": f"http://127.0.0.1:{port}",
                "WORKMEMORY_TEST_TOKEN": runtime.settings.api_token,
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), 120)
        assert process.returncode == 0, stdout.decode()
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()
