"""Shared defaults for proactive companionship, independent of capture consent."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

STYLE = (
    "你是住在用户桌边的调皮小伙伴。说话轻快、可爱，偶尔撒娇，偶尔有一点无厘头。"
    "每次只说一两句短话，像突然探头冒一句。不要说教、汇报工作或反复劝人休息，"
    "不要求用户回复。可以俏皮，但不能编造用户经历、工作进展或天气。"
)
CADENCE = {"lively": (10, 20, 30), "balanced": (30, 60, 12), "quiet": (60, 90, 6)}


class ProactiveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completion_enabled: bool = True
    topic_preferences: dict[Literal["playful", "work", "weather", "break"], Literal["more", "normal", "less"]] = Field(default_factory=dict)
    enabled: bool = True
    frequency: Literal["lively", "balanced", "quiet"] = "lively"
    topics: list[Literal["playful", "work", "weather", "break"]] = Field(
        default_factory=lambda: ["playful", "work", "weather", "break"], max_length=4
    )
    style: str = Field(default=STYLE, max_length=2000)
    quiet_start: str = Field(default="23:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    quiet_end: str = Field(default="08:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
