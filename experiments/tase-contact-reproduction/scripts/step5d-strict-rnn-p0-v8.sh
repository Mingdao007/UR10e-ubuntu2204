#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
BASE_WRAPPER="${SCRIPT_DIR}/step5d-strict-rnn-p0.sh"

usage() {
  cat <<'EOF'
Usage:
  step5d-strict-rnn-p0-v8.sh status
  step5d-strict-rnn-p0-v8.sh capture-ready PHASE_S
  STEP5D_P0_CONFIRM='LIVE STEP5D STRICT RNN NO CONTACT P0 V8' \
    step5d-strict-rnn-p0-v8.sh capture-bridge PHASE_S
  step5d-strict-rnn-p0-v8.sh validate-run RUN_DIR_OR_CSV PHASE_S

PHASE_S must be exactly 2, 10, or 60. They run serially on the same frozen
fingerprint; only the final continuous verified 60 second artifact can pass P0.

This wrapper does not upload a package, load or Play TP, zero force sensing,
write TCP/payload, or authorize contact/v30 live motion.
EOF
}

require_phase() {
  local phase="${1:-}"
  case "${phase}" in
    2|10|60) printf '%s\n' "${phase}" ;;
    *)
      echo "refusing P0 v8: PHASE_S must be exactly 2, 10, or 60" >&2
      exit 2
      ;;
  esac
}

export STEP5D_P0_PROFILE_OVERRIDE="step5d_strict_rnn_no_contact_p0_v8"
export STEP5D_P0_CAPTURE_KEY_OVERRIDE="no_contact_p0_v8_capture"
export STEP5D_P0_CONFIRM_TOKEN_OVERRIDE="LIVE STEP5D STRICT RNN NO CONTACT P0 V8"
export STEP5D_P0_VERIFIER_OVERRIDE="${SCRIPT_DIR}/../tools/verify_step5d_no_contact_p0_v8.py"
case "${1:-}" in
  status|live-ready)
    exec "${BASE_WRAPPER}" status
    ;;
  capture-ready|capture-bridge)
    if [[ $# -ne 2 ]]; then
      usage
      exit 2
    fi
    phase="$(require_phase "$2")"
    export STEP5D_P0_PHASE_S="${phase}"
    exec "${BASE_WRAPPER}" "$1"
    ;;
  validate-run)
    if [[ $# -ne 3 ]]; then
      usage
      exit 2
    fi
    phase="$(require_phase "$3")"
    exec "${BASE_WRAPPER}" validate-run "$2" --phase-s "${phase}"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
