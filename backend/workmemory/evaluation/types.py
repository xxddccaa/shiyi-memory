"""Versioned task contracts shared by the runner, oracle and reports."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Check(Strict):
    table: str
    where: dict[str, Any] = Field(default_factory=dict)
    count: int = 1
    values: dict[str, Any] = Field(default_factory=dict)
    contains: dict[str, list[str]] = Field(default_factory=dict)


class Permit(Strict):
    action: str
    values: dict[str, Any] = Field(default_factory=dict)
    text_contains: list[str] = Field(default_factory=list)
    bind: str | None = None
    # Maximum number of confirmations of this action in one stage, excluding replay checks.
    count: int = 1


class Stage(Strict):
    message: str
    goal: str
    permits: list[Permit] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    context: str | None = None
    allow_followups: bool = True
    decision: Literal["confirm", "reject", "none"] = "confirm"
    hook: Literal["none", "expired", "changed", "duplicate", "new_todo", "cancel", "retry", "resend"] = "none"
    before: Literal["none", "new_conversation", "compact", "pet"] = "none"
    required_tools: list[str] = Field(default_factory=list)
    forbidden_answer: list[str] = Field(default_factory=list)


class Scenario(Strict):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    version: int = 1
    group: str
    description: str
    seed: list[str] = Field(default_factory=list)
    openings: list[str] = Field(min_length=3, max_length=3)
    stages: list[Stage] = Field(min_length=1)
    smoke: bool = False
    pipeline: bool = False
    pending: bool = False


class UserAction(Strict):
    action: Literal["send", "confirm", "reject", "finish"]
    text: str
    card_id: str
    reason: str

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == "send" and not self.text.strip():
            raise ValueError("empty user message")
        if self.action in {"confirm", "reject"} and not self.card_id:
            raise ValueError("card missing")
        return self


class Judgment(Strict):
    verdict: Literal["pass", "fail", "incomplete"]
    understanding: bool
    completion: bool
    grounded: bool
    no_false_success: bool
    appropriate_clarification: bool
    reasons: list[str] = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent(self):
        if self.verdict == "pass" and not all((self.understanding, self.completion, self.grounded,
                                               self.no_false_success, self.appropriate_clarification)):
            raise ValueError("inconsistent judge verdict")
        return self


def verdict(checks, judgment=None, error=None):
    """Errors never count as passes, and a judge cannot overrule hard failures."""
    if error or judgment is None or judgment.verdict == "incomplete":
        return "incomplete"
    if any(not item["passed"] for item in checks) or judgment.verdict == "fail":
        return "fail"
    return "pass"
