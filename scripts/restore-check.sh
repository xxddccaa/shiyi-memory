#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
[[ $# == 1 && -f "$1" ]] || { printf '用法：bash scripts/restore-check.sh BACKUP.dump\n' >&2; exit 2; }
# Resolve before changing cwd so caller-relative dump paths work.
BACKUP_PATH="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
cd "$PROJECT_ROOT"
uv run python scripts/native_database.py restore-check "$BACKUP_PATH"
printf '已在独立临时数据库恢复并验证，业务库未覆盖。\n'
