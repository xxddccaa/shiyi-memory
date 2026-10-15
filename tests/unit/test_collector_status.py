import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from workmemory.collector.status import collector_status
from workmemory.ingestion.service import Event


def test_collector_heartbeat_requires_fresh_live_process(tmp_path):
    settings = SimpleNamespace(
        collector=SimpleNamespace(
            interval_seconds=5, ocr_mode="always", capture_mode="timer", ocr_min_interval=5
        )
    )
    path = tmp_path / "status.json"
    assert not collector_status(settings, path)["running"]
    data = {
        "pid": os.getpid(),
        "updated_at": datetime.now(UTC).isoformat(),
        "paused": True,
        "accessibility_granted": False,
        "screen_recording_granted": False,
    }
    path.write_text(json.dumps(data))
    status = collector_status(settings, path)
    assert status["running"] and status["paused"]
    assert status["accessibility_granted"] is False
    data["updated_at"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    path.write_text(json.dumps(data))
    assert not collector_status(settings, path)["running"]


def test_paddleocr_text_event_is_accepted():
    import uuid

    event = Event(
        id=uuid.uuid4(),
        device_id="synthetic",
        captured_at=datetime.now(UTC),
        app="synthetic",
        text="中英文 OCR",
        method="paddleocr",
    )
    assert event.method == "paddleocr"


def test_capture_health_distinguishes_permission_pause_and_dedup():
    from workmemory.collector.status import capture_health
    clock = datetime.now(UTC)
    state = {"running": True, "paused": False, "accessibility_granted": False,
             "screen_recording_granted": False, "ocr_mode": "always"}
    assert capture_health(state, {}, clock)["state"] == "permission_required"
    state["paused"] = True
    assert capture_health(state, {}, clock)["state"] == "paused"
    state.update(paused=False, accessibility_granted=True, screen_recording_granted=True)
    assert capture_health(state, {}, clock)["state"] == "unconfirmed"
    state["last_capture_at"] = clock.isoformat()
    receipt = {"received_at": clock.isoformat(), "last_batch": {"unchanged": 1},
               "last_observation_at": (clock - timedelta(hours=8)).isoformat()}
    assert capture_health(state, receipt, clock)["state"] == "unchanged"
    state["screen_locked"] = True
    assert capture_health(state, receipt, clock)["state"] == "locked"
