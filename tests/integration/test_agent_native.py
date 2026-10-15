"""Opt-in actual DesktopPet/WKWebView; temporary data, isolated DB, deterministic model."""

import asyncio
import os
import socket

import pytest
import uvicorn
from test_agent import ToolModel, seed
from workmemory.config import ROOT
from workmemory.main import create_app
from workmemory.reminders.scheduler import reminder_loop

pytestmark = pytest.mark.skipif(
    os.environ.get("WORKMEMORY_NATIVE_TEST") != "1", reason="Explicit macOS native UI check"
)


async def test_native_pet_chat(runtime, tmp_path):
    output = ROOT / ".cache/pet-native"
    output.mkdir(exist_ok=True, parents=True)
    build = ROOT / "macos/.build/arm64-apple-macosx/debug"
    # Launch the isolated accessory bundle through the same macOS entry as production.
    import plistlib
    bundle = output / "PetWebCheck.app"
    (bundle / "Contents/MacOS").mkdir(parents=True, exist_ok=True)
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": "PetWebCheck", "CFBundleIdentifier": "local.workmemory.petcheck",
        "CFBundleName": "拾忆原生验收", "CFBundlePackageType": "APPL", "LSUIElement": True,
        "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "14.0",
    }))
    executable = bundle / "Contents/MacOS/PetWebCheck"
    console = output / "console.log"
    console.write_text("")
    (output / "report.json").unlink(missing_ok=True)
    (output / "pid.txt").unlink(missing_ok=True)
    compile_process = await asyncio.create_subprocess_exec(
        "swiftc",
        "-I",
        str(build / "Modules"),
        str(ROOT / "tests/native/pet_chat.swift"),
        str(ROOT / "macos/Sources/WorkMemory/DesktopPet.swift"),
        *map(str, (build / "WorkMemoryCore.build").glob("*.o")),
        "-o",
        str(executable),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await asyncio.wait_for(compile_process.communicate(), 60)
    assert compile_process.returncode == 0, stdout.decode()
    sign = await asyncio.create_subprocess_exec("codesign", "--force", "--sign", "-", str(bundle), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    assert await sign.wait() == 0
    model = ToolModel()
    async def chat_tools(messages, tools, **kwargs):
        import json
        if "你正在为桌宠生成一句主动陪伴气泡" in json.dumps(messages, ensure_ascii=False):
            return {"text": json.dumps({"answer": "本喵正在偷偷给你加油～", "evidence_ids": [], "memory_status": "not_needed", "operation_status": "none", "operation_ids": []}), "tool_calls": []}
        if "慢速原生验收" in json.dumps(messages, ensure_ascii=False):
            await asyncio.sleep(2)
            return {"text": json.dumps({"answer": "原生后台回答已完成", "evidence_ids": [], "memory_status": "not_needed", "operation_status": "none"}), "tool_calls": []}
        return await model(messages, tools, **kwargs)
    runtime.ollama.chat_tools = chat_tools
    await seed(runtime)
    # The developer may be using a fullscreen app while this isolated UI check runs.
    from workmemory.companion.state import save_preferences
    async with runtime.sessions.begin() as session:
        await save_preferences(session, {"hide_fullscreen": False, "pet_visible": True})
    from workmemory.pets.skins import SkinLibrary
    library = SkinLibrary(tmp_path / "desktop-pets")
    import io

    from PIL import Image
    png = io.BytesIO()
    Image.new("RGBA", (24, 64), "red").save(png, format="PNG")
    # The first skin is uploaded only after DesktopPet has started with no library.
    assert not library.root.exists()
    png_path = tmp_path / "synthetic.png"
    png_path.write_bytes(png.getvalue())
    app = create_app(
        runtime.settings,
        engine=runtime.engine,
        ollama=runtime.ollama,
        memory=runtime.memory,
        run_worker=False,
        mcp_store=runtime.mcp.store,
        pet_library=library,
    )
    # Test-only trigger in the isolated server, absent from the application router.
    @app.post("/api/test/proactive")
    async def trigger_companion():
        from datetime import timedelta

        from workmemory.companion import proactive
        from workmemory.companion.proactive_config import ProactiveSettings
        from workmemory.db.models import AppState
        from workmemory.jobs.queue import now
        rt = app.state.runtime
        async with rt.sessions.begin() as session:
            await save_preferences(session, {"proactive": ProactiveSettings(topics=["playful"], quiet_start="00:00", quiet_end="00:00").model_dump()})
        await proactive.heartbeat(rt, True)
        await proactive.tick(rt)
        async with rt.sessions.begin() as session:
            ledger = await session.get(AppState, "proactive")
            ledger.value = {**ledger.value, "next_at": (now() - timedelta(seconds=1)).isoformat()}
        identifier = await proactive.tick(rt)
        assert identifier
        await proactive.generate(rt, identifier)
        from workmemory.db.models import ProactiveUtterance
        async with rt.sessions() as session:
            row = await session.get(ProactiveUtterance, identifier)
            assert row.status == "ready", (row.status, row.error, row.model_calls)
        return {"id": str(identifier)}

    # Register ahead of the frontend static mount.
    app.router.routes.insert(0, app.router.routes.pop())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    runtime.settings.server.port = port
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    process = None
    reminder_task = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        reminder_task = asyncio.create_task(reminder_loop(app.state.runtime))
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/open", "-n", "-W", "--stdout", str(console), "--stderr", "/dev/null", str(bundle),
            cwd=ROOT,
            env={
                **os.environ,
                "WORKMEMORY_ENDPOINT": f"http://127.0.0.1:{port}",
                "WORKMEMORY_TEST_TOKEN": runtime.settings.api_token,
                "WORKMEMORY_NATIVE_OUTPUT": str(output),
                "WORKMEMORY_NATIVE_DATA": str(tmp_path),
                "WORKMEMORY_TEST_PNG": str(png_path),
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), 210)
        assert process.returncode == 0, stdout.decode()
        assert (output / "report.json").exists(), console.read_text()
    finally:
        if reminder_task:
            reminder_task.cancel()
            await asyncio.gather(reminder_task, return_exceptions=True)
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        if (output / "pid.txt").exists():
            import signal
            pid = int((output / "pid.txt").read_text())
            check = await asyncio.create_subprocess_exec("ps", "-p", str(pid), "-o", "command=", stdout=asyncio.subprocess.PIPE)
            command, _ = await check.communicate()
            if command.decode().strip() == str(executable):
                os.kill(pid, signal.SIGTERM)
        server.should_exit = True
        await asyncio.wait_for(server_task, 15)
        sock.close()
