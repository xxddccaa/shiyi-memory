"""Create project-local secrets and derived configs, never print credentials."""

import json
import os
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    os.umask(0o077)
    for folder in [
        "config",
        "logs",
        ".run",
        ".cache",
        "models/ollama",
        "models/paddleocr",
        "data/collector/spool",
        "data/reme",
        "data/backups",
        "data/exports",
        "data/neo4j/data",
        "data/neo4j/logs",
        "data/neo4j/import",
        "data/neo4j/plugins",
    ]:
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    for private in ["data", "config", "logs", ".run"]:
        (ROOT / private).chmod(0o700)
    local = ROOT / "config/app.local.yaml"
    if not local.exists():
        shutil.copyfile(ROOT / "config/app.example.yaml", local)
    env = ROOT / "config/secrets.env"
    if not env.exists():
        env.write_text(
            "\n".join(
                f"{key}={secrets.token_hex(24)}"
                for key in ["POSTGRES_PASSWORD", "API_TOKEN", "NEO4J_PASSWORD"]
            )
            + "\n"
        )
    env.chmod(0o600)
    from workmemory.config import get_settings

    s = get_settings()
    from urllib.parse import urlparse

    bolt = urlparse(s.memory.neo4j_uri)
    if bolt.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Neo4j must be local")
    values = dict(
        PROJECT_ROOT=str(ROOT),
        POSTGRES_PORT=s.database.port,
        POSTGRES_DB=s.database.name,
        POSTGRES_USER=s.database.user,
        NEO4J_BOLT_PORT=bolt.port or 7687,
        NEO4J_HTTP_PORT=s.memory.neo4j_http_port,
    )
    inventory = ROOT / "infra/images.lock.json"
    if inventory.exists():
        for name, image in json.loads(inventory.read_text()).items():
            values[name.upper() + "_IMAGE"] = image["digest"]
    (ROOT / "config/runtime.env").write_text("".join(f"{k}='{v}'\n" for k, v in values.items()))
    (ROOT / "config/collector.json").write_text(
        json.dumps(
            {
                "endpoint": f"http://127.0.0.1:{s.server.port}",
                "token": s.api_token,
                "spoolPath": str(ROOT / "data/collector/spool"),
                "intervalSeconds": s.collector.interval_seconds,
                "maxSpoolBytes": s.collector.spool_max_mb * 1024**2,
                "statusPath": str(ROOT / ".run/collector-status.json"),
                "ocrEndpoint": f"http://127.0.0.1:{s.ocr.port}",
                "ocrSignature": s.ocr.detection_model + ":" + s.ocr.recognition_model + ":" + str(s.ocr.cpu_threads),
                "ocrMode": s.collector.ocr_mode,
                "captureMode": s.collector.capture_mode,
                "frameRate": s.collector.frame_rate,
                "mergeSeconds": s.collector.merge_seconds,
                "maxGroupSeconds": s.collector.max_group_seconds,
                "stableSeconds": s.collector.stable_seconds,
                "ocrMinInterval": s.collector.ocr_min_interval,
                "imageBudgetBytes": s.collector.image_budget_mb * 1024**2,
                "maxPendingGroups": s.collector.max_pending_groups,
                "captureStatePath": str(ROOT / "data/collector/state.json"),
                "ocrTimeoutSeconds": s.ocr.timeout_seconds,
                "excludedApps": s.collector.excluded_apps,
                "excludedTitlePatterns": s.collector.excluded_title_patterns,
            },
            indent=2,
        )
    )
    (ROOT / "config/ocr.json").write_text(
        json.dumps({**s.ocr.model_dump(), "token": s.api_token}, indent=2)
    )
    (ROOT / ".run/service-config.json").write_text(json.dumps({
        "server": s.server.model_dump(), "ocr": s.ocr.model_dump(),
        "database": s.database.model_dump(),
        "memory": s.memory.model_dump(), "ollama": s.ollama.model_dump(),
    }, indent=2))
    print("Local configuration ready")


if __name__ == "__main__":
    main()
