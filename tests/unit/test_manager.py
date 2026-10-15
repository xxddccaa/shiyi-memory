import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "manager", Path(__file__).parents[2] / "scripts/manager.py"
)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "ROOT", tmp_path)
    return tmp_path


def test_offline_status_without_dependencies(root, monkeypatch):
    monkeypatch.setattr(manager, "capture", lambda *a, **kw: "")
    monkeypatch.setattr(manager, "request_json", lambda *a, **kw: {})
    value = manager.status()
    assert not value["installed"] and not value["ready"]
    assert len(value["modules"]) == 6
    assert next(m for m in value["modules"] if m["module"] == "neo4j")["state"] == "未启用"


def test_external_container_and_paths_rejected(root, monkeypatch, tmp_path_factory):
    other = tmp_path_factory.mktemp("outside")
    (other / "keep").write_text("keep")
    (root / "data").symlink_to(other)
    with pytest.raises(RuntimeError):
        manager.safe_remove("data")
    with pytest.raises(RuntimeError):
        manager.safe_remove("data/keep")
    assert (other / "keep").read_text() == "keep"
    monkeypatch.setattr(
        manager, "containers", lambda: [{"Mounts": [{"Type": "bind", "Source": str(other)}]}]
    )
    with pytest.raises(RuntimeError):
        manager.assert_container_ownership()


def test_uninstall_preserves_user_records(root, monkeypatch):
    monkeypatch.setattr(manager, "stop", lambda **kwargs: None)
    for path in [
        ".venv/runtime",
        "macos/WorkMemory.app/binary",
        "data/postgres/important",
        "models/weights",
        "config/secrets.env",
        "config/generation.local.json",
        "data/backups/backup",
    ]:
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("kept")
    manager.uninstall()
    assert not (root / ".venv").exists()
    for path in [
        "data/postgres/important",
        "models/weights",
        "config/secrets.env",
        "config/generation.local.json",
        "data/backups/backup",
    ]:
        assert (root / path).exists()


def test_purge_cancel_does_nothing(root, monkeypatch):
    calls = []
    monkeypatch.setattr(manager, "stop", lambda **kwargs: calls.append("stop"))
    monkeypatch.setattr("builtins.input", lambda prompt: "取消")
    manager.uninstall(purge=True)
    assert not calls


def test_repeated_build_skips_signing_and_frontend(root, monkeypatch):
    calls = []
    monkeypatch.setattr(manager, "script", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(manager, "stop_collector", lambda: None)
    (root / "macos/WorkMemory.app").mkdir(parents=True)
    (root / "frontend/dist").mkdir(parents=True)
    (root / "frontend/dist/index.html").write_text("built")
    (root / "agent/node_modules/@earendil-works/pi-coding-agent").mkdir(parents=True)
    manager.build_if_needed()
    assert len(calls) == 5
    calls.clear()
    manager.build_if_needed()
    assert calls == []


def test_start_uses_installed_artifacts(root, monkeypatch):
    calls = []
    monkeypatch.setattr(manager, "require_installed", lambda: None)
    monkeypatch.setattr(manager, "database_start", lambda *a: None)
    monkeypatch.setattr(manager, "python_script", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(manager, "collector_pids", lambda: [123])
    monkeypatch.setattr(manager, "show_status", lambda *a: None)
    monkeypatch.setattr(manager, "status", lambda: {})
    manager.start(open_windows=False)
    assert calls == [
        ("services.py", "start", "ollama"),
        ("services.py", "start", "ocr"),
        ("services.py", "start", "backend"),
    ]


def test_permission_reset_is_explicit_and_scoped(root, monkeypatch):
    calls = []
    binary = root / "macos/WorkMemory.app/Contents/MacOS/WorkMemory"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"installed application")
    monkeypatch.setattr(manager.os, "geteuid", lambda: 501)
    monkeypatch.setattr(manager, "stop_collector", lambda: None)
    monkeypatch.setattr(manager, "run", lambda args, *a, **kw: calls.append(args))
    monkeypatch.setattr(manager, "script", lambda *a, **kw: None)
    manager.permissions()
    resets = [call for call in calls if call[0] == "tccutil"]
    assert len(resets) == 2
    assert all(call[-1] == "local.workmemory.collector" for call in resets)
    assert [call[2] for call in resets] == ["Accessibility", "ScreenCapture"]
    assert not any(call[0] == "sudo" or "--sign" in call for call in calls)
    assert binary.read_bytes() == b"installed application"


def test_remote_selection_only_requires_local_embedding_and_hides_key(root, monkeypatch):
    manager.save_json(
        root / "config/generation.local.json",
        {
            "revision": 1,
            "generation": {
                "provider": "openai_compatible",
                "base_url": "http://synthetic.test/v1",
                "model": "remote-qwen",
                "api_key": "synthetic-secret",
            },
        },
    )
    config = manager.config_snapshot()
    assert manager.required_local_models(config) == ["qwen3-embedding:0.6b"]
    assert "synthetic-secret" not in str(manager.generation_selection(config))
    monkeypatch.setattr(manager, "capture", lambda *a, **kw: "")
    monkeypatch.setattr(manager, "owns_process", lambda name: True)

    def request(url, *args):
        if url.endswith("/api/tags"):
            return {"models": [{"name": "qwen3-embedding:0.6b"}]}
        if url.endswith("/api/status"):
            return {
                "ollama": {"generation": {**manager.generation_selection(config), "ready": True}}
            }
        return {}

    monkeypatch.setattr(manager, "request_json", request)
    value = manager.status()
    assert next(m for m in value["modules"] if m["module"] == "ollama")["ready"]
    assert value["generation"]["ready"]
    assert "synthetic-secret" not in str(value)


def test_explicit_purge_removes_generation_config_and_interrupted_save(root, monkeypatch):
    monkeypatch.setattr(manager, "stop", lambda **kwargs: None)
    for name in ["generation.local.json", ".generation-interrupted"]:
        path = root / "config" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("synthetic-secret")
    manager.uninstall(purge=True, confirmed=True)
    assert list((root / "config").iterdir()) == []


def test_native_reme_start_does_not_require_docker(root, monkeypatch):
    calls = []
    monkeypatch.setattr(manager, "python_script", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(manager, "config_snapshot", lambda: {"memory": {"backend": "graphiti"}})
    monkeypatch.setattr(manager, "capture", lambda *args, **kwargs: "reme")
    monkeypatch.setattr(manager, "docker_ready", lambda: pytest.fail("ReMe startup must not open Docker"))
    manager.database_start()
    assert calls == [("native_database.py", "start")]


@pytest.mark.parametrize("config,services", [
    ({"captureMode": "interaction", "ocrMode": "always"}, ["Accessibility", "ScreenCapture", "ListenEvent"]),
    ({"captureMode": "timer", "ocrMode": "off"}, ["Accessibility"]),
])
def test_permissions_reset_only_requirements_of_current_capture_mode(root, config, services):
    manager.save_json(root / "config/collector.json", config)
    assert [s for s, _, _ in manager.permission_requirements()] == services


@pytest.fixture
def permission_wizard(root, monkeypatch):
    binary = root / "macos/WorkMemory.app/Contents/MacOS/WorkMemory"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"unchanged signing identity")
    manager.save_json(root / "data/collector/state.json", {"consent": True, "running": False})
    calls = []
    monkeypatch.setattr(manager.os, "geteuid", lambda: 501)
    monkeypatch.setattr(manager.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(manager, "stop_collector", lambda: calls.append("stop"))
    monkeypatch.setattr(manager, "script", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(manager, "run", lambda args, *a, **kw: calls.append(args))
    monkeypatch.setattr("builtins.input", lambda prompt: calls.append("user-system-confirmation"))
    monkeypatch.setattr(manager, "permission_ingestion_since", lambda _: 1)
    return binary, calls


def test_guided_permissions_verify_fresh_state_and_preserve_paused_choice(root, permission_wizard, monkeypatch):
    binary, calls = permission_wizard
    original = (root / "data/collector/state.json").read_bytes()
    monkeypatch.setattr(manager, "permission_heartbeat", lambda _: {
        "accessibility_granted": True, "screen_recording_granted": True, "paused": True,
    })
    monkeypatch.setattr(manager, "permission_ingestion_since", lambda _: pytest.fail("paused capture must stay paused"))
    manager.permissions(guided=True)
    report = manager.read_json(root / ".run/permissions-result.json")
    assert report["permissions_ready"] and report["binary_unchanged"] and report["paused"]
    assert calls.count("stop") == 2 and calls.count("user-system-confirmation") == 2
    pages = [str(c[-1]) for c in calls if isinstance(c, list) and c[0] == "open" and "Privacy_" in str(c[-1])]
    assert [page.rsplit("_", 1)[-1] for page in pages] == ["Accessibility", "ScreenCapture"]
    assert (root / "data/collector/state.json").read_bytes() == original
    assert binary.read_bytes() == b"unchanged signing identity"


def test_guided_permissions_do_not_report_success_when_old_grants_still_fail(root, permission_wizard, monkeypatch):
    monkeypatch.setattr(manager, "permission_heartbeat", lambda _: {"accessibility_granted": False, "screen_recording_granted": False})
    ticks = iter([0, 1, 31])
    monkeypatch.setattr(manager.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(manager.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="仍未获得有效"):
        manager.permissions(guided=True)
    assert manager.read_json(root / ".run/permissions-result.json")["permissions_ready"] is False


def test_guided_permissions_detect_application_replacement(permission_wizard, monkeypatch):
    binary, _ = permission_wizard
    monkeypatch.setattr("builtins.input", lambda _: binary.write_bytes(b"replaced externally"))
    with pytest.raises(RuntimeError, match="其他构建替换"):
        manager.permissions(guided=True)


@pytest.mark.parametrize("pid,updated,accepted", [(123, 199, False), (456, 205, False), (123, 205, True)])
def test_permission_heartbeat_requires_current_process_and_new_sample(root, monkeypatch, pid, updated, accepted):
    monkeypatch.setattr(manager, "collector_pids", lambda: [123])
    monkeypatch.setattr(manager.time, "time", lambda: 207)
    manager.save_json(root / ".run/collector-status.json", {"pid": pid,
        "updated_at": manager.datetime.fromtimestamp(updated, manager.timezone.utc).isoformat(),
        "accessibility_granted": True, "screen_recording_granted": True})
    assert bool(manager.permission_heartbeat(200)) is accepted
