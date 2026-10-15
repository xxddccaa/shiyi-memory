"""Opt-in current model, real Pi and isolated PostgreSQL; all input is synthetic."""
import asyncio
import json
import os

import pytest
from sqlalchemy import select
from test_agent import seed
from test_proactive import due
from workmemory.agent.runtime import AgentManager
from workmemory.companion import proactive
from workmemory.companion.proactive_config import ProactiveSettings
from workmemory.companion.state import save_preferences
from workmemory.config import ROOT
from workmemory.config.generation import GenerationStore
from workmemory.db.models import ProactiveUtterance
from workmemory.inference.ollama import Ollama

pytestmark = pytest.mark.skipif(os.environ.get("WORKMEMORY_PROACTIVE_LIVE") != "1", reason="Explicit synthetic model check")


async def test_current_model_proactive_samples(runtime, monkeypatch):
    rt = runtime
    rt.ollama = Ollama(rt.settings.ollama, GenerationStore(rt.settings.ollama).current)
    rt.agent = AgentManager(rt)
    await rt.ollama.start()
    async with rt.sessions.begin() as session:
        await save_preferences(session, {"proactive": ProactiveSettings(quiet_start="00:00", quiet_end="00:00").model_dump()})
    await seed(rt, text="合成青柠项目今天核对了一项测试步骤，后面还需继续验证。")
    from workmemory.mcp import weather
    rt.mcp.store.save(0, server={"id": "weather", "label": "合成天气", "transport": "weather", "enabled": True, "allowed_tools": ["get_weather"]},
        city={"name": "合成城市", "latitude": 30, "longitude": 120}, set_city=True)
    async def synthetic_weather(name, args):
        return {"weather": {"current": {"time": "2026-10-12T17:00", "temperature_2m": 18, "weather_code": 0}, "current_units": {"temperature_2m": "°C"}}}
    monkeypatch.setattr(weather, "call", synthetic_weather)
    async def presence():
        while True:
            await proactive.heartbeat(rt, True)
            await asyncio.sleep(2)
    heartbeat_task = asyncio.create_task(presence())
    outputs = []
    attempts = []
    original_chat = rt.ollama.chat_tools
    async def record(messages, tools, **kwargs):
        response = await original_chat(messages, tools, **kwargs)
        attempts.append(response.get("text"))
        return response
    rt.ollama.chat_tools = record
    try:
        for topic in ["playful", "work", "weather", "break"]:
            identifier = await due(rt, topic)
            await proactive.generate(rt, identifier)
            async with rt.sessions() as session:
                row = await session.scalar(select(ProactiveUtterance).where(ProactiveUtterance.id == identifier))
                assert row.status == "ready", (topic, row.status, row.error, row.model_calls)
                assert row.content and len(row.content) <= 80
                assert "青柠" not in row.content and "蓝鲸" not in row.content
                outputs.append({"topic": topic, "text": row.content, "model_calls": row.model_calls})
        (ROOT / ".cache/proactive-live.json").write_text(json.dumps(outputs, ensure_ascii=False, indent=2))
    finally:
        (ROOT / ".cache/proactive-live-attempts.json").write_text(json.dumps(attempts, ensure_ascii=False, indent=2))
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        await rt.agent.close()
        await rt.ollama.close()
