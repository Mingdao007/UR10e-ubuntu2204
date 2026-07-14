#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
BASE_WRAPPER="${SCRIPT_DIR}/step5d-strict-rnn-p0.sh"
DECISION_TOOL="${SCRIPT_DIR}/../tools/ur10e_decision_manifest.py"

usage() {
  cat <<'EOF'
Usage:
  step5d-strict-rnn-p0-v8.sh status
  step5d-strict-rnn-p0-v8.sh capture-ready
  STEP5D_P0_CONFIRM='LIVE STEP5D STRICT RNN NO CONTACT P0 V8' \
    step5d-strict-rnn-p0-v8.sh capture-bridge
  step5d-strict-rnn-p0-v8.sh validate-run RUN_DIR_OR_CSV

The direct canary duration is resolved from the frozen current-stage config.
Short offline diagnostics cannot replace this real no-contact run.

This wrapper does not upload a package, load or Play TP, zero force sensing,
write TCP/payload, or authorize contact/v30 live motion.
EOF
}

configured_duration() {
  python3 "${DECISION_TOOL}" p0-duration
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
    if [[ $# -ne 1 ]]; then
      usage
      exit 2
    fi
    export STEP5D_P0_PHASE_S="$(configured_duration)"
    exec "${BASE_WRAPPER}" "$1"
    ;;
  validate-run)
    if [[ $# -ne 2 ]]; then
      usage
      exit 2
    fi
    exec "${BASE_WRAPPER}" validate-run "$2" --phase-s "$(configured_duration)"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
