#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CANARY="step5d_strict_rnn_ablation_v33c20"
FULL="step5d_strict_rnn_ablation_v33"
PACKAGE_DIR="${ROOT}/programs/step5/step5d"

usage() {
  cat <<'EOF'
Usage:
  step5d-strict-rnn-contact-v33.sh status
  step5d-strict-rnn-contact-v33.sh generate-deliver
  step5d-strict-rnn-contact-v33.sh verify
  STEP5D_V33_CONTACT_CONFIRM='LIVE STEP5D STRICT RNN CONTACT V33C20' \
    step5d-strict-rnn-contact-v33.sh contact-bridge

generate-deliver regenerates, validates, uploads, and freshly reads back both
v33c20 and v33 TP triplets. It never loads/Plays a program or starts a bridge.
contact-bridge targets only the current 20 s canary and still requires the
separate current-stage review/read-back/live-authorization gates.
EOF
}

case "${1:-}" in
  status)
    exec python3 "${ROOT}/tools/step5d_runtime_interface.py" --root "${ROOT}" --program "${CANARY}" live-ready
    ;;
  verify)
    exec python3 "${ROOT}/tools/verify_step5d_contact_v33.py" --root "${ROOT}"
    ;;
  generate-deliver)
    for profile in "${CANARY}" "${FULL}"; do
      python3 "${ROOT}/tools/build_step5d_liveprep.py" \
        --program "${profile}" \
        --output-dir "${PACKAGE_DIR}" \
        --reuse-existing-metadata \
        --local-only
    done
    python3 "${ROOT}/tools/verify_step5d_contact_v33.py" --root "${ROOT}"
    for profile in "${CANARY}" "${FULL}"; do
      python3 "${ROOT}/tools/upload_ur_tp_package.py" "${profile}" \
        --local-dir "${PACKAGE_DIR}" \
        --force-upload-readback
    done
    ;;
  contact-bridge)
    if [[ "${STEP5D_V33_CONTACT_CONFIRM:-}" != "LIVE STEP5D STRICT RNN CONTACT V33C20" ]]; then
      echo "refusing v33c20 contact bridge: explicit confirmation token is missing"
      exit 40
    fi
    export STEP5D_CONFIRM="LIVE STEP5D STRICT RNN LIVEPREP"
    export STEP5D_VERSION="${CANARY}"
    export STEP5D_STAGE25_CONTROL_MODE="speedj_rnn_live"
    export STEP5D_RNN_BACKEND="cupy"
    export STEP5D_RNN_INNER_ITERATIONS="512"
    export STEP5D_EPSILON="0.010"
    export STEP5D_SIGR_EXPONENT_R="0.800"
    export STEP5D_QDOT_LIMIT_RAD_S="0.500"
    export STEP5D_BASELINE_S="1.0"
    export STEP5D_REZERO_S="1.0"
    export STEP5D_SENSOR_STALE_S="2.0"
    export STEP5D_NORMAL_FOLLOW_MODE="filtered_live"
    export STEP5D_NORMAL_FILTER_ALPHA="0.55"
    export STEP5D_NORMAL_MIN_FORCE_N="2.0"
    export STEP5D_PRELOAD_FILTERED_MIN_N="5.0"
    export STEP5D_PRELOAD_FILTERED_MAX_N="22.0"
    export STEP5D_PRELOAD_RAW_MIN_N="3.0"
    export STEP5D_PRELOAD_RAW_MAX_N="25.0"
    export STEP5D_PRELOAD_FORCE_NORM_MAX_N="100.0"
    export STEP5D_PRELOAD_HOLD_S="0.0"
    export STEP5D_PRELOAD_TIMEOUT_S="60.0"
    exec "${SCRIPT_DIR}/step5d-liveprep-operator.sh" contact-bridge
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
