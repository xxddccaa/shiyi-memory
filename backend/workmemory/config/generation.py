"""User-selected generation service, independent of local embedding/OCR configuration."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from workmemory.config import ROOT


class GenerationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["ollama", "openai_compatible"] = "ollama"
    base_url: str = Field(max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    timeout_seconds: int = Field(default=120, ge=10, le=86400)
    temperature: float = Field(default=0.1, ge=0, le=2)
    max_tokens: int = Field(default=32768, ge=256)
    context_window: int | None = Field(default=None, ge=1024)
    thinking: bool | None = False
    structured_output: Literal["json_schema", "json_object"] = "json_schema"

    @model_validator(mode="after")
    def output_fits_context(self):
        if self.context_window is not None and self.max_tokens >= self.context_window:
            raise ValueError("最大输出必须小于模型上下文容量，以留出输入空间")
        if self.context_window is not None:
            import math
            reserve = max(self.max_tokens + math.ceil(.02 * self.context_window), min(16384, math.ceil(.2 * self.context_window)))
            if reserve >= self.context_window:
                raise ValueError("最大输出加上下文压缩预留空间必须小于模型容量")
        return self

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError("接口地址须为 HTTP(S) 基础地址，不能包含凭据、查询参数或片段")
        try:
            _ = parsed.port
        except ValueError:
            raise ValueError("接口端口无效") from None
        if parsed.path.rstrip("/").endswith(("/chat/completions", "/api/chat")):
            raise ValueError("请填写基础地址（兼容接口通常以 /v1 结尾），不要填写聊天请求路径")
        return value

    @field_validator("model")
    @classmethod
    def valid_model(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("请填写有效模型名称")
        return value

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if len(value.get_secret_value()) > 8192 or any(
            ord(c) < 33 or ord(c) > 126 for c in value.get_secret_value()
        ):
            raise ValueError("API Key 包含无效字符")
        return value

    def public(self):
        return {
            **self.model_dump(exclude={"api_key"}),
            "api_key_configured": bool(self.api_key.get_secret_value()),
        }

    def stored(self):
        return {**self.model_dump(exclude={"api_key"}), "api_key": self.api_key.get_secret_value()}


def local_generation(settings):
    return GenerationConfig(
        base_url=settings.url,
        model=settings.generation_model,
        timeout_seconds=settings.timeout_seconds,
        max_tokens=min(4096, settings.context_size // 2),
        context_window=settings.context_size,
    )


class GenerationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    provider: Literal["ollama", "openai_compatible"]
    base_url: str
    model: str
    api_key: SecretStr | None = Field(default=None, repr=False)
    clear_api_key: bool = False
    timeout_seconds: int = 120
    temperature: float = 0.1
    max_tokens: int = 32768
    context_window: int | None = None
    thinking: bool | None = False
    structured_output: Literal["json_schema", "json_object"] = "json_schema"

    def resolve(self, current, local_url):
        values = self.model_dump(exclude={"revision", "api_key", "clear_api_key"})
        # Never forward a saved key to a newly entered service.
        same_service = (
            self.provider == current.provider
            and self.base_url.strip().rstrip("/") == current.base_url
        )
        key = ""
        if not self.clear_api_key:
            if self.api_key is not None:
                key = self.api_key.get_secret_value()
            elif same_service:
                key = current.api_key.get_secret_value()
        values["api_key"] = key
        config = GenerationConfig.model_validate(values)
        if config.provider == "ollama" and config.base_url != local_url.rstrip("/"):
            raise ValueError("本地 Ollama 使用项目管理的地址；远程服务请选择 OpenAI 兼容接口")
        return config


class GenerationStore:
    def __init__(self, settings, path: Path | None = None):
        self.path = path if path is not None else ROOT / "config/generation.local.json"
        self.local = local_generation(settings)
        self.local_embedding_model = settings.embedding_model
        self.current = self.local
        self.revision = 0
        self.lock = asyncio.Lock()
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            self.current = GenerationConfig.model_validate(saved["generation"])
            self.revision = saved["revision"]
            if self.current.provider == "ollama" and self.current.base_url != self.local.base_url:
                raise ValueError("保存的本地模型地址与 Ollama 配置不一致，请检查模型配置")

    def required_local_models(self):
        names = [self.local_embedding_model]
        if self.current.provider == "ollama":
            names.insert(0, self.current.model)
        return list(dict.fromkeys(names))

    def public(self):
        return {
            "revision": self.revision,
            "generation": self.current.public(),
            "local_default": self.local.public(),
        }

    def save(self, config):
        # One atomic file holds configuration and its key; never expose keys through APIs.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"revision": self.revision + 1, "generation": config.stored()}
        fd, name = tempfile.mkstemp(prefix=".generation-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)
        self.current = config
        self.revision = payload["revision"]


def agent_limits(settings, generation=None):
    """Use the selected model's window; legacy YAML remains a fallback, not another cap.

    One character per remaining input token is a conservative *target*, not a tokenizer.
    Provider tokenization, tool schemas and complete recent tool rounds can differ.
    """
    limits = settings.agent.model_copy()
    if generation is not None and generation.context_window is not None:
        limits.context_chars = max(1, generation.context_window - generation.max_tokens)
    return limits
