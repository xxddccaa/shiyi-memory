"""Real Graphiti/Neo4j storage check with a deterministic model, not an LLM benchmark.

Briefly recreates this project's Neo4j container. Touches only a random synthetic group.
"""

import asyncio
import json
import subprocess
import uuid
from datetime import UTC, datetime

from workmemory.config import ROOT, get_settings
from workmemory.memory.base import MemoryDocument
from workmemory.memory.graphiti_adapter import GraphitiAdapter


class SyntheticModel:
    def __init__(self, settings, marker):
        self.settings = settings.ollama
        self.names = [f"存储验收{marker}", f"发布计划{marker}"]
        self.fact = f"{self.names[0]}项目原计划周五发布。"
        self.schemas = set()

    async def chat(self, messages, schema=None, **kwargs):
        title = schema["title"]
        self.schemas.add(title)
        entities = [{"name": name, "entity_type_id": 0} for name in self.names]
        edge = {
            "source_entity_name": self.names[0],
            "target_entity_name": self.names[1],
            "relation_type": "HAS_PLAN",
            "fact": self.fact,
            "valid_at": None,
            "invalid_at": None,
        }
        responses = {
            "ExtractedEntities": {"extracted_entities": entities},
            "CombinedExtraction": {"extracted_entities": entities, "edges": [edge]},
            "ExtractedEdges": {"edges": [edge]},
            "SummarizedEntities": {
                "summaries": [{"name": name, "summary": self.fact} for name in self.names]
            },
            "EntitySummary": {"summary": self.fact},
            "NodeResolutions": {
                "entity_resolutions": [
                    {"id": i, "name": name, "duplicate_candidate_id": -1}
                    for i, name in enumerate(self.names)
                ]
            },
            "BatchEdgeTimestamps": {"timestamps": [{"valid_at": None, "invalid_at": None}]},
            "EdgeTimestamps": {"valid_at": None, "invalid_at": None},
        }
        if title not in responses:
            raise AssertionError(f"Unexpected Graphiti response schema: {title}")
        return json.dumps(responses[title], ensure_ascii=False)

    async def embed(self, texts, **kwargs):
        # This check validates the real 1024-dimension storage/search contract only.
        return [[0.01] * self.settings.embedding_dimensions for _ in texts]


async def main():
    settings = get_settings()
    episode_id = uuid.uuid4()
    group = "workmemory_" + episode_id.hex
    model = SyntheticModel(settings, episode_id.hex[:8])
    adapter = GraphitiAdapter(settings, model)
    report = {"recorded_at": datetime.now(UTC).isoformat(), "model": "deterministic test double"}
    compose = [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "--env-file",
        str(ROOT / "config/secrets.env"),
        "--env-file",
        str(ROOT / "config/runtime.env"),
        "-f",
        str(ROOT / "infra/compose.yaml"),
        "--profile",
        "graphiti",
    ]
    try:
        await adapter.start()
        await adapter.upsert(MemoryDocument(episode_id, 1, model.fact, datetime.now(UTC)))
        assert episode_id in [hit.episode_id for hit in await adapter.search(model.names[0])]
        report["graphiti_1024d_write_and_search"] = "passed"
        await adapter.close()
        print("Recreating Neo4j with its existing bind mounts", flush=True)
        await asyncio.to_thread(
            subprocess.run,
            [*compose, "up", "-d", "--wait", "--force-recreate", "neo4j"],
            check=True,
        )
        adapter = GraphitiAdapter(settings, model)
        await adapter.start()
        assert episode_id in [hit.episode_id for hit in await adapter.search(model.names[0])]
        report["neo4j_container_recreation"] = "passed"
        model.fact = f"{model.names[0]}项目已改为周二发布。"
        await adapter.correct(MemoryDocument(episode_id, 2, model.fact, datetime.now(UTC)))
        rows, _, _ = await adapter.graph.driver.execute_query(
            "MATCH (n {group_id: $group}) RETURN n.content AS content, n.summary AS summary",
            group=group,
        )
        assert rows and all("周五" not in str(dict(row)) for row in rows)
        report["graphiti_correction_removes_old_node_text"] = "passed"
        await adapter.delete(episode_id)
        await adapter.delete(episode_id)
        rows, _, _ = await adapter.graph.driver.execute_query(
            "MATCH (n {group_id: $group}) RETURN count(n) AS count", group=group
        )
        assert rows[0]["count"] == 0
        assert episode_id not in [hit.episode_id for hit in await adapter.search(model.names[0])]
        report["graphiti_erasure_and_idempotent_delete"] = "passed"
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        try:
            await adapter.delete(episode_id)
        finally:
            await adapter.close()
            report["schemas"] = sorted(model.schemas)
            (ROOT / "data/exports/graph-storage-verification.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False)
            )
            print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
