#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$PROJECT_ROOT"
uv run python scripts/local_config.py
uv sync --project ocr --locked
"$PROJECT_ROOT/ocr/.venv/bin/python" -m workmemory_ocr.engine
