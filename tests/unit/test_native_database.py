"""Reject foreign instances/paths before any PostgreSQL command can mutate them."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from workmemory.config import get_settings

spec = importlib.util.spec_from_file_location("native_database", Path(__file__).parents[2] / "scripts/native_database.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


@pytest.fixture
def pg(tmp_path, monkeypatch):
    binary = tmp_path / "bin"
    binary.mkdir()
    monkeypatch.setattr(native, "postgres_bin", lambda: binary)
    return native.NativePostgres(get_settings(), root=tmp_path / "project")


def own(pg):
    pg.data.mkdir(parents=True)
    (pg.data / "PG_VERSION").write_text("17")
    pg.owner.write_text(json.dumps({"root": str(pg.root), "data_directory": str(pg.data), "major": 17}))


def test_parent_symlink_is_rejected_before_creating_files(pg, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    pg.root.mkdir()
    (pg.root / "data").symlink_to(outside)
    with pytest.raises(RuntimeError, match="不属于本项目"):
        pg.prepare()
    assert not list(outside.iterdir())


def test_unmarked_legacy_database_never_becomes_empty_native_database(pg):
    old = pg.root / "data/postgres"
    old.mkdir(parents=True)
    (old / "PG_VERSION").write_text("17")
    with pytest.raises(RuntimeError, match="旧 Docker 数据"):
        pg.prepare()
    assert not pg.data.exists()


def test_foreign_owner_or_server_version_rejected(pg):
    own(pg)
    pg.check_owned()
    (pg.data / "PG_VERSION").write_text("18")
    with pytest.raises(RuntimeError, match="版本不匹配"):
        pg.check_owned()
    (pg.data / "PG_VERSION").write_text("17")
    pg.owner.write_text(json.dumps({"root": "/another-project"}))
    with pytest.raises(RuntimeError, match="归属"):
        pg.stop()


def test_pid_substring_is_not_process_ownership(pg, monkeypatch):
    own(pg)
    (pg.data / "postmaster.pid").write_text("1234\n")
    def run(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=f"python fake-postgres --text {pg.data}")
    monkeypatch.setattr(native.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="PID"):
        pg.running()


def test_marker_for_other_checkout_rejected(pg):
    own(pg)
    pg.marker.parent.mkdir()
    pg.marker.write_text(json.dumps({"mode": "native", "root": "/another-project"}))
    with pytest.raises(RuntimeError, match="部署标记"):
        pg.check_marker()


def test_interrupted_migration_cannot_be_marked_ready_by_init(pg):
    own(pg)
    old = pg.root / "data/postgres"
    old.mkdir()
    (old / "PG_VERSION").write_text("17")
    with pytest.raises(RuntimeError, match="旧 Docker 数据"):
        pg.prepare()
    pg.prepare(migration=True)
    assert not pg.marker.exists()
