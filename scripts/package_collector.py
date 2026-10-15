"""Validate a staged signed application before replacing the installed bundle."""

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from workmemory.config import ROOT


def signature(app):
    result = subprocess.run(
        ["codesign", "-dv", "--verbose=4", str(app)], capture_output=True, text=True
    )
    return "\n".join(
        line
        for line in result.stderr.splitlines()
        if line.startswith(
            ("Identifier=", "CDHash=", "Authority=", "TeamIdentifier=", "Signature=")
        )
    )


def package():
    settings = ROOT / "config/signing.local.json"
    identity = json.loads(settings.read_text()).get("identity", "-") if settings.exists() else "-"
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("签名身份不能为空")
    installed = ROOT / "macos/WorkMemory.app"
    previous = ROOT / ".run/previous-WorkMemory.app"
    for path in (ROOT / "macos", ROOT / ".run", installed, previous):
        if path.is_symlink() or path.resolve().is_relative_to(ROOT) is False:
            raise ValueError("原生应用路径不属于本项目")
    capture_choice_path = ROOT / "data/collector/state.json"
    capture_choice = (
        json.loads(capture_choice_path.read_text()) if capture_choice_path.exists() else None
    )
    before = signature(installed) if installed.exists() else None

    def digest(app):
        return (
            hashlib.sha256((app / "Contents/MacOS/WorkMemory").read_bytes()).hexdigest()
            if app.exists()
            else None
        )

    before_hash = digest(installed)
    with tempfile.TemporaryDirectory(prefix=".package-", dir=ROOT / "macos") as directory:
        staged = Path(directory) / "WorkMemory.app"
        (staged / "Contents/MacOS").mkdir(parents=True)
        shutil.copy2(ROOT / "macos/.build/release/WorkMemory", staged / "Contents/MacOS/WorkMemory")
        config = {
            "CFBundleName": "拾忆采集",
            "CFBundleExecutable": "WorkMemory",
            "CFBundleIdentifier": "local.workmemory.collector",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "0.1.0",
            "LSUIElement": True,
            "LSMinimumSystemVersion": "14.0",
            "NSHighResolutionCapable": True,
            "NSScreenCaptureUsageDescription": "临时读取当前窗口并在内存中识别文本，不保存截图。",
            "WorkMemoryConfigPath": str(ROOT / "config/collector.json"),
        }
        (staged / "Contents/Info.plist").write_bytes(plistlib.dumps(config))
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", identity, str(staged)], check=True
        )
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(staged)], check=True)
        record = {
            "capture_choice": capture_choice,
            "created_at": datetime.now(UTC).isoformat(),
            "before_signature": before,
            "before_sha256": before_hash,
            "after_signature": signature(staged),
            "after_sha256": digest(staged),
        }
        print("原生应用已完成暂存验证；替换后 macOS 可能要求重新授权，将保留原有采集选择。")
        from manager import stop_collector

        stop_collector()
        previous.parent.mkdir(exist_ok=True)
        if previous.exists():
            shutil.rmtree(previous)
        if installed.exists():
            os.replace(installed, previous)
        try:
            os.replace(staged, installed)
        except BaseException:
            if previous.exists() and not installed.exists():
                os.replace(previous, installed)
            raise
        target = ROOT / ".run/native-update.json"
        target.write_text(json.dumps(record, ensure_ascii=False))
        target.chmod(0o600)


if __name__ == "__main__":
    package()
