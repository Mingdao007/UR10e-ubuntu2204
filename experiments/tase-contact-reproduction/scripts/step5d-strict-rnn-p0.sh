#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LIVEPREP_OPERATOR="${SCRIPT_DIR}/step5d-liveprep-operator.sh"
BRIDGE_OPERATOR="${SCRIPT_DIR}/bridge-line-operator.sh"
P0_VERIFIER="${ROOT}/tools/verify_step5d_no_contact_p0.py"
P0_PROFILE="step5d_strict_rnn_no_contact_p0_v1"
P0_CONFIRM_TOKEN="LIVE STEP5D STRICT RNN NO CONTACT P0"

usage() {
  cat <<EOF
Usage:
  step5d-strict-rnn-p0.sh status
  step5d-strict-rnn-p0.sh live-ready
  step5d-strict-rnn-p0.sh capture-ready
  STEP5D_P0_CONFIRM='LIVE STEP5D STRICT RNN NO CONTACT P0' step5d-strict-rnn-p0.sh capture-bridge
  step5d-strict-rnn-p0.sh validate-run RUN_DIR_OR_CSV [verifier args...]

Boundary:
  - status/live-ready only report current Step5d runtime readiness.
  - capture-ready reports the dedicated no-contact P0 capture profile readiness.
  - capture-bridge starts only the dedicated no-contact P0 bridge path after
    STEP5D_P0_CONFIRM is set exactly; it does not mark P0 passed.
  - validate-run only checks an already captured bridge_rtde_500hz.csv artifact.
  - This wrapper never loads a program, presses Play, zeroes/tares force sensing,
    writes TCP/payload, or changes the full live authorization gate.
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
  capture-ready)
    echo "Step5d strict RNN no-contact P0 capture readiness: no bridge is started by this command." >&2
    BRIDGE_PROFILE="${P0_PROFILE}" \
    BRIDGE_DURATION_S=3 \
    BRIDGE_BASELINE_S=1 \
    BRIDGE_REZERO_S=0.25 \
    BRIDGE_RTDE_HZ=500 \
    BRIDGE_SENSOR_STALE_S=0.10 \
    BRIDGE_SOCKET_TIMEOUT_S=0.0 \
    BRIDGE_TARGET_FORCE_N=1.0 \
    BRIDGE_FORCE_P_GAIN=0.001 \
    BRIDGE_FORCE_I_GAIN=0.00001 \
    BRIDGE_FORCE_DAMPING=7.0 \
    BRIDGE_INTEGRAL_LIMIT_N_S=1.0 \
    MAX_NORMAL_FORCE_N=2 \
    MAX_FORCE_NORM_N=5 \
    MAX_TORQUE_NORM_NM=3.0 \
    BRIDGE_NORMAL_FOLLOW_MODE=locked \
    BRIDGE_NORMAL_FILTER_ALPHA=0.55 \
    BRIDGE_NORMAL_MIN_FORCE_N=0.001 \
    BRIDGE_MOTION_LIMIT_M_S=0.004 \
    BRIDGE_TOTAL_LINEAR_LIMIT_M_S=0.004 \
    BRIDGE_NORMAL_VELOCITY_LIMIT_M_S=0.003 \
    BRIDGE_ANGULAR_LIMIT_RAD_S=0.015 \
    STEP5D_STAGE25_CONTROL_MODE=speedj_rnn_live \
      "${BRIDGE_OPERATOR}" live-ready
    echo "Teach Pendant target: /programs/andyl/kunwei/step5/step5d/${P0_PROFILE}.urp"
    ;;
  capture-bridge)
    if [[ "${STEP5D_P0_CONFIRM:-}" != "${P0_CONFIRM_TOKEN}" ]]; then
      echo "refusing no-contact P0 bridge start: set STEP5D_P0_CONFIRM='${P0_CONFIRM_TOKEN}'"
      exit 40
    fi
    tmp_log="$(mktemp)"
    cleanup() {
      rm -f "${tmp_log}"
    }
    trap cleanup EXIT
    BRIDGE_PROFILE="${P0_PROFILE}" \
    BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE=1 \
    BRIDGE_STAGE25_ONLY=1 \
    BRIDGE_DURATION_S=3 \
    BRIDGE_BASELINE_S=1 \
    BRIDGE_REZERO_S=0.25 \
    BRIDGE_RTDE_HZ=500 \
    BRIDGE_SENSOR_STALE_S=0.10 \
    BRIDGE_SOCKET_TIMEOUT_S=0.0 \
    BRIDGE_TARGET_FORCE_N=1.0 \
    BRIDGE_FORCE_P_GAIN=0.001 \
    BRIDGE_FORCE_I_GAIN=0.00001 \
    BRIDGE_FORCE_DAMPING=7.0 \
    BRIDGE_INTEGRAL_LIMIT_N_S=1.0 \
    MAX_NORMAL_FORCE_N=2 \
    MAX_FORCE_NORM_N=5 \
    MAX_TORQUE_NORM_NM=3.0 \
    BRIDGE_NORMAL_FOLLOW_MODE=locked \
    BRIDGE_NORMAL_FILTER_ALPHA=0.55 \
    BRIDGE_NORMAL_MIN_FORCE_N=0.001 \
    BRIDGE_MOTION_LIMIT_M_S=0.004 \
    BRIDGE_TOTAL_LINEAR_LIMIT_M_S=0.004 \
    BRIDGE_NORMAL_VELOCITY_LIMIT_M_S=0.003 \
    BRIDGE_ANGULAR_LIMIT_RAD_S=0.015 \
    STEP5D_STAGE25_CONTROL_MODE=speedj_rnn_live \
    STEP5D_PRELOAD_FILTERED_MIN_N=0.0 \
    STEP5D_PRELOAD_FILTERED_MAX_N=2.0 \
    STEP5D_PRELOAD_RAW_MIN_N=0.0 \
    STEP5D_PRELOAD_RAW_MAX_N=2.0 \
    STEP5D_PRELOAD_FORCE_NORM_MAX_N=5.0 \
    STEP5D_PRELOAD_HOLD_S=0.0 \
      "${BRIDGE_OPERATOR}" line-bridge-fast | tee "${tmp_log}"
    run_dir="$(python3 - "${tmp_log}" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
matches = re.findall(r"^\[operator\] bridge output:\s*(\S+)\s*$", text, flags=re.M)
if matches:
    print(matches[-1])
PY
)"
    if [[ -z "${run_dir}" ]]; then
      echo "refusing to validate no-contact P0: bridge output run dir was not found"
      exit 24
    fi
    python3 "${P0_VERIFIER}" "${run_dir}" --output "${run_dir}/step5d_no_contact_p0_summary.json"
    echo "[operator] Step5d no-contact P0 summary: ${run_dir}/step5d_no_contact_p0_summary.json"
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
