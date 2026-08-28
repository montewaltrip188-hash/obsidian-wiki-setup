#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "$(uname -m)" in
    arm64|aarch64) RUNTIME="$ROOT/runtime/targets/macos-arm64" ;;
    x86_64|amd64) RUNTIME="$ROOT/runtime/targets/macos-x64" ;;
    *) printf '不支持的 macOS 架构\n' >&2; exit 2 ;;
esac
PYTHON="$RUNTIME/python/bin/python3"
[ -x "$PYTHON" ] || { printf '缺少候选包内离线运行时\n' >&2; exit 2; }
exec "$PYTHON" "$ROOT/tools/manage_wiki_skills.py" "$@"
