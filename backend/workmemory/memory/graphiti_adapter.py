import asyncio
import json
import os
import uuid
from datetime import UTC, datetime

os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.nodes import EpisodeType, EpisodicNode

from workmemory.memory.base import MemoryAdapter, MemoryHit


class LocalLLM(LLMClient):
    def __init__(self, ollama):
        super().__init__(
            LLMConfig(
                model=getattr(ollama, "generation_model", ollama.settings.generation_model),
                temperature=0.1,
                max_tokens=2400,
            ),
            cache=False,
        )
        self.ollama = ollama

    async def _generate_response(
        self, messages, response_model=None, max_tokens=2400, model_size=None, **kwargs
    ):
        output = await self.ollama.chat(
            [{"role": m.role, "content": m.content} for m in messages],
            schema=response_model.model_json_schema() if response_model else "json",
            max_tokens=min(max_tokens, 2400),
        )
        return json.loads(output)


class LocalEmbedder(EmbedderClient):
    def __init__(self, ollama):
        self.ollama = ollama

    async def create(self, input_data):
        text = input_data if isinstance(input_data, str) else " ".join(str(x) for x in input_data)
        return (await self.ollama.embed([text]))[0]

    async def create_batch(self, input_data_list):
        return await self.ollama.embed(input_data_list)


class LocalReranker(CrossEncoderClient):
    def __init__(self, ollama):
        self.ollama = ollama

    async def rank(self, query, passages):
        if not passages:
            return []
        import numpy as np

        vectors = np.array(await self.ollama.embed([query, *passages]))
        scores = (
            vectors[1:]
            @ vectors[0]
            / (np.linalg.norm(vectors[1:], axis=1) * np.linalg.norm(vectors[0]) + 1e-9)
        )
        return sorted(zip(passages, scores.tolist()), key=lambda x: x[1], reverse=True)


class GraphitiAdapter(MemoryAdapter):
    name = "graphiti"

    def __init__(self, settings, ollama):
        self.settings = settings
        self.graph = Graphiti(
            settings.memory.neo4j_uri,
            "neo4j",
            settings.neo4j_password,
            llm_client=LocalLLM(ollama),
            embedder=LocalEmbedder(ollama),
            cross_encoder=LocalReranker(ollama),
            max_coroutines=1,
            store_raw_episode_content=True,
        )
        self.lock = asyncio.Lock()

    async def start(self):
        await self.graph.build_indices_and_constraints()

    async def close(self):
        await self.graph.close()

    async def _remove(self, episode_id):
        try:
            await self.graph.remove_episode(str(episode_id))
        except NodeNotFoundError:
            pass

    async def upsert(self, document):
        async with self.lock:
            # Deterministic UUID makes retry after an external write / DB crash idempotent.
            await self._remove(document.episode_id)
            # Graphiti's uuid argument resumes an existing episode; it does not create one.
            # Seed it through the public node API so retries keep a deterministic source ID.
            await EpisodicNode(
                uuid=str(document.episode_id),
                name=f"{document.project or '工作片段'} / r{document.revision}",
                group_id=f"workmemory_{document.episode_id.hex}",
                source=EpisodeType.text,
                source_description=f"workmemory:{document.episode_id}:r{document.revision}",
                content=document.text,
                created_at=datetime.now(UTC),
                valid_at=document.reference_time,
            ).save(self.graph.driver)
            await self.graph.add_episode(
                name=f"{document.project or '工作片段'} / r{document.revision}",
                episode_body=document.text,
                source_description=f"workmemory:{document.episode_id}:r{document.revision}",
                reference_time=document.reference_time,
                source=EpisodeType.text,
                # Episode isolation prevents shared node summaries retaining erased facts.
                group_id=f"workmemory_{document.episode_id.hex}",
                previous_episode_uuids=[],
                uuid=str(document.episode_id),
                update_communities=False,
                custom_extraction_instructions="用中文提取工作项目、决策、明确事实与事实变化。不要推测。",
            )
        return [str(document.episode_id)]

    async def delete(self, episode_id):
        async with self.lock:
            await self._remove(episode_id)

    async def search(self, query, limit=10):
        async with self.lock:
            edges = await self.graph.search(query, num_results=limit)
        ids = dict.fromkeys(source for edge in edges for source in edge.episodes)
        return [MemoryHit(uuid.UUID(source)) for source in ids]

    async def status(self):
        await self.graph.driver.health_check()
        return {"name": self.name, "ready": True}
