"""Real PaddleOCR checks using synthetic pixels only, followed by text-only ingestion."""

import io
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]


def main():
    config = json.loads((ROOT / "config/collector.json").read_text())
    headers = {"Authorization": "Bearer " + config["token"]}
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "input": "synthetic in-memory images",
        "desktop_capture_verified": False,
        "samples": [],
    }
    episode_id = None
    with httpx.Client(headers=headers, trust_env=False, timeout=90) as client:
        health = client.get(config["ocrEndpoint"] + "/health")
        health.raise_for_status()
        report["service"] = health.json()
        try:
            for size in [24, 36]:
                image = Image.new("RGB", (1100, 220), "white")
                draw = ImageDraw.Draw(image)
                font = ImageFont.truetype("/System/Library/Fonts/STHeiti Light.ttc", size)
                draw.multiline_text(
                    (25, 25),
                    "工作记忆助手 PaddleOCR 2026\n星舟项目上线时间改为周一，等待验收。",
                    font=font,
                    fill="black",
                    spacing=18,
                )
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                started = time.monotonic()
                response = client.post(
                    config["ocrEndpoint"] + "/ocr",
                    content=buffer.getvalue(),
                    headers={"Content-Type": "image/png"},
                )
                response.raise_for_status()
                result = response.json()
                assert all(word in result["text"] for word in ["周一", "验收", "PaddleOCR"]), (
                    "Chinese/English OCR mismatch"
                )
                report["samples"].append(
                    {
                        "font_size": size,
                        "seconds": round(time.monotonic() - started, 3),
                        "line_count": result["line_count"],
                        "passed": True,
                    }
                )
            # Closed five-minute bucket, unique ID: exercise the real text ingestion contract.
            event_id = str(uuid.uuid4())
            device_id = "ocr-verification-" + uuid.uuid4().hex[:12]
            captured_at = datetime.now(UTC) - timedelta(minutes=6)
            key = f"{device_id}|{datetime.fromtimestamp(int(captured_at.timestamp()) // 300 * 300, UTC).isoformat()}|"
            episode_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
            event = {
                "id": event_id,
                "device_id": device_id,
                "captured_at": captured_at.isoformat(),
                "app": "ocr.synthetic",
                "title": "合成 OCR 验收",
                "text": result["text"],
                "method": "paddleocr",
            }
            for _ in range(2):
                response = client.post(config["endpoint"] + "/api/ingest", json={"events": [event]})
                response.raise_for_status()
                assert event_id in response.json()["accepted"] + response.json()["skipped"]
            sources = client.get(config["endpoint"] + f"/api/episodes/{episode_id}/sources")
            sources.raise_for_status()
            activities = sources.json()["activities"]
            assert len(activities) == 1 and activities[0]["method"] == "paddleocr"
            assert "周一" in activities[0]["text"]
            report["text_ingestion_and_idempotency"] = "passed"
            report["passed"] = True
        finally:
            if episode_id:
                deleted = client.delete(config["endpoint"] + f"/api/episodes/{episode_id}")
                deleted.raise_for_status()
            (ROOT / "data/exports/ocr-verification.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
