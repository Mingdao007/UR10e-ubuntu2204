#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BASE_WRAPPER="${SCRIPT_DIR}/step5d-strict-rnn-p0.sh"
PROFILE="step5d_strict_rnn_no_contact_p0_v9"

usage() {
  cat <<'EOF'
Usage:
  step5d-strict-rnn-p0-v9.sh status
  step5d-strict-rnn-p0-v9.sh generate-deliver
  step5d-strict-rnn-p0-v9.sh validate-run RUN_DIR_OR_CSV
  step5d-strict-rnn-p0-v9.sh capture-ready
  STEP5D_P0_CONFIRM='LIVE STEP5D STRICT RNN NO CONTACT P0 V9' \
    step5d-strict-rnn-p0-v9.sh capture-bridge

P0 v9 is a 60 s tangential free-space canary: 0..2 mm, 20 s period,
three cycles, zero commanded normal motion, and weak posture hold only.
generate-deliver always uploads the generated TP triplet and performs a fresh
controller read-back. It never loads, Plays, starts a bridge, or moves the robot.
capture commands fail closed until controller read-back and live authorization
are recorded in the canonical current-stage state.
EOF
}

configured_duration() {
  python3 - "${ROOT}/config/step5_stage_table.json" "${PROFILE}" <<'PY'
import json
import sys
table = json.load(open(sys.argv[1], encoding="utf-8"))
row = next(item for item in table["stages"] if item.get("id") == sys.argv[2])
print(float(row["duration_s"]))
PY
}

export STEP5D_P0_PROFILE_OVERRIDE="${PROFILE}"
export STEP5D_P0_CAPTURE_KEY_OVERRIDE="no_contact_p0_v9_capture"
export STEP5D_P0_CONFIRM_TOKEN_OVERRIDE="LIVE STEP5D STRICT RNN NO CONTACT P0 V9"
export STEP5D_P0_VERIFIER_OVERRIDE="${ROOT}/tools/verify_step5d_no_contact_p0_v9.py"

case "${1:-}" in
  status)
    exec python3 - "${ROOT}/config/current_stage.json" "${PROFILE}" <<'PY'
import json
import sys

current = json.load(open(sys.argv[1], encoding="utf-8"))
profile = sys.argv[2]
candidate = current.get("p0_v9_candidate") or {}
capture = (current.get("bridge_trigger") or {}).get("no_contact_p0_v9_capture") or {}
if candidate.get("profile") != profile or capture.get("profile") != profile:
    raise SystemExit("P0 v9 status refuses inconsistent current-stage candidate/capture binding")
print(f"profile={profile}")
print(f"state={candidate.get('state')}")
print(f"current={str(bool(candidate.get('current'))).lower()}")
print("rnn_backend=cupy rnn_inner_iterations=512 normal_motion_policy=normal_zero")
print(f"local_package_verified={str(bool((candidate.get('claim_boundary') or {}).get('local_package_verified'))).lower()}")
print(f"controller_uploaded={str(bool(capture.get('controller_uploaded'))).lower()}")
print(f"controller_readback_verified={str(bool(capture.get('controller_readback_verified'))).lower()}")
print(f"capture_authorized={str(bool(capture.get('capture_authorized'))).lower()}")
print("live_execution=not_run")
PY
    ;;
  live-ready)
    exec "${BASE_WRAPPER}" status
    ;;
  generate-deliver|generate-local)
    if [[ $# -ne 1 ]]; then
      usage
      exit 2
    fi
    python3 "${ROOT}/tools/build_step5d_liveprep.py" \
      --program "${PROFILE}" \
      --output-dir "${ROOT}/programs/step5/step5d" \
      --local-only
    exec python3 "${ROOT}/tools/upload_ur_tp_package.py" "${PROFILE}" \
      --local-dir "${ROOT}/programs/step5/step5d" \
      --force-upload-readback
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
