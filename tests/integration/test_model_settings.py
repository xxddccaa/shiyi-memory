import json

import httpx
import pytest
from workmemory.api import model_settings
from workmemory.config.generation import GenerationStore
from workmemory.inference.generation import probe
from workmemory.inference.ollama import Ollama
from workmemory.main import create_app

pytestmark = pytest.mark.integration


@pytest.fixture
async def configured_api(runtime, tmp_path, monkeypatch):
    store = GenerationStore(runtime.settings.ollama, tmp_path / "generation.json")
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": runtime.settings.ollama.generation_model}]}
            )
        body = json.loads(request.content)
        if body["model"] == "broken":
            return httpx.Response(401, text="upstream accidentally echoed synthetic-secret")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"answer":"连接成功","product":391}'}}]}
        )

    async def synthetic_probe(config, context_size):
        return await probe(config, context_size, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(model_settings, "probe", synthetic_probe)
    model = Ollama(runtime.settings.ollama)
    await model.client.aclose()
    model.client = httpx.AsyncClient(
        base_url=runtime.settings.ollama.url, transport=httpx.MockTransport(handler)
    )
    app = create_app(
        runtime.settings,
        engine=runtime.engine,
        memory=runtime.memory,
        ollama=model,
        run_worker=False,
        mcp_store=runtime.mcp.store,
        generation_store=store,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + runtime.settings.api_token},
        ) as client:
            yield client, store, model, seen


def payload(revision=0, **fields):
    return {
        "revision": revision,
        "provider": "openai_compatible",
        "base_url": "http://synthetic.test/v1",
        "model": "synthetic-qwen",
        "api_key": "synthetic-secret",
        **fields,
    }


async def test_test_save_reload_revision_and_key_boundaries(configured_api):
    client, store, model, seen = configured_api
    initial = (await client.get("/api/config/models")).json()
    assert initial["generation"]["provider"] == "ollama"
    assert initial["embedding"]["dimensions"] == 1024
    tested = await client.post("/api/config/models/test", json=payload())
    assert tested.status_code == 200
    assert store.revision == 0 and not store.path.exists()
    assert model.generation.provider == "ollama"
    saved = await client.post("/api/config/models", json=payload())
    assert saved.status_code == 200
    assert "synthetic-secret" not in saved.text and saved.json()["generation"]["api_key_configured"]
    assert model.generation.provider == "openai_compatible"
    assert model.generation_model == "synthetic-qwen"
    await model.chat([{"role": "user", "content": "后续合成请求"}])
    assert seen[-1].url.host == "synthetic.test"
    assert seen[-1].headers["Authorization"] == "Bearer synthetic-secret"
    assert GenerationStore(model.settings, store.path).current == model.generation
    reloaded = (await client.get("/api/config/models")).json()
    assert reloaded["revision"] == 1 and "api_key" not in reloaded["generation"]
    count = len(seen)
    assert (await client.post("/api/config/models", json=payload())).status_code == 409
    assert len(seen) == count
    # A blank field preserves the key for this destination, not the next destination.
    assert (
        await client.post("/api/config/models", json=payload(1, api_key=None))
    ).status_code == 200
    assert seen[-1].headers["Authorization"] == "Bearer synthetic-secret"
    assert (
        await client.post(
            "/api/config/models", json=payload(2, api_key=None, base_url="http://new.test/v1")
        )
    ).status_code == 200
    assert "Authorization" not in seen[-1].headers
    assert not store.current.api_key.get_secret_value()


async def test_failed_save_and_persistence_failure_leave_current_config(
    configured_api, monkeypatch
):
    client, store, model, _ = configured_api
    original = model.generation
    failed = await client.post("/api/config/models", json=payload(model="broken"))
    assert failed.status_code == 422 and "401" in failed.text
    assert "synthetic-secret" not in failed.text
    assert store.revision == 0 and not store.path.exists() and model.generation == original

    def disk_failure(config):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", disk_failure)
    failed = await client.post("/api/config/models", json=payload())
    assert failed.status_code == 500
    assert store.revision == 0 and model.generation == original


async def test_auth_validation_and_origin_never_expose_key(configured_api):
    client, store, _, seen = configured_api
    assert (
        await client.get("/api/config/models", headers={"Authorization": ""})
    ).status_code == 401
    assert (
        await client.post(
            "/api/config/models", json=payload(), headers={"Origin": "https://other.test"}
        )
    ).status_code == 403
    for values in [
        payload(revision=-1),
        payload(api_key={"secret": "synthetic-secret"}),
        payload(base_url="http://host/v1/chat/completions"),
        payload(timeout_seconds=0),
        payload(api_key="invalid\nsynthetic-secret"),
    ]:
        response = await client.post("/api/config/models", json=values)
        assert response.status_code == 422 and "synthetic-secret" not in response.text
    assert not seen and store.revision == 0


async def test_concurrent_saves_reject_stale_page_before_second_probe(configured_api):
    import asyncio

    client, store, model, seen = configured_api
    responses = await asyncio.gather(
        client.post("/api/config/models", json=payload(model="first")),
        client.post("/api/config/models", json=payload(model="second")),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(seen) == 1 and store.revision == 1
    assert model.generation == store.current


async def test_app_start_reads_saved_generation_service(runtime, tmp_path, monkeypatch):
    from workmemory import main
    from workmemory.config.generation import GenerationConfig

    path = tmp_path / "generation.json"
    store = GenerationStore(runtime.settings.ollama, path)
    store.save(
        GenerationConfig(
            provider="openai_compatible", base_url="http://synthetic.test/v1", model="saved-model"
        )
    )
    settings_seen = []

    def factory(settings, generation):
        settings_seen.append(generation)
        return runtime.ollama

    monkeypatch.setattr(main, "Ollama", factory)
    app = create_app(
        runtime.settings,
        engine=runtime.engine,
        memory=runtime.memory,
        run_worker=False,
        mcp_store=runtime.mcp.store,
        generation_store=GenerationStore(runtime.settings.ollama, path),
    )
    async with app.router.lifespan_context(app):
        assert app.state.runtime.generation_store.revision == 1
        assert settings_seen[0].model == "saved-model"


async def test_large_model_settings_survive_save_and_drive_wire_request(configured_api):
    client, store, model, seen = configured_api
    response = await client.post("/api/config/models", json=payload(max_tokens=65536, context_window=1000000))
    assert response.status_code == 200
    assert json.loads(seen[-1].content)["max_tokens"] == 65536
    restarted = GenerationStore(model.settings, store.path)
    assert restarted.current.context_window == 1000000
    assert restarted.current.max_tokens == 65536
    caps = (await client.get("/api/agent/capabilities")).json()
    assert caps["budget"]["context_chars"] == 934464
    await model.chat_tools([{"role": "user", "content": "合成测试"}], [])
    assert json.loads(seen[-1].content)["max_tokens"] == 65536
