"""Dense synthetic window benchmark; images stay in memory and contain no personal data."""

import io
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]


def main():
    config = json.loads((ROOT / "config/collector.json").read_text())
    report = {
        "at": datetime.now(UTC).isoformat(),
        "input": "synthetic dense windows, not real desktop captures",
        "samples": [],
    }
    font = ImageFont.truetype("/System/Library/Fonts/STHeiti Light.ttc", 26)
    layouts = [("web", 42, 34), ("chat", 22, 58), ("editor", 50, 29), ("document", 44, 34)]
    with httpx.Client(
        trust_env=False, timeout=120, headers={"Authorization": "Bearer " + config["token"]}
    ) as client:
        report["service"] = client.get(config["ocrEndpoint"] + "/health").json()
        for name, rows, spacing in layouts:
            image = Image.new("RGB", (2200, 1600), "#fdfdfd")
            draw = ImageDraw.Draw(image)
            for index in range(rows):
                line = f"第{index + 1}条 蓝鲸项目：上线时间改为周一，预算1000元，状态已完成。等待接口验收与最终确认。"
                if name == "editor":
                    line = f"{index + 1:03d}  const project = '蓝鲸'; // 周一验收，预算1000元，状态已完成。"
                draw.text((35, 25 + index * spacing), line, fill="#17252a", font=font)
            png = io.BytesIO()
            image.save(png, format="PNG")
            latencies = []
            for _ in range(3):
                started = time.monotonic()
                response = client.post(
                    config["ocrEndpoint"] + "/ocr",
                    headers={"Content-Type": "image/png"},
                    content=png.getvalue(),
                )
                response.raise_for_status()
                result = response.json()
                latencies.append(round(time.monotonic() - started, 3))
            report["samples"].append(
                {
                    "layout": name,
                    "input_lines": rows,
                    "recognized_lines": result["line_count"],
                    "seconds": latencies,
                    "critical_words_found": {
                        word: word in result["text"] for word in ["周一", "1000", "已完成", "验收"]
                    },
                }
            )
            print(name, latencies, flush=True)
    output = ROOT / "data/exports/ocr-dense-benchmark.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(output)


if __name__ == "__main__":
    main()
