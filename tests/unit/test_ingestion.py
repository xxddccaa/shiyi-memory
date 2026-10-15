import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from workmemory.config import ROOT, get_settings
from workmemory.ingestion.service import Event, allowed, episode_key, normalize


def event(**kwargs):
    defaults = dict(
        id=uuid.uuid4(),
        device_id="unit",
        captured_at=datetime(2020, 10, 9, 8, 3, tzinfo=UTC),
        app="editor",
        text="工作文本",
    )
    defaults.update(kwargs)
    return Event(**defaults)


def test_bucket_timezone_and_project_partition():
    original = event()
    second = original.model_copy(update={"captured_at": original.captured_at.astimezone()})
    assert episode_key(original, 5) == episode_key(second, 5)
    assert episode_key(original, 5)[1].minute == 0
    assert (
        episode_key(original.model_copy(update={"project_hint": "蓝鲸"}), 5)[0]
        != episode_key(original, 5)[0]
    )


def test_sensitive_windows_filtered_and_normalization():
    settings = get_settings()
    assert not allowed(event(title="密码 password"), settings)
    assert not allowed(event(app="com.apple.keychainaccess"), settings)
    assert normalize(" a\x00  b\n c ") == "a b\n c"


def test_paths_independent_of_cwd_and_no_cloud(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert get_settings().path("data/postgres") == ROOT / "data/postgres"
    with pytest.raises(ValueError):
        get_settings().path("../outside")
    from workmemory.config import OllamaConfig

    with pytest.raises(ValidationError):
        OllamaConfig(url="https://cloud.example.com")


def test_future_and_naive_dates_rejected():
    with pytest.raises(ValidationError):
        event(captured_at=datetime.now())
    with pytest.raises(ValidationError):
        event(captured_at=datetime.now(UTC) + timedelta(days=1))


def test_interaction_before_and_after_share_episode_at_boundary():
    trigger = datetime(2020, 10, 9, 8, 5, tzinfo=UTC)
    group = uuid.uuid4()
    before = event(
        captured_at=trigger - timedelta(milliseconds=250),
        interaction_id=group,
        event_at=trigger,
        phase="before",
    )
    after = event(
        captured_at=trigger + timedelta(seconds=1),
        interaction_id=group,
        event_at=trigger,
        phase="after",
    )
    assert episode_key(before, 5) == episode_key(after, 5)
