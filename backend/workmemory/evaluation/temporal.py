"""Deterministic local-date hints for judging; original evidence remains untouched."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from workmemory.evaluation.oracle import stable_json

AWARE_TIMESTAMP = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})(?!\d)"
)


def time_context(payload, timezone):
    zone = ZoneInfo(timezone)
    anchor = datetime.fromisoformat(payload["time_anchor"])
    if anchor.tzinfo is None:
        raise ValueError("Evaluation time anchor requires a timezone")
    local_anchor = anchor.astimezone(zone)
    timestamps = []
    for raw in sorted(set(AWARE_TIMESTAMP.findall(stable_json(payload)))):
        try:
            local = datetime.fromisoformat(raw).astimezone(zone)
        except ValueError:
            continue
        timestamps.append({"original": raw, "local": local.isoformat(), "local_date": local.date().isoformat()})
    return {
        "timezone": timezone,
        "local_anchor": local_anchor.isoformat(),
        "today": local_anchor.date().isoformat(),
        "yesterday": (local_anchor.date() - timedelta(days=1)).isoformat(),
        "timestamps": timestamps,
    }
