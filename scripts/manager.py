"""Chinese lifecycle manager. Status uses only the Python standard library."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.docker.compose.project=workmemory"
NAMES = {
    "postgres": "PostgreSQL",
    "neo4j": "Neo4j",
    "backend": "后端／网页",
    "ollama": "Ollama",
    "ocr": "PaddleOCR",
    "collector": "拾忆采集",
}
MESSAGES = {
    "accessibility_permission_required": "请在菜单栏授予辅助功能权限",
    "screen_recording_permission_required": "请在菜单栏授予屏幕录制权限",
    "input_monitoring_permission_required": "请在菜单栏授予输入监控权限；当前只能跟随画面和应用变化",
    "ocr_unavailable": "OCR 请求未成功，请检查 OCR 服务",
    "spool_full": "本地队列已满，恢复服务后重新开始采集",
    "screen_stream_failed": "无法读取窗口；检查屏幕录制权限或重新打开采集端",
}


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temp.chmod(0o600)
    temp.replace(path)


def capture(args, timeout=8):
    try:
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def request_json(url, token=None):
    try:
        request = urllib.request.Request(url)
        if token:
            request.add_header("Authorization", "Bearer " + token)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=3
        ) as response:
            return json.load(response)
    except Exception:
        return {}


def run(args, title=None, timeout=None):
    if title:
        print(f"\n→ {title}", flush=True)
    log_path = ROOT / "logs/manager.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as log:
        log.write(
            f"\n{datetime.now(timezone.utc).isoformat()} {title or Path(str(args[0])).name}\n"
        )
        # Inherit the terminal so OS permission prompts and model progress remain usable.
        result = subprocess.run([str(a) for a in args], cwd=ROOT, timeout=timeout)
        log.write(f"exit={result.returncode}\n")
    if result.returncode:
        raise RuntimeError(
            f"{title or '操作'}未完成（退出码 {result.returncode}），再次安装／修复会从已有成果继续。"
        )


def script(name, *args, **kwargs):
    run(["bash", ROOT / "scripts" / name, *args], **kwargs)


def python_script(name, *args, **kwargs):
    run([ROOT / ".venv/bin/python", ROOT / "scripts" / name, *args], **kwargs)


def compose_args(*args):
    return [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "--env-file",
        str(ROOT / "config/secrets.env"),
        "--env-file",
        str(ROOT / "config/runtime.env"),
        "-f",
        str(ROOT / "infra/compose.yaml"),
        *args,
    ]


def containers():
    ids = capture(["docker", "ps", "-aq", "--filter", "label=" + LABEL])
    if not ids:
        return []
    return json.loads(capture(["docker", "inspect", *ids.split()]) or "[]")


def owns_container(item):
    mounts = item.get("Mounts", [])
    return bool(mounts) and all(
        m.get("Type") == "bind" and inside(Path(m.get("Source", "/"))) for m in mounts
    )


def inside(path):
    try:
        path.resolve().relative_to(ROOT.resolve())
        return path.resolve() != ROOT.resolve()
    except ValueError:
        return False


def assert_container_ownership():
    for item in containers():
        if not owns_container(item):
            raise RuntimeError(
                "检测到其他目录的同名 Docker 项目，已停止操作。请使用不同 Compose 项目名或切回原项目。"
            )


def collector_pids():
    executable = str(ROOT / "macos/WorkMemory.app/Contents/MacOS/WorkMemory")
    result = []
    for line in capture(["ps", "-axo", "pid=,command="]).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and (parts[1] == executable or parts[1].startswith(executable + " ")):
            result.append(int(parts[0]))
    return result


def stop_collector():
    for pid in collector_pids():
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if pid not in collector_pids():
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("采集端仍在退出，暂未继续停止或卸载。")


def owns_process(name):
    state = read_json(ROOT / f".run/{name}.json")
    pid = state.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    command = capture(["ps", "-p", str(pid), "-o", "command="])
    expected = state.get("command", [])
    return bool(expected) and str(ROOT) in command and all(str(arg) in command for arg in expected)


def config_snapshot():
    return read_json(ROOT / ".run/service-config.json") or {
        "server": {"port": 8765},
        "ocr": {"port": 8766},
        "memory": {"backend": "reme"},
        "ollama": {
            "url": "http://127.0.0.1:11434",
            "generation_model": "qwen3:8b",
            "embedding_model": "qwen3-embedding:0.6b",
        },
    }


def generation_selection(config):
    """Read only public fields; works before Python dependencies are installed."""
    path = ROOT / "config/generation.local.json"
    if path.exists():
        selected = read_json(path).get("generation", {})
        # Invalid saved files must not silently select/download a different model.
        return {key: selected.get(key) for key in ["provider", "base_url", "model"]}
    return {
        "provider": "ollama",
        "base_url": config["ollama"]["url"],
        "model": config["ollama"]["generation_model"],
    }


def required_local_models(config):
    selected = generation_selection(config)
    names = [config["ollama"]["embedding_model"]]
    if selected["provider"] == "ollama" and selected["model"]:
        names.insert(0, selected["model"])
    return list(dict.fromkeys(names))


def native_status():
    """Status stays usable before dependencies have been installed."""
    value = {"installed": (ROOT / "data/postgres-native/PG_VERSION").exists(),
             "running": False, "ready": False, "mode": "native"}
    if (ROOT / ".venv/bin/python").exists():
        output = capture([str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/native_database.py"), "status"])
        try:
            value.update(json.loads(output))
        except (ValueError, TypeError):
            value["detail"] = "数据库路径、归属或原生程序待检查"
    return value


def status():
    config = config_snapshot()
    ocr_port = config["ocr"].get("port", 8766)
    api_port = config["server"].get("port", 8765)
    ollama_url = config["ollama"].get("url", "http://127.0.0.1:11434")
    secret = ""
    try:
        for line in (ROOT / "config/secrets.env").read_text().splitlines():
            if line.startswith("API_TOKEN="):
                secret = line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    with ThreadPoolExecutor(max_workers=5) as pool:
        queries = {
            "backend": pool.submit(request_json, f"http://127.0.0.1:{api_port}/health"),
            "ocr": pool.submit(request_json, f"http://127.0.0.1:{ocr_port}/health"),
            "ollama": pool.submit(request_json, ollama_url + "/api/tags"),
            "api": pool.submit(request_json, f"http://127.0.0.1:{api_port}/api/status", secret),
            "containers": pool.submit(containers),
            "native": pool.submit(native_status),
        }
        results = {key: task.result() for key, task in queries.items()}
    selected = results["api"].get("backend", config["memory"].get("backend", "reme"))
    generation = results["api"].get("ollama", {}).get("generation") or {
        **generation_selection(config),
        "ready": False,
        "detail": "后端未提供生成服务状态",
    }
    modules = []
    native = results["native"]
    modules.append({"module": "postgres", "label": "PostgreSQL（原生）", "enabled": True,
                    **native, "state": "已就绪" if native["ready"] else
                    "进程存在，未就绪" if native["running"] else
                    "未运行" if native["installed"] else
                    "待迁移旧 Docker 数据" if (ROOT / "data/postgres/PG_VERSION").exists() else "未安装"})
    for name in ["neo4j"]:
        found = next(
            (
                c
                for c in results["containers"]
                if c.get("Config", {}).get("Labels", {}).get("com.docker.compose.service") == name
                and owns_container(c)
            ),
            None,
        )
        enabled = name == "postgres" or selected == "graphiti"
        running = bool(found and found["State"].get("Running"))
        ready = bool(running and found["State"].get("Health", {}).get("Status") == "healthy")
        modules.append(
            {
                "module": name,
                "label": NAMES[name],
                "enabled": enabled,
                "installed": found is not None,
                "running": running,
                "ready": ready,
                "state": ("运行中（未选用）" if running else "未启用")
                if not enabled
                else "已就绪"
                if ready
                else "启动中"
                if running
                else "未运行",
            }
        )
    for name, binary in [
        ("backend", ".venv/bin/python"),
        ("ocr", "ocr/.venv/bin/python"),
        ("ollama", ".cache/ollama/ollama"),
    ]:
        running = owns_process(name)
        reachable = bool(results[name])
        ready = running and reachable
        module = {
            "module": name,
            "label": NAMES[name],
            "enabled": True,
            "installed": (ROOT / binary).exists(),
            "running": running,
            "ready": ready,
            "state": "已就绪" if ready else "进程存在，未就绪" if running else "未运行",
        }
        if name == "ollama":
            models = {m["name"] for m in results[name].get("models", [])}
            module["models"] = [
                {"name": name, "ready": name in models} for name in required_local_models(config)
            ]
            module["ready"] = ready and all(m["ready"] for m in module["models"])
            if ready and not module["ready"]:
                module["state"] = "服务运行，模型缺失"
        if name == "ocr":
            module["model"] = results[name].get("detection_model")
            module["last_seconds"] = results[name].get("last_seconds")
        modules.append(module)
    collector = read_json(ROOT / ".run/collector-status.json")
    running = collector.get("pid") in collector_pids()
    if running:
        try:
            age = (
                time.time()
                - datetime.fromisoformat(collector["updated_at"].replace("Z", "+00:00")).timestamp()
            )
            running = 0 <= age < 120
        except (KeyError, ValueError):
            running = False
    collector_ready = (
        running
        and collector.get("accessibility_granted", False)
        and (collector.get("ocr_mode") == "off" or collector.get("screen_recording_granted", False))
        and (
            collector.get("capture_mode") != "interaction"
            or collector.get("input_monitoring_granted", False)
        )
    )
    collector_state = (
        "未运行"
        if not running
        else "用户暂停"
        if collector.get("paused")
        else "正在采集"
        if collector.get("capture_active", True)
        else "等待权限或可采集窗口"
    )
    modules.append(
        {
            "module": "collector",
            "label": NAMES["collector"],
            "installed": (ROOT / "macos/WorkMemory.app").exists(),
            "enabled": True,
            "running": running,
            "ready": bool(collector_ready),
            "state": collector_state if collector_ready or not running else "缺少采集权限",
        }
    )
    details = (
        {
            key: collector.get(key)
            for key in [
                "paused",
                "capture_active",
                "screen_locked",
                "capture_mode",
                "interval_seconds",
                "accessibility_granted",
                "screen_recording_granted",
                "input_monitoring_granted",
                "last_capture_at",
                "last_ocr_at",
                "last_ocr_seconds",
                "pending_groups",
                "merged_groups",
                "dropped_groups",
                "missing_before",
                "image_bytes",
                "recent_ocr_seconds",
            ]
        }
        if running
        else {}
    )
    details["spooled_batches"] = len(list((ROOT / "data/collector/spool").glob("*.json")))
    error = collector.get("last_error") if running else None
    details["advice"] = MESSAGES.get(error, error)
    return {
        "root": str(ROOT),
        "installed": (ROOT / ".venv/bin/python").exists(),
        "ready": bool(generation["ready"] and all(m["ready"] for m in modules if m["enabled"])),
        "generation": generation,
        "memory_backend": selected,
        "modules": modules,
        "collector": details,
        "jobs": results["api"].get("jobs", {}),
        "capture_statistics": results["api"].get("capture_statistics", {}),
        "disk_free_gb": round(shutil.disk_usage(ROOT).free / 1024**3, 1),
    }


def show_status(value, as_json=False):
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return
    print("\n拾忆工作状态")
    for module in value["modules"]:
        print(f"  {module['label']:<12} {module['state']}")
        for model in module.get("models", []):
            print(f"    {model['name']}：{'已就绪' if model['ready'] else '未下载'}")
    print(f"  当前记忆后端：{value['memory_backend']}　可用磁盘：{value['disk_free_gb']} GB")
    generation = value.get("generation", {})
    print(
        f"  整理与问答：{generation.get('model') or '未配置'}（{'已就绪' if generation.get('ready') else '待检查'}）"
    )
    if generation.get("base_url"):
        print(f"    {generation['base_url']}")
    collector = value["collector"]
    if collector.get("capture_mode") == "timer":
        print(
            f"  采集方式：每 {collector.get('interval_seconds') or 5:g} 秒一次，重复内容不新增整理任务"
        )
    print(
        f"  待上传：{collector.get('spooled_batches', 0)} 批；待识别：{collector.get('pending_groups') or 0} 组"
    )
    print(f"  摘要／同步任务：{value['jobs'] or '后端离线，无法查询'}")
    if collector.get("last_capture_at"):
        print(
            f"  最近采集：{collector['last_capture_at']}；OCR：{collector.get('last_ocr_seconds')} 秒"
        )
    if collector.get("advice"):
        print("  处理建议：" + collector["advice"])
    if not value["installed"]:
        print("  下一步：选择安装／修复。")
    elif any(
        m["enabled"] and not m["ready"] and m["module"] != "collector" for m in value["modules"]
    ):
        print("  下一步：选择启动全部；仍未就绪时选择诊断。")


@contextlib.contextmanager
def operation_lock():
    (ROOT / ".run").mkdir(exist_ok=True)
    with (ROOT / ".run/manage.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("另一个管理操作正在执行，请等待它结束。") from None
        (ROOT / ".run/manager.pid").write_text(str(os.getpid()))
        save_json(ROOT / ".run/manager-owner.json", {"pid": os.getpid(), "root": str(ROOT)})
        try:
            bootstrap = ROOT / ".run/bootstrap.lock"
            if bootstrap.exists():
                try:
                    pid = int((bootstrap / "pid").read_text())
                    if pid > 0:
                        os.kill(pid, 0)
                        raise RuntimeError("基础依赖安装正在执行，请等待它结束。")
                except ProcessLookupError:
                    pass
                except (ValueError, FileNotFoundError):
                    raise RuntimeError("基础依赖安装锁正在创建，请稍后重试。") from None
            yield
        finally:
            (ROOT / ".run/manager.pid").unlink(missing_ok=True)
            (ROOT / ".run/manager-owner.json").unlink(missing_ok=True)
            fcntl.flock(lock, fcntl.LOCK_UN)


def stage(name, action):
    state_path = ROOT / ".run/install-state.json"
    state = read_json(state_path)
    state.update(stage=name, completed=False, updated_at=datetime.now(timezone.utc).isoformat())
    save_json(state_path, state)
    action()
    state.update(completed=True)
    save_json(state_path, state)


def fingerprint(paths):
    digest = hashlib.sha256(str(ROOT).encode())
    for path in sorted(paths):
        if path.is_file():
            digest.update(str(path.relative_to(ROOT)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def build_if_needed():
    record = read_json(ROOT / ".run/build-state.json")
    swift_hash = fingerprint(
        list((ROOT / "macos/Sources").rglob("*.swift"))
        + [ROOT / "macos/Package.swift", ROOT / "scripts/collector-build.sh", ROOT / "scripts/package_collector.py", ROOT / "config/signing.local.json"]
    )
    web_hash = fingerprint(
        list((ROOT / "frontend/src").rglob("*"))
        + [
            ROOT / "frontend/package-lock.json",
            ROOT / "frontend/package.json",
            ROOT / "frontend/index.html",
            ROOT / "frontend/vite.config.ts",
        ]
    )
    agent_hash = fingerprint([ROOT / "agent/package.json", ROOT / "agent/package-lock.json", ROOT / "agent/worker.mjs", ROOT / "agent/session.mjs"])
    if record.get("agent") != agent_hash or not (ROOT / "agent/node_modules/@earendil-works/pi-coding-agent").exists():
        script("agent-npm.sh", "ci", "--ignore-scripts", title="安装内置 Agent")
        script("agent-npm.sh", "run", "check", title="检查内置 Agent")
        record["agent"] = agent_hash
    if record.get("web") != web_hash or not (ROOT / "frontend/dist/index.html").exists():
        script("npm.sh", "ci", title="安装前端依赖")
        script("npm.sh", "run", "build", title="构建网页")
        record["web"] = web_hash
    if record.get("swift") != swift_hash or not (ROOT / "macos/WorkMemory.app").exists():
        script("collector-build.sh", title="构建采集端（升级后 macOS 可能要求重新授权）")
        record["swift"] = swift_hash
    save_json(ROOT / ".run/build-state.json", record)


def require_installed():
    for path in [
        ".venv/bin/python",
        "ocr/.venv/bin/python",
        "frontend/dist/index.html",
        "agent/node_modules/@earendil-works/pi-coding-agent/dist/index.js",
        "macos/WorkMemory.app",
        "config/runtime.env",
        "config/collector.json",
        "config/ocr.json",
        ".cache/ollama/ollama",
    ]:
        if not (ROOT / path).exists():
            raise RuntimeError(f"尚缺少 {path}，请先选择安装／修复。")


def docker_ready():
    if not capture(["docker", "info", "--format", "{{.ServerVersion}}"]):
        if Path("/Applications/Docker.app").exists():
            run(["open", "/Applications/Docker.app"], "打开 Docker Desktop")
            for _ in range(30):
                if capture(["docker", "info", "--format", "{{.ServerVersion}}"]):
                    return
                time.sleep(1)
        raise RuntimeError("Docker 尚未就绪，请完成 Docker 界面的启动或许可提示，再重试。")


def database_start(graphiti=False):
    python_script("native_database.py", "start", title="启动原生 PostgreSQL")
    selected = config_snapshot()["memory"].get("backend", "reme")
    # The database is authoritative after a web UI backend switch.
    result = capture([str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/selected_backend.py")])
    if result in {"reme", "graphiti"}:
        selected = result
    if graphiti or selected == "graphiti":
        docker_ready()
        assert_container_ownership()
        run(compose_args("--profile", "graphiti", "up", "-d", "--wait", "neo4j"), "启动 Neo4j")


def start(graphiti=False, open_windows=True):
    require_installed()
    database_start(graphiti)
    for name in ["ollama", "ocr", "backend"]:
        python_script("services.py", "start", name, title="启动 " + NAMES[name])
    if not collector_pids():
        script("collector-start.sh", title="打开拾忆采集")
        for _ in range(25):
            if read_json(ROOT / ".run/collector-status.json").get("pid") in collector_pids():
                break
            time.sleep(0.2)
    if open_windows:
        script("open-ui.sh", title="打开工作空间")
    show_status(status())


def stop(remove_containers=False):
    stop_collector()
    if (ROOT / ".venv/bin/python").exists():
        for name in ["backend", "ocr", "ollama"]:
            python_script("services.py", "stop", name, title="停止 " + NAMES[name])
    else:
        # Repair/uninstall remains possible after a partial venv deletion.
        for name in ["backend", "ocr", "ollama"]:
            if owns_process(name):
                os.kill(read_json(ROOT / f".run/{name}.json")["pid"], signal.SIGTERM)
                for _ in range(50):
                    if not owns_process(name):
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError(NAMES[name] + "尚未退出")
    if (ROOT / "data/postgres-native/PG_VERSION").exists():
        python_script("native_database.py", "stop", title="停止原生 PostgreSQL")
    if (ROOT / "config/runtime.env").exists() and capture(
        ["docker", "info", "--format", "{{.ServerVersion}}"]
    ):
        assert_container_ownership()
        run(
            compose_args("--profile", "graphiti", "down" if remove_containers else "stop"),
            "停止项目数据库",
        )


def install(graphiti=False, open_windows=True):
    stage("Python 依赖", lambda: run(["uv", "sync", "--locked"], "安装 Python 依赖"))
    stage(
        "本地配置", lambda: python_script("upgrade_config.py", title="升级采集配置，保留记忆和凭据")
    )
    stage("停止旧服务", stop)
    stage("网页与采集端", build_if_needed)
    # migrate() runs under this manager operation lock. Its CLI acquires the lock itself.
    from migrate_postgres import migrate
    stage("原生数据库安装／迁移", migrate)
    stage("数据库", lambda: database_start(graphiti))
    stage("迁移", lambda: run([ROOT / ".venv/bin/alembic", "upgrade", "head"], "升级数据库结构"))
    if graphiti or config_snapshot()["memory"].get("backend") == "graphiti":
        python_script("lock_images.py")
    python_script("local_config.py")
    stage("Ollama", lambda: script("install-ollama.sh", title="准备原生 Ollama"))
    python_script("services.py", "start", "ollama")
    config = config_snapshot()
    models = request_json(config["ollama"]["url"] + "/api/tags").get("models", [])
    names = {m["name"] for m in models}
    if any(name not in names for name in required_local_models(config)):
        stage(
            "语言与嵌入模型", lambda: script("models-pull.sh", title="下载缺失模型，支持中断续传")
        )
    stage("OCR 模型", lambda: script("ocr-prepare.sh", title="准备 PaddleOCR tiny 模型"))
    stage("启动与检查", lambda: start(graphiti, open_windows))
    inventory = {
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "root": str(ROOT),
        "uv": capture(["uv", "--version"]),
        "python": capture([str(ROOT / ".venv/bin/python"), "--version"]),
        "node": capture(["node", "--version"]),
        "docker": capture(["docker", "--version"]),
        "images": read_json(ROOT / "infra/images.lock.json"),
        "ocr": read_json(ROOT / "models/paddleocr/inventory.json"),
    }
    inventory["downloads"] = [
        {"name": p.name, "sha256": sha256_file(p)}
        for p in (ROOT / ".cache/downloads").glob("*")
        if p.is_file() and not p.name.endswith(".partial")
    ]
    save_json(ROOT / ".run/installation.json", inventory)
    print("\n安装已完成。首次使用请在叶子菜单中授权并开始采集。")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_remove(relative):
    path = ROOT / relative
    if not inside(path) or path.is_symlink():
        raise RuntimeError("清除路径不属于项目或是符号链接，已拒绝：" + relative)
    # Do not traverse directory symlinks. shutil.rmtree unlinks child links rather than following them.
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def uninstall(purge=False, confirmed=False):
    if purge and not confirmed:
        print(
            "将永久清除本项目的全部记忆、原始文本、模型、配置凭据、备份和日志。\n目录：" + str(ROOT)
        )
        if input("请输入“清除拾忆全部数据”确认：").strip() != "清除拾忆全部数据":
            print("已取消。")
            return
    if (
        purge
        and (ROOT / "data/postgres").exists()
        and not capture(["docker", "info", "--format", "{{.ServerVersion}}"])
    ):
        raise RuntimeError(
            "无法确认数据库容器已停止。请启动 Docker 后再彻底清除，避免删除运行中的数据库文件。"
        )
    stop(remove_containers=True)
    paths = [
        ".venv",
        "ocr/.venv",
        "frontend/node_modules",
        "agent/node_modules",
        "frontend/dist",
        "macos/.build",
        "macos/WorkMemory.app",
        ".tools",
        ".cache/ollama",
        ".cache/downloads",
    ]
    if purge:
        paths += [
            "data",
            "models",
            "logs",
            ".cache",
            "config/app.local.yaml",
            "config/secrets.env",
            "config/runtime.env",
            "config/collector.json",
            "config/ocr.json",
            "config/generation.local.json",
            "config/mcp.local.json",
            "config/database.local.json",
        ]
    if purge:
        paths += [str(path.relative_to(ROOT)) for pattern in (".generation-*", ".mcp-*") for path in (ROOT / "config").glob(pattern)]
    for relative in paths:
        if (ROOT / relative).exists() or (ROOT / relative).is_symlink():
            safe_remove(relative)
    # Keep the active lock inode until this process exits. Purged runtime files contain no user text.
    for name in [
        "collector-status.json",
        "service-config.json",
        "build-state.json",
        "backend.json",
        "ocr.json",
        "ollama.json",
    ]:
        (ROOT / ".run" / name).unlink(missing_ok=True)
    print(
        "已彻底清除项目数据。"
        if purge
        else "已卸载运行组件。记忆、配置、模型和备份已保留，双击管理入口可重新安装。"
    )


def permission_requirements():
    config = read_json(ROOT / "config/collector.json")
    required = [("Accessibility", "辅助功能", "accessibility_granted")]
    if config.get("ocrMode", "always") != "off":
        required.append(("ScreenCapture", "屏幕录制", "screen_recording_granted"))
    if config.get("captureMode", "timer") == "interaction":
        required.append(("ListenEvent", "输入监控", "input_monitoring_granted"))
    return required


def permission_heartbeat(since):
    value = read_json(ROOT / ".run/collector-status.json")
    try:
        updated = datetime.fromisoformat(value["updated_at"]).timestamp()
        if value.get("pid") not in collector_pids() or updated < since or not 0 <= time.time() - updated < 10:
            return {}
    except (KeyError, TypeError, ValueError):
        return {}
    return value


def permission_ingestion_since(since):
    # Metadata only; never print captured text or make a synthetic business record.
    try:
        from native_database import NativePostgres
        pg = NativePostgres()
        pg.assert_connection()
        stamp = datetime.fromtimestamp(since, timezone.utc).isoformat()
        return int(pg.sql(
            f"SELECT count(*) FROM observations WHERE captured_at >= '{stamp}'::timestamptz",
            pg.settings.database.name,
        ))
    except (ImportError, RuntimeError, OSError, subprocess.SubprocessError):
        return None


def permissions(guided=False):
    """Explicit user action only. Supported TCC reset + system UI, never a silent grant."""
    app = ROOT / "macos/WorkMemory.app"
    binary = app / "Contents/MacOS/WorkMemory"
    if not binary.is_file():
        raise RuntimeError("请先安装采集端。")
    if os.geteuid() == 0:
        raise RuntimeError("请以当前登录用户运行，不要加 sudo；授权密码只在 macOS 系统窗口中输入。")
    if guided and not sys.stdin.isatty():
        raise RuntimeError("引导模式需要交互终端，请双击修复拾忆权限.command。")
    run(["codesign", "--verify", "--deep", "--strict", app], "检查当前应用签名")
    fingerprint_before = sha256_file(binary)
    required = permission_requirements()
    print("只清除拾忆的旧授权，不需要 sudo。最终允许操作需要在 macOS 设置中点击。")
    print("本次需要：" + "、".join(label for _, label, _ in required))
    stop_collector()
    try:
        for service, label, _ in required:
            run(["tccutil", "reset", service, "local.workmemory.collector"], "清除拾忆的" + label + "旧授权")
    finally:
        # Even a partial reset failure should not leave an originally usable app stopped.
        script("collector-start.sh")
    run(["open", "-R", app], "在 Finder 中选中当前应用")
    for service, label, _ in required:
        run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_" + service], "打开" + label + "设置")
        print("将 Finder 选中的 WorkMemory.app 加入列表，并打开它的开关。")
        print("若仍有旧拾忆条目，先移除旧条目再添加。若没有添加按钮，请在拾忆菜单点击“授予" + label + "权限”。")
        if not guided:
            print("请依次完成上述权限设置；双击修复拾忆权限.command 可逐步引导并自动核验。")
            return
        input("完成这一项后回到终端按回车继续（密码只输入 macOS 系统弹窗）：")
        if sha256_file(binary) != fingerprint_before:
            raise RuntimeError("授权过程中应用已被其他构建替换，请停止构建后重新运行权限修复。")
    stop_collector()
    since = time.time()
    script("collector-start.sh", title="重开当前应用，让新权限生效")
    print("正在检查实际权限。若原来已经开始采集，请切到普通工作窗口，等待最多30秒核验截图和OCR；暂停选择会保留。")
    deadline = time.monotonic() + 30
    state = {}
    ready = False
    while time.monotonic() < deadline:
        if sha256_file(binary) != fingerprint_before:
            raise RuntimeError("应用在核验期间被替换，请停止构建后重新运行权限修复。")
        state = permission_heartbeat(since)
        ready = bool(state) and all(state.get(key) is True for _, _, key in required)
        captured = bool(state.get("captures", 0) > 0 and state.get("last_ocr_at"))
        if ready and (state.get("paused") or captured or not any(s == "ScreenCapture" for s, _, _ in required)):
            break
        time.sleep(1)
    new_records = permission_ingestion_since(since) if ready and not state.get("paused") else None
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "permissions_ready": ready,
              "binary_unchanged": sha256_file(binary) == fingerprint_before,
              "permissions": {label: state.get(key) for _, label, key in required},
              "paused": state.get("paused"), "captures": state.get("captures", 0),
              "last_ocr_at": state.get("last_ocr_at"), "new_observations": new_records}
    save_json(ROOT / ".run/permissions-result.json", report)
    if not ready:
        missing = "、".join(label for _, label, key in required if state.get(key) is not True)
        raise RuntimeError("新应用仍未获得有效的" + missing + "权限。请确认添加的是 Finder 选中的当前应用；本次没有再次编译或签名。")
    print("权限已生效，原生应用签名保持不变。")
    if state.get("paused"):
        print("保留了原有暂停/未开始选择，本次没有开启采集。")
    elif state.get("captures", 0) > 0 and state.get("last_ocr_at"):
        print("实际截图和 OCR 已完成；" + ("已核验新采集入库。" if new_records else "暂无新入库确认，可能是内容去重或后端尚未处理，请查看状态。"))
    else:
        print("权限已就绪，但本次尚未核验新截图/OCR；请切到非排除窗口并查看采集状态。")


def diagnose():
    value = status()
    show_status(value)
    print("\n诊断建议：")
    for name in ["uv", "node", "brew", "swift"]:
        print(f"  {name}：{shutil.which(name) or '未安装；请选择安装／修复'}")
    for module in value["modules"]:
        if module["enabled"] and not module["ready"]:
            print(
                f"  {module['label']}：{'选择启动全部；检查 logs/' + module['module'] + '.log' if module['installed'] else '选择安装／修复'}"
            )
    if not value.get("generation", {}).get("ready"):
        print("  整理与问答：在网页偏好与状态中检查地址、模型名和密钥，再测试连接。")
    if value["collector"].get("advice"):
        print("  " + value["collector"]["advice"])
    print(
        "  若升级后 macOS 权限失效，请在系统设置中重新添加 macos/WorkMemory.app；管理器不会自动重置系统权限。"
    )


def dispatch(args):
    if args.action == "status":
        show_status(status(), args.json)
        return
    if args.action == "doctor":
        diagnose()
        return
    if args.action == "logs":
        for name in ["manager", "postgres", "backend", "ocr", "ollama"]:
            path = ROOT / f"logs/{name}.log"
            print(f"\n{name}：{path}")
            if path.exists():
                print(capture(["tail", "-n", "25", str(path)]))
        return
    with operation_lock():
        if args.action in {"install", "repair"}:
            install(args.graphiti, not args.no_open)
        elif args.action == "start":
            start(args.graphiti, not args.no_open)
        elif args.action == "stop":
            stop()
        elif args.action == "restart":
            stop()
            start(args.graphiti, not args.no_open)
        elif args.action == "backup":
            python_script("native_database.py", "start", title="启动原生 PostgreSQL")
            script("backup.sh", title="备份 PostgreSQL")
        elif args.action == "permissions":
            permissions(guided=args.guided)
        elif args.action in {"uninstall", "purge"}:
            uninstall(args.action == "purge")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="拾忆安装和服务管理")
    parser.add_argument(
        "action",
        nargs="?",
        choices=[
            "install",
            "repair",
            "start",
            "stop",
            "restart",
            "status",
            "doctor",
            "logs",
            "backup",
            "uninstall",
            "purge",
            "permissions",
        ],
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--graphiti", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--guided", action="store_true", help="权限修复：逐项引导并核验实际权限")
    args = parser.parse_args()
    if args.action:
        dispatch(args)
        return
    choices = {
        "1": "install",
        "2": "start",
        "3": "stop",
        "4": "restart",
        "5": "status",
        "6": "doctor",
        "7": "logs",
        "8": "backup",
        "9": "uninstall",
        "10": "purge",
        "11": "permissions",
    }
    while True:
        print(
            "\n拾忆管理\n1 安装／修复　2 启动全部　3 停止全部　4 重启\n5 查看状态　6 诊断　7 查看日志　8 备份\n9 卸载（保留数据）　10 彻底清除\n11 重新授权拾忆（清除本应用旧授权）　0 退出"
        )
        choice = input("请选择：").strip()
        if choice == "0":
            return
        if choice not in choices:
            continue
        args.action = choices[choice]
        args.guided = args.action == "permissions"
        try:
            if args.action == "install":
                # Re-enter the shell bootstrap even when some runtime tools already exist.
                script("manage.sh", "install")
            else:
                dispatch(args)
            if args.action in {"uninstall", "purge"}:
                return
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            print("\n操作未完成：" + str(error))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print("操作未完成：" + str(error), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。安装成果已保留，下次可继续。")
        sys.exit(130)
