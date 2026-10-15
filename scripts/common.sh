#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PROJECT_ROOT UV_CACHE_DIR="$PROJECT_ROOT/.cache/uv"
export PATH="$PROJECT_ROOT/.tools/uv:$PROJECT_ROOT/.tools/node/bin:/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
export UV_PYTHON_INSTALL_DIR="$PROJECT_ROOT/.tools/python"
export PYTHONPATH="$PROJECT_ROOT/backend"
compose() {
  docker compose --project-directory "$PROJECT_ROOT" \
    --env-file "$PROJECT_ROOT/config/secrets.env" \
    --env-file "$PROJECT_ROOT/config/runtime.env" -f "$PROJECT_ROOT/infra/compose.yaml" "$@"
}
