#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$PROJECT_ROOT"
bash scripts/dev-up.sh --graphiti
bash scripts/models-pull.sh
uv run python scripts/verify_memory.py
uv run python scripts/benchmark.py --iterations 3
bash scripts/doctor.sh
printf 'Live acceptance complete. Reports are in data/exports/.\n'
