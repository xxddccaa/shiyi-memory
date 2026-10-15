#!/usr/bin/env bash
set -euo pipefail
export WORKMEMORY_NPM_PROJECT=agent
exec bash "$(dirname "$0")/npm.sh" "$@"
