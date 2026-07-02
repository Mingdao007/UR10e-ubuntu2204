#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BRIDGE_OPERATOR="${SCRIPT_DIR}/bridge-line-operator.sh"
READBACK_GATE="${ROOT}/tools/verify_current_stage_readback.py"

current_step5d_version() {
  python3 - "${ROOT}/config/current_stage.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    current = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
program = current.get("program") or current.get("current_stage_id") or ""
if program.startswith("step5d_strict_rnn_liveprep_"):
    print(program)
PY
}

STEP5D_VERSION="${STEP5D_VERSION:-$(current_step5d_version)}"
if [[ -z "${STEP5D_VERSION}" ]]; then
  EXPECTED_PROGRAM="<no current Step5d live-prep package>"
elif [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v1" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v2" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v3" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v4" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v5" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v6" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v7" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v8" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v9" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v10" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v11" ]]; then
  EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/step5d/${STEP5D_VERSION}.urp"
else
  EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/${STEP5D_VERSION}.urp"
fi

usage() {
  cat <<EOF
Usage:
  step5d-liveprep-operator.sh prep-long-checks  # manual diagnostics only
  STEP5D_CONFIRM='LIVE STEP5D STRICT RNN LIVEPREP' step5d-liveprep-operator.sh contact-bridge

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - Contact-capable live-prep package, not a completed reproduction claim.
  - Bridge profile: ${STEP5D_VERSION}.
  - Force target: 12.0 N, Step5/Step6 positive normal-load convention.
  - Stage 25.0: registers 37..42 are qd0..qd5 rad/s; TP executes speedj.
  - qdot cap: v12/v13/v14/v15/v15a/v16/v17/v18/v19/v20 default to 0.05 rad/s; retained evidence packages may differ.
  - Raw normal guard: v18/v19/v20 defaults to 100 N, force norm guard 100 N, torque guard 4.0 Nm.
  - Stage 25.3 runs bridge deadband acquire with Cartesian registers 37..39.
  - Stage 25.3 keeps press recovery on low load and stops outside the 40 N normal-load / 100 N force-norm recovery envelope.
  - Stage 25.3 must hold filtered 8-13 N, raw-sanity 7.5-14 N, and force_norm <=25 N for 0.100 s before Stage 25.0 speedj starts.
  - Stage 22/24 pre-contact search posture is gravity-down: TCP +Z targets base -Z with rotvec [pi,0,0].
  - Stage 22 entry movel is 1.5x faster than v18: 0.060 m/s at 0.090 m/s^2.
  - Stage 24 far search is 1.5x faster than v18: 0.0225 m/s down; near search remains 0.0025 m/s.
  - Stage 25.0 v20 freezes path_time during cage-primary active reacquire/no-contact diagnostics, resets outer-loop state on low-load active reacquire, and caps reacquire predicted TCP speed at 0.035 m/s before the 0.050 m/s hard stop.
  - No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.
  - This wrapper never loads a program or presses Play.
  - contact-bridge requires a fresh cached long-check result; refresh it during
    prep-long-checks, then the live trigger runs only short checks.
EOF
}

require_current_stage_readback_gate() {
  python3 "${READBACK_GATE}" --root "${ROOT}" --program "${STEP5D_VERSION}"
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  prep-long-checks)
    if [[ -z "${STEP5D_VERSION}" ]]; then
      echo "refusing Step5d prep: set STEP5D_VERSION to the controller-readback-verified live-prep package before running diagnostics"
      exit 40
    fi
    BRIDGE_PROFILE="${STEP5D_VERSION}" "${BRIDGE_OPERATOR}" prep-long-checks
    ;;
  contact-bridge)
    if [[ -z "${STEP5D_VERSION}" ]]; then
      echo "refusing live Step5d bridge start: set STEP5D_VERSION to the controller-readback-verified live-prep package and provide explicit live confirmation"
      exit 40
    fi
    require_current_stage_readback_gate
    if [[ "${STEP5D_CONFIRM:-}" != "LIVE STEP5D STRICT RNN LIVEPREP" ]]; then
      echo "refusing live Step5d bridge start: set STEP5D_CONFIRM='LIVE STEP5D STRICT RNN LIVEPREP'"
      exit 40
    fi
    BRIDGE_PROFILE="${STEP5D_VERSION}" \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}" \
    WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-10}" \
    AUTOWATCH_WAIT_FOR_PLAY_S="${AUTOWATCH_WAIT_FOR_PLAY_S:-10}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-100}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-100}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-4.0}" \
    BRIDGE_TARGET_FORCE_N="${BRIDGE_TARGET_FORCE_N:-${STEP4E_TARGET_FORCE_N:-12.0}}" \
    STEP4E_TARGET_FORCE_N="${STEP4E_TARGET_FORCE_N:-12.0}" \
    BRIDGE_NORMAL_FOLLOW_MODE="${BRIDGE_NORMAL_FOLLOW_MODE:-${STEP4E_NORMAL_FOLLOW_MODE:-filtered_live}}" \
    BRIDGE_NORMAL_FILTER_ALPHA="${BRIDGE_NORMAL_FILTER_ALPHA:-${STEP4E_NORMAL_FILTER_ALPHA:-0.55}}" \
    BRIDGE_NORMAL_MIN_FORCE_N="${BRIDGE_NORMAL_MIN_FORCE_N:-${STEP4E_NORMAL_MIN_FORCE_N:-2.0}}" \
    BRIDGE_FORCE_P_GAIN="${BRIDGE_FORCE_P_GAIN:-${STEP4E_FORCE_P_GAIN:-0.0010}}" \
    BRIDGE_FORCE_I_GAIN="${BRIDGE_FORCE_I_GAIN:-${STEP4E_FORCE_I_GAIN:-0.00001}}" \
    BRIDGE_FORCE_DAMPING="${BRIDGE_FORCE_DAMPING:-${STEP4E_FORCE_DAMPING:-7.0}}" \
    BRIDGE_NORMAL_VELOCITY_LIMIT_M_S="${BRIDGE_NORMAL_VELOCITY_LIMIT_M_S:-${STEP4E_NORMAL_VELOCITY_LIMIT_M_S:-0.0100}}" \
    BRIDGE_TOTAL_LINEAR_LIMIT_M_S="${BRIDGE_TOTAL_LINEAR_LIMIT_M_S:-${STEP4E_TOTAL_LINEAR_LIMIT_M_S:-0.0040}}" \
    BRIDGE_ANGULAR_LIMIT_RAD_S="${BRIDGE_ANGULAR_LIMIT_RAD_S:-${STEP4E_ANGULAR_LIMIT_RAD_S:-0.150}}" \
    BRIDGE_INTEGRAL_LIMIT_N_S="${BRIDGE_INTEGRAL_LIMIT_N_S:-${STEP4E_INTEGRAL_LIMIT_N_S:-1.0}}" \
      "${BRIDGE_OPERATOR}" line-bridge-fast
    ;;
  *)
    usage
    exit 2
    ;;
esac
