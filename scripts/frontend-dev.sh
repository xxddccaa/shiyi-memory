#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$PROJECT_ROOT"
export WORKMEMORY_API_URL="$(uv run python -c 'from workmemory.config import get_settings; print("http://127.0.0.1:"+str(get_settings().server.port))')"
bash scripts/npm.sh run dev
