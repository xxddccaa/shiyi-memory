"""Real browser and PostgreSQL; capture status is a deterministic metadata fixture."""
import asyncio
import os
import socket

import pytest
import uvicorn
from test_pipeline import ready
from workmemory.config import ROOT
from workmemory.db.models import Episode
from workmemory.evidence.archive import archive_episode
from workmemory.main import create_app

pytestmark = pytest.mark.skipif(os.environ.get("WORKMEMORY_BROWSER_TEST") != "1", reason="Explicit browser integration")


async def test_evidence_browser(runtime, monkeypatch):
    from workmemory.api import routes
    monkeypatch.setattr(routes, "collector_status", lambda settings: {
        "running": True, "paused": False, "screen_locked": False,
        "accessibility_granted": False, "screen_recording_granted": False, "ocr_mode": "always"})
    identifier = await ready(runtime)
    async with runtime.sessions.begin() as session:
        episode = await session.get(Episode, identifier)
        original = episode.summary
        episode.summary = "合成旧摘要：未经原文确认的成果"
        await archive_episode(session, episode)
        episode.summary = original
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama,
        memory=runtime.memory, run_worker=False, mcp_store=runtime.mcp.store)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    runtime.settings.server.port = port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    process = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.05)
        assert server.started
        process = await asyncio.create_subprocess_exec("bash", "scripts/npm.sh", "run", "test:e2e", "--", "evidence.spec.ts",
            cwd=ROOT, env={**os.environ, "WORKMEMORY_ENDPOINT": f"http://127.0.0.1:{port}",
                          "WORKMEMORY_TEST_TOKEN": runtime.settings.api_token},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        stdout, _ = await asyncio.wait_for(process.communicate(), 120)
        assert process.returncode == 0, stdout.decode()
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()
