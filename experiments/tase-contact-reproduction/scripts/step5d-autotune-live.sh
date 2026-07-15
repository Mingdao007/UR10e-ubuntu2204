#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export BRIDGE_PROFILE="step5d_strict_rnn_autotune_v1"
export STEP5D_AUTOTUNE_CAMPAIGN_ROOT="${STEP5D_AUTOTUNE_CAMPAIGN_ROOT:-${ROOT}/runs/step5d_native_autotune_campaign_v1}"

exec "${SCRIPT_DIR}/bridge-line-operator.sh" line-bridge-fast
