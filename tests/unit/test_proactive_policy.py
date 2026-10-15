from datetime import datetime, timezone

import pytest
from workmemory.companion.proactive import quiet
from workmemory.companion.proactive_config import ProactiveSettings


@pytest.mark.parametrize(
    "hour, expected", [(0, True), (7, True), (8, False), (22, False), (23, True)]
)
def test_quiet_window_crosses_midnight(hour, expected):
    clock = datetime(2026, 10, 12, hour, tzinfo=timezone.utc)
    assert quiet(ProactiveSettings(), clock, "UTC") is expected


def test_equal_quiet_times_disable_quiet_hours():
    assert not quiet(
        ProactiveSettings(quiet_start="00:00", quiet_end="00:00"), datetime.now(timezone.utc), "UTC"
    )
