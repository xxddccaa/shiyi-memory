"""One-time upgrade of old defaults; explicit AX mode and custom settings survive."""

import json

import yaml
from local_config import main as local_config
from workmemory.config import ROOT, get_settings

local_config()
marker = ROOT / ".run/interaction-config-v1.json"
if not marker.exists():
    path = ROOT / "config/app.local.yaml"
    data = yaml.safe_load(path.read_text())
    collector = data.setdefault("collector", {})
    if "capture_mode" not in collector:
        collector["capture_mode"] = "timer"
        collector["interval_seconds"] = 5
        collector["ocr_min_interval"] = 5
        if collector.get("ocr_mode", "fallback") == "fallback":
            collector["ocr_mode"] = "always"
        ocr = data.setdefault("ocr", {})
        for role in ["det", "rec"]:
            key = "detection_model" if role == "det" else "recognition_model"
            if ocr.get(key, f"PP-OCRv6_small_{role}") == f"PP-OCRv6_small_{role}":
                ocr[key] = f"PP-OCRv6_tiny_{role}"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    state = ROOT / "data/collector/state.json"
    # An existing explicit start counts as consent; never infer consent on a fresh install.
    heartbeat = ROOT / ".run/collector-status.json"
    if not state.exists() and heartbeat.exists():
        old = json.loads(heartbeat.read_text())
        if old.get("paused") is False or old.get("captures", 0) > 0:
            state.write_text(json.dumps({"consent": True, "running": not old.get("paused", True)}))
            state.chmod(0o600)
    marker.write_text(json.dumps({"version": 1}))
    get_settings.cache_clear()
    local_config()
print("采集配置已更新；用户暂停状态将被保留。")
