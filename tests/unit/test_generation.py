import asyncio
import json
import stat

import httpx
import pytest
from pydantic import ValidationError
from workmemory.config import OllamaConfig
from workmemory.config.generation import GenerationConfig, GenerationStore, GenerationUpdate
from workmemory.inference.generation import (
    ModelConfigurationChanged,
    ProbeResult,
    chat_request,
    content,
    probe,
)
from workmemory.inference.ollama import Ollama


def remote(**values):
    return GenerationConfig(
        **{
            "provider": "openai_compatible",
            "base_url": "http://synthetic.test/v1",
            "model": "synthetic-qwen",
            "thinking": False,
            **values,
        }
    )


def response():
    return {
        "choices": [
            {"message": {"content": '{"answer":"连接成功","product":391}'}, "finish_reason": "stop"}
        ]
    }


def test_generation_store_persists_private_key_and_revision(tmp_path):
    settings = OllamaConfig()
    store = GenerationStore(settings, tmp_path / "generation.json")
    assert store.current.provider == "ollama"
    assert store.required_local_models() == [settings.generation_model, settings.embedding_model]
    store.save(remote(api_key="synthetic-secret"))
    assert store.revision == 1
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert "synthetic-secret" not in json.dumps(store.public())
    assert "synthetic-secret" not in repr(store.current)
    restarted = GenerationStore(settings, store.path)
    assert restarted.current == store.current and restarted.revision == 1
    assert restarted.required_local_models() == [settings.embedding_model]
    assert restarted.current.api_key.get_secret_value() == "synthetic-secret"


def test_api_key_retention_depends_on_destination():
    current = remote(api_key="synthetic-secret")
    fields = {**current.model_dump(exclude={"api_key"}), "revision": 1}
    update = GenerationUpdate(**fields)
    assert (
        update.resolve(current, "http://localhost").api_key.get_secret_value() == "synthetic-secret"
    )
    assert (
        not update.model_copy(update={"base_url": "https://other.test/v1"})
        .resolve(current, "http://localhost")
        .api_key.get_secret_value()
    )
    assert (
        not update.model_copy(update={"clear_api_key": True})
        .resolve(current, "http://localhost")
        .api_key.get_secret_value()
    )
    with pytest.raises(ValueError):
        update.model_copy(update={"provider": "ollama"}).resolve(current, "http://localhost")


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/config",
        "http://name:key@host/v1",
        "https://host/v1?key=secret",
        "https://host/v1#key",
        "http://host:wrong",
        "http://host/v1/chat/completions",
    ],
)
def test_bad_base_urls_rejected(url):
    with pytest.raises(ValidationError):
        remote(base_url=url)


def test_wire_formats_and_custom_parameters():
    messages = [{"role": "user", "content": "合成测试"}]
    schema = ProbeResult.model_json_schema()
    path, body = chat_request(remote(max_tokens=4096), messages, schema, 9999, 8192)
    assert path == "/chat/completions"
    assert body["max_tokens"] == 4096
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"]["json_schema"]["schema"] == schema
    _, compatible = chat_request(
        remote(thinking=None, structured_output="json_object"), messages, schema, 1000, 8192
    )
    assert "chat_template_kwargs" not in compatible
    assert compatible["response_format"] == {"type": "json_object"}
    assert "product" in compatible["messages"][0]["content"]
    assert len(messages) == 1
    native = GenerationConfig(base_url="http://localhost:11434", model="native")
    path, body = chat_request(native, messages, schema, 2000, 8192)
    assert path == "/api/chat" and body["format"] == schema
    assert body["think"] is False and body["options"]["num_ctx"] == 8192
    assert "response_format" not in body


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]},
    ],
)
def test_invalid_model_responses_fail_closed(data):
    with pytest.raises(ValueError):
        content(remote(), data)


async def test_probe_validates_structured_answer_without_memories():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    result = await probe(remote(), 8192, transport=httpx.MockTransport(handler))
    assert result["structured_output_valid"] and result["product"] == 391
    assert "17" in seen[0]["messages"][-1]["content"]
    with pytest.raises(ValidationError):
        await probe(
            remote(),
            8192,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": '{"answer":"wrong","product":390}'}}]
                    },
                )
            ),
        )


async def scheduler(handler, generation=None):
    settings = OllamaConfig()
    model = Ollama(settings, generation)
    await model.client.aclose()
    model.client = httpx.AsyncClient(base_url=settings.url, transport=httpx.MockTransport(handler))
    await model.start()
    return model


async def test_remote_generation_local_embeddings_and_no_key_leak():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1] * 1024]})
        return httpx.Response(200, json=response())

    model = await scheduler(handler, remote(api_key="synthetic-secret"))
    try:
        await model.chat([{"role": "user", "content": "合成测试"}])
        assert len((await model.embed(["合成嵌入"]))[0]) == 1024
        assert seen[0].url.host == "synthetic.test"
        assert seen[0].headers["Authorization"] == "Bearer synthetic-secret"
        assert seen[1].url.host == "127.0.0.1"
        assert "Authorization" not in seen[1].headers
        assert json.loads(seen[1].content)["model"] == model.settings.embedding_model
        assert model.metrics[0]["load_seconds"] is None
    finally:
        await model.close()


async def test_remote_failure_never_falls_back_to_local_generation():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(503, json={"error": "unavailable"})

    model = await scheduler(handler, remote())
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await model.chat([])
        assert seen == ["http://synthetic.test/v1/chat/completions"]
    finally:
        await model.close()


async def test_switch_allows_active_but_rejects_queued_old_requests():
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def handler(request):
        seen.append(request.url.host)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=response())

    model = await scheduler(handler, remote())
    try:
        active = asyncio.create_task(model.chat([]))
        await asyncio.wait_for(entered.wait(), 2)
        queued = asyncio.create_task(model.chat([]))
        await asyncio.sleep(0)
        model.configure_generation(remote(base_url="http://new.test/v1"))
        release.set()
        assert await active
        with pytest.raises(ModelConfigurationChanged):
            await queued
        assert await model.chat([])
        assert seen == ["synthetic.test", "new.test"]
    finally:
        release.set()
        await model.close()


async def test_embedding_health_survives_remote_failure():
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3-embedding:0.6b"}]})
        return httpx.Response(503)

    model = await scheduler(handler, remote())
    try:
        health = await model.health()
        assert health["embedding"]["ready"]
        assert not health["generation"]["ready"] and not health["models_ready"]
    finally:
        await model.close()


def test_atomic_replace_failure_preserves_previous_configuration(tmp_path, monkeypatch):
    from workmemory.config import generation

    store = GenerationStore(OllamaConfig(), tmp_path / "generation.json")
    store.save(remote())
    original = store.path.read_bytes()

    def fail_replace(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(generation.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.save(remote(model="new-model"))
    assert store.path.read_bytes() == original
    assert store.revision == 1 and store.current.model == "synthetic-qwen"
    assert not list(tmp_path.glob(".generation-*"))
