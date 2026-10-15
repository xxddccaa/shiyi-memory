from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import URL

ROOT = Path(__file__).resolve().parents[3]


class ServerConfig(BaseModel):
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)


class DatabaseConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 5432
    name: str = "workmemory"
    user: str = "workmemory"


class MemoryConfig(BaseModel):
    backend: Literal["reme", "graphiti"] = "reme"
    reme_workspace: str = "data/reme"
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_http_port: int = Field(default=7474, ge=1024, le=65535)


class OllamaConfig(BaseModel):
    url: str = "http://127.0.0.1:11434"
    generation_model: str = "qwen3:8b"
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_dimensions: int = 1024
    context_size: int = 8192
    timeout_seconds: int = 240

    @model_validator(mode="after")
    def local_only(self):
        from urllib.parse import urlparse

        if urlparse(self.url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Ollama must run locally; cloud fallback is disabled")
        return self


class OCRConfig(BaseModel):
    port: int = Field(default=8766, ge=1024, le=65535)
    detection_model: str = Field(
        default="PP-OCRv6_tiny_det", pattern=r"^PP-OCRv6_(tiny|small|medium)_det$"
    )
    recognition_model: str = Field(
        default="PP-OCRv6_tiny_rec", pattern=r"^PP-OCRv6_(tiny|small|medium)_rec$"
    )
    cpu_threads: int = Field(default=2, ge=1, le=8)
    timeout_seconds: int = Field(default=45, ge=5, le=120)
    max_image_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)


class CollectorConfig(BaseModel):
    interval_seconds: int = Field(default=5, ge=5)
    spool_max_mb: int = Field(default=50, ge=1)
    ocr_mode: Literal["fallback", "always", "off"] = "always"
    capture_mode: Literal["interaction", "timer"] = "timer"
    frame_rate: int = Field(default=4, ge=1, le=4)
    merge_seconds: float = Field(default=0.6, ge=0.2, le=2)
    max_group_seconds: float = Field(default=2, ge=1, le=5)
    stable_seconds: float = Field(default=0.5, ge=0.25, le=2)
    ocr_min_interval: float = Field(default=5, ge=1, le=30)
    image_budget_mb: int = Field(default=64, ge=32, le=128)
    max_pending_groups: int = Field(default=3, ge=1, le=5)
    similarity_threshold: float = Field(default=0.95, ge=0.5, le=1)
    important_patterns: list[str] | None = None

    @model_validator(mode="after")
    def valid_patterns(self):
        import re

        for pattern in (self.important_patterns or []) + self.excluded_title_patterns:
            re.compile(pattern)
        return self

    excluded_apps: list[str] = []
    excluded_title_patterns: list[str] = []


class AgentConfig(BaseModel):
    # Execution limits, unrelated to provider pricing. Existing YAML inherits these defaults.
    max_model_calls: int = Field(default=1024, ge=2, le=100000)
    max_tool_calls: int = Field(default=16384, ge=1, le=1000000)
    max_memory_searches: int = Field(default=4096, ge=1, le=100000)
    max_external_calls: int = Field(default=4096, ge=1, le=100000)
    tool_result_chars: int = Field(default=16000000, ge=2000, le=1000000000)
    context_chars: int = Field(default=64000, ge=1)
    run_timeout_seconds: int = Field(default=86400, ge=60, le=604800)

    def public(self):
        return {
            "models": self.max_model_calls,
            "tools": self.max_tool_calls,
            "memory_searches": self.max_memory_searches,
            "external_calls": self.max_external_calls,
            "tool_result_chars": self.tool_result_chars,
            "context_chars": self.context_chars,
            "run_timeout_seconds": self.run_timeout_seconds,
        }


class Settings(BaseModel):
    server: ServerConfig = ServerConfig()
    database: DatabaseConfig = DatabaseConfig()
    memory: MemoryConfig = MemoryConfig()
    ollama: OllamaConfig = OllamaConfig()
    collector: CollectorConfig = CollectorConfig()
    ocr: OCRConfig = OCRConfig()
    agent: AgentConfig = Field(default_factory=AgentConfig)
    retention_days: int = Field(default=30, ge=1)
    episode_minutes: int = Field(default=5, ge=1, le=60)
    timezone: str = "Asia/Shanghai"
    postgres_password: str = Field(repr=False)
    api_token: str = Field(repr=False)
    neo4j_password: str = Field(repr=False)

    def path(self, value: str) -> Path:
        resolved = (ROOT / value).resolve()
        if not resolved.is_relative_to(ROOT):
            raise ValueError("All application files must be inside the project root")
        return resolved

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.database.user,
            password=self.postgres_password,
            host=self.database.host,
            port=self.database.port,
            database=self.database.name,
        )


@lru_cache
def get_settings() -> Settings:
    local = ROOT / "config/app.local.yaml"
    data = yaml.safe_load(
        (local if local.exists() else ROOT / "config/app.example.yaml").read_text()
    )
    secrets = dotenv_values(ROOT / "config/secrets.env")
    if not secrets.get("POSTGRES_PASSWORD") or not secrets.get("API_TOKEN"):
        raise RuntimeError("Run scripts/bootstrap.sh to create local credentials first")
    return Settings(
        **data,
        postgres_password=secrets["POSTGRES_PASSWORD"],
        api_token=secrets["API_TOKEN"],
        neo4j_password=secrets["NEO4J_PASSWORD"],
    )
