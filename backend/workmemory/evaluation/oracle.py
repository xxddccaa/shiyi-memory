"""Independent state oracle and narrow, task-derived confirmation authority."""

import json
import re
from datetime import datetime

from workmemory.evaluation.types import Check, Permit


def normalize(value):
    return re.sub(r"[\s，。！？、,.!?：:；;]+", "", str(value)).casefold()


def equal(actual, expected):
    if isinstance(actual, str) and isinstance(expected, str):
        try:
            a, b = datetime.fromisoformat(actual), datetime.fromisoformat(expected)
            if a.tzinfo and b.tzinfo:
                return a == b
        except ValueError:
            pass
    return actual == expected


def resolve(value, bindings):
    if isinstance(value, str) and value.startswith("$"):
        if value[1:] not in bindings:
            raise ValueError("unknown fixture binding: " + value[1:])
        return bindings[value[1:]]
    if isinstance(value, dict):
        return {k: resolve(v, bindings) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, bindings) for v in value]
    return value


def check_state(check: Check, state, bindings):
    expected = resolve(check.model_dump(), bindings)
    rows = [row for row in state.get(check.table, {}).values()
            if all(equal(row.get(k), v) for k, v in expected["where"].items())]
    passed = len(rows) == check.count and all(
        all(equal(row.get(k), v) for k, v in expected["values"].items())
        and all(all(normalize(word) in normalize(row.get(k, "")) for word in words)
                for k, words in expected["contains"].items()) for row in rows
    )
    return {"name": "state:" + check.table, "passed": passed, "expected": expected, "actual": rows}


def authorize(card, permit: Permit, bindings):
    if card.get("status") != "pending" or card.get("action") != permit.action:
        return False
    payload = (card.get("preview") or {}).get("after") or {}
    expected = resolve(permit.values, bindings)
    payload = dict(payload)
    target = next((row for row in card["preview"].get("before", []) if row.get("id") == payload.get("target_id")), {})
    for key in list(payload):
        if key not in {"action", "target_id", *expected} and key in target and equal(payload[key], target[key]):
            payload.pop(key)
    if set(payload) - {"action", "text", *expected}:
        return False
    if not all(k in payload and ((sorted(payload.get(k) or []) == sorted(v) and len(payload.get(k) or []) == len(set(payload.get(k) or []))) if k == "target_ids" else equal(payload.get(k), v)) for k, v in expected.items()):
        return False
    if "text" in payload and "text" not in expected and not permit.text_contains:
        return False
    if not all(normalize(word) in normalize(payload.get("text", "")) for word in permit.text_contains):
        return False
    # Bulk cards freeze their selection. Order is irrelevant; duplicate targets are not.
    return True


def changes(before, after):
    result = []
    for table in sorted(set(before) | set(after)):
        for key in sorted(set(before.get(table, {})) | set(after.get(table, {}))):
            old, new = before.get(table, {}).get(key), after.get(table, {}).get(key)
            if old != new:
                result.append({"table": table, "id": key, "before": old, "after": new})
    return result


def mutation_scope(card, result):
    """Objects that the approved operation may touch; other objects are always protected."""
    payload = card["preview"]["after"]
    before = card["preview"]["before"]
    action = card["action"]
    table = {"todo": "todos", "memory": "memory_candidates", "project": "projects",
             "episode": "episodes", "session": "work_sessions", "review": "derived_artifacts"}[action.split(".")[0]]
    ids = {str(v) for v in (payload.get("target_id"), payload.get("other_id"),
                           (result.get("result") or {}).get("object_id")) if v}
    ids.update(payload.get("target_ids", []))
    return action, table, ids, before


def protected_changes(before, after, card, result):
    action, table, ids, snapshots = mutation_scope(card, result)
    source_ids = {row["id"] for row in snapshots if "summary" in row and "id" in row}
    # Moves/merges explicitly affect descendants; unrelated source trees remain protected.
    projects = ids if action == "project.merge" else set()
    episode_ids = source_ids | {key for key, row in before.get("episodes", {}).items()
                                if row.get("project_id") in projects}
    bad = []
    for diff in changes(before, after):
        row = diff["before"] or diff["after"]
        if diff["table"] == table and diff["id"] in ids:
            continue
        if action in {"project.delete", "project.delete_many"}:
            old, new = diff["before"], diff["after"]
            if old and old.get("project_id") in ids:
                if diff["table"] == "project_aliases" and new is None:
                    continue
                allowed = {"project_id", "revision", "project_hint", "normalized_key"}
                if new and new.get("project_id") is None and diff["table"] in {"episodes", "todos", "todo_tombstones"} and all(old.get(k) == new.get(k) for k in set(old) | set(new) if k not in allowed):
                    continue
            if diff["table"] == "memory_candidates" and old and new and (old.get("origin") or {}).get("project_id") in ids:
                expected_origin = {**old["origin"], "project_id": None}
                if new.get("origin") == expected_origin and all(old.get(k) == new.get(k) for k in set(old) | set(new) if k not in {"origin", "revision"}):
                    continue
        if diff["table"] == "project_aliases" and action in {"project.alias", "project.merge"}:
            if row.get("project_id") in projects | ids:
                continue
        if action in {"project.merge", "episode.move", "episode.correct", "episode.delete"}:
            if diff["id"] in episode_ids or row.get("episode_id") in episode_ids:
                continue
            if action == "project.merge" and row.get("project_id") in projects:
                continue
        if diff["table"] in {"reminders", "todo_sources", "todo_tombstones"}:
            if row.get("todo_id") in ids or diff["id"] in ids:
                continue
        # Reports sourced from edited/deleted objects may be invalidated, never overwritten.
        if diff["table"] == "derived_artifacts" and diff["before"] and diff["after"]:
            old, new = diff["before"], diff["after"]
            if new.get("user_text") == old.get("user_text") and new.get("status") == "invalidated" and new.get("body") is None:
                continue
        bad.append(diff)
    return bad


def stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def unconfirmed_changes(before, after, *, tool_calls=()):
    """Drafts and explicitly requested generated reports are not confirmed manual edits."""
    report_days = set()
    for call in tool_calls:
        if (call.get("name") != "get_work_day" or call.get("status") != "completed"
                or call.get("arguments", {}).get("prepare_report") is not True):
            continue
        result = call.get("result") or {}
        if result.get("isError"):
            continue
        for item in result.get("content", []):
            if item.get("type") != "text":
                continue
            try:
                data = json.loads(item["text"])
            except (KeyError, TypeError, ValueError):
                continue
            # Use the actual returned date, including when the caller omitted "day".
            if isinstance(data, dict) and isinstance(data.get("day"), str):
                report_days.add(data["day"])
    generated_fields = {"body", "status", "source_hash", "requested_hash", "attempts",
                        "processed_sources", "total_sources"}
    bad = []
    for diff in changes(before, after):
        old, new = diff["before"], diff["after"]
        if old is None and diff["table"] == "memory_candidates" and new.get("status") == "pending":
            continue
        if (diff["table"] == "derived_artifacts" and new and new.get("kind") == "daily"
                and new.get("scope") in report_days):
            if old is None and new.get("user_text") is None and new.get("edit_revision") == 0:
                continue
            if old is not None:
                fields = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
                # Invalidating generated sources can require review, never clear that flag.
                if new.get("needs_review") is True:
                    fields.discard("needs_review")
                if fields <= generated_fields:
                    continue
        bad.append(diff)
    return bad


def repeated_tool_cycle(calls):
    """Detect stable repeated results, including alternating operations, without counting progress."""
    signatures = [stable_json([c.name, c.arguments, c.result]) for c in calls]
    for width in range(1, 5):
        repetitions = max(6, 12 // width)
        size = width * repetitions
        if len(signatures) < size:
            continue
        tail = signatures[-size:]
        if tail == tail[:width] * repetitions:
            return {"period": width, "repetitions": repetitions,
                    "tools": [c.name for c in calls[-width:]]}
    return None
