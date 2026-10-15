"""Calendar schedules. The clock is supplied by callers for deterministic verification."""
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

REPEATS = {"once", "daily", "weekdays", "weekly"}
SCHEDULE_FIELDS = {"due_at", "timezone", "repeat", "weekdays", "paused", "notify_pet"}


def zone(name):
    try:
        return ZoneInfo(name)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise HTTPException(422, "请选择有效的 IANA 时区，例如 Asia/Shanghai") from None


def wall_datetime(day, wall, tz):
    """First fold on repeated times; nonexistent times move to the first valid minute."""
    value = datetime.combine(day, wall, tz).replace(fold=0)
    for _ in range(24 * 60 + 1):
        if value.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == value.replace(tzinfo=None):
            return value.astimezone(UTC)
        value += timedelta(minutes=1)
    raise HTTPException(422, "该日期在此时区不存在，请重新选择时间")


def allowed_days(repeat, weekdays):
    return set(weekdays) if repeat == "weekly" else {1, 2, 3, 4, 5} if repeat == "weekdays" else set(range(1, 8))


def validate_schedule(values, *, current=None, clock, default_timezone="Asia/Shanghai"):
    def field(key, default):
        return values.get(key, getattr(current, key, default) if current else default)
    tzname = field("timezone", default_timezone)
    tz = zone(tzname)
    repeat = field("repeat", "once")
    days = field("weekdays", [])
    if repeat not in REPEATS or not isinstance(days, list) or any(type(d) is not int or d not in range(1, 8) for d in days):
        raise HTTPException(422, "重复规则无效，星期须为 1（周一）至 7（周日）")
    if repeat == "weekly" and not days:
        raise HTTPException(422, "每周重复需要指定星期")
    if repeat != "weekly":
        if "weekdays" in values and days:
            raise HTTPException(422, "只有每周重复使用自选星期")
        days = []
    due = field("due_at", None)
    if due is None or due.tzinfo is None:
        raise HTTPException(422, "设置提醒需要明确的日期、时间和时区；暂停提醒请使用 paused=true")
    if due <= clock:
        raise HTTPException(422, "提醒时间已过去，请重新选择未来时间")
    local = due.astimezone(tz)
    if local.isoweekday() not in allowed_days(repeat, days):
        raise HTTPException(422, "首次提醒日期不在所选重复星期内，请调整日期")
    for key in ("paused", "notify_pet"):
        if key in values and type(values[key]) is not bool:
            raise HTTPException(422, "提醒开关不能为空")
    return dict(due_at=due.astimezone(UTC), timezone=tzname, repeat=repeat,
                weekdays=sorted(set(days)), local_time=local.strftime("%H:%M:%S"),
                paused=field("paused", False), notify_pet=field("notify_pet", True))


def due_occurrences(todo, clock):
    """Return latest elapsed occurrence, elapsed count, next future occurrence."""
    first = todo.due_at
    if first is None or first > clock:
        return None, 0, first
    if todo.repeat == "once":
        return first, 1, None
    tz = zone(todo.timezone)
    wall = time.fromisoformat(todo.local_time)
    allowed = allowed_days(todo.repeat, todo.weekdays)
    start, end = first.astimezone(tz).date(), clock.astimezone(tz).date()
    distance = (end - start).days
    weeks, rest = divmod(distance, 7)
    count = weeks * len(allowed) + sum((start + timedelta(days=i)).isoweekday() in allowed for i in range(rest + 1))
    today = wall_datetime(end, wall, tz)
    if end.isoweekday() in allowed and today > clock:
        count -= 1
    candidate = end
    while candidate.isoweekday() not in allowed or wall_datetime(candidate, wall, tz) > clock:
        candidate -= timedelta(days=1)
    latest = max(first, wall_datetime(candidate, wall, tz))
    candidate = end
    while candidate.isoweekday() not in allowed or wall_datetime(candidate, wall, tz) <= clock:
        candidate += timedelta(days=1)
    return latest, max(count, 1), wall_datetime(candidate, wall, tz)


def schedule_label(todo):
    label = {"once": "一次性", "daily": "每天", "weekdays": "周一至周五", "weekly": "每周"}[todo.repeat]
    if todo.repeat == "weekly":
        label += "、".join("一二三四五六日"[d - 1] for d in todo.weekdays)
    return label
