#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$PROJECT_ROOT"
NPM_PROJECT="${WORKMEMORY_NPM_PROJECT:-frontend}"
[[ "$NPM_PROJECT" == frontend || "$NPM_PROJECT" == agent ]] || exit 1
export npm_config_cache="$PROJECT_ROOT/.cache/npm"
PROXY_URL="$(npm config get https-proxy)"
if [[ "$PROXY_URL" != null ]] && ! uv run python - "$PROXY_URL" <<'PY'
import socket,sys
from urllib.parse import urlparse
p=urlparse(sys.argv[1])
try:
    socket.create_connection((p.hostname,p.port or 80),timeout=2).close()
except OSError:
    raise SystemExit(1)
PY
then
  printf 'Configured npm proxy is unavailable; using direct access for this project.\n'
  mkdir -p "$PROJECT_ROOT/.cache/npm"
  : > "$PROJECT_ROOT/.cache/npm/empty-global.npmrc"
  npm --prefix "$PROJECT_ROOT/$NPM_PROJECT" --userconfig=/dev/null --globalconfig="$PROJECT_ROOT/.cache/npm/empty-global.npmrc" "$@"
else
  npm --prefix "$PROJECT_ROOT/$NPM_PROJECT" "$@"
fi
