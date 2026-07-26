#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROFILE="step5d_strict_rnn_ablation_v31"
PACKAGE_DIR="${ROOT}/programs/step5/step5d"

usage() {
  cat <<'EOF'
Usage:
  step5d-strict-rnn-contact-v31.sh status
  step5d-strict-rnn-contact-v31.sh generate-deliver
  step5d-strict-rnn-contact-v31.sh verify
  step5d-strict-rnn-contact-v31.sh live-ready
  STEP5D_V31_CONTACT_CONFIRM='LIVE STEP5D STRICT RNN CONTACT V31' \
    step5d-strict-rnn-contact-v31.sh contact-bridge

generate-deliver always regenerates v31, runs the deterministic package gate,
uploads the complete TP triplet, and performs a fresh controller read-back.
It never loads/Plays the TP program and never starts a bridge.

contact-bridge remains blocked until package/read-back, frozen fingerprint,
Review v3, and explicit live/contact authorization are all recorded.
EOF
}

case "${1:-}" in
  status)
    exec python3 "${ROOT}/tools/step5d_runtime_interface.py" --root "${ROOT}" --program "${PROFILE}" live-ready
    ;;
  verify)
    exec python3 "${ROOT}/tools/verify_step5d_contact_v31.py" --root "${ROOT}"
    ;;
  generate-deliver)
    python3 "${ROOT}/tools/build_step5d_liveprep.py" \
      --program "${PROFILE}" \
      --output-dir "${PACKAGE_DIR}" \
      --reuse-existing-metadata \
      --local-only
    python3 "${ROOT}/tools/verify_step5d_contact_v31.py" --root "${ROOT}"
    exec python3 "${ROOT}/tools/upload_ur_tp_package.py" "${PROFILE}" \
      --local-dir "${PACKAGE_DIR}" \
      --force-upload-readback
    ;;
  live-ready)
    STEP5D_VERSION="${PROFILE}" exec "${SCRIPT_DIR}/step5d-liveprep-operator.sh" live-ready
    ;;
  contact-bridge)
    if [[ "${STEP5D_V31_CONTACT_CONFIRM:-}" != "LIVE STEP5D STRICT RNN CONTACT V31" ]]; then
      echo "refusing v31 contact bridge: explicit confirmation token is missing"
      exit 40
    fi
    export STEP5D_CONFIRM="LIVE STEP5D STRICT RNN LIVEPREP"
    export STEP5D_VERSION="${PROFILE}"
    export STEP5D_STAGE25_CONTROL_MODE="speedj_rnn_live"
    export STEP5D_RNN_BACKEND="cupy"
    export STEP5D_RNN_INNER_ITERATIONS="512"
    export STEP5D_EPSILON="0.010"
    export STEP5D_SIGR_EXPONENT_R="0.800"
    export STEP5D_QDOT_LIMIT_RAD_S="0.500"
    export STEP5D_BASELINE_S="1.0"
    export STEP5D_REZERO_S="1.0"
    export STEP5D_NORMAL_FOLLOW_MODE="filtered_live"
    export STEP5D_NORMAL_MIN_FORCE_N="2.0"
    export STEP5D_DEFAULT_MAX_NORMAL_FORCE_N="60"
    export STEP5D_DEFAULT_MAX_FORCE_NORM_N="100"
    export STEP5D_DEFAULT_MAX_TORQUE_NORM_NM="3.0"
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
