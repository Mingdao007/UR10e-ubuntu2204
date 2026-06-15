#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
BASE_OPERATOR="${ROOT}/scripts/step4e-line-v1-operator.sh"
STEP5D_VERSION="${STEP5D_VERSION:-}"
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
  - Force target: 5.0 N, Step5/Step6 positive normal-load convention.
  - Stage 25.0: registers 37..42 are qd0..qd5 rad/s; TP executes speedj.
  - qdot cap: 0.30 rad/s.
  - Raw normal guard: 100 N, force norm guard: 100 N, torque guard: 3.0 Nm.
  - Stage 25.3 runs bridge deadband acquire with Cartesian registers 37..39.
  - Stage 25.3 keeps press recovery on low load and stops only outside the 40 N normal-load / 100 N force-norm hard envelope.
  - Stage 25.3 must hold filtered 2-15 N with force_norm <=25 N for 0.150 s before Stage 25.0 speedj starts.
  - No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.
  - This wrapper never loads a program or presses Play.
  - contact-bridge owns cached long checks; do not run prep-long-checks as a
    separate bridge-start step.
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  prep-long-checks)
    if [[ -z "${STEP5D_VERSION}" ]]; then
      echo "refusing Step5d prep: no current live-prep package after v11 incomplete run; set STEP5D_VERSION explicitly only for retained evidence replay"
      exit 40
    fi
    STEP4E_VERSION="${STEP5D_VERSION}" "${BASE_OPERATOR}" prep-long-checks
    ;;
  contact-bridge)
    if [[ -z "${STEP5D_VERSION}" ]]; then
      echo "refusing live Step5d bridge start: no current live-prep package after v11 incomplete run; make a v12/root-cause plan or set STEP5D_VERSION explicitly only for retained evidence replay"
      exit 40
    fi
    if [[ "${STEP5D_CONFIRM:-}" != "LIVE STEP5D STRICT RNN LIVEPREP" ]]; then
      echo "refusing live Step5d bridge start: set STEP5D_CONFIRM='LIVE STEP5D STRICT RNN LIVEPREP'"
      exit 40
    fi
    STEP4E_VERSION="${STEP5D_VERSION}" \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-100}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-100}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-3.0}" \
    STEP4E_NORMAL_FOLLOW_MODE="${STEP4E_NORMAL_FOLLOW_MODE:-filtered_live}" \
    STEP4E_NORMAL_FILTER_ALPHA="${STEP4E_NORMAL_FILTER_ALPHA:-0.35}" \
    STEP4E_NORMAL_MIN_FORCE_N="${STEP4E_NORMAL_MIN_FORCE_N:-2.0}" \
      "${BASE_OPERATOR}" line-bridge
    ;;
  *)
    usage
    exit 2
    ;;
esac
