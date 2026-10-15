import asyncio
import contextvars
import heapq
import itertools
import time
from collections import deque

import httpx

from workmemory.config.generation import local_generation
from workmemory.inference.generation import (
    ModelConfigurationChanged,
    chat_request,
    content,
    headers,
)

priority_context = contextvars.ContextVar("inference_priority", default=20)


class Ollama:
    """Shared priority scheduler: selected generation service plus local Ollama embeddings."""

    def __init__(self, settings, generation=None):
        self.settings = settings
        self.generation = generation or local_generation(settings)
        self.generation_revision = 0
        self.client = httpx.AsyncClient(
            base_url=settings.url,
            timeout=settings.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )
        self.condition = asyncio.Condition()
        self.pending = []
        self.sequence = itertools.count()
        self.metrics = deque(maxlen=200)
        self.runner = None
        self.current = None

    @property
    def generation_model(self):
        return self.generation.model

    def configure_generation(self, config):
        self.generation = config
        self.generation_revision += 1

    async def start(self):
        self.runner = asyncio.create_task(self._run())

    async def close(self):
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        for _, _, _, future, _ in self.pending:
            future.cancel()
        self.pending.clear()
        await self.client.aclose()

    async def request(self, path, body, priority=None, *, generation=None):
        future = asyncio.get_running_loop().create_future()
        async with self.condition:
            heapq.heappush(
                self.pending,
                (
                    priority if priority is not None else priority_context.get(),
                    next(self.sequence),
                    (path, body, generation, self.generation_revision),
                    future,
                    time.monotonic(),
                ),
            )
            self.condition.notify()
        return await future

    async def _run(self):
        while True:
            async with self.condition:
                await self.condition.wait_for(lambda: bool(self.pending))
                _, _, (path, body, generation, revision), future, submitted = heapq.heappop(
                    self.pending
                )
            if future.cancelled():
                continue
            if generation is not None and revision != self.generation_revision:
                future.set_exception(ModelConfigurationChanged("生成配置已变更，请重试"))
                continue
            started = time.monotonic()
            self.current = body.get("model")
            try:
                if generation is None:
                    response = await self.client.post(path, json=body)
                else:
                    response = await self.client.post(
                        generation.base_url + path,
                        json=body,
                        headers=headers(generation),
                        timeout=httpx.Timeout(generation.timeout_seconds, connect=10),
                    )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or data.get("error"):
                    raise ValueError("模型服务拒绝了请求")
                self.metrics.append(
                    {
                        "model": self.current,
                        "role": "generation" if generation else "embedding",
                        "provider": generation.provider if generation else "ollama",
                        "seconds": round(time.monotonic() - started, 3),
                        "wait_seconds": round(started - submitted, 3),
                        "load_seconds": (data.get("load_duration", 0) / 1e9)
                        if generation is None or generation.provider == "ollama"
                        else None,
                        "eval_tokens": data.get(
                            "eval_count", data.get("usage", {}).get("completion_tokens", 0)
                        ),
                        "at": time.time(),
                    }
                )
                if not future.done():
                    future.set_result(data)
            except asyncio.CancelledError:
                future.cancel()
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                self.current = None

    async def chat(self, messages, schema=None, priority=None, max_tokens=1600):
        config = self.generation
        path, body = chat_request(config, messages, schema, max_tokens, self.settings.context_size)
        result = await self.request(path, body, priority, generation=config)
        return content(config, result)

    async def chat_tools(self, messages, tools, priority=0):
        from workmemory.agent.protocol import tool_request, tool_response

        config = self.generation
        path, body = tool_request(config, messages, tools, self.settings.context_size)
        result = await self.request(path, body, priority, generation=config)
        return tool_response(config, result)

    async def agent_summary(self, messages, max_tokens):
        from workmemory.agent.protocol import tool_response
        config = self.generation
        path, body = chat_request(config, messages, None, min(max_tokens, config.max_tokens), self.settings.context_size)
        result = await self.request(path, body, 0, generation=config)
        return tool_response(config, result)

    async def embed(self, texts, priority=None):
        vectors = []
        for start in range(0, len(texts), 8):
            result = await self.request(
                "/api/embed",
                {
                    "model": self.settings.embedding_model,
                    "input": [text[:6000] for text in texts[start : start + 8]],
                    "truncate": True,
                    "keep_alive": "5m",
                    "options": {"num_ctx": self.settings.context_size},
                },
                priority,
            )
            batch = result["embeddings"]
            if len(batch) != len(texts[start : start + 8]) or any(
                len(v) != self.settings.embedding_dimensions for v in batch
            ):
                raise ValueError("Embedding shape differs from configured model")
            vectors.extend(batch)
        return vectors

    async def local_models(self):
        result = await self.client.get("/api/tags", timeout=2)
        result.raise_for_status()
        return result.json().get("models", [])

    async def health(self):
        config = self.generation
        generation = {
            "provider": config.provider,
            "model": config.model,
            "base_url": config.base_url,
            "ready": False,
        }

        async def local_status():
            try:
                return await self.local_models(), True
            except (httpx.HTTPError, ValueError):
                return [], False

        async def remote_status():
            if config.provider == "ollama":
                return
            try:
                result = await self.client.get(
                    config.base_url + "/models", headers=headers(config), timeout=2
                )
                result.raise_for_status()
                generation["ready"] = config.model in {
                    m["id"] for m in result.json().get("data", [])
                }
                if not generation["ready"]:
                    generation["detail"] = "服务模型列表中未找到此名称，可在设置中测试实际请求"
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                generation["detail"] = "模型列表检查失败，可在设置中测试实际请求"

        (models, local_reachable), _ = await asyncio.gather(local_status(), remote_status())
        names = {m["name"] for m in models}
        embedding = {
            "provider": "ollama",
            "model": self.settings.embedding_model,
            "base_url": self.settings.url,
            "ready": self.settings.embedding_model in names,
        }
        if config.provider == "ollama":
            generation["ready"] = local_reachable and config.model in names
        return {
            "reachable": local_reachable,
            "models_ready": bool(generation["ready"] and embedding["ready"]),
            "generation": generation,
            "embedding": embedding,
            "models": [
                {"name": m["name"], "digest": m.get("digest"), "size": m.get("size")}
                for m in models
            ],
        }
