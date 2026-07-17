#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")/.." && pwd)"
if [[ "${STEP5D_V3_HERMETIC_PARSER_CI:-}" == "1" ]]; then
  export PYTHONPATH="${EXPERIMENT_ROOT}/tests:${EXPERIMENT_ROOT}/tools${PYTHONPATH:+:${PYTHONPATH}}"
  exec python3 -m step5d_v3_parser_ci_stubs --experiment-root "${EXPERIMENT_ROOT}" "$@"
fi
export PYTHONPATH="${EXPERIMENT_ROOT}/tools${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m step5d_autotune_v3.cli --experiment-root "${EXPERIMENT_ROOT}" "$@"
