"""Application-owned Pi checkpoints and native command journal. No Pi file sessions."""
import asyncio
import codecs
import difflib
import json
import os
import signal
import sys
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import delete, select, update

from workmemory.agent.errors import AgentFailure
from workmemory.config import ROOT
from workmemory.db.models import (
    AgentRun,
    AgentToolCall,
    AgentToolOutput,
    AgentWorkspace,
    ChatMessage,
    ConversationDigest,
    EvidenceDependency,
    PiSession,
)
from workmemory.evidence.service import (
    copy_dependencies,
    dependencies,
    remember,
    resolve,
    valid_dependencies,
)
from workmemory.jobs.queue import now

CODING_TOOLS = {"read", "write", "edit", "bash", "grep", "find", "ls", "read_tool_output"}


def local_directory(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    try:
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError()
    except (OSError, ValueError):
        raise HTTPException(422, "工作目录不存在或无法访问") from None
    return str(path)


async def purge_session(session, identifier):
    row = await session.get(PiSession, identifier)
    if row:
        row.data, row.context, row.status = None, {}, "invalidated"
        row.epoch += 1
    calls = select(AgentToolCall.id).where(AgentToolCall.conversation_id == identifier)
    await session.execute(delete(AgentToolOutput).where(AgentToolOutput.call_id.in_(calls)))
    await session.execute(update(AgentToolCall).where(AgentToolCall.conversation_id == identifier)
        .values(arguments=None, result=None, status="invalidated"))
    await session.execute(delete(EvidenceDependency).where(
        EvidenceDependency.consumer_kind == "pi_session", EvidenceDependency.consumer_id == identifier))


async def load_session(session, conversation, run, retention_days):
    row = await session.get(PiSession, conversation.id)
    if row and not await valid_dependencies(session, await dependencies(session, "pi_session", row.id), retention_days=retention_days):
        await purge_session(session, row.id)
    if not row:
        row = PiSession(id=conversation.id, epoch=0, context={}, status="ready")
        session.add(row)
        await session.flush()
    await copy_dependencies(session, "pi_session", row.id, "run", run.id)
    # Stop the old summarizer for migrated conversations, including queued jobs.
    await session.execute(update(ConversationDigest).where(ConversationDigest.conversation_id == conversation.id)
        .values(content=None, status="superseded"))
    workspace = None
    if conversation.coding_enabled:
        target = await session.get(AgentWorkspace, conversation.workspace_id) if conversation.workspace_id else None
        if not target:
            raise AgentFailure("workspace_unavailable", stage="agent")
        try:
            path = local_directory(target.path)
        except HTTPException:
            raise AgentFailure("workspace_unavailable", stage="agent") from None
        workspace = {"path": path, "extensions": target.extensions}
    recovery = []
    if row.data:
        pending = {}
        for entry in row.data.get("entries", []):
            message = entry.get("message", {})
            if message.get("role") == "assistant":
                for block in message.get("content", []):
                    if block.get("type") == "toolCall":
                        pending[block["id"]] = block
            elif message.get("role") == "toolResult":
                pending.pop(message.get("toolCallId"), None)
        for call_id, block in pending.items():
            journal = await session.scalar(select(AgentToolCall).where(
                AgentToolCall.conversation_id == conversation.id, AgentToolCall.call_id == call_id)
                .order_by(AgentToolCall.created_at.desc()).limit(1))
            result = journal.result if journal and journal.result and journal.status in {"completed", "failed"} else None
            recovery.append({"role": "toolResult", "toolCallId": call_id, "toolName": block["name"],
                "content": result.get("content", []) if result else [{"type": "text", "text": "此工具在上轮中断，执行结果尚未核实。先只读检查实际状态，不要自动重放。"}],
                "isError": bool(result.get("isError")) if result else True,
                "timestamp": int(now().timestamp() * 1000)})
    # Explicitly accepted companion bubbles arrived outside the Pi transcript.
    # Import through SessionManager.appendMessage on the next run; do not rewrite native entries.
    for identifier in row.context.get("companion_pending", []) if row.data else []:
        message = await session.get(ChatMessage, uuid.UUID(identifier))
        if not message or message.status != "ready" or not message.content:
            continue
        snippet = await resolve(session, f"chat:{message.id}")
        if not snippet:
            continue
        await remember(session, "run", run.id, [snippet])
        await copy_dependencies(session, "message", message.id, "run", run.id)
        recovery.append({"role": "assistant", "content": [{"type": "text", "text": message.content}],
            "api": "openai-completions", "provider": "workmemory", "model": "application-model", "stopReason": "stop",
            "timestamp": int(message.created_at.timestamp() * 1000),
            "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                      "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}})
    return {"data": row.data, "epoch": row.epoch, "recovery": recovery}, workspace


class CodingRun:
    def __init__(self, runtime, run_id, conversation_id, epoch, workspace):
        self.runtime, self.run_id, self.conversation_id = runtime, run_id, conversation_id
        self.epoch, self.workspace = epoch, workspace
        self.processes = set()

    async def check(self, session):
        # Match answer publication: canonical sources before run/checkpoint rows.
        # Compaction lifecycle RPCs can overlap a model request or cancellation.
        deps = await dependencies(session, "run", self.run_id)
        if not await valid_dependencies(session, deps, lock=True, retention_days=self.runtime.settings.retention_days):
            raise AgentFailure("source_invalidated", stage="validation")
        run = await session.scalar(select(AgentRun).where(AgentRun.id == self.run_id).with_for_update())
        row = await session.scalar(select(PiSession).where(PiSession.id == self.conversation_id).with_for_update())
        if not run or run.status not in {"queued", "running"}:
            raise AgentFailure("cancelled", stage="agent")
        if not row or row.epoch != self.epoch:
            raise AgentFailure("source_invalidated", stage="validation")
        source = await session.get(ChatMessage, run.message_id)
        if not source or source.status != "ready" or not source.content or source.created_at <= now() - timedelta(days=30):
            raise AgentFailure("source_invalidated", stage="validation")
        return row, source

    async def handle(self, request):
        kind = request["type"]
        if kind == "native_bash":
            return await self.bash(request)
        if kind == "output_read":
            async with self.runtime.sessions.begin() as session:
                await self.check(session)
                return await output_page(session, uuid.UUID(request["output_id"]), self.conversation_id, request.get("offset", 0))
        async with self.runtime.sessions.begin() as session:
            row, source = await self.check(session)
            if kind == "checkpoint":
                if request.get("epoch") != row.epoch:
                    raise AgentFailure("source_invalidated", stage="validation")
                row.data, row.context, row.status, row.updated_at = request["data"], request["context"], "ready", now()
                await copy_dependencies(session, "run", self.run_id, "pi_session", row.id)
                await remember(session, "pi_session", row.id, [await resolve(session, f"chat:{source.id}")])
                return {"saved": True}
            if kind == "session_event":
                from workmemory.db.models import AgentEvent
                event = request.get("kind")
                if event not in {"compaction_start", "compaction_end"}:
                    raise AgentFailure("tool_protocol")
                row.context = {**row.context, "compacting": event == "compaction_start"}
                session.add(AgentEvent(run_id=self.run_id, kind=event, data={"failed": bool(request.get("failed"))}))
                from workmemory.companion.state import emit
                await emit(session, "run", self.run_id)
                return {}
            call = await session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == self.run_id, AgentToolCall.call_id == request["call_id"]))
            if kind == "tool_begin":
                if call:
                    if call.status in {"completed", "failed"} and call.result:
                        return {"result": call.result}
                    raise AgentFailure("execution_uncertain", stage="tool")
                call = AgentToolCall(id=uuid.uuid4(), run_id=self.run_id, conversation_id=self.conversation_id,
                    call_id=request["call_id"], name=request["name"], arguments=request["arguments"], status="running",
                    side_effect=request["name"] not in {"read", "grep", "find", "ls", "read_tool_output", "search_memories", "get_evidence", "get_neighbors", "get_project_brief", "list_workspace", "list_objects", "get_object", "get_work_day", "propose_operation", "propose_merge"} and not request["name"].startswith(("mcp_", "weather__")))
                session.add(call)
                await session.flush()
                await copy_dependencies(session, "run", self.run_id, "pi_session", row.id)
                from workmemory.db.models import AgentEvent
                session.add(AgentEvent(run_id=self.run_id, kind="tool_started", data={"tool": call.name, "call_id": str(call.id)}))
                return {"id": str(call.id)}
            if kind == "tool_end" and call:
                result = dict(request["result"])
                if change := request.get("change"):
                    # Keep a reviewer-readable patch, not two complete copies of a file.
                    result["change"] = {"path": change["path"], "diff": "".join(difflib.unified_diff(
                        (change.get("before") or "").splitlines(keepends=True), (change.get("after") or "").splitlines(keepends=True),
                        fromfile=change["path"], tofile=change["path"]))}
                call.result, call.status = result, "failed" if result.get("isError") else "completed"
                await copy_dependencies(session, "run", self.run_id, "pi_session", row.id)
                from workmemory.db.models import AgentEvent
                session.add(AgentEvent(run_id=self.run_id, kind="tool_finished", data={"tool": call.name, "call_id": str(call.id)}))
                return {}
            raise AgentFailure("tool_protocol", stage="tool")

    async def bash(self, request):
        if not self.workspace:
            raise AgentFailure("workspace_unavailable", stage="tool")
        async with self.runtime.sessions.begin() as session:
            await self.check(session)
            call = await session.scalar(select(AgentToolCall).where(AgentToolCall.run_id == self.run_id, AgentToolCall.call_id == request["call_id"], AgentToolCall.status == "running"))
            if not call:
                raise AgentFailure("tool_protocol", stage="tool")
            identifier = call.id
        args = request["arguments"]
        timeout = args.get("timeout")
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            return {"isError": True, "content": [{"type": "text", "text": "timeout 必须是正秒数。"}]}
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TMPDIR", "HOME", "USER", "SHELL"}}
        process = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / "scripts/agent_command.py"), cwd=self.workspace["path"], env=env,
            start_new_session=True, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self.processes.add(process)
        process.stdin.write((json.dumps({"command": args["command"], "parent_pid": os.getpid()}) + "\n").encode())
        await process.stdin.drain()
        process.stdin.close()
        tail, sequence, total = "", 0, 0
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        async def drain():
            nonlocal tail, sequence, total
            while block := await process.stdout.read(32768):
                text = decoder.decode(block)
                if not text:
                    continue
                async with self.runtime.sessions.begin() as session:
                    await self.check(session)
                    session.add(AgentToolOutput(call_id=identifier, sequence=sequence, content=text))
                sequence += 1
                total += len(text)
                tail = (tail + text)[-16000:]
            rest = decoder.decode(b"", final=True)
            if rest:
                async with self.runtime.sessions.begin() as session:
                    await self.check(session)
                    session.add(AgentToolOutput(call_id=identifier, sequence=sequence, content=rest))
                tail += rest
            await process.wait()
        async def watch_sources():
            while True:
                await asyncio.sleep(0.5)
                async with self.runtime.sessions.begin() as session:
                    await self.check(session)
        draining = asyncio.create_task(drain())
        watching = asyncio.create_task(watch_sources())
        try:
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait([draining, watching], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            return {"isError": process.returncode != 0,
                "content": [{"type": "text", "text": tail + f"\n退出码: {process.returncode}\noutput_id: {identifier}" + ("\n此前输出已省略；用 read_tool_output 分页读取。" if total > 16000 else "")}],
                "details": {"output_id": str(identifier), "exit_code": process.returncode, "truncated": total > 16000}}
        except TimeoutError:
            return {"isError": True, "content": [{"type": "text", "text": f"命令超时，已停止。output_id: {identifier}"}]}
        finally:
            draining.cancel()
            watching.cancel()
            await asyncio.gather(draining, watching, return_exceptions=True)
            await self.stop_process(process)
            self.processes.discard(process)

    @staticmethod
    async def stop_process(process):
        # Kill the owned process group even if its shell has exited leaving children.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    async def close(self):
        await asyncio.gather(*(self.stop_process(p) for p in list(self.processes)), return_exceptions=True)
        async with self.runtime.sessions.begin() as session:
            row = await session.get(PiSession, self.conversation_id)
            if row and row.epoch == self.epoch and row.context.get("compacting"):
                row.context = {**row.context, "compacting": False}
            await session.execute(update(AgentToolCall).where(AgentToolCall.run_id == self.run_id, AgentToolCall.status == "running")
                .values(status="uncertain"))


async def output_page(session, identifier, conversation_id, offset=0):
    call = await session.get(AgentToolCall, identifier)
    if not call or call.conversation_id != conversation_id or call.status == "invalidated":
        raise HTTPException(404, "执行输出不存在或已失效")
    rows = (await session.scalars(select(AgentToolOutput).where(AgentToolOutput.call_id == identifier, AgentToolOutput.sequence >= max(0, offset))
        .order_by(AgentToolOutput.sequence).limit(9))).all()
    return {"text": "".join(r.content for r in rows[:8]), "next_offset": rows[8].sequence if len(rows) > 8 else None}
