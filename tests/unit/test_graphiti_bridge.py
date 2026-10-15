from graphiti_core.prompts.models import Message
from pydantic import BaseModel
from workmemory.config import get_settings
from workmemory.memory.graphiti_adapter import GraphitiAdapter


class Fact(BaseModel):
    fact: str


async def test_installed_graphiti_calls_local_model_and_embedder_contract():
    class Model:
        settings = get_settings().ollama

        async def chat(self, messages, schema=None, **kwargs):
            assert schema["title"] == "Fact"
            return '{"fact":"合成接口测试"}'

        async def embed(self, texts, **kwargs):
            return [[0.01] * 1024 for _ in texts]

    adapter = GraphitiAdapter(get_settings(), Model())
    try:
        result = await adapter.graph.llm_client.generate_response(
            [Message(role="user", content="测试")], response_model=Fact
        )
        assert result["fact"] == "合成接口测试"
        assert len(await adapter.graph.embedder.create("中文样本")) == 1024
        assert len(await adapter.graph.embedder.create_batch(["一", "二"])) == 2
        assert len(await adapter.graph.cross_encoder.rank("样本", ["第一", "第二"])) == 2
    finally:
        await adapter.close()
