#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"
cd "$PROJECT_ROOT"
swift build --package-path macos -c release
uv run python scripts/package_collector.py
