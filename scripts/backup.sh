#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$PROJECT_ROOT"
uv run python scripts/native_database.py backup
printf '备份已保留创建时的历史内容；记忆索引可从业务摘要重建。\n'
