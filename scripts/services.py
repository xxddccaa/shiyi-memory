"""Start/stop only processes created by this checkout; refuse occupied ports."""

import argparse
import json
import os
import signal
import socket
import subprocess
import time
from urllib.parse import urlparse

import httpx
from workmemory.config import ROOT, get_settings


def occupied(port):
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def command(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
    )
    return result.stdout.strip()


def verified(name):
    path = ROOT / f".run/{name}.json"
    if not path.exists():
        return None
    info = json.loads(path.read_text())
    expected = info["command"]
    actual = command(info["pid"])
    if str(ROOT) not in actual or not all(arg in actual for arg in expected):
        return None
    return info


def assert_ollama():
    s = get_settings()
    info = verified("ollama")
    if (
        not info
        or info.get("models_path") != str(ROOT / "models/ollama")
        or info["port"] != urlparse(s.ollama.url).port
    ):
        raise RuntimeError("Target Ollama is not managed by this project; refusing model download")
    # macOS has no /proc. Inspect only this managed process and do not emit its environment.
    environment = subprocess.check_output(
        ["ps", "eww", "-p", str(info["pid"]), "-o", "command="], text=True
    )
    if f"OLLAMA_MODELS={ROOT / 'models/ollama'}" not in environment:
        raise RuntimeError("Managed Ollama model directory does not match project")
    return info


def start(name):
    s = get_settings()
    if name == "ollama":
        binary = ROOT / ".cache/ollama/ollama"
        if not binary.exists():
            raise RuntimeError("Run scripts/install-ollama.sh first")
        args = [str(binary), "serve"]
        port = urlparse(s.ollama.url).port or 11434
        url = s.ollama.url + "/api/version"
    elif name == "ocr":
        binary = ROOT / "ocr/.venv/bin/python"
        if not binary.exists():
            raise RuntimeError("Run scripts/ocr-prepare.sh first")
        args = [str(binary), "-m", "workmemory_ocr.server"]
        port = s.ocr.port
        url = f"http://127.0.0.1:{port}/health"
    else:
        args = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/serve.py")]
        port = s.server.port
        url = f"http://127.0.0.1:{port}/health"
    if occupied(port):
        if verified(name):
            try:
                response = httpx.get(url, timeout=3, trust_env=False)
                response.raise_for_status()
                if name == "ocr" and (
                    response.json().get("detection_model") != s.ocr.detection_model
                    or response.json().get("recognition_model") != s.ocr.recognition_model
                ):
                    raise RuntimeError(
                        "OCR configuration changed; restart services after preparing its models"
                    )
            except httpx.HTTPError as exc:
                raise RuntimeError(
                    f"{name} process exists but is not ready; use restart or doctor"
                ) from exc
            print(f"{name} already ready on {port}")
            return
        raise RuntimeError(
            f"Port {port} is occupied by an unmanaged service; change app.local.yaml"
        )
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(ROOT / "backend"),
        OLLAMA_HOST=f"127.0.0.1:{port}",
        OLLAMA_MODELS=str(ROOT / "models/ollama"),
        OLLAMA_NUM_PARALLEL="1",
        OLLAMA_MAX_LOADED_MODELS="1",
        OLLAMA_CONTEXT_LENGTH=str(s.ollama.context_size),
        OLLAMA_NO_CLOUD="1",
        OLLAMA_KEEP_ALIVE="5m",
    )
    with open(ROOT / f"logs/{name}.log", "ab") as log:
        process = subprocess.Popen(
            args, cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True
        )
    # OCR is background work: reduce contention with typing and window rendering.
    # This changes CPU scheduling only, not model, image fidelity, or capture cadence.
    if name == "ocr":
        os.setpriority(os.PRIO_PROCESS, process.pid, 10)
    info = {
        "pid": process.pid,
        "command": args,
        "port": port,
        "models_path": str(ROOT / "models/ollama") if name == "ollama" else None,
    }
    path = ROOT / f".run/{name}.json"
    path.write_text(json.dumps(info))
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited; inspect logs/{name}.log")
        try:
            if httpx.get(url, timeout=1, trust_env=False).is_success:
                print(f"{name} ready on {port}")
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"{name} did not become ready; inspect logs/{name}.log")


def stop(name):
    info = verified(name)
    if not info:
        print(f"No managed {name} process to stop")
        return
    os.kill(info["pid"], signal.SIGTERM)
    for _ in range(20):
        if not command(info["pid"]):
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"{name} is still stopping; left it running for graceful cleanup")
    (ROOT / f".run/{name}.json").unlink(missing_ok=True)
    print(f"Stopped {name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "verify"])
    parser.add_argument("service", choices=["ollama", "backend", "ocr"])
    args = parser.parse_args()
    if args.action == "verify":
        assert_ollama()
        print("Ollama model path verified inside project")
    else:
        {"start": start, "stop": stop}[args.action](args.service)
