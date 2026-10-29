#!/usr/bin/env bash
set -Eeuo pipefail
command -v python3 >/dev/null || { printf '需要安装 Python 3.9 或更新版本。\n' >&2; exit 1; }
exec python3 "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/deploy.py" "$@"
