#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$PROJECT_ROOT"
uv run python scripts/local_config.py
uv run python scripts/migrate_postgres.py
uv run alembic upgrade head
