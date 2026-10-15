"""Adversarial checks for the evaluator: false success must not pass."""

import copy

import pytest
from pydantic import ValidationError
from workmemory.evaluation.oracle import (
    authorize,
    check_state,
    protected_changes,
    unconfirmed_changes,
)
from workmemory.evaluation.report import comparison, exit_code, render
from workmemory.evaluation.scenarios import library
from workmemory.evaluation.types import Check, Judgment, Permit, verdict


def good_judge():
    return Judgment(verdict="pass", understanding=True, completion=True, grounded=True,
                    no_false_success=True, appropriate_clarification=True,
                    reasons=["合成通过"], evidence=["state"])


def card(action="todo.delete", **payload):
    return {"id": "card", "status": "pending", "action": action,
            "preview": {"before": [], "after": {"action": action, **payload}}}


def test_model_claim_cannot_override_database_failure():
    check = check_state(Check(table="todos", where={"id": "created"}), {"todos": {}}, {})
    assert not check["passed"]
    assert verdict([check], good_judge()) == "fail"
    assert verdict([check], None, "judge_timeout") == "incomplete"


@pytest.mark.parametrize("error", ["case_timeout", "judge_ValidationError", "user_ValidationError", "simulator_internal_hint"])
def test_infrastructure_error_never_passes(error):
    assert verdict([], good_judge(), error) == "incomplete"


def test_inconsistent_positive_judge_rejected():
    with pytest.raises(ValidationError):
        Judgment(**{**good_judge().model_dump(), "grounded": False})


def test_wrong_target_and_extra_changes_are_not_authorized():
    permit = Permit(action="todo.update", values={"target_id": "$target", "done": True})
    assert authorize(card("todo.update", target_id="a", done=True), permit, {"target": "a"})
    assert not authorize(card("todo.update", target_id="b", done=True), permit, {"target": "a"})
    assert not authorize(card("todo.update", target_id="a", done=True, text="恶意修改"), permit, {"target": "a"})
    assert not authorize(card("todo.delete", target_id="a"), permit, {"target": "a"})


def test_bulk_card_has_exact_unique_frozen_targets():
    permit = Permit(action="todo.delete_many", values={"target_ids": "$targets"})
    bindings = {"targets": ["a", "b"]}
    assert authorize(card("todo.delete_many", target_ids=["b", "a"]), permit, bindings)
    assert not authorize(card("todo.delete_many", target_ids=["a"]), permit, bindings)
    assert not authorize(card("todo.delete_many", target_ids=["a", "b", "c"]), permit, bindings)
    assert not authorize(card("todo.delete_many", target_ids=["a", "b", "a"]), permit, bindings)


def test_pending_candidate_is_not_confirmed_memory_but_auto_confirmation_fails():
    before = {"memory_candidates": {}}
    after = {"memory_candidates": {"a": {"status": "pending", "text": "用户原话"}}}
    assert not unconfirmed_changes(before, after)
    after["memory_candidates"]["a"]["status"] = "confirmed"
    assert unconfirmed_changes(before, after)


def test_generated_review_refresh_requires_current_tool_and_preserves_manual_fields():
    import json

    old = {"id": "r", "kind": "daily", "scope": "2026-10-10", "user_text": "人工回顾",
           "edit_revision": 1, "needs_review": False, "body": {}, "source_hash": "", "status": "ready"}
    before = {"derived_artifacts": {"r": old}}
    after = copy.deepcopy(before)
    after["derived_artifacts"]["r"].update(body={"unknowns": ["没有记录"]}, source_hash="fresh")
    call = {"name": "get_work_day", "status": "completed", "arguments": {"prepare_report": True},
            "result": {"content": [{"type": "text", "text": json.dumps({"day": "2026-10-10"})}]}}
    assert not unconfirmed_changes(before, after, tool_calls=[call])
    assert unconfirmed_changes(before, after)
    assert unconfirmed_changes(before, after, tool_calls=[{**call, "arguments": {}}])
    assert unconfirmed_changes(before, after, tool_calls=[{**call, "status": "failed"}])
    assert unconfirmed_changes(before, after, tool_calls=[{**call, "result": {**call["result"], "isError": True}}])
    wrong_day = copy.deepcopy(call)
    wrong_day["result"]["content"][0]["text"] = json.dumps({"day": "2026-10-09"})
    assert unconfirmed_changes(before, after, tool_calls=[wrong_day])
    for key, value in (("user_text", "偷偷改写"), ("edit_revision", 2), ("scope", "2026-10-09"), ("id", "another")):
        tampered = copy.deepcopy(after)
        tampered["derived_artifacts"]["r"][key] = value
        assert unconfirmed_changes(before, tampered, tool_calls=[call]), key
    assert unconfirmed_changes(before, {"derived_artifacts": {}}, tool_calls=[call])
    # New auto reports need the same observed tool; they cannot contain manual edits.
    created = copy.deepcopy(after)
    created["derived_artifacts"]["r"].update(user_text=None, edit_revision=0)
    assert not unconfirmed_changes({}, created, tool_calls=[call])
    assert unconfirmed_changes({}, created)
    created["derived_artifacts"]["r"]["user_text"] = "未经确认"
    assert unconfirmed_changes({}, created, tool_calls=[call])


def test_unapproved_update_is_caught_even_when_final_count_correct():
    before = {"todos": {"a": {"id": "a", "text": "周报"}, "b": {"id": "b", "text": "预算"}}}
    after = copy.deepcopy(before)
    after["todos"]["b"]["text"] = "周报"
    assert unconfirmed_changes(before, after)
    assert protected_changes(before, after, card(target_id="a"), {"result": {"object_id": "a"}})


def test_wrong_deletion_and_unrelated_manual_review_edits_fail():
    before = {"todos": {"a": {"id": "a"}, "b": {"id": "b"}}, "derived_artifacts": {"r": {"user_text": "人工原文", "status": "ready"}}}
    after = {"todos": {"a": {"id": "a"}}, "derived_artifacts": {"r": {"user_text": "被篡改", "status": "invalidated", "body": None}}}
    bad = protected_changes(before, after, card(target_id="a"), {"result": {"object_id": "a"}})
    assert {v["table"] for v in bad} == {"todos", "derived_artifacts"}


def test_time_checks_compare_instants_not_offset_spelling():
    expected = Check(table="todos", where={"id": "a"}, values={"due_at": "$tomorrow"})
    assert check_state(expected, {"todos": {"a": {"id": "a", "due_at": "2026-10-11T07:00:00+00:00"}}},
                       {"tomorrow": "2026-10-11T15:00:00+08:00"})["passed"]


def test_curated_matrix_covers_all_supported_operations_and_history_regressions():
    from workmemory.workspace.operations import LABELS

    cases = library()
    assert len(cases) >= 50 and all(len(c.openings) == 3 and len(set(c.openings)) == 3 for c in cases)
    assert sum(c.smoke for c in cases) == 12
    assert {p.action for c in cases for s in c.stages for p in s.permits} == set(LABELS)
    assert {"chat-model", "work-today", "work-summary", "todo-bulk-69", "todo-delete-context", "memory-compacted-forget"} <= {c.id for c in cases}


def report_fixture(status="pass"):
    c = library()[0]
    return {"status": "complete", "planned": 1, "error": None, "coverage": [{"id": c.id, "group": c.group, "variants": 3}],
            "meta": {"model_fingerprint": "x", "role_model": "27B", "library_fingerprint": "a", "prompt_fingerprint": "p", "timezone": "Asia/Shanghai"},
            "cases": [{"id": c.id, "variant": 1, "repeat": 1, "status": status, "group": c.group, "scenario": c.model_dump(), "trace": [], "seconds": 1}]}


def test_exit_status_missing_cases_and_baseline_changes():
    baseline = report_fixture()
    current = report_fixture("fail")
    assert exit_code(baseline) == 0 and exit_code(current) == 1
    assert comparison(current, baseline)["regressions"]
    current["planned"] = 2
    assert exit_code(current) == 2
    current["meta"]["model_fingerprint"] = "different"
    compared = comparison(current, baseline)
    assert not compared["comparable"] and not compared["regressions"]


def test_report_escapes_model_injected_html():
    report = report_fixture()
    attack = '<img src=x onerror="alert(1)">'
    report["cases"][0]["error"] = attack
    report["cases"][0]["trace"] = [{"type": "message", "stage": 1, "role": "assistant", "content": attack}]
    rendered = render(report)
    assert attack not in rendered and "&lt;img" in rendered


def test_repeating_unchanged_fields_is_not_unauthorized_change():
    p = Permit(action="todo.update", values={"target_id": "a", "done": True})
    c = card("todo.update", target_id="a", done=True, text="周报")
    c["preview"]["before"] = [{"id": "a", "text": "周报", "done": False}]
    assert authorize(c, p, {})
    c["preview"]["after"]["text"] = "月报"
    assert not authorize(c, p, {})


def test_missing_explicit_clear_field_is_not_authorized():
    p = Permit(action="todo.update", values={"target_id": "a", "due_at": None})
    assert not authorize(card("todo.update", target_id="a"), p, {})


class RoleProvider:
    def __init__(self, response):
        from workmemory.config import OllamaConfig
        from workmemory.config.generation import GenerationConfig

        self.generation = GenerationConfig(provider="openai_compatible", base_url="http://127.0.0.1:9999/v1",
                                           model="target-model", api_key="synthetic-key")
        self.generation_model = self.generation.model
        self.settings = OllamaConfig()
        self.response = response
        self.requests = []

    async def request(self, path, body, priority, generation):
        import json

        self.requests.append((path, body, priority, generation))
        return {"choices": [{"message": {"content": json.dumps(self.response)}, "finish_reason": "stop"}]}


async def test_user_schema_cannot_select_message_ids_as_cards():
    from workmemory.evaluation.environment import InfrastructureError
    from workmemory.evaluation.runner import Roles

    provider = RoleProvider({"action": "confirm", "text": "", "card_id": "message-not-card", "reason": "wrong"})
    roles = Roles(provider, "judge-27B")
    with pytest.raises(InfrastructureError, match="user_ValidationError"):
        await roles.ask("user", {"task": "创建待办", "confirmation_policy": "confirm", "view": {"cards": []}})
    schema = provider.requests[0][1]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["action"]["enum"] == ["send", "finish"]
    assert schema["properties"]["card_id"]["enum"] == [""]


async def test_roles_share_scheduler_without_reconfiguring_target_model():
    from workmemory.evaluation.runner import Roles

    provider = RoleProvider({"action": "confirm", "text": "", "card_id": "visible-card", "reason": "符合任务"})
    roles = Roles(provider, "judge-27B")
    result = await roles.ask("user", {"task": "创建待办", "confirmation_policy": "confirm",
                                      "view": {"cards": [{"id": "visible-card", "status": "pending"}]}})
    assert result.card_id == "visible-card"
    assert provider.generation.model == "target-model"
    assert provider.requests[0][3].model == "judge-27B"
    assert provider.requests[0][3].base_url == provider.generation.base_url
    assert "synthetic-key" not in repr(roles.calls)


async def test_judge_has_new_context_and_validated_structured_result():
    from workmemory.evaluation.runner import Roles

    provider = RoleProvider(good_judge().model_dump())
    roles = Roles(provider, "judge-27B")
    roles.calls.append({"role": "user", "result": {"text": "do not copy simulator private chain"}})
    await roles.ask("judge", {"task": "synthetic", "hard_checks": []})
    messages = provider.requests[0][1]["messages"]
    assert len(messages) == 2 and "do not copy simulator private chain" not in repr(messages)


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_periodic_tool_loops_stop_even_when_targets_alternate(width):
    from types import SimpleNamespace

    from workmemory.evaluation.oracle import repeated_tool_cycle

    pattern = [SimpleNamespace(name="propose_operation", arguments={"target_id": str(i)},
                               result={"proposal": str(i), "status": "pending"}) for i in range(width)]
    repetitions = max(6, 12 // width)
    assert repeated_tool_cycle(pattern * repetitions)["period"] == width
    assert repeated_tool_cycle((pattern * repetitions)[:-1]) is None
    progressed = pattern * repetitions
    progressed[-1] = SimpleNamespace(name="propose_operation", arguments={"target_id": "new"},
                                    result={"proposal": "new", "status": "pending"})
    assert repeated_tool_cycle(progressed) is None


def test_same_tool_with_new_pages_is_progress():
    from types import SimpleNamespace

    from workmemory.evaluation.oracle import repeated_tool_cycle

    calls = [SimpleNamespace(name="list_objects", arguments={"cursor": i}, result={"items": [i]}) for i in range(30)]
    assert repeated_tool_cycle(calls) is None
    calls = [SimpleNamespace(name="list_objects", arguments={}, result={"revision": i}) for i in range(30)]
    assert repeated_tool_cycle(calls) is None


def test_judge_time_context_resolves_same_instant_without_rewriting_evidence():
    import json

    from workmemory.evaluation.temporal import time_context

    utc = "2026-10-10T16:00:00+00:00"
    local = "2026-10-11T00:00:00+08:00"
    payload = {"time_anchor": "2026-10-12T00:35:00+08:00", "day": "2026-10-11",
               "trace": [{"captured_at": utc}], "tool_result": json.dumps({"captured_at": local})}
    original = copy.deepcopy(payload)
    context = time_context(payload, "Asia/Shanghai")
    dates = {row["original"]: row for row in context["timestamps"]}
    assert context["today"] == "2026-10-12" and context["yesterday"] == "2026-10-11"
    assert dates[utc]["local"] == dates[local]["local"] == local
    assert dates[utc]["local_date"] == context["yesterday"]
    assert payload == original


def test_judge_time_context_keeps_actual_wrong_dates_distinct():
    from workmemory.evaluation.temporal import time_context

    wrong, right = "2026-10-10T15:59:59Z", "2026-10-10T16:00:00Z"
    payload = {"time_anchor": "2026-10-12T00:35:00+08:00", "evidence": [wrong, right]}
    context = time_context(payload, "Asia/Shanghai")
    dates = {row["original"]: row["local_date"] for row in context["timestamps"]}
    assert dates[wrong] == "2026-10-10" != context["yesterday"]
    assert dates[right] == context["yesterday"]
    # Date hints never override the existing independent failure rule.
    assert verdict([{"passed": False}], good_judge()) == "fail"


def test_judge_time_context_uses_configured_zone_and_does_not_guess_missing_offsets():
    from workmemory.evaluation.temporal import time_context

    instant = "2026-10-10T15:30:00Z"
    payload = {"time_anchor": "2026-10-11T00:30:00+09:00", "evidence": [instant,
               "2026-10-10T15:30:00", "2026-99-99T25:99:99+00:00"]}
    tokyo, shanghai = (time_context(payload, zone) for zone in ("Asia/Tokyo", "Asia/Shanghai"))
    assert tokyo["today"] == "2026-10-11" and shanghai["today"] == "2026-10-10"
    assert next(row for row in tokyo["timestamps"] if row["original"] == instant)["local_date"] == tokyo["today"]
    assert len(tokyo["timestamps"]) == 2
    with pytest.raises(ValueError, match="requires a timezone"):
        time_context({"time_anchor": "2026-10-12T00:35:00"}, "Asia/Shanghai")


async def test_single_turn_case_cannot_invent_followup_time():
    from workmemory.evaluation.runner import Roles
    provider = RoleProvider({"action":"finish", "text":"", "card_id":"", "reason":"已询问时间，等待真实用户提供"})
    await Roles(provider,"judge-27B").ask("user",{"task":"缺时间应澄清", "confirmation_policy":"none", "allow_followups":False,"view":{"cards":[]}})
    schema = provider.requests[0][1]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["action"]["enum"] == ["finish"]


def test_project_deletion_allows_detaching_but_never_overwriting_or_deleting_todos():
    before = {"projects": {"p": {"id": "p", "state": "active"}}, "todos": {"t": {"id": "t", "project_id": "p", "text": "保留任务", "revision": 1}}}
    after = copy.deepcopy(before)
    after["projects"]["p"]["state"] = "deleted"
    after["todos"]["t"].update(project_id=None, revision=2)
    op = card("project.delete_many", target_ids=["p"])
    assert protected_changes(before, after, op, {}) == []
    after["todos"]["t"]["text"] = "擅自改写"
    assert protected_changes(before, after, op, {})
    after["todos"] = {}
    assert protected_changes(before, after, op, {})
