#!/usr/bin/env bash
# This stage uses only macOS system utilities. It works before Python or uv exists.
set -euo pipefail
source "$(dirname "$0")/common.sh"
export PATH="$PROJECT_ROOT/.tools/uv:$PROJECT_ROOT/.tools/node/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
export UV_PYTHON_INSTALL_DIR="$PROJECT_ROOT/.tools/python"
export UV_PYTHON_PREFERENCE=managed
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { printf '首版支持 Apple Silicon Mac。\n'; exit 1; }
MAC_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
[[ "$MAC_MAJOR" -ge 14 ]] || { printf '需要 macOS 14 或更高版本。\n'; exit 1; }
mkdir -p "$PROJECT_ROOT/.tools" "$PROJECT_ROOT/.cache/downloads" "$PROJECT_ROOT/.run"
# Shell-stage ownership lock, independent of Python availability.
if ! mkdir "$PROJECT_ROOT/.run/bootstrap.lock" 2>/dev/null; then
  LOCK_PID="$(cat "$PROJECT_ROOT/.run/bootstrap.lock/pid" 2>/dev/null || true)"
  if [[ "$LOCK_PID" =~ ^[0-9]+$ ]] && kill -0 "$LOCK_PID" 2>/dev/null; then
    printf '另一个安装正在运行，请等待它结束。\n'; exit 1
  fi
  rm -f "$PROJECT_ROOT/.run/bootstrap.lock/pid"
  rmdir "$PROJECT_ROOT/.run/bootstrap.lock" 2>/dev/null || { printf '安装锁需要检查：.run/bootstrap.lock\n'; exit 1; }
  mkdir "$PROJECT_ROOT/.run/bootstrap.lock"
fi
printf '%s\n' "$$" > "$PROJECT_ROOT/.run/bootstrap.lock/pid"
trap 'rm -f "$PROJECT_ROOT/.run/bootstrap.lock/pid"; rmdir "$PROJECT_ROOT/.run/bootstrap.lock" 2>/dev/null || true' EXIT
MANAGER_PID="$(cat "$PROJECT_ROOT/.run/manager.pid" 2>/dev/null || true)"
if [[ "$MANAGER_PID" =~ ^[0-9]+$ ]] && kill -0 "$MANAGER_PID" 2>/dev/null; then
  printf '另一个管理操作正在执行，请等待它结束。\n'; exit 1
fi
if ! xcode-select -p >/dev/null 2>&1 || ! xcrun --find swift >/dev/null 2>&1; then
  xcode-select --install || true
  printf '请完成 macOS 弹出的开发工具安装，再次选择安装／修复即可继续。\n'; exit 2
fi
download() {
  local target="$1" url="$2"
  curl --fail --location --retry 3 --connect-timeout 20 --continue-at - "$url" -o "$target"
}
if ! command -v uv >/dev/null 2>&1; then
  UV_VERSION=0.9.18
  UV_ARCHIVE="$PROJECT_ROOT/.cache/downloads/uv.tar.gz"
  UV_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz"
  download "$UV_ARCHIVE" "$UV_URL"
  curl --fail --location --retry 3 "$UV_URL.sha256" -o "$UV_ARCHIVE.sha256"
  EXPECTED_SHA=dc3bee4abbb3bac267a3985a23ea7617d19d41ff381dbaf560ba415ad65af68f
  ACTUAL_SHA="$(shasum -a 256 "$UV_ARCHIVE" | awk '{print $1}')"
  [[ "$EXPECTED_SHA" == "$ACTUAL_SHA" && ${#EXPECTED_SHA} == 64 ]] || { printf 'uv 下载校验失败。\n'; exit 1; }
  mkdir -p "$PROJECT_ROOT/.tools/uv"
  tar -xzf "$UV_ARCHIVE" --strip-components=1 -C "$PROJECT_ROOT/.tools/uv"
fi
uv python install --no-bin 3.12
NODE_OK=false
if command -v node >/dev/null 2>&1; then
  if node -e 'const [m,n]=process.versions.node.split(".").map(Number); process.exit(m>22 || m===22&&n>=19 ? 0 : 1)' >/dev/null 2>&1; then NODE_OK=true; fi
fi
if [[ "$NODE_OK" != true ]]; then
  NODE_VERSION=22.19.0
  NODE_FILENAME="node-v$NODE_VERSION-darwin-arm64.tar.gz"
  NODE_ARCHIVE="$PROJECT_ROOT/.cache/downloads/$NODE_FILENAME"
  download "$NODE_ARCHIVE" "https://nodejs.org/dist/v$NODE_VERSION/$NODE_FILENAME"
  curl --fail --location --retry 3 "https://nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt" -o "$PROJECT_ROOT/.cache/downloads/node-shasums.txt"
  EXPECTED_SHA=c59006db713c770d6ec63ae16cb3edc11f49ee093b5c415d667bb4f436c6526d
  ACTUAL_SHA="$(shasum -a 256 "$NODE_ARCHIVE" | awk '{print $1}')"
  [[ "$EXPECTED_SHA" == "$ACTUAL_SHA" && ${#EXPECTED_SHA} == 64 ]] || { printf 'Node 下载校验失败。\n'; exit 1; }
  mkdir -p "$PROJECT_ROOT/.tools/node"
  tar -xzf "$NODE_ARCHIVE" --strip-components=1 -C "$PROJECT_ROOT/.tools/node"
fi
if ! command -v brew >/dev/null 2>&1; then
  printf '原生 PostgreSQL 17 需要 Homebrew。请先从 brew.sh 安装 Homebrew，再次选择安装／修复。\n'
  exit 2
fi
if [[ ! -x "$(brew --prefix postgresql@17 2>/dev/null)/bin/postgres" ]]; then
  HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew install postgresql@17
fi
printf '基础运行环境已就绪；默认数据库不需要 Docker。\n'
