"""Backup ownership/retention and capture diagnostics use content-free fixtures."""

import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from workmemory.collector.status import capture_health
from workmemory.config import ROOT
from workmemory.maintenance.backups import BackupFiles, checksum


def test_retention_keeps_daily_weekly_and_all_unregistered_files(tmp_path):
    files = BackupFiles(tmp_path)
    clock = datetime(2026, 10, 13, 4, tzinfo=UTC)
    for days in range(45):
        path = files.path(f"auto-{days}.dump")
        path.write_bytes(b"synthetic dump")
        files.metadata(
            path,
            {
                "created_at": (clock - timedelta(days=days)).isoformat(),
                "managed_by": "workmemory-daily-v1",
            },
        )
    manual = files.path("manual-keep.dump")
    manual.write_bytes(b"manual")
    unregistered = files.path("imported.dump")
    unregistered.write_bytes(b"unregistered")
    files.prune(clock, "UTC")
    assert all(files.path(f"auto-{days}.dump").exists() for days in range(7))
    assert not files.path("auto-40.dump").exists()
    # Latest representative from each of the 4 calendar weeks, plus daily slots.
    for days in (0, 2, 9, 16):
        assert files.path(f"auto-{days}.dump").exists()
    assert manual.read_bytes() == b"manual" and unregistered.read_bytes() == b"unregistered"
    assert len([item for item in files.list() if item["automatic"]]) == 9


def test_backup_paths_reject_traversal_symlinks_and_concurrent_workers(tmp_path):
    files = BackupFiles(tmp_path)
    with pytest.raises(ValueError):
        files.path("../outside.dump")
    outside = tmp_path / "outside.dump"
    outside.write_bytes(b"keep")
    (files.directory / "symlink.dump").symlink_to(outside)
    with pytest.raises(ValueError):
        files.path("symlink.dump")
    with files.lock():
        with pytest.raises(BlockingIOError):
            with files.lock():
                pytest.fail("second process acquired the same backup lock")
    assert outside.read_bytes() == b"keep"


def test_owned_partial_recovers_and_unknown_dump_is_never_adopted(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "maintenance_task_test", ROOT / "scripts/maintenance_task.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    files = BackupFiles(tmp_path)
    dump = files.path("manual-test.dump")
    partial = dump.with_suffix(".dump.partial")
    partial.write_bytes(b"interrupted")
    dump.with_suffix(".owner.json").write_text(json.dumps({"id": dump.name, "automatic": False}))

    class PG:
        def backup(self, path):
            assert not partial.exists()
            path.write_bytes(b"synthetic valid archive")

        def run(self, *args):
            assert args[:2] == ("pg_restore", "--list")

    result = module.execute_locked(PG(), files, "backup", dump.name, False)
    assert result["sha256"] == checksum(dump) and not partial.exists()
    assert module.execute_locked(PG(), files, "backup", dump.name, False) == result
    unknown = files.path("unregistered.dump")
    unknown.write_bytes(b"keep")
    with pytest.raises(ValueError, match="拒绝接管"):
        module.execute_locked(PG(), files, "backup", unknown.name, False)
    assert unknown.read_bytes() == b"keep"
    assert os.stat(dump.with_suffix(".json")).st_mode & 0o777 == 0o600


def test_capture_normal_skips_do_not_become_false_incidents():
    clock = datetime.now(UTC)
    state = dict(
        running=True,
        capture_active=True,
        telemetry_version=2,
        paused=False,
        accessibility_granted=True,
        screen_recording_granted=True,
        ocr_mode="always",
        last_progress_at=(clock - timedelta(minutes=10)).isoformat(),
    )
    assert capture_health(state, {}, clock)["state"] == "stalled"
    for reason in ("secure_input", "excluded_app", "excluded_title", "no_window"):
        assert capture_health({**state, "skip_reason": reason}, {}, clock)["state"] == "excluded"
    assert capture_health({**state, "thermal_state": "critical"}, {}, clock)["state"] == "thermal"
    assert capture_health({**state, "screen_locked": True}, {}, clock)["state"] == "locked"
    assert capture_health({**state, "paused": True}, {}, clock)["state"] == "paused"
    assert capture_health({**state, "queue_full": True}, {}, clock)["state"] == "error"
    assert (
        capture_health(
            {**state, "last_progress_at": clock.isoformat(), "skip_reason": "empty_text"}, {}, clock
        )["state"]
        == "empty"
    )
    failing = {
        **state,
        "spooled_batches": 3,
        "upload_failure_since": (clock - timedelta(minutes=5)).isoformat(),
    }
    assert capture_health(failing, {}, clock)["state"] == "error"
