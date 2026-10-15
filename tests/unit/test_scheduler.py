import asyncio

import httpx
from workmemory.config import OllamaConfig
from workmemory.inference.ollama import Ollama


async def test_interactive_requests_overtake_background_queue():
    model = Ollama(OllamaConfig())
    seen = []

    async def handler(request):
        import json

        body = json.loads(request.content)
        seen.append(body["model"])
        return httpx.Response(200, json={"ok": True})

    await model.client.aclose()
    model.client = httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(handler)
    )
    background = asyncio.create_task(model.request("/test", {"model": "background"}, 20))
    interactive = asyncio.create_task(model.request("/test", {"model": "interactive"}, 0))
    await asyncio.sleep(0)
    await model.start()
    await asyncio.gather(background, interactive)
    assert seen == ["interactive", "background"]
    await model.close()
