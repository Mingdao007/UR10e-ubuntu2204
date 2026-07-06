#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LIVEPREP_OPERATOR="${SCRIPT_DIR}/step5d-liveprep-operator.sh"
P0_VERIFIER="${ROOT}/tools/verify_step5d_no_contact_p0.py"

usage() {
  cat <<EOF
Usage:
  step5d-strict-rnn-p0.sh status
  step5d-strict-rnn-p0.sh live-ready
  step5d-strict-rnn-p0.sh validate-run RUN_DIR_OR_CSV [verifier args...]

Boundary:
  - status/live-ready only report current Step5d runtime readiness.
  - validate-run only checks an already captured bridge_rtde_500hz.csv artifact.
  - This wrapper does not start a live run, load a program, press Play, zero/tare
    force sensing, or change the current live authorization gate.
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  status|live-ready)
    echo "Step5d strict RNN P0 status only: no live run is started and no-contact P0 is not marked passed by this command." >&2
    STEP5D_STAGE25_CONTROL_MODE=speedj_rnn_live "${LIVEPREP_OPERATOR}" live-ready
    ;;
  validate-run)
    if [[ $# -lt 2 ]]; then
      echo "usage error: validate-run requires RUN_DIR_OR_CSV"
      exit 2
    fi
    run_path="$2"
    shift 2
    python3 "${P0_VERIFIER}" "${run_path}" "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
