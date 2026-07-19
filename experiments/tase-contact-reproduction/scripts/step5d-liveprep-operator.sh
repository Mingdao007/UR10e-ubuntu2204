#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BRIDGE_OPERATOR="${SCRIPT_DIR}/bridge-line-operator.sh"
READBACK_GATE="${ROOT}/tools/verify_step5d_current_binding.py"
RUNTIME_INTERFACE="${ROOT}/tools/step5d_runtime_interface.py"
TASE_PROTOCOL_DEFAULTS="$(python3 "${ROOT}/tools/tase_protocol_table.py" operator-env step5d-liveprep)"
eval "${TASE_PROTOCOL_DEFAULTS}"

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
if program.startswith(("step5d_strict_rnn_liveprep_", "step5d_strict_rnn_ablation_")):
    print(program)
PY
}

STEP5D_VERSION="${STEP5D_VERSION:-$(current_step5d_version)}"
if [[ -z "${STEP5D_VERSION}" ]]; then
  EXPECTED_PROGRAM="<no current Step5d TP package>"
elif [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v1" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v2" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v3" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v4" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v5" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v6" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v7" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v8" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v9" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v10" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v11" ]]; then
  EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/step5d/${STEP5D_VERSION}.urp"
else
  EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/${STEP5D_VERSION}.urp"
fi
if [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v27" || "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v28" || "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v29" ]]; then
  STEP5D_DEFAULT_MAX_NORMAL_FORCE_N="${STEP5D_DEFAULT_MAX_NORMAL_FORCE_N:-${TASE_STEP5D_MAX_NORMAL_FORCE_N}}"
  STEP5D_DEFAULT_MAX_FORCE_NORM_N="${STEP5D_DEFAULT_MAX_FORCE_NORM_N:-${TASE_STEP5D_MAX_FORCE_NORM_N}}"
  STEP5D_DEFAULT_MAX_TORQUE_NORM_NM="${STEP5D_DEFAULT_MAX_TORQUE_NORM_NM:-${TASE_STEP5D_MAX_TORQUE_NORM_NM}}"
elif [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v24" || "${STEP5D_VERSION}" == step5d_strict_rnn_ablation_v* ]]; then
  STEP5D_DEFAULT_MAX_NORMAL_FORCE_N="${STEP5D_DEFAULT_MAX_NORMAL_FORCE_N:-25}"
  STEP5D_DEFAULT_MAX_FORCE_NORM_N="${STEP5D_DEFAULT_MAX_FORCE_NORM_N:-25}"
  STEP5D_DEFAULT_MAX_TORQUE_NORM_NM="${STEP5D_DEFAULT_MAX_TORQUE_NORM_NM:-4.0}"
else
  STEP5D_DEFAULT_MAX_NORMAL_FORCE_N="${STEP5D_DEFAULT_MAX_NORMAL_FORCE_N:-100}"
  STEP5D_DEFAULT_MAX_FORCE_NORM_N="${STEP5D_DEFAULT_MAX_FORCE_NORM_N:-100}"
  STEP5D_DEFAULT_MAX_TORQUE_NORM_NM="${STEP5D_DEFAULT_MAX_TORQUE_NORM_NM:-4.0}"
fi
if [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v25" ]]; then
  STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N:-10.5}"
  STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N:-12.8}"
  STEP5D_DEFAULT_PRELOAD_RAW_MIN_N="${STEP5D_DEFAULT_PRELOAD_RAW_MIN_N:-9.5}"
  STEP5D_DEFAULT_PRELOAD_RAW_MAX_N="${STEP5D_DEFAULT_PRELOAD_RAW_MAX_N:-13.5}"
  STEP5D_DEFAULT_PRELOAD_FORCE_NORM_MAX_N="${STEP5D_DEFAULT_PRELOAD_FORCE_NORM_MAX_N:-25.0}"
elif [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v26" ]]; then
  STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N:-7.0}"
  STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N:-18.0}"
  STEP5D_DEFAULT_PRELOAD_RAW_MIN_N="${STEP5D_DEFAULT_PRELOAD_RAW_MIN_N:-5.0}"
  STEP5D_DEFAULT_PRELOAD_RAW_MAX_N="${STEP5D_DEFAULT_PRELOAD_RAW_MAX_N:-20.0}"
  STEP5D_DEFAULT_PRELOAD_FORCE_NORM_MAX_N="${STEP5D_DEFAULT_PRELOAD_FORCE_NORM_MAX_N:-25.0}"
elif [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v27" || "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v28" || "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v29" ]]; then
  STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N:-${TASE_STEP5D_PRELOAD_FILTERED_MIN_N}}"
  STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N:-${TASE_STEP5D_PRELOAD_FILTERED_MAX_N}}"
  STEP5D_DEFAULT_PRELOAD_RAW_MIN_N="${STEP5D_DEFAULT_PRELOAD_RAW_MIN_N:-${TASE_STEP5D_PRELOAD_RAW_MIN_N}}"
  STEP5D_DEFAULT_PRELOAD_RAW_MAX_N="${STEP5D_DEFAULT_PRELOAD_RAW_MAX_N:-${TASE_STEP5D_PRELOAD_RAW_MAX_N}}"
  STEP5D_DEFAULT_PRELOAD_FORCE_NORM_MAX_N="${STEP5D_DEFAULT_PRELOAD_FORCE_NORM_MAX_N:-${TASE_STEP5D_PRELOAD_FORCE_NORM_MAX_N}}"
  STEP5D_DEFAULT_REZERO_S="${STEP5D_DEFAULT_REZERO_S:-${TASE_STEP5D_REZERO_S}}"
else
  STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N:-7.5}"
  STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N:-14.0}"
  STEP5D_DEFAULT_PRELOAD_RAW_MIN_N="${STEP5D_DEFAULT_PRELOAD_RAW_MIN_N:-7.0}"
  STEP5D_DEFAULT_PRELOAD_RAW_MAX_N="${STEP5D_DEFAULT_PRELOAD_RAW_MAX_N:-15.0}"
  STEP5D_DEFAULT_PRELOAD_FORCE_NORM_MAX_N="${STEP5D_DEFAULT_PRELOAD_FORCE_NORM_MAX_N:-25.0}"
fi
STEP5D_DEFAULT_REZERO_S="${STEP5D_DEFAULT_REZERO_S:-1.0}"
if [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v25" ]]; then
  STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S="${STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S:-0.150}"
  STEP5D_STAGE25_CONTROL_MODE_DEFAULT="${STEP5D_STAGE25_CONTROL_MODE_DEFAULT:-speedl_cartesian_oracle}"
elif [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v26" ]]; then
  STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S="${STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S:-0.015}"
  STEP5D_STAGE25_CONTROL_MODE_DEFAULT="${STEP5D_STAGE25_CONTROL_MODE_DEFAULT:-speedl_cartesian_oracle}"
elif [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v27" || "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v28" ]]; then
  STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S="${STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S:-${TASE_STEP5D_ANGULAR_LIMIT_RAD_S}}"
  STEP5D_STAGE25_CONTROL_MODE_DEFAULT="${STEP5D_STAGE25_CONTROL_MODE_DEFAULT:-${TASE_STEP5D_STAGE25_CONTROL_MODE_DEFAULT}}"
elif [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v29" ]]; then
  STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S="${STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S:-${TASE_STEP5D_ANGULAR_LIMIT_RAD_S}}"
  STEP5D_STAGE25_CONTROL_MODE_DEFAULT="${STEP5D_STAGE25_CONTROL_MODE_DEFAULT:-speedj_rnn_live}"
  STEP5D_EPSILON="${STEP5D_EPSILON:-0.010}"
  STEP5D_SIGR_EXPONENT_R="${STEP5D_SIGR_EXPONENT_R:-0.800}"
  STEP5D_RNN_INNER_ITERATIONS="${STEP5D_RNN_INNER_ITERATIONS:-1024}"
  STEP5D_RNN_BACKEND="${STEP5D_RNN_BACKEND:-cupy}"
  STEP5D_QDOT_LIMIT_RAD_S="${STEP5D_QDOT_LIMIT_RAD_S:-0.050}"
else
  STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S="${STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S:-0.015}"
  STEP5D_STAGE25_CONTROL_MODE_DEFAULT="${STEP5D_STAGE25_CONTROL_MODE_DEFAULT:-speedj_rnn_live}"
fi

usage() {
  cat <<EOF
Usage:
  step5d-liveprep-operator.sh prep-long-checks  # explicit diagnose-bench snapshot only
  step5d-liveprep-operator.sh live-ready        # read-only binding/profile status
  STEP5D_CONFIRM='LIVE STEP5D STRICT RNN LIVEPREP' step5d-liveprep-operator.sh contact-bridge

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - Contact-capable Step5d TP package, not a completed reproduction claim.
  - Bridge profile: ${STEP5D_VERSION}.
  - Force target defaults to 12.0 N, Step5/Step6 positive normal-load convention.
  - v25/v26/v27/v28/v29 Stage 25.0 uses register 47 layout tag: 523 Cartesian speedl vx/vy/vz/wx/wy/wz, 524 joint speedj qd0..qd5.
  - v25/v26/v27/v28 first live mode defaults to STEP5D_STAGE25_CONTROL_MODE=speedl_cartesian_oracle; v29 live authorization accepts only the exact speedj_rnn_live/cupy/1024/epsilon=0.010/r=0.800/qdot=0.050 profile.
  - v24 and older Stage 25.0: registers 37..42 are qd0..qd5 rad/s; TP executes speedj.
  - qdot cap: v12+ live-prep packages default to 0.05 rad/s; retained evidence packages may differ.
  - Raw normal/force guards: current v24/v25/v26 defaults to 25/25 N, v27/v28/v29 defaults to Step5b envelope 50/60 N with torque guard 3.0 Nm; retained v18-v23 evidence packages used 100/100/4.0.
  - Stage 25.3 runs bridge deadband acquire with Cartesian registers 37..39.
  - v21+ Stage 25.3 can receive preload min/max/hold/timeout from STEP5D_PRELOAD_* at bridge time.
  - Stage 25.3 keeps press recovery on low load and v24 stops outside the 20 N normal-load / 25 N force-norm recovery envelope.
  - v24 default preload gate is filtered 7.5-14 N, raw-sanity 7-15 N, and force_norm <=25 N for 0.100 s before Stage 25.95 verifies near-zero qdot registers and Stage 25.0 speedj starts.
  - v25 default preload gate is filtered 10.5-12.8 N, raw-sanity 9.5-13.5 N, and force_norm <=25 N for 0.100 s before Stage 25.95 verifies registers 37..47 clear.
  - v26 default preload tube is filtered 7-18 N, raw-sanity 5-20 N, and force_norm <=25 N for 0.100 s before Stage 25.95 verifies registers 37..47 clear.
  - v27/v28/v29 default preload tube is filtered 5-22 N, raw-sanity 3-25 N, and force_norm <=35 N for 0.100 s before Stage 25.95 verifies registers 37..47 clear.
  - Stage 22/24 pre-contact search posture is gravity-down: TCP +Z targets base -Z with rotvec [pi,0,0].
  - Stage 22 entry movel is 1.5x faster than v18: 0.060 m/s at 0.090 m/s^2.
  - Stage 24 far search is 1.5x faster than v18: 0.0225 m/s down; near search remains 0.0025 m/s.
  - Stage 25.0 v24 computes/logs the strict RNN qdot path, but low-load/no-contact writes zero qdot instead of executing active_reacquire_solver qdot and adds post-RNN tracking reversal detection.
  - No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.
  - This wrapper never loads a program or presses Play.
  - prep-long-checks is optional diagnostics; it does not authorize or block contact-bridge.
  - contact-bridge relies on exact binding and the bridge's actual RTDE/sensor/prewarm ready sentinel.
  - Review v3 cannot bypass exact profile, readback, Dashboard, RTDE, runtime,
    or explicit user live/contact authorization gates.
EOF
  if [[ -n "${STEP5D_VERSION}" ]]; then
    python3 "${RUNTIME_INTERFACE}" --root "${ROOT}" --program "${STEP5D_VERSION}" live-ready 2>/dev/null || true
  fi
}

require_current_stage_readback_gate() {
  python3 "${READBACK_GATE}" --root "${ROOT}" --program "${STEP5D_VERSION}"
}

selected_stage25_control_mode() {
  printf "%s" "${STEP5D_STAGE25_CONTROL_MODE:-${STEP5D_STAGE25_CONTROL_MODE_DEFAULT}}"
}

require_live_bridge_authorization_gate() {
  python3 "${READBACK_GATE}" \
    --root "${ROOT}" \
    --program "${STEP5D_VERSION}" \
    --stage25-control-mode "$(selected_stage25_control_mode)" \
    --rnn-backend "${STEP5D_RNN_BACKEND:-numpy}" \
    --rnn-inner-iterations "${STEP5D_RNN_INNER_ITERATIONS:-1}" \
    --epsilon "${STEP5D_EPSILON:-0.022}" \
    --sigr-exponent-r "${STEP5D_SIGR_EXPONENT_R:-1.0}" \
    --qdot-cap-rad-s "${STEP5D_QDOT_LIMIT_RAD_S:-0.050}" \
    --require-live-bridge-authorization
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  prep-long-checks)
    if [[ -z "${STEP5D_VERSION}" ]]; then
      echo "refusing Step5d prep: set STEP5D_VERSION to the controller-readback-verified Step5d package before running diagnostics"
      exit 40
    fi
    BRIDGE_PROFILE="${STEP5D_VERSION}" "${BRIDGE_OPERATOR}" prep-long-checks
    ;;
  live-ready|status)
    if [[ -z "${STEP5D_VERSION}" ]]; then
      echo "refusing Step5d live-ready: set STEP5D_VERSION or current_stage to a Step5d package"
      exit 40
    fi
    python3 "${RUNTIME_INTERFACE}" --root "${ROOT}" --program "${STEP5D_VERSION}" live-ready
    ;;
  contact-bridge)
    if [[ -z "${STEP5D_VERSION}" ]]; then
      echo "refusing live Step5d bridge start: set STEP5D_VERSION to the controller-readback-verified exact runtime profile and provide explicit live confirmation"
      exit 40
    fi
    require_current_stage_readback_gate
    if [[ "${STEP5D_CONFIRM:-}" != "LIVE STEP5D STRICT RNN LIVEPREP" ]]; then
      echo "refusing live Step5d bridge start: set STEP5D_CONFIRM='LIVE STEP5D STRICT RNN LIVEPREP'"
      exit 40
    fi
    require_live_bridge_authorization_gate
    BRIDGE_PROFILE="${STEP5D_VERSION}" \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-${STEP5D_DURATION_S:-${TASE_STEP5D_BRIDGE_DURATION_S}}}" \
    STEP5D_REZERO_S="${STEP5D_REZERO_S:-${STEP5D_DEFAULT_REZERO_S:-1.0}}" \
    WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-20}" \
    AUTOWATCH_WAIT_FOR_PLAY_S="${AUTOWATCH_WAIT_FOR_PLAY_S:-20}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-${STEP5D_MAX_NORMAL_FORCE_N:-${STEP5D_DEFAULT_MAX_NORMAL_FORCE_N}}}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-${STEP5D_MAX_FORCE_NORM_N:-${STEP5D_DEFAULT_MAX_FORCE_NORM_N}}}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-${STEP5D_MAX_TORQUE_NORM_NM:-${STEP5D_DEFAULT_MAX_TORQUE_NORM_NM}}}" \
    BRIDGE_TARGET_FORCE_N="${BRIDGE_TARGET_FORCE_N:-${STEP5D_TARGET_FORCE_N:-${TASE_STEP5D_TARGET_FORCE_N}}}" \
    STEP4E_TARGET_FORCE_N="${STEP4E_TARGET_FORCE_N:-${STEP5D_TARGET_FORCE_N:-${TASE_STEP5D_TARGET_FORCE_N}}}" \
    BRIDGE_NORMAL_FOLLOW_MODE="${BRIDGE_NORMAL_FOLLOW_MODE:-${STEP5D_NORMAL_FOLLOW_MODE:-filtered_live}}" \
    BRIDGE_NORMAL_FILTER_ALPHA="${BRIDGE_NORMAL_FILTER_ALPHA:-${STEP5D_NORMAL_FILTER_ALPHA:-${TASE_STEP5D_NORMAL_FILTER_ALPHA}}}" \
    BRIDGE_NORMAL_MIN_FORCE_N="${BRIDGE_NORMAL_MIN_FORCE_N:-${STEP5D_NORMAL_MIN_FORCE_N:-${TASE_STEP5D_NORMAL_MIN_FORCE_N}}}" \
    BRIDGE_FORCE_P_GAIN="${BRIDGE_FORCE_P_GAIN:-${STEP5D_FORCE_P_GAIN:-${TASE_STEP5D_FORCE_P_GAIN}}}" \
    BRIDGE_FORCE_I_GAIN="${BRIDGE_FORCE_I_GAIN:-${STEP5D_FORCE_I_GAIN:-${TASE_STEP5D_FORCE_I_GAIN}}}" \
    BRIDGE_FORCE_DAMPING="${BRIDGE_FORCE_DAMPING:-${STEP5D_FORCE_DAMPING:-${TASE_STEP5D_FORCE_DAMPING}}}" \
    BRIDGE_NORMAL_VELOCITY_LIMIT_M_S="${BRIDGE_NORMAL_VELOCITY_LIMIT_M_S:-${STEP5D_NORMAL_VELOCITY_LIMIT_M_S:-${TASE_STEP5D_NORMAL_VELOCITY_LIMIT_M_S}}}" \
    BRIDGE_TOTAL_LINEAR_LIMIT_M_S="${BRIDGE_TOTAL_LINEAR_LIMIT_M_S:-${STEP5D_TOTAL_LINEAR_LIMIT_M_S:-${TASE_STEP5D_TOTAL_LINEAR_LIMIT_M_S}}}" \
    BRIDGE_ANGULAR_LIMIT_RAD_S="${BRIDGE_ANGULAR_LIMIT_RAD_S:-${STEP5D_ANGULAR_LIMIT_RAD_S:-${STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S}}}" \
    BRIDGE_INTEGRAL_LIMIT_N_S="${BRIDGE_INTEGRAL_LIMIT_N_S:-${STEP5D_INTEGRAL_LIMIT_N_S:-${TASE_STEP5D_INTEGRAL_LIMIT_N_S}}}" \
    STEP5D_STAGE25_CONTROL_MODE="${STEP5D_STAGE25_CONTROL_MODE:-${STEP5D_STAGE25_CONTROL_MODE_DEFAULT}}" \
    STEP5D_EPSILON="${STEP5D_EPSILON:-}" \
    STEP5D_SIGR_EXPONENT_R="${STEP5D_SIGR_EXPONENT_R:-}" \
    STEP5D_RNN_INNER_ITERATIONS="${STEP5D_RNN_INNER_ITERATIONS:-}" \
    STEP5D_RNN_BACKEND="${STEP5D_RNN_BACKEND:-}" \
    STEP5D_QDOT_LIMIT_RAD_S="${STEP5D_QDOT_LIMIT_RAD_S:-}" \
    STEP5D_PRELOAD_FILTERED_MIN_N="${STEP5D_PRELOAD_FILTERED_MIN_N:-${STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N}}" \
    STEP5D_PRELOAD_FILTERED_MAX_N="${STEP5D_PRELOAD_FILTERED_MAX_N:-${STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N}}" \
    STEP5D_PRELOAD_RAW_MIN_N="${STEP5D_PRELOAD_RAW_MIN_N:-${STEP5D_DEFAULT_PRELOAD_RAW_MIN_N}}" \
    STEP5D_PRELOAD_RAW_MAX_N="${STEP5D_PRELOAD_RAW_MAX_N:-${STEP5D_DEFAULT_PRELOAD_RAW_MAX_N}}" \
    STEP5D_PRELOAD_FORCE_NORM_MAX_N="${STEP5D_PRELOAD_FORCE_NORM_MAX_N:-${STEP5D_DEFAULT_PRELOAD_FORCE_NORM_MAX_N}}" \
    STEP5D_PRELOAD_HOLD_S="${STEP5D_PRELOAD_HOLD_S:-${TASE_STEP5D_PRELOAD_HOLD_S}}" \
      "${BRIDGE_OPERATOR}" line-bridge-fast
    ;;
  *)
    usage
    exit 2
    ;;
esac
