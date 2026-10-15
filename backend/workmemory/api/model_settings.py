"""Authenticated model settings. Tests use synthetic text and never write memories."""

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from workmemory.config.generation import GenerationUpdate
from workmemory.inference.generation import error_message, probe

router = APIRouter(prefix="/api/config/models")


def resolved(body, rt):
    store = rt.generation_store
    if body.revision != store.revision:
        raise HTTPException(409, "模型配置已被其他页面更新，请重新加载后再保存。")
    try:
        return body.resolve(store.current, rt.settings.ollama.url)
    except ValidationError as exc:
        raise HTTPException(422, "模型配置无效，请检查地址、名称和参数范围。") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("")
async def model_settings(request: Request):
    rt = request.app.state.runtime
    local_models = []
    try:
        local_models = [m["name"] for m in await rt.ollama.local_models()]
    except (httpx.HTTPError, ValueError):
        pass
    return {
        **rt.generation_store.public(),
        "local_models": local_models,
        "embedding": {
            "provider": "ollama",
            "base_url": rt.settings.ollama.url,
            "model": rt.settings.ollama.embedding_model,
            "dimensions": rt.settings.ollama.embedding_dimensions,
        },
        "ocr": {
            "provider": "paddleocr",
            "detection_model": rt.settings.ocr.detection_model,
            "recognition_model": rt.settings.ocr.recognition_model,
        },
    }


async def checked(config, rt):
    try:
        return await probe(config, rt.settings.ollama.context_size)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, error_message(exc)) from exc


@router.post("/test")
async def test_model(body: GenerationUpdate, request: Request):
    rt = request.app.state.runtime
    async with rt.generation_store.lock:
        config = resolved(body, rt)
        return await checked(config, rt)


@router.post("")
async def save_model(body: GenerationUpdate, request: Request):
    rt = request.app.state.runtime
    async with rt.generation_store.lock:
        config = resolved(body, rt)
        result = await checked(config, rt)
        try:
            rt.generation_store.save(config)
        except OSError as exc:
            raise HTTPException(500, "模型配置无法写入本地文件，原设置仍然有效。") from exc
        rt.ollama.configure_generation(config)
        return {**rt.generation_store.public(), "test": result}
