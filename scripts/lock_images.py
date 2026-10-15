import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
result = {}
lock = ROOT / "infra/images.lock.json"
if lock.exists():
    result = json.loads(lock.read_text())
for name, image in [("postgres", "postgres:17"), ("neo4j", "neo4j:5.26-community")]:
    output = subprocess.run(
        ["docker", "image", "inspect", result.get(name, {}).get("digest", image)],
        capture_output=True,
        text=True,
    )
    if output.returncode == 0:
        info = json.loads(output.stdout)[0]
        if info.get("RepoDigests"):
            previous = result.get(name, {})
            digest = previous.get("digest")
            if digest not in info["RepoDigests"]:
                digest = info["RepoDigests"][0]
            result[name] = {**previous, "tag": image, "digest": digest, "id": info["Id"]}
lock.write_text(json.dumps(result, indent=2) + "\n")
print("Recorded image digests:", ", ".join(result))
