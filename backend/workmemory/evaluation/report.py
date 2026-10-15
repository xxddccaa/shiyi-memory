"""Portable, escaped HTML replay and machine-readable incremental reports."""

import html
import json
import os
from collections import Counter
from pathlib import Path


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    os.replace(temp, path)


def comparison(report, baseline):
    if not baseline:
        return None
    same = all(report["meta"].get(k) == baseline.get("meta", {}).get(k)
               for k in ("model_fingerprint", "role_model", "library_fingerprint", "prompt_fingerprint", "harness_fingerprint", "timezone", "max_turns", "timeout"))
    old = {(c["id"], c["variant"], c["repeat"]): c for c in baseline.get("cases", [])}
    result = {"comparable": same, "regressions": [], "fixes": [], "unmatched": []}
    for case in report["cases"]:
        key = (case["id"], case["variant"], case["repeat"])
        previous = old.pop(key, None)
        label = f"{key[0]} / {key[1]} / {key[2]}"
        if not same or previous is None:
            result["unmatched"].append(label)
        elif previous["status"] == "pass" and case["status"] != "pass":
            result["regressions"].append(label)
        elif previous["status"] != "pass" and case["status"] == "pass":
            result["fixes"].append(label)
    result["unmatched"].extend(f"{i} / {v} / {r}" for i, v, r in old)
    return result


def exit_code(report):
    if report.get("error") or len(report["cases"]) < report["planned"] or any(c["status"] == "incomplete" for c in report["cases"]):
        return 2
    return 1 if any(c["status"] == "fail" for c in report["cases"]) else 0


def render(report):
    def esc(value):
        return html.escape(str(value))
    labels = {"pass": "通过", "fail": "失败", "incomplete": "未完成", "complete": "已结束", "running": "运行中"}
    roles = {"user": "用户", "assistant": "拾忆"}
    counts = Counter(c["status"] for c in report["cases"])
    sections = []
    for case in report["cases"]:
        key = f"{case['id']} · 话语 {case['variant']} · 第 {case['repeat']} 次"
        failures = [c for c in case.get("hard_checks", []) if not c["passed"]]
        reason = case.get("error") or case.get("behavior_error") or "；".join((case.get("judgment") or {}).get("reasons", []))
        chat = []
        for item in case.get("trace", []):
            if item["type"] == "message":
                chat.append(f'<div class="bubble {esc(item["role"])}"><small>阶段 {item["stage"]} · {esc(roles.get(item["role"], item["role"]))} · {esc(item.get("status", ""))}</small><p>{esc(item.get("content") or "[内容已失效]")}</p></div>')
            elif item["type"] == "decision":
                chat.append(f'<div class="decision">操作：{esc(item["card"]["action"])} → {esc(item["result"]["status"])}</div>')
        evidence = json.dumps({"hard_failures": failures, "judge": case.get("judgment"), "time_context": case.get("time_context"), "trace": case.get("trace"),
            "initial": case.get("initial"), "final": case.get("final"), "runs": case.get("runs"), "roles": case.get("role_calls")}, ensure_ascii=False, indent=2)
        sections.append(f'<article data-status="{esc(case["status"])}"><details><summary><b class="{esc(case["status"])}">{esc(labels[case["status"]])}</b> {esc(key)} <span>{case.get("seconds", 0)} 秒</span></summary><p>{esc(case["scenario"]["openings"][case["variant"]-1])}</p><p>{esc(reason)}</p>{"".join(chat)}<details><summary>完整工具、断言和数据证据</summary><pre>{esc(evidence)}</pre></details></details></article>')
    matrix = []
    for group in sorted({s["group"] for s in report["coverage"]}):
        selected = [c for c in report["cases"] if c["group"] == group]
        c = Counter(v["status"] for v in selected)
        total = sum(s["variants"] for s in report["coverage"] if s["group"] == group)
        matrix.append(f'<tr><td>{esc(group)}</td><td>{total}</td><td>{len(selected)}</td><td>{c["pass"]}</td><td>{c["fail"]}</td><td>{c["incomplete"]}</td></tr>')
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>拾忆对话回归报告</title>
<style>body{{font:15px/1.65 system-ui,sans-serif;max-width:1100px;margin:36px auto;padding:0 20px;color:#253248;background:#f6f8fb}}h1{{font-size:26px}}article,table,.box{{background:white;border:1px solid #dde3ee;border-radius:12px;padding:16px;margin:12px 0}}summary{{cursor:pointer}}summary span{{float:right;color:#68768b}}.pass{{color:#087f5b}}.fail{{color:#c92a2a}}.incomplete{{color:#9c6500}}pre{{white-space:pre-wrap;word-break:break-word;max-height:700px;overflow:auto;font-size:12px}}.bubble{{max-width:90%;padding:12px 16px;background:#eef2f8;border-radius:12px;margin:10px 0}}.bubble.user{{margin-left:auto;background:#e7f0ff}}p{{white-space:pre-wrap}}small{{color:#68768b}}.decision{{padding:10px;background:#fff7df}}td,th{{text-align:left;padding:6px 18px}}button,input{{padding:8px 14px;margin:6px;border:1px solid #cbd5e1;border-radius:8px;background:white}}[hidden]{{display:none}}</style>
<h1>拾忆 · 日常对话回归</h1><p>真实 Pi / PostgreSQL / ReMe · 合成资料 · 独立 27B 评审</p>
<div class="box">计划 {report["planned"]} · 已运行 {len(report["cases"])} · 通过 {counts["pass"]} · 失败 {counts["fail"]} · 未完成 {counts["incomplete"]}<br>运行状态：{esc(labels[report["status"]])} · {esc(report.get("error") or "")}<br>模型：{esc(report["meta"].get("model", "未知"))} · 评审：{esc(report["meta"].get("role_model", "未知"))}</div>
<p>通过只代表已覆盖场景本次未发现回归。接口共享会话测试不等同于原生桌宠界面或真实桌面采集验收。</p>
<table><thead><tr><th>场景</th><th>库内话语</th><th>已运行</th><th>通过</th><th>失败</th><th>未完成</th></tr></thead><tbody>{"".join(matrix)}</tbody></table>
<details class="box"><summary>版本、配置与历史对比</summary><pre>{esc(json.dumps({"meta": report["meta"], "comparison": report.get("comparison"), "isolation": report.get("isolation")}, ensure_ascii=False, indent=2))}</pre></details>
<div><button onclick="filter('all')">全部</button><button onclick="filter('fail')">失败</button><button onclick="filter('incomplete')">未完成</button><input id="search" placeholder="搜索用例／话语" oninput="filter(active)"></div>
{"".join(sections)}<script>let active='all';function filter(s){{active=s;const q=document.getElementById('search').value.toLowerCase();document.querySelectorAll('article').forEach(e=>e.hidden=(s!=='all'&&e.dataset.status!==s)||!e.textContent.toLowerCase().includes(q));}}</script></html>'''


def save(directory: Path, report, baseline=None):
    report["comparison"] = comparison(report, baseline)
    report["counts"] = dict(Counter(c["status"] for c in report["cases"]))
    atomic_json(directory / "report.json", report)
    temp = directory / "report.html.tmp"
    temp.write_text(render(report))
    os.replace(temp, directory / "report.html")
