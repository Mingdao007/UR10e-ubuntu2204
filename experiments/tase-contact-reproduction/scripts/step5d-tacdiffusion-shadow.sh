#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPERIMENT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BRIDGE="${EXPERIMENT_ROOT}/tools/run_step5d_tacdiffusion_bridge.py"

if [[ ! -f "${BRIDGE}" ]]; then
  echo "bridge entrypoint missing: ${BRIDGE}" >&2
  exit 2
fi

exec python3 "${BRIDGE}" "$@"

