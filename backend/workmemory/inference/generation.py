"""Generation wire protocols and a synthetic compatibility check. No service fallback."""

import json
import time
from typing import Literal

import httpx
from pydantic import BaseModel, Field


class ModelConfigurationChanged(RuntimeError):
    pass


def headers(config):
    key = config.api_key.get_secret_value()
    return {"Authorization": "Bearer " + key} if key else {}


def chat_request(config, messages, schema, max_tokens, context_size):
    if config.provider == "ollama":
        body = {
            "model": config.model,
            "messages": messages,
            "stream": False,
            "keep_alive": "5m",
            "options": {
                "num_ctx": config.context_window or context_size,
                "num_predict": min(max_tokens, config.max_tokens),
                "temperature": config.temperature,
            },
        }
        if config.thinking is not None:
            body["think"] = config.thinking
        if schema:
            body["format"] = schema
        return "/api/chat", body
    body = {
        "model": config.model,
        "messages": list(messages),
        "stream": False,
        "max_tokens": min(max_tokens, config.max_tokens),
        "temperature": config.temperature,
    }
    if config.thinking is not None:
        body["chat_template_kwargs"] = {"enable_thinking": config.thinking}
    if schema:
        if isinstance(schema, dict) and config.structured_output == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "workmemory", "schema": schema},
            }
        else:
            body["response_format"] = {"type": "json_object"}
            instruction = "只返回合法 JSON 对象。"
            if isinstance(schema, dict):
                instruction += "必须符合以下 JSON schema：" + json.dumps(schema, ensure_ascii=False)
            body["messages"] = [{"role": "system", "content": instruction}, *messages]
    return "/chat/completions", body


def content(config, data):
    if not isinstance(data, dict):
        raise ValueError("模型响应不是 JSON 对象")
    if data.get("error"):
        raise ValueError("模型服务拒绝了请求")
    if config.provider == "ollama":
        if data.get("done_reason") == "length":
            raise ValueError("模型输出达到长度上限，请调整最大输出或关闭思考模式")
        message = data.get("message")
        text = message.get("content") if isinstance(message, dict) else None
    else:
        choices = data.get("choices", [])
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("模型响应缺少 choices")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise ValueError("模型输出达到长度上限，请调整最大输出或关闭思考模式")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("模型响应缺少 message")
        if message.get("refusal"):
            raise ValueError("模型拒绝回答测试问题")
        text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("模型没有返回可用正文，请检查思考模式与输出设置")
    return text


class ProbeResult(BaseModel):
    model_config = {"extra": "forbid"}
    answer: str = Field(min_length=1)
    product: Literal[391]


async def probe(config, context_size, *, transport=None):
    schema = ProbeResult.model_json_schema()
    path, body = chat_request(
        config,
        [
            {
                "role": "system",
                "content": "这是连接测试。用中文简短回复，并严格按 JSON schema 输出。",
            },
            {
                "role": "user",
                "content": "请在 answer 中写连接成功，在 product 中填写 17 乘以 23 的结果。",
            },
        ],
        schema,
        config.max_tokens,
        context_size,
    )
    started = time.monotonic()
    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        transport=transport,
        timeout=httpx.Timeout(config.timeout_seconds, connect=10),
    ) as client:
        response = await client.post(config.base_url + path, json=body, headers=headers(config))
        response.raise_for_status()
        value = ProbeResult.model_validate_json(content(config, response.json()))
    return {
        "ok": True,
        "seconds": round(time.monotonic() - started, 3),
        "structured_output_valid": True,
        "answer": "连接成功",
        "product": value.product,
    }


def error_message(error):
    # Never reflect upstream bodies, request headers or validation inputs containing credentials.
    if isinstance(error, httpx.TimeoutException):
        return "模型连接超时，请检查地址、网络和超时设置。"
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        hints = {
            401: "API Key 无效或缺失",
            403: "没有访问此模型的权限",
            404: "接口或模型不存在，请检查基础地址和模型名",
            429: "服务请求过多或额度不足",
            400: "服务拒绝参数，请检查模型名、思考模式或切换为 JSON 对象模式",
        }
        return f"模型服务返回 HTTP {status}：{hints.get(status, '服务未就绪，请检查服务端状态')}。"
    if isinstance(error, httpx.HTTPError):
        return "无法连接模型服务，请检查地址和网络。"
    return "模型输出未通过 JSON 格式与计算结果校验，请检查结构化输出、思考模式和最大输出设置。"
