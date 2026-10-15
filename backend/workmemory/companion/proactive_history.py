"""Read-only, source-checked short-term companionship history for the Web UI."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from workmemory.companion.proactive import STATE_KEY, blocked_reason, config_hash, stamp
from workmemory.companion.proactive_config import CADENCE, ProactiveSettings
from workmemory.companion.state import preferences
from workmemory.db.models import AppState, ProactiveUtterance
from workmemory.evidence.service import dependencies, valid_dependencies
from workmemory.jobs.queue import now

REASONS = {
    "disabled": "主动陪伴已关闭",
    "no_topics": "还没有选择陪伴话题",
    "hidden": "桌宠已隐藏",
    "saving": "节能模式中，暂停主动陪伴",
    "muted": "正在临时安静",
    "completion_limit": "完成鼓励已达到次数或间隔限制",
    "quiet_hours": "当前在安静时段内",
    "offline": "暂未收到桌宠的新鲜心跳，请确认桌宠正在运行",
    "unavailable": "桌宠暂时不便展示（可能正在全屏、锁屏、聊天或显示正式提醒）",
    "interactive": "助手正在回答，稍后再陪伴",
    "daily_limit": "今天已达到展示上限",
    "model_busy": "模型正在忙，本次机会已跳过",
    "generating": "已有一句正在生成，本次机会已跳过",
    "no_context": "所选话题暂无可用材料，请检查近期工作记录或天气设置",
    "cancelled": "生成期间展示条件发生变化，本次已跳过",
    "source_invalidated": "所依据的内容已变化或失效",
    "model_changed": "生成服务已切换，本次已跳过",
    "invalid_answer": "这次生成的内容未通过检查，已跳过",
    "tool_unavailable": "天气服务或常用城市尚不可用",
    "budget_exhausted": "这次生成达到执行上限，已跳过",
    "generation_failed": "这次生成未成功，已跳过",
}
TOPICS = {
    "playful": "俏皮闲聊",
    "work": "工作鼓励",
    "weather": "天气关心",
    "break": "休息邀请",
    "opportunity": "陪伴机会",
}


def outcome(row, clock, source_valid):
    if not source_valid or row.status == "invalidated":
        return "invalidated", "内容已失效", "所依据的内容已变化或失效，正文不再展示"
    if row.status == "attached":
        return "attached", "已接入聊天", "点击气泡后已加入共享会话"
    if row.shown_at:
        return "shown", "桌宠已确认展示", "气泡只停留约10秒；展示回执不代表你一定看到了"
    if row.status == "skipped":
        return "skipped", "已跳过", REASONS.get(row.error, REASONS["generation_failed"])
    if row.status == "dismissed":
        return "not_shown", "未确认展示", "这句已关闭，没有收到桌宠展示回执"
    if row.expires_at <= clock or row.status == "expired":
        if row.content:
            return "not_shown", "生成后未确认展示", "展示机会已过，没有收到桌宠展示回执"
        return "skipped", "生成超时", "生成未在有效时间内完成，已跳过"
    if row.status == "running":
        return "running", "正在想一句话", ""
    return "ready", "等待桌宠展示", ""


async def history(runtime, limit=30):
    clock = now()
    timezone = runtime.settings.timezone
    async with runtime.sessions() as session:
        prefs = await preferences(session)
        config = ProactiveSettings.model_validate(prefs["proactive"])
        ledger = await session.get(AppState, STATE_KEY)
        state = ledger.value if ledger else {}
        day = clock.astimezone(ZoneInfo(timezone)).date().isoformat()
        count = state.get("count", 0) if state.get("day") == day else 0
        cap = CADENCE[config.frequency][2]
        reason = await blocked_reason(session, prefs, state, clock, timezone)
        if not reason and count >= cap:
            reason = "daily_limit"
        next_at = (
            state.get("next_at")
            if (
                not reason
                and state.get("eligible")
                and state.get("settings_hash") == config_hash(prefs)
            )
            else None
        )
        conditions = (
            ProactiveUtterance.preview.is_(False),
            ProactiveUtterance.created_at > clock - timedelta(hours=24),
        )
        total = await session.scalar(
            select(func.count()).select_from(ProactiveUtterance).where(*conditions)
        )
        rows = (
            await session.scalars(
                select(ProactiveUtterance)
                .where(*conditions)
                .order_by(ProactiveUtterance.created_at.desc(), ProactiveUtterance.id.desc())
                .limit(limit)
            )
        ).all()
        items = []
        for row in rows:
            # History outlives the display lease, never the underlying sources.
            source_valid = await valid_dependencies(
                session,
                await dependencies(session, "proactive", row.id),
                lock=True,
                retention_days=runtime.settings.retention_days,
            )
            status, label, detail = outcome(row, clock, source_valid)
            items.append(
                {
                    "id": str(row.id),
                    "topic": "事项完成" if row.trigger == "completion" else TOPICS.get(row.topic, "陪伴机会"),
                    "trigger": row.trigger,
                    "text": row.content if source_valid and status != "invalidated" else None,
                    "status": status,
                    "label": label,
                    "detail": detail,
                    "created_at": row.created_at.isoformat(),
                    "shown_at": row.shown_at.isoformat() if row.shown_at else None,
                }
            )
        active = next((item for item in items if item["status"] in {"running", "ready"}), None)
        if reason:
            status_text = REASONS[reason]
        elif active:
            status_text = active["label"]
        else:
            status_text = "正在等待下一次陪伴机会" if next_at else "正在安排下一次陪伴机会"
        muted_until = stamp(state.get("muted_until"))
        return {
            "items": items,
            "total": total,
            "retention_hours": 24,
            "status": {
                "reason": reason,
                "text": status_text,
                "next_at": next_at,
                "today_shown": count,
                "daily_limit": cap,
                "timezone": timezone,
                "muted_until": muted_until.isoformat()
                if muted_until and muted_until > clock
                else None,
                "checked_at": clock.isoformat(),
            },
        }
