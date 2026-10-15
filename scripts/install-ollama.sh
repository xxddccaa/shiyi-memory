#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$PROJECT_ROOT"
mkdir -p .cache/ollama
if [[ -x .cache/ollama/ollama ]]; then exit 0; fi
uv run python - <<'PY'
import json, urllib.request
from workmemory.config import ROOT
release={'version':'v0.40.2','digest':'sha256:e888b7637291ceb80b622c00ea067f62c86d9c50d419b3eb903032e4f1f8a4f6','url':'https://github.com/ollama/ollama/releases/download/v0.40.2/ollama-darwin.tgz'}
(ROOT/'.cache/ollama-release.json').write_text(json.dumps(release,indent=2))
print('Selected native Ollama',release['version'])
PY
DOWNLOAD_URL="$(uv run python -c 'import json; print(json.load(open(".cache/ollama-release.json"))["url"])')"
curl --silent --show-error --fail --location --retry 3 --continue-at - "$DOWNLOAD_URL" -o .cache/ollama-darwin.tgz
uv run python - <<'PY'
import hashlib,json
from pathlib import Path
metadata=json.loads(Path('.cache/ollama-release.json').read_text())
digest=metadata.get('digest')
if digest and digest.startswith('sha256:'):
    assert hashlib.file_digest(open('.cache/ollama-darwin.tgz','rb'),'sha256').hexdigest()==digest.split(':')[1], 'Ollama download checksum mismatch'
PY
tar -xzf .cache/ollama-darwin.tgz -C .cache/ollama
