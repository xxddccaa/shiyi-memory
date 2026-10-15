import asyncio
import uuid
from pathlib import PurePosixPath

import numpy as np
import yaml
from reme import ReMe
from reme.components.embedding_store.base_embedding_store import BaseEmbeddingStore

from workmemory.config import ROOT
from workmemory.memory.base import MemoryAdapter, MemoryHit

_schedulers = {}


class OllamaEmbeddingStore(BaseEmbeddingStore):
    """ReMe extension API: embeddings share the application's inference scheduler."""

    def __init__(self, scheduler_id, dimensions=1024, **kwargs):
        super().__init__(**kwargs)
        self.scheduler = _schedulers[scheduler_id]
        self.dimensions = dimensions

    async def health_check(self, timeout=None):
        try:
            status = await self.scheduler.health()
            self.is_healthy = status.get("embedding", {}).get("ready", status["models_ready"])
        except Exception:
            self.is_healthy = False
        return self.is_healthy

    async def get_embeddings(self, input_text, **kwargs):
        result = await self.scheduler.embed(input_text)
        self.is_healthy = True
        return [np.array(v, dtype=np.float32) for v in result]


class ReMeAdapter(MemoryAdapter):
    name = "reme"

    def __init__(self, settings, ollama):
        self.settings, self.ollama = settings, ollama
        self.app = None
        self.lock = asyncio.Lock()
        self.scheduler_id = str(uuid.uuid4())

    async def start(self):
        _schedulers[self.scheduler_id] = self.ollama
        config = yaml.safe_load((ROOT / "config/reme/app.yaml").read_text())
        config["workspace_dir"] = str(self.settings.path(self.settings.memory.reme_workspace))
        config["components"]["embedding_store"]["default"].update(
            scheduler_id=self.scheduler_id, dimensions=self.settings.ollama.embedding_dimensions
        )
        self.app = ReMe(**config)
        await self.app.start()

    async def close(self):
        if self.app:
            await self.app.close()
        _schedulers.pop(self.scheduler_id, None)

    @staticmethod
    def path(episode_id):
        return f"digest/workmemory/{episode_id}.md"

    async def _index(self, path, change):
        result = await self.app.run_job(
            "index",
            changes=[
                {
                    "path": str(self.settings.path(self.settings.memory.reme_workspace) / path),
                    "change": change,
                }
            ],
        )
        if not result.success:
            raise RuntimeError("ReMe indexing failed")

    async def upsert(self, document):
        path = self.path(document.episode_id)
        async with self.lock:
            result = await self.app.run_job(
                "write",
                path=path,
                name=document.project or "工作片段",
                description=document.text[:180],
                content=document.text,
                metadata={
                    "episode_id": str(document.episode_id),
                    "revision": document.revision,
                    "captured_at": document.reference_time.isoformat(),
                },
            )
            if not result.success:
                raise RuntimeError("ReMe write failed")
            await self._index(path, "modified")
        return [path]

    async def delete(self, episode_id):
        path = self.path(episode_id)
        async with self.lock:
            result = await self.app.run_job("delete", path=path)
            if not result.success and result.metadata.get("error") != "not found":
                raise RuntimeError("ReMe deletion failed")
            await self._index(path, "deleted")

    async def search(self, query, limit=10):
        async with self.lock:
            response = await self.app.run_job("search", query=query, limit=limit)
        if not response.success:
            raise RuntimeError("ReMe search failed")
        results = []
        for hit in response.metadata.get("results", []):
            path = PurePosixPath(hit["path"])
            if str(path.parent) == "digest/workmemory":
                try:
                    results.append(MemoryHit(uuid.UUID(path.stem), float(hit.get("score", 0))))
                except ValueError:
                    continue
        return results

    async def status(self):
        return {
            "name": self.name,
            "ready": self.app is not None,
            "workspace": str(self.settings.path(self.settings.memory.reme_workspace)),
        }
