"""Content-free sampling of collector latency, bounded queues and native process RSS."""

import argparse
import json
import statistics
import time
from datetime import datetime, timezone

from manager import ROOT, capture, read_json, save_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    samples = []
    start = time.monotonic()
    output = ROOT / "data/exports/collector-monitor.json"
    while time.monotonic() - start < args.seconds:
        state = read_json(ROOT / ".run/collector-status.json")
        sample = {
            k: state.get(k)
            for k in [
                "captures",
                "ocr_requests",
                "pending_groups",
                "spooled_batches",
                "merged_groups",
                "dropped_groups",
                "missing_before",
                "image_bytes",
                "capture_active",
                "last_error",
            ]
        }
        sample["at"] = datetime.now(timezone.utc).isoformat()
        sample["processes"] = {}
        for name in ["collector", "ocr", "backend", "ollama"]:
            pid = (
                state.get("pid")
                if name == "collector"
                else read_json(ROOT / f".run/{name}.json").get("pid")
            )
            if isinstance(pid, int) and pid > 0:
                sample["processes"][name] = capture(["ps", "-p", str(pid), "-o", "rss=,pcpu="])
        samples.append(sample)
        latencies = state.get("recent_ocr_seconds", [])
        report = {
            "elapsed_seconds": round(time.monotonic() - start, 1),
            "samples": samples,
            "ocr_median_seconds": statistics.median(latencies) if latencies else None,
            "ocr_max_seconds": max(latencies) if latencies else None,
            "capture_verified": any(s.get("capture_active") for s in samples)
            and any(s.get("captures", 0) for s in samples),
        }
        save_json(output, report)
        time.sleep(min(args.interval, max(0, args.seconds - (time.monotonic() - start))))
    print(
        json.dumps(
            {
                "report": str(output),
                "samples": len(samples),
                "capture_verified": report["capture_verified"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
