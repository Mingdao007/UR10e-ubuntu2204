#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)"

if (($# == 0)); then
  set -- start
fi

exec python3 "${ROOT}/tools/step5d-autotunectl.py" --root "${ROOT}" "$@"
