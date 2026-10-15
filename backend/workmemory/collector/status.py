"""Read a content-free heartbeat written by the signed collector process."""

import json
import os
from datetime import UTC, datetime

import httpx

from workmemory.config import ROOT


def collector_status(settings, path=None):
    result = {
        "running": False,
        "paused": None,
        "accessibility_granted": None,
        "screen_recording_granted": None,
        "ocr_mode": settings.collector.ocr_mode,
        "ocr_engine": "paddleocr",
        "capture_mode": settings.collector.capture_mode,
        "interval_seconds": max(
            settings.collector.interval_seconds, settings.collector.ocr_min_interval
        ),
    }
    path = path or ROOT / ".run/collector-status.json"
    try:
        if path.stat().st_size > 16384:
            return result
        state = json.loads(path.read_text())
        updated = datetime.fromisoformat(state["updated_at"])
        age = (datetime.now(UTC) - updated).total_seconds()
        if not 0 <= age < max(120, settings.collector.interval_seconds * 3):
            return result
        pid = int(state["pid"])
        if pid <= 0:
            return result
        os.kill(pid, 0)
        keys = [
            "telemetry_version", "process_id", "stage", "skip_reason", "queue_full",
            "sample_attempts", "busy_skips", "screenshots", "screenshot_failures", "ocr_attempts", "ocr_successes", "empty_results",
            "upload_failure_since", "upload_failures", "upload_accepted", "upload_filtered", "last_progress_at", "last_screenshot_at", "last_upload_at",
            "paused",
            "accessibility_granted",
            "screen_recording_granted",
            "ocr_engine",
            "ocr_mode",
            "captures",
            "ocr_requests",
            "ocr_cache_hits",
            "ocr_cache_misses",
            "last_capture_at",
            "last_ocr_at",
            "last_ocr_seconds",
            "last_method",
            "last_error",
            "updated_at",
            "capture_mode",
            "interval_seconds",
            "input_monitoring_granted",
            "pending_groups",
            "merged_groups",
            "dropped_groups",
            "image_bytes",
            "spooled_batches",
            "missing_before",
            "capture_active",
            "screen_locked",
            "recent_ocr_seconds",
            "ocr_failures",
            "thermal_state",
            "resource_policy",
            "system_low_power",
            "desktop_pet",
        ]
        result.update({key: state[key] for key in keys if key in state})
        result["running"] = True
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return result


async def ocr_status(settings):
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            response = await client.get(f"http://127.0.0.1:{settings.ocr.port}/health")
            response.raise_for_status()
            return {"reachable": True, **response.json()}
    except (httpx.HTTPError, ValueError):
        return {"reachable": False, "ready": False, "engine": "paddleocr"}


def capture_health(collector, receipts, clock=None):
    """Only classify what heartbeat/receipt metadata actually establishes."""
    clock = clock or datetime.now(UTC)

    def recent(value):
        try:
            return 0 <= (clock - datetime.fromisoformat(value)).total_seconds() < max(120, collector.get("interval_seconds", 5) * 6)
        except (TypeError, ValueError):
            return False

    if not collector.get("running"):
        state, label, detail = "offline", "采集端尚未连接", "没有当前进程的新鲜心跳。"
    elif collector.get("paused"):
        state, label, detail = "paused", "采集已暂停", "保留你的暂停选择，已有文字仍可上传。"
    elif collector.get("screen_locked"):
        state, label, detail = "locked", "锁屏期间暂停采集", "恢复后只采集新画面。"
    elif (collector.get("accessibility_granted") is False or
          collector.get("ocr_mode") != "off" and collector.get("screen_recording_granted") is False):
        state, label, detail = "permission_required", "当前应用的采集权限未生效", "设置中的旧开关可能对应旧版本，请重新添加当前应用后核验。"
    elif collector.get("capture_mode") == "interaction" and collector.get("input_monitoring_granted") is False:
        state, label, detail = "permission_required", "操作采集权限未生效", "当前操作触发模式需要输入监控权限。"
    elif collector.get("thermal_state") == "critical":
        state, label, detail = "thermal", "设备温度过高，采集暂挂", "温度恢复后继续采集新画面。"
    elif collector.get("queue_full"):
        state, label, detail = "error", "文字队列已满，采集暂挂", "未确认文字仍保留，上传确认后自动恢复，不改变你的采集选择。"
    elif collector.get("spooled_batches", 0) and collector.get("upload_failure_since") and not recent(collector["upload_failure_since"]):
        state, label, detail = "error", "文字上传持续失败", "本地队列保留未确认文字，请检查后端服务。"
    elif collector.get("skip_reason") in {"excluded_app", "excluded_title", "secure_input", "no_window"}:
        labels = {"excluded_app": "当前应用不采集", "excluded_title": "当前窗口不采集", "secure_input": "安全输入期间不采集", "no_window": "等待可采集窗口"}
        state, label, detail = "excluded", labels[collector["skip_reason"]], "这是正常跳过，不代表采集故障。"
    elif collector.get("last_error"):
        state, label, detail = "error", "采集链路报告异常", "请在偏好与状态中检查采集、OCR及上传状态。"
    elif collector.get("telemetry_version") == 2 and collector.get("capture_active") and not recent(collector.get("last_progress_at")):
        state, label, detail = "stalled", "采集暂未取得进展", "当前窗口可采集，但链路长时间没有进展，请检查截图和OCR状态。"
    elif collector.get("spooled_batches", 0):
        state, label, detail = "upload_pending", "有文字等待上传确认", "本地队列保留未确认记录，尚不能将它们视为已入库。"
    elif collector.get("telemetry_version") == 2 and collector.get("skip_reason") == "empty_text":
        state, label, detail = "empty", "画面已识别，暂无文字", "空画面不创建工作记录。"
    elif recent(collector.get("last_screenshot_at") or collector.get("last_capture_at")):
        if recent(receipts.get("received_at")) and receipts.get("last_batch", {}).get("unchanged", 0):
            state, label, detail = "unchanged", "采集有活动，重复文字已过滤", "页面没有重要变化时不会重复创建记忆。"
        else:
            state, label, detail = "capturing", "最近有截图活动", "OCR、上报和入库分别按下方时间核对。"
    else:
        state, label, detail = "unconfirmed", "暂未确认新的采集", "可能没有可采集窗口；仅凭没有新入库记录不能判定故障。"
    return {"state": state, "label": label, "detail": detail}
