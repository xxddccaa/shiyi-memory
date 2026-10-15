"""Versioned, local-only MCP settings. Secrets never appear in public representations."""

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from workmemory.config import ROOT


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,24}$")
    label: str = Field(min_length=1, max_length=80)
    transport: Literal["http", "stdio", "weather"] = "http"
    enabled: bool = False
    url: str | None = Field(default=None, max_length=2000)
    command: str | None = Field(default=None, max_length=1000)
    args: list[str] = Field(default_factory=list, max_length=30)
    token: str | None = Field(default=None, max_length=8000, repr=False)
    env: dict[str, str] = Field(default_factory=dict, repr=False)
    allowed_tools: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid(self):
        if self.id == "workmemory":
            raise ValueError("保留的服务标识")
        if self.transport == "http":
            parsed = urlsplit(self.url or "")
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError("服务地址无效")
        if self.transport == "stdio" and (not self.command or not Path(self.command).is_absolute()):
            raise ValueError("请选择已安装程序的绝对路径")
        if self.transport == "stdio" and Path(self.command).name in {
            "npx",
            "npm",
            "uvx",
            "pip",
            "pip3",
            "brew",
            "bash",
            "zsh",
            "sh",
        }:
            raise ValueError("请选择已安装服务程序，不能使用 Shell 或自动下载安装命令")
        if self.transport == "weather" and self.id != "weather":
            raise ValueError("天气服务标识无效")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) for k in self.env):
            raise ValueError("环境变量名称无效")
        return self

    def public(self):
        value = self.model_dump(exclude={"token", "env"})
        value.update(has_token=bool(self.token), env_keys=sorted(self.env))
        return value


DEFAULTS = [
    ServerConfig(
        id="exa",
        label="联网搜索 · Exa",
        url="https://mcp.exa.ai/mcp?tools=web_search_exa",
        allowed_tools=["web_search_exa"],
    ),
    ServerConfig(
        id="weather",
        label="天气 · Open-Meteo",
        transport="weather",
        allowed_tools=["search_cities", "get_weather"],
    ),
]


class MCPStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else ROOT / "config/mcp.local.json"
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists():
                return 0, [v.model_copy(deep=True) for v in DEFAULTS], None
            data = json.loads(self.path.read_text())
            return (
                data["revision"],
                [ServerConfig.model_validate(v) for v in data["servers"]],
                data.get("city"),
            )

    def public(self):
        revision, servers, city = self.read()
        return {"revision": revision, "servers": [v.public() for v in servers], "city": city}

    def save(self, expected_revision, server=None, remove=None, city=None, set_city=False):
        with self.lock:
            revision, servers, current_city = self.read()
            if expected_revision != revision:
                raise HTTPException(409, "MCP 设置已更新，请刷新后再保存")
            values = {v.id: v for v in servers}
            if server:
                raw = dict(server)
                existing = values.get(raw.get("id"))
                if existing:
                    same = all(
                        raw.get(k, getattr(existing, k)) == getattr(existing, k)
                        for k in ["transport", "url", "command", "args"]
                    )
                    raw.setdefault("token", existing.token if same else None)
                    raw.setdefault("env", existing.env if same else {})
                try:
                    checked = ServerConfig.model_validate(raw)
                except ValueError:
                    raise HTTPException(422, "MCP 配置无效，请检查字段、地址或程序路径") from None
                values[checked.id] = checked
            if remove:
                values.pop(remove, None)
            if len(values) > 12:
                raise HTTPException(422, "最多配置 12 个外部服务")
            content = json.dumps(
                {
                    "revision": revision + 1,
                    "servers": [v.model_dump() for v in values.values()],
                    "city": city if set_city else current_city,
                },
                ensure_ascii=False,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=".mcp-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(content + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, self.path)
            finally:
                Path(temp).unlink(missing_ok=True)
            return self.public()
