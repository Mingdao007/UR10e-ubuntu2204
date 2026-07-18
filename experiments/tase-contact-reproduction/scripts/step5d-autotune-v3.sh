#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")/.." && pwd)"
export PYTHONPATH="${EXPERIMENT_ROOT}/tools${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m step5d_autotune_v3.cli --experiment-root "${EXPERIMENT_ROOT}" "$@"
