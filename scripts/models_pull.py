import hashlib
import json
import platform
import time

import httpx
from services import assert_ollama
from workmemory.config import ROOT, get_settings
from workmemory.config.generation import GenerationStore

s = get_settings()
assert_ollama()
with httpx.Client(base_url=s.ollama.url, timeout=600, trust_env=False) as client:
    for name in GenerationStore(s.ollama).required_local_models():
        print(f"Pulling {name} into {ROOT / 'models/ollama'}", flush=True)
        for attempt in range(1, 6):
            try:
                progress = -1
                with client.stream(
                    "POST", "/api/pull", json={"model": name, "stream": True}
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line:
                            continue
                        status = json.loads(line)
                        if "error" in status:
                            raise RuntimeError(status["error"])
                        total = status.get("total", 0)
                        percent = int(status.get("completed", 0) / total * 100) if total else 100
                        if percent // 10 != progress:
                            print(name, status.get("status"), f"{percent}%", flush=True)
                            progress = percent // 10
                break
            except (httpx.HTTPError, RuntimeError) as exc:
                if attempt == 5:
                    raise RuntimeError(
                        f"Download of {name} failed after 5 attempts; rerun to resume"
                    ) from exc
                print(
                    f"Retrying {name} after {type(exc).__name__} (attempt {attempt}/5)", flush=True
                )
                time.sleep(min(30, 2**attempt))
        show = client.post("/api/show", json={"model": name})
        show.raise_for_status()
        if name == "qwen3:8b" and show.json()["details"]["quantization_level"] != "Q4_K_M":
            raise RuntimeError(
                "qwen3:8b no longer resolves to requested Q4_K_M; choose an explicit matching tag"
            )
    models = client.get("/api/tags").json()["models"]
    inventory = {
        "recorded_at": time.time(),
        "models_path": str(ROOT / "models/ollama"),
        "ollama_version": client.get("/api/version").json()["version"],
        "platform": platform.platform(),
        "models": models,
        "details": {},
    }
    for model in models:
        inventory["details"][model["name"]] = client.post(
            "/api/show", json={"model": model["name"]}
        ).json()["details"]
    binary = ROOT / ".cache/ollama/ollama"
    inventory["ollama_binary_sha256"] = hashlib.file_digest(binary.open("rb"), "sha256").hexdigest()
    temp = ROOT / "models/inventory.json.tmp"
    temp.write_text(json.dumps(inventory, indent=2, ensure_ascii=False))
    temp.replace(ROOT / "models/inventory.json")
    print("Model digests recorded in models/inventory.json")
