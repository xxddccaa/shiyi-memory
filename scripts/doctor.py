import asyncio
import json
import shutil
import subprocess

import httpx
from services import assert_ollama
from sqlalchemy import text
from workmemory.collector.status import collector_status, ocr_status
from workmemory.config import ROOT, get_settings
from workmemory.config.generation import GenerationStore
from workmemory.db import make_engine
from workmemory.inference.ollama import Ollama


async def main():
    s = get_settings()
    checks = []
    engine = make_engine()
    try:
        async with engine.connect() as conn:
            version = await conn.scalar(text("SHOW server_version"))
            migration = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        checks.append(
            {
                "check": "postgres",
                "ok": str(version).startswith("17."),
                "version": version,
                "migration": migration,
            }
        )
    except Exception as e:
        checks.append({"check": "postgres", "ok": False, "error": type(e).__name__})
    finally:
        await engine.dispose()
    try:
        info = assert_ollama()
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.get(s.ollama.url + "/api/tags")
            response.raise_for_status()
            models = [m["name"] for m in response.json()["models"]]
            ps = (await client.get(s.ollama.url + "/api/ps")).json()
        checks.append(
            {
                "check": "ollama",
                "ok": all(
                    name in models for name in GenerationStore(s.ollama).required_local_models()
                ),
                "models_path": info["models_path"],
                "models": models,
                "loaded_models": [
                    {
                        "name": m["name"],
                        "size_vram": m.get("size_vram"),
                        "context_length": m.get("context_length"),
                    }
                    for m in ps.get("models", [])
                ],
            }
        )
    except Exception as e:
        checks.append({"check": "ollama", "ok": False, "error": type(e).__name__})
    model = None
    try:
        model = Ollama(s.ollama, GenerationStore(s.ollama).current)
        health = await model.health()
        checks.append(
            {"check": "generation", "ok": health["generation"]["ready"], **health["generation"]}
        )
    except Exception as e:
        checks.append({"check": "generation", "ok": False, "error": type(e).__name__})
    finally:
        if model:
            await model.close()
    ocr = await ocr_status(s)
    checks.append({"check": "paddleocr", "ok": ocr["ready"], **ocr})
    from native_database import NativePostgres
    try:
        pg = NativePostgres()
        pg.check_marker()
        pg.assert_connection()
        checks.append({"check": "native_postgres_owner", "ok": True, "path": str(pg.data)})
    except (RuntimeError, OSError):
        checks.append({"check": "native_postgres_owner", "ok": False})
    if s.memory.backend == "graphiti" and shutil.which("docker"):
        output = subprocess.run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=workmemory",
                                 "--filter", "label=com.docker.compose.service=neo4j"], capture_output=True, text=True)
        from manager import owns_container
        for container in output.stdout.split():
            item = json.loads(subprocess.check_output(["docker", "inspect", container], text=True))[0]
            checks.append({"check": "neo4j_container_owner", "ok": owns_container(item)})
    checks.append(
        {
            "check": "disk",
            "ok": shutil.disk_usage(ROOT).free > 5 * 1024**3,
            "free_gb": round(shutil.disk_usage(ROOT).free / 1024**3, 1),
        }
    )
    checks.append(
        {
            "check": "collector",
            "ok": (ROOT / "macos/WorkMemory.app/Contents/MacOS/WorkMemory").exists(),
            "spooled_batches": len(list((ROOT / "data/collector/spool").glob("*.json"))),
            "runtime": collector_status(s),
            "permissions": "Grant Accessibility; PaddleOCR also needs Screen Recording, then click Start",
        }
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    raise SystemExit(0 if all(c["ok"] for c in checks) else 1)


if __name__ == "__main__":
    asyncio.run(main())
