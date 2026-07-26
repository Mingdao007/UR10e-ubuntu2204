#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
EXP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

export STEP4E_VERSION="v2"
export BRIDGE_DURATION_S="240"

exec "${EXP_DIR}/scripts/step4e-line-v1-operator.sh" "$@"
