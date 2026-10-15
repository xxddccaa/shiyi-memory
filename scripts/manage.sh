#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
export PATH="$PROJECT_ROOT/.tools/uv:$PROJECT_ROOT/.tools/node/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
export UV_PYTHON_INSTALL_DIR="$PROJECT_ROOT/.tools/python"
export UV_PYTHON_PREFERENCE=managed
if [[ "${1:-}" == install || "${1:-}" == repair ]]; then
  bash "$PROJECT_ROOT/scripts/runtime-bootstrap.sh"
fi
MANAGER_PYTHON=""
if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  MANAGER_PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif command -v uv >/dev/null 2>&1; then
  MANAGER_PYTHON="$(uv python find --managed-python 3.12 2>/dev/null || true)"
fi
if [[ -z "$MANAGER_PYTHON" ]] && command -v python3 >/dev/null 2>&1; then
  CANDIDATE_PYTHON="$(command -v python3)"
  if [[ "$CANDIDATE_PYTHON" != /usr/bin/python3 ]] || xcode-select -p >/dev/null 2>&1; then MANAGER_PYTHON="$CANDIDATE_PYTHON"; fi
fi
if [[ -z "$MANAGER_PYTHON" ]]; then
  if [[ "${1:-}" == status && "${2:-}" == --json ]]; then
    printf '{"installed":false,"ready":false,"message":"尚未安装 Python。请运行安装／修复。"}\n'; exit 0
  fi
  printf '拾忆尚未安装运行环境。\n1. 安装／修复\n0. 退出\n'
  if [[ -n "${1:-}" ]]; then printf '请运行：bash scripts/manage.sh install\n'; exit 1; fi
  read -r -p '请选择：' choice
  [[ "$choice" == 1 ]] && exec bash "$PROJECT_ROOT/scripts/manage.sh" install
  exit 0
fi
exec "$MANAGER_PYTHON" "$PROJECT_ROOT/scripts/manager.py" "$@"
