"""A model-supplied time must be grounded in an actual user utterance."""
import re

from fastapi import HTTPException
from sqlalchemy import select

from workmemory.db.models import AgentRun, ChatMessage

# This only checks that a quoted time is concrete; it never routes conversation,
# decides whether chat is allowed, or guesses a calendar value.
CLOCK = re.compile(
    r"(?:\d{1,2}[:：]\d{2}|[0-9零〇一二两三四五六七八九十百半]+\s*(?:点|小时|小時|時|时|分钟|分後|分后|刻钟|秒钟|秒)|"
    r"\b\d+\s*(?:a\.?m\.?|p\.?m\.?|minutes?|hours?|seconds?)\b|正午|中午十二|午夜|noon|midnight)",
    re.IGNORECASE,
)


async def verify_time_source(session, run_id, expression):
    if not expression or not CLOCK.search(expression):
        raise HTTPException(422, "缺少用户明确给出的具体时间。请先询问日期和几点提醒；不得自行默认10分钟后，也不能引用助手或工具中的时间。")
    run = await session.get(AgentRun, run_id)
    current = await session.get(ChatMessage, run.message_id)
    messages = (await session.scalars(select(ChatMessage).where(
        ChatMessage.conversation_id == run.conversation_id,
        ChatMessage.created_at <= current.created_at,
        ChatMessage.role == "user", ChatMessage.status == "ready", ChatMessage.content.is_not(None),
    ).order_by(ChatMessage.created_at.desc()).limit(4))).all()
    if not any(expression in m.content for m in messages):
        raise HTTPException(422, "time_expression必须逐字引用本次设置过程中用户提供的时间，不能编造或从记忆记录取时间；未提供时先询问。")
