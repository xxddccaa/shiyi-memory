import json
import stat
import sys

import pytest
from fastapi import HTTPException
from mcp.types import Tool, ToolAnnotations
from pydantic import ValidationError
from workmemory.agent.output import bounded_result
from workmemory.mcp.config import MCPStore, ServerConfig
from workmemory.mcp.runtime import read_allowed, tool_name


def test_atomic_revision_secrets_connection_change_and_reload(tmp_path):
    path = tmp_path / "mcp.json"
    store = MCPStore(path)
    server = {
        "id": "reader",
        "label": "Reader",
        "url": "https://example.test/mcp",
        "token": "synthetic-key",
        "env": {"TOKEN": "synthetic-env"},
        "allowed_tools": ["search"],
    }
    public = store.save(0, server=server)
    assert "synthetic-key" not in json.dumps(public) and "synthetic-env" not in json.dumps(public)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert MCPStore(path).public() == public
    with pytest.raises(HTTPException) as e:
        store.save(0, server=server)
    assert e.value.status_code == 409
    clean = {k: v for k, v in server.items() if k not in {"token", "env"}}
    store.save(1, server={**clean, "enabled": True})
    assert next(s for s in store.read()[1] if s.id == "reader").token == "synthetic-key"
    store.save(2, server={**clean, "url": "https://other.test/mcp"})
    reader = next(s for s in store.read()[1] if s.id == "reader")
    assert reader.token is None and reader.env == {}
    store.save(3, city="上海", set_city=True)
    assert store.read()[2] == "上海"
    store.save(4, remove="reader")
    assert all(s.id != "reader" for s in store.read()[1])
    assert not list(tmp_path.glob(".mcp-*"))


@pytest.mark.parametrize(
    "values",
    [
        {"url": "file:///tmp/server"},
        {"url": "https://user:password@example.test"},
        {"transport": "stdio", "command": "npx"},
        {"transport": "stdio", "command": "/usr/bin/npx", "args": ["-y", "package"]},
        {"transport": "stdio", "command": "/bin/sh"},
        {"url": "https://example.test", "env": {"BAD=ENV": "value"}},
    ],
)
def test_invalid_config_never_becomes_runnable(values):
    with pytest.raises(ValidationError):
        ServerConfig(id="test", label="Test", **values)


def test_namespaces_and_readonly_contract():
    server = ServerConfig(id="reader", label="Reader", transport="stdio", command=sys.executable)
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    assert read_allowed(server, Tool(name="search", inputSchema={}, annotations=readonly))
    assert not read_allowed(
        server, Tool(name="delete_record", inputSchema={}, annotations=readonly)
    )
    assert not read_allowed(server, Tool(name="search", inputSchema={}))
    assert tool_name("a", "search") != tool_name("b", "search")
    assert len(tool_name("a", "long" * 100)) <= 64
    assert tool_name("a", "test.name") != tool_name("a", "test_name")


def test_output_budget_keeps_json_valid_and_marks_truncation():
    value = {
        "items": [{"id": f"summary:{i}", "content": "合成文本" * 800} for i in range(20)],
        "report": {"progress": [{"text": "报告" * 3000}]},
        "next_cursor": "cursor",
    }
    for budget in [500, 2000, 8000]:
        bounded = bounded_result(value, budget)
        assert len(json.dumps(bounded, ensure_ascii=False)) <= budget
        assert bounded.get("truncated") or bounded.get("error")
    assert len(value["items"]) == 20  # do not mutate persisted reports


def test_bare_citation_only_maps_to_unique_actual_evidence():
    import uuid

    from workmemory.evidence.service import canonical_citation

    identifier = str(uuid.uuid4())
    known = {f"summary:{identifier}"}
    assert canonical_citation(identifier, known) == f"summary:{identifier}"
    assert canonical_citation("not-found", known) == "not-found"
    assert canonical_citation(f"raw:{identifier}", known) == f"raw:{identifier}"
    assert canonical_citation(identifier, {*known, f"todo:{identifier}"}) == identifier
