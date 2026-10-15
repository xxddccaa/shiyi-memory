"""Run the isolated synthetic suite; --live additionally sends only fixture data to the saved model."""

import argparse
import os
import subprocess

from workmemory.config import ROOT

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="拾忆隔离验收：10k片段、60查询、50关键变化")
    parser.add_argument(
        "--live", action="store_true", help="额外测试实际Pi及已保存模型；仅发送合成数据"
    )
    parser.add_argument("--answers", type=int, default=60, choices=range(1, 61), metavar="1-60")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="仅复测指定问答 ID，可重复；例如 unknown-5，需要 --live",
    )
    args = parser.parse_args()
    if args.case and not args.live:
        parser.error("--case requires --live")
    env = {**os.environ, "WORKMEMORY_BENCHMARK": "1"}
    if args.live:
        env.update(WORKMEMORY_BENCHMARK_LIVE="1", WORKMEMORY_BENCHMARK_ANSWERS=str(args.answers))
        if args.case:
            env["WORKMEMORY_BENCHMARK_CASES"] = ",".join(args.case)
    result = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "tests/integration/test_companion_benchmark.py",
        ],
        cwd=ROOT,
        env=env,
    )
    print("合成报告：.cache/companion-benchmark.json；独立临时数据库已由测试夹具清理。")
    raise SystemExit(result.returncode)
