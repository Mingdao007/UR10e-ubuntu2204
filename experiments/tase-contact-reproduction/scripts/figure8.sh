#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")/.." && pwd)"
exec python3 "${ROOT}/tools/tase_figure8_entrypoint.py" "$@"
