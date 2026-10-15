"""MCP host: real Streamable HTTP locally, HTTP/stdio externally, scoped run authority."""

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent, Tool
from starlette.responses import JSONResponse

from workmemory.db.models import AgentRun, ExternalSource
from workmemory.evidence.service import (
    copy_dependencies,
    dependencies,
    remember,
    resolve,
    valid_dependencies,
)
from workmemory.jobs.queue import now
from workmemory.mcp import weather
from workmemory.mcp.config import MCPStore
from workmemory.workspace.catalog import TOOLS
from workmemory.workspace.tools import execute

logging.getLogger("mcp").setLevel(logging.CRITICAL)
CALLER = contextvars.ContextVar("mcp_run", default=None)


def tool_name(server, name):
    value = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    base = f"mcp_{server}__{value}"
    return (
        base[:50] + "_" + hashlib.sha256(name.encode()).hexdigest()[:10]
        if len(base) > 64 or value != name
        else base
    )


def read_allowed(server, tool):
    # Explicit local selection AND a read-only contract are required. Annotations alone
    # never grant access. Stdio is a user-trusted installed program, not an OS sandbox.
    dangerous = re.search(
        r"(?:^|[_-])(write|delete|remove|create|update|send|execute|exec|shell|run|install|upload|edit|patch|set|post)(?:$|[_-])",
        tool.name,
        re.I,
    )
    if dangerous:
        return False
    known_exa = (
        urlsplit(server.url or "").hostname == "mcp.exa.ai" and tool.name == "web_search_exa"
    )
    return known_exa or bool(
        tool.annotations and tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
    )


def validate(schema, args):
    if not isinstance(args, dict) or len(json.dumps(args)) > 12000:
        raise ValueError("工具参数过大或格式错误")
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(args)


class MCPManager:
    def __init__(self, runtime, store=None):
        self.runtime = runtime
        self.store = store or MCPStore()
        self.tokens = {}
        self.cache = {}
        self.status = {}
        self.start_lock = asyncio.Lock()
        self.server_task = None
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.server = Server("workmemory")
        self.sessions = self.session_manager()

        @self.server.list_tools()
        async def list_tools():
            return [
                Tool(name=t["name"], description=t["description"], inputSchema=t["parameters"])
                for t in self.local_tools()
            ]

        @self.server.call_tool()
        async def call_tool(name, arguments):
            run_id = CALLER.get()
            if run_id is None:
                raise ValueError("运行凭证无效")
            await self.check_run(run_id)
            descriptor = next((t for t in self.local_tools() if t["name"] == name), None)
            if not descriptor:
                raise ValueError("工具未启用")
            validate(descriptor["parameters"], arguments)
            if name.startswith("weather__"):
                result = await weather.call(name.removeprefix("weather__"), arguments)
            else:
                try:
                    result = await execute(self.runtime, run_id, name, arguments)
                except HTTPException as exc:
                    if exc.status_code not in {404, 409, 422}:
                        raise
                    # These are local business validation messages, never provider bodies.
                    result = {"error": {"code": "object_changed" if exc.status_code == 409 else "invalid_arguments",
                                        "message": str(exc.detail)}}
            return [
                TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))
            ]

    def session_manager(self):
        return StreamableHTTPSessionManager(
            self.server,
            json_response=True,
            stateless=True,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1:*", "localhost:*", "127.0.0.1", "localhost"],
                allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
            ),
        )

    def local_tools(self):
        result = list(TOOLS)
        _, servers, _ = self.store.read()
        for server in servers:
            if server.enabled and server.transport == "weather":
                result.extend(
                    {**t, "name": "weather__" + t["name"]}
                    for t in weather.TOOLS
                    if t["name"] in server.allowed_tools
                )
        return result

    async def check_run(self, run_id):
        async with self.runtime.sessions() as session:
            run = await session.get(AgentRun, run_id)
            if not run or run.status not in {"running", "queued"}:
                raise ValueError("运行已结束或取消")

    async def _serve(self):
        async with self.sessions.run():
            self.ready.set()
            await self.stop.wait()

    async def start(self):
        async with self.start_lock:
            if self.server_task is None:
                self.server_task = asyncio.create_task(self._serve())
            await asyncio.wait_for(self.ready.wait(), 5)

    async def close(self):
        if self.server_task:
            self.stop.set()
            await self.server_task
            self.server_task = None
            self.ready = asyncio.Event()
            self.stop = asyncio.Event()
            self.sessions = self.session_manager()
        self.tokens.clear()
        self.cache.clear()

    async def asgi(self, scope, receive, send):
        if scope["type"] != "http":
            return
        headers = dict(scope.get("headers", []))
        token = headers.get(b"authorization", b"").decode().removeprefix("Bearer ")
        credential = self.tokens.get(token)
        if not credential or credential[1] < time.monotonic():
            await JSONResponse({"detail": "MCP 运行凭证无效"}, status_code=401)(
                scope, receive, send
            )
            return
        await self.start()
        marker = CALLER.set(credential[0])
        try:
            await self.sessions.handle_request(scope, receive, send)
        finally:
            CALLER.reset(marker)

    @asynccontextmanager
    async def connection(self, server=None, run_id=None):
        if server is None:
            await self.start()
            token = secrets.token_urlsafe(32)
            self.tokens[token] = (run_id, time.monotonic() + 120)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.asgi),
                    headers={"Authorization": "Bearer " + token},
                    timeout=30,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    async with streamable_http_client(
                        "http://127.0.0.1/api/mcp", http_client=client
                    ) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            yield session
            finally:
                self.tokens.pop(token, None)
        elif server.transport == "stdio":
            env = {k: os.environ[k] for k in ["PATH", "LANG", "TMPDIR"] if k in os.environ}
            env.update(server.env)
            # Only explicit configuration can start a program. No shell or package downloads.
            with open(os.devnull, "w") as errors:
                async with stdio_client(
                    StdioServerParameters(command=server.command, args=server.args, env=env),
                    errlog=errors,
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
        else:
            headers = {"Authorization": "Bearer " + server.token} if server.token else {}
            async with httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(15, read=25),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with streamable_http_client(server.url, http_client=client) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session

    async def discover(self, server, refresh=False):
        key = hashlib.sha256(server.model_dump_json().encode()).hexdigest()
        if not refresh and key in self.cache and time.monotonic() - self.cache[key][0] < 300:
            return self.cache[key][1]
        if server.transport == "weather":
            return [{**t, "read_only": True, "eligible": True} for t in weather.TOOLS]
        async with asyncio.timeout(20):
            async with self.connection(server) as session:
                value = await session.list_tools()
        tools = []
        for tool in value.tools[:60]:
            if len(json.dumps(tool.inputSchema)) > 12000:
                continue
            # Annotations describe the remote service; local explicit allow-list grants access.
            tools.append(
                {
                    "name": tool.name,
                    "description": (tool.description or tool.name)[:900],
                    "parameters": tool.inputSchema,
                    "read_only": bool(tool.annotations and tool.annotations.readOnlyHint),
                    "eligible": read_allowed(server, tool),
                }
            )
        self.cache[key] = (time.monotonic(), tools)
        return tools

    async def catalog(self):
        result = self.local_tools()
        _, servers, _ = self.store.read()

        async def load(server):
            try:
                tools = await self.discover(server)
                allowed = [t for t in tools if t["name"] in server.allowed_tools and t["eligible"]]
                self.status[server.id] = {"available": True, "error": None}
                return [
                    {
                        "name": tool_name(server.id, t["name"]),
                        "description": f"外部服务 {server.label}：" + t["description"],
                        "parameters": t["parameters"],
                    }
                    for t in allowed
                ]
            except Exception:
                self.status[server.id] = {"available": False, "error": "连接或工具发现失败"}
                return []

        results = await asyncio.gather(
            *(load(s) for s in servers if s.enabled and s.transport != "weather")
        )
        for tools in results:
            result.extend(tools)
        return result[:50]

    async def test(self, server):
        try:
            tools = await self.discover(server, refresh=True)
            if server.transport == "weather":
                value = await weather.call("search_cities", {"name": "Shanghai"})
                if not value.get("cities"):
                    raise ValueError("城市查询无结果")
            self.status[server.id] = {"available": True, "error": None}
            return {"ok": True, "tools": tools, "notice": "连接及工具发现通过；尚未自动启用。"}
        except Exception:
            self.status[server.id] = {"available": False, "error": "连接测试失败"}
            raise HTTPException(502, "MCP 连接测试失败，请检查地址、程序及凭据") from None

    async def call(self, run_id, name, args):
        await self.check_run(run_id)
        local = next((t for t in self.local_tools() if t["name"] == name), None)
        external = name.startswith("weather__") or local is None
        server = None
        remote_name = name
        if local:
            descriptor = local
        else:
            _, servers, _ = self.store.read()
            for candidate in servers:
                if (
                    not candidate.enabled
                    or candidate.transport == "weather"
                    or not name.startswith(f"mcp_{candidate.id}__")
                ):
                    continue
                try:
                    discovered = await self.discover(candidate)
                except Exception:
                    continue
                for tool in discovered:
                    if (
                        tool["eligible"]
                        and tool["name"] in candidate.allowed_tools
                        and tool_name(candidate.id, tool["name"]) == name
                    ):
                        server, descriptor, remote_name = candidate, tool, tool["name"]
                        break
                if server:
                    break
            if not server:
                return {"error": {"code": "tool_unavailable", "message": "工具未启用或已不可用"}}
        try:
            validate(descriptor["parameters"], args)
        except Exception:
            return {
                "error": {
                    "code": "invalid_arguments",
                    "message": "工具参数不符合声明，请更正后重试",
                }
            }
        if external and not await self.outbound_allowed(run_id, args):
            return {
                "error": {
                    "code": "private_query",
                    "message": "查询包含私人来源正文或内部标识，未发送。请改用公开关键词或单独提供城市。",
                }
            }
        try:
            attempts = 2 if external else 1
            for attempt in range(attempts):
                try:
                    async with asyncio.timeout(25 if external else 30):
                        async with self.connection(server, run_id) as session:
                            value = await session.call_tool(remote_name, args)
                    break
                except (httpx.TransportError, TimeoutError):
                    if attempt + 1 == attempts:
                        raise
            if value.isError:
                return {
                    "error": {
                        "code": "tool_failed",
                        "message": "工具未完成。对象可能已变化，或参数需要调整；没有确认执行任何修改。",
                    }
                }
            text = "\n".join(getattr(block, "text", "") for block in value.content)
            if len(text) > 200000:
                return {
                    "error": {"code": "result_too_large", "message": "工具结果过大，请缩小查询范围"}
                }
            await self.check_run(run_id)
            if not external:
                return json.loads(text)
            provider = server.id if server else "weather"
            result = json.loads(text) if provider == "weather" else {"text": text}
            return await self.external_result(run_id, provider, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            return {
                "error": {
                    "code": "external_unavailable" if external else "tool_failed",
                    "message": "工具连接失败或超时，请稍后重试。本地记忆仍可使用。",
                }
            }

    async def outbound_allowed(self, run_id, args):
        outgoing = json.dumps(args, ensure_ascii=False)
        if len(outgoing) > 2500:
            return False
        compact = re.sub(r"\s+", "", outgoing).casefold()
        async with self.runtime.sessions() as session:
            for dep in await dependencies(session, "run", run_id):
                if dep.source_kind == "external":
                    continue
                if str(dep.source_id) in outgoing:
                    return False
                item = await resolve(session, dep.snippet_id, self.runtime.settings.retention_days)
                text = re.sub(r"\s+", "", item["content"]).casefold() if item else ""
                # Reject copied private passages, including excerpts spanning word boundaries.
                if len(text) >= 20 and any(
                    text[i : i + 20] in compact for i in range(len(text) - 19)
                ):
                    return False
        return True

    async def external_result(self, run_id, provider, result):
        records = []
        if provider == "weather":
            records = [
                (
                    result["title"],
                    result["source_url"],
                    json.dumps(result, ensure_ascii=False)[:6000],
                )
            ]
        else:
            text = result.get("text", "")
            blocks = re.split(r"(?m)(?=^Title:)", text)
            for block in blocks:
                urls = re.findall(r"https?://[^\s<>\"\]]+", block)
                if not urls:
                    continue
                title = re.search(r"(?m)^Title:\s*(.+)", block)
                records.append(
                    (
                        title.group(1) if title else "联网查询结果",
                        urls[0].rstrip(").,;"),
                        block[:1400],
                    )
                )
            records = records[:4]
        async with self.runtime.sessions.begin() as session:
            deps = await dependencies(session, "run", run_id)
            if not await valid_dependencies(
                session, deps, lock=True, retention_days=self.runtime.settings.retention_days
            ):
                raise ValueError("来源已失效")
            run = await session.get(AgentRun, run_id)
            if not run or run.status not in {"queued", "running"}:
                raise ValueError("运行已取消")
            sources = []
            for title, url, content in records:
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
                    continue
                row = ExternalSource(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    conversation_id=run.conversation_id,
                    provider=provider,
                    title=title[:300],
                    url=url[:2000],
                    content=content,
                    retrieved_at=now(),
                    expires_at=now() + timedelta(days=30),
                )
                session.add(row)
                await session.flush()
                await copy_dependencies(session, "run", run_id, "external", row.id)
                if question := await resolve(session, f"chat:{run.message_id}"):
                    await remember(session, "external", row.id, [question])
                await remember(
                    session, "run", run_id, [await resolve(session, f"external:{row.id}")]
                )
                sources.append(
                    {
                        "id": str(row.id),
                        "title": row.title,
                        "url": row.url,
                        "retrieved_at": row.retrieved_at.isoformat(),
                        "content": content,
                    }
                )
        if provider == "weather":
            # Structured weather once, not repeated as a second full excerpt in each source.
            result.pop("source_url", None)
            result.pop("title", None)
            for item in sources:
                item["content"] = "天气数据见本次工具返回，时间和单位以 weather 中字段为准。"
        else:
            result = {
                "text": "\n\n".join(r[2] for r in records)[:4000] or result.get("text", "")[:2500]
            }
        return {
            **result,
            "external_sources": sources,
            "notice": "外部实时资料，不是用户个人记忆；回答须引用来源ID并注明适用时间。",
        }
