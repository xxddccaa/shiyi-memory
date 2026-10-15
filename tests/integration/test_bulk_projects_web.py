"""Real browser, Pi and isolated PostgreSQL; no production projects are modified."""

import asyncio
import json
import os
import socket

import pytest
import uvicorn
from test_agent import ToolModel
from test_bulk_projects import seed_projects
from workmemory.config import ROOT
from workmemory.main import create_app

pytestmark = pytest.mark.skipif(os.environ.get("WORKMEMORY_BROWSER_TEST") != "1", reason="Explicit browser integration")


@pytest.mark.parametrize("surface", ["web", "pet", "projects"])
async def test_bulk_projects_browser(runtime, surface):
    await seed_projects(runtime)
    probe = ToolModel()

    async def model(messages, tools, **kwargs):
        if tools and tools[0]["name"] == "echo_probe":
            return await probe(messages, tools, **kwargs)
        results = [m for m in messages if m["role"] == "toolResult"]
        if not results:
            return {"text": "", "tool_calls": [{"id": "bulk", "name": "propose_operation", "arguments": {"action": "project.delete_many", "all_projects": True}}]}
        data = json.loads(results[-1]["content"][0]["text"])
        return {"text": json.dumps({"answer": "已准备32个项目的完整清单，请核对后一次确认。", "evidence_ids": [], "memory_status": "not_needed", "operation_status": "pending", "operation_ids": [data["proposal"]["id"]]}), "tool_calls": []}

    runtime.ollama.chat_tools = model
    app = create_app(runtime.settings, engine=runtime.engine, ollama=runtime.ollama, memory=runtime.memory, run_worker=False, mcp_store=runtime.mcp.store)
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
            await asyncio.sleep(0.05)
        assert server.started
        process = await asyncio.create_subprocess_exec(
            "bash", "scripts/npm.sh", "run", "test:e2e", "--", "bulk-projects.spec.ts", cwd=ROOT,
            env={**os.environ, "WORKMEMORY_ENDPOINT": f"http://127.0.0.1:{port}", "WORKMEMORY_TEST_TOKEN": runtime.settings.api_token, "WORKMEMORY_PROJECT_SURFACE": surface},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), 100)
        assert process.returncode == 0, output.decode()
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()
