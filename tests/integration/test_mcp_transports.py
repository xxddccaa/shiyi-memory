"""Exercise HTTP and installed-program transports using real MCP SDK, no outside network."""

import asyncio
import json
import socket
import sys
from contextlib import asynccontextmanager

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from test_workspace_mcp import active_run
from workmemory.mcp.config import ServerConfig

pytestmark = pytest.mark.integration


@asynccontextmanager
async def http_server():
    server = FastMCP("synthetic", stateless_http=True, json_response=True)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def search(query: str) -> str:
        return f"Title: Synthetic documentation\nURL: https://example.com/{query}\nBounded test excerpt"

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def delete_record(identifier: str) -> str:
        raise AssertionError("Write tool must never execute")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = server.streamable_http_app()
    runner = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    task = asyncio.create_task(runner.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if runner.started:
                break
            await asyncio.sleep(0.02)
        assert runner.started
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        runner.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


async def test_http_discovery_readonly_namespaces_schema_and_disabled(runtime):
    async with http_server() as url:
        run = await active_run(runtime)
        for revision, identifier in enumerate(["first", "second"]):
            runtime.mcp.store.save(
                revision,
                server={
                    "id": identifier,
                    "label": identifier,
                    "url": url,
                    "enabled": True,
                    "allowed_tools": ["search", "delete_record"],
                },
            )
        names = {t["name"] for t in await runtime.mcp.catalog()}
        assert {"mcp_first__search", "mcp_second__search"} <= names
        assert not any("delete_record" in n for n in names)
        bad = await runtime.mcp.call(run.id, "mcp_first__search", {"query": 5})
        assert bad["error"]["code"] == "invalid_arguments"
        result = await runtime.mcp.call(run.id, "mcp_first__search", {"query": "public"})
        assert result["external_sources"][0]["url"] == "https://example.com/public"
        runtime.mcp.store.save(
            2,
            server={
                "id": "first",
                "label": "first",
                "url": url,
                "enabled": False,
                "allowed_tools": ["search"],
            },
        )
        assert (await runtime.mcp.call(run.id, "mcp_first__search", {"query": "x"}))["error"][
            "code"
        ] == "tool_unavailable"
        assert (await runtime.mcp.call(run.id, "mcp_second__delete_record", {"identifier": "x"}))[
            "error"
        ]["code"] == "tool_unavailable"


async def test_installed_stdio_transport_does_not_inherit_model_secrets(
    runtime, tmp_path, monkeypatch
):
    program = tmp_path / "reader.py"
    program.write_text("""from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
import os
server = FastMCP("synthetic-stdio")
@server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def search(query: str) -> str:
    assert "SYNTHETIC_MODEL_KEY" not in os.environ
    assert os.environ.get("READ_TEST") == "allowed"
    return "Title: Stdio result\\nhttps://example.com/stdio\\n" + query
server.run(transport="stdio")
""")
    monkeypatch.setenv("SYNTHETIC_MODEL_KEY", "do-not-forward")
    config = ServerConfig(
        id="reader",
        label="Reader",
        transport="stdio",
        command=sys.executable,
        args=[str(program)],
        env={"READ_TEST": "allowed"},
        enabled=True,
        allowed_tools=["search"],
    )
    runtime.mcp.store.save(0, server=config.model_dump())
    result = await runtime.mcp.test(config)
    assert result["ok"] and result["tools"][0]["eligible"]
    run = await active_run(runtime)
    value = await runtime.mcp.call(run.id, "mcp_reader__search", {"query": "public synthetic"})
    assert "public synthetic" in value["text"]
    assert "do-not-forward" not in json.dumps(value)


async def test_failed_discovery_does_not_remove_local_tools(runtime):
    runtime.mcp.store.save(
        0,
        server={
            "id": "offline",
            "label": "Offline",
            "url": "http://127.0.0.1:1/mcp",
            "enabled": True,
            "allowed_tools": ["search"],
        },
    )
    tools = await runtime.mcp.catalog()
    assert "get_work_day" in {t["name"] for t in tools}
    assert not runtime.mcp.status["offline"]["available"]
