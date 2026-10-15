#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$PROJECT_ROOT"
uv run python - <<'PY'
import subprocess
from workmemory.config import get_settings
s=get_settings()
subprocess.run(['open',f'http://127.0.0.1:{s.server.port}/#token={s.api_token}'],check=True)
print('Opened local workspace; access token passed via URL fragment, not sent in server access logs.')
PY
