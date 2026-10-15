"""Synthetic daily-task acceptance using actual Pi, model, memory and authenticated APIs."""

import argparse
import asyncio
import hashlib
import json
import logging
import signal
import subprocess
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from workmemory.config import ROOT, get_settings
from workmemory.config.generation import GenerationStore
from workmemory.evaluation.environment import Isolation, fingerprint
from workmemory.evaluation.report import atomic_json, exit_code, save
from workmemory.evaluation.runner import JUDGE_PROMPT, PROMPT_VERSION, USER_PROMPT, CaseRunner
from workmemory.evaluation.scenarios import library


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("必须为正整数")
    return result


def file_hashes():
    paths = ["config/generation.local.json", "config/app.local.yaml", "config/mcp.local.json",
             "config/collector.json", "macos/WorkMemory.app/Contents/MacOS/WorkMemory"]
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() if (ROOT / p).is_file() else None for p in paths}


def source_fingerprint():
    paths = []
    for folder in ("backend", "agent", "frontend/src", "config/prompts"):
        paths.extend(p for p in (ROOT / folder).rglob("*") if p.is_file() and p.suffix in {".py", ".mjs", ".ts", ".tsx", ".json", ".txt"}
                     and "node_modules" not in p.parts and "__pycache__" not in p.parts)
    return fingerprint({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)})


async def run(args, cases):
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    settings = get_settings()
    store = GenerationStore(settings.ollama)
    config = store.current
    role_model = args.role_model or (config.model if "27b" in config.model.casefold() else "Qwen3.8-27B")
    stamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d-%H%M%S")
    directory = ROOT / "data/exports/dialogue-evals" / (stamp + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    baseline_path = ((ROOT / args.baseline).resolve() if not args.baseline.is_absolute() else args.baseline) if args.baseline else None
    baseline = json.loads(baseline_path.read_text()) if baseline_path else None
    variants = [args.variant] if args.variant else [1] if args.suite == "smoke" and not args.case else [1, 2, 3]
    planned = [(case, variant, repeat) for repeat in range(1, args.repeat + 1) for case in cases for variant in variants]
    before = file_hashes()
    code = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    public = config.public()
    public.pop("base_url", None)
    report = {"format_version": 1, "status": "running", "planned": len(planned), "cases": [], "error": None,
        "coverage": [{"id": c.id, "group": c.group, "variants": 3} for c in library()],
        "meta": {"started_at": datetime.now(ZoneInfo(settings.timezone)).isoformat(), "commit": code.stdout.strip() if code.returncode == 0 else None,
                 "source_fingerprint": source_fingerprint(),
                 "harness_fingerprint": fingerprint({str(p.relative_to(ROOT)): p.read_text() for p in sorted((ROOT / "backend/workmemory/evaluation").glob("*.py"))}),
                 "selection": {"cases": [c.id for c in cases], "variants": variants, "repeat": args.repeat},
                 "library_fingerprint": fingerprint([c.model_dump() for c in library()]),
                 "prompt_version": PROMPT_VERSION, "prompt_fingerprint": fingerprint([USER_PROMPT, JUDGE_PROMPT]),
                 "model": config.model, "role_model": role_model, "model_config": public,
                 "model_fingerprint": fingerprint(config.model_dump(exclude={"api_key"})),
                 "timezone": settings.timezone, "suite": args.suite, "max_turns": args.max_turns, "timeout": args.timeout,
                 "fixture_mode": "synthetic PostgreSQL + actual ReMe + actual Pi + actual saved provider"}}
    save(directory, report, baseline)
    print(f"报告目录：{directory.relative_to(ROOT)}", flush=True)
    print(f"准备运行 {len(planned)} 条；被测模型：{config.model}；模拟／评审：{role_model}", flush=True)
    isolation = Isolation(settings)
    try:
        async with isolation:
            report["isolation"] = {"database": isolation.name, "synthetic_only": True, "cleanup": "pending"}
            for index, (scenario, variant, repeat) in enumerate(planned, 1):
                if file_hashes() != before:
                    report["error"] = "protected_configuration_changed_during_run"
                    break
                print(f"[{index}/{len(planned)}] {scenario.id} / 话语{variant} / 第{repeat}次", flush=True)
                async with isolation.case(store) as (rt, client):
                    partial = directory / f"{scenario.id}-v{variant}-r{repeat}.json"
                    runner = CaseRunner(rt, client, scenario, variant, repeat, args.max_turns, args.timeout,
                                        checkpoint=lambda value: atomic_json(partial, value), role_model=role_model)
                    try:
                        result = await runner.execute()
                    except asyncio.CancelledError:
                        report["cases"].append(runner.report)
                        raise
                    report["cases"].append(result)
                print(f"  {result['status']} · {result['seconds']}秒 · {result.get('error') or result.get('behavior_error') or ''}", flush=True)
                save(directory, report, baseline)
    except asyncio.CancelledError:
        report["error"] = "interrupted"
    except Exception as exc:
        report["error"] = "environment_" + type(exc).__name__
    finally:
        report.setdefault("isolation", {})["cleanup"] = "complete" if not isolation.created else "failed"
        report["isolation"]["protected_files_unchanged"] = file_hashes() == before
        if not report["isolation"]["protected_files_unchanged"]:
            report["error"] = "protected_configuration_changed_during_run"
        report["status"] = "complete" if len(report["cases"]) == report["planned"] and not report["error"] else "incomplete"
        report["meta"]["finished_at"] = datetime.now(ZoneInfo(settings.timezone)).isoformat()
        save(directory, report, baseline)
    print(f"结果：{report['counts']}；退出码 {exit_code(report)}", flush=True)
    print(f"回放：{directory / 'report.html'}", flush=True)
    return exit_code(report)


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser(description="拾忆日常对话回归：真实Pi、独立PostgreSQL/ReMe、27B评审，只使用合成数据")
    parser.add_argument("--suite", choices=["smoke", "full"], default="smoke")
    parser.add_argument("--case", action="append", default=[], help="指定任务ID，可重复；默认运行该任务的三种话语")
    parser.add_argument("--variant", type=int, choices=[1, 2, 3], help="只运行指定话语")
    parser.add_argument("--role-model", help="模拟用户与评审模型ID，默认已保存的27B或Qwen3.8-27B；沿用已选服务，不改被测Pi配置")
    parser.add_argument("--repeat", type=positive, default=1)
    parser.add_argument("--max-turns", type=positive, default=32)
    parser.add_argument("--timeout", type=positive, default=1200, help="单用例秒数，不改变产品额度")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--list", action="store_true", help="列出覆盖矩阵，不连接模型或数据库")
    args = parser.parse_args()
    all_cases = library()
    unknown = set(args.case) - {c.id for c in all_cases}
    if unknown:
        parser.error("未知用例：" + ", ".join(sorted(unknown)))
    cases = [c for c in all_cases if c.id in args.case] if args.case else [c for c in all_cases if args.suite == "full" or c.smoke]
    if args.list:
        for case in cases:
            print(f"{case.id:30} {case.group:8} 3种话语 · {len(case.stages)}阶段")
        print(f"任务 {len(cases)}；全部库 {len(all_cases)}任务 / {len(all_cases)*3}种话语；核心12条")
        return 0
    # Third-party logs can include prompts/URLs. Structured reports contain only synthetic evidence.
    logging.getLogger().setLevel(logging.CRITICAL)
    for name in ("reme", "reme_ai", "alembic", "httpx", "httpcore", "mcp", "sqlalchemy"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        return asyncio.run(run(args, cases))
    except KeyboardInterrupt:
        return 2
    except Exception as exc:
        print("无法启动评测：" + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
