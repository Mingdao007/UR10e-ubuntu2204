#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
BASE_OPERATOR="${ROOT}/scripts/step4e-line-v1-operator.sh"
EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/step5c_joint_rnn_cycloid_v1.urp"

usage() {
  cat <<EOF
Usage:
  step5c-joint-rnn-operator.sh prep-long-checks
  STEP5C_CONFIRM='LIVE STEP5C JOINT RNN CONTACT RUN' step5c-joint-rnn-operator.sh contact-bridge

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - Contact cycloid joint-space speedj run.
  - Bridge profile: step5c_joint_rnn_cycloid_v1.
  - Force target: 5.0 N.
  - qdot cap: 0.15 rad/s.
  - Raw normal guard: 50 N, force norm guard: 60 N, torque guard: 3.0 Nm.
  - No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.
  - This wrapper never loads a program or presses Play.
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  prep-long-checks)
    STEP4E_VERSION=step5c_joint_rnn_cycloid_v1 "${BASE_OPERATOR}" prep-long-checks
    ;;
  contact-bridge)
    if [[ "${STEP5C_CONFIRM:-}" != "LIVE STEP5C JOINT RNN CONTACT RUN" ]]; then
      echo "refusing live Step5c joint-RNN bridge start: set STEP5C_CONFIRM='LIVE STEP5C JOINT RNN CONTACT RUN'"
      exit 40
    fi
    STEP4E_VERSION=step5c_joint_rnn_cycloid_v1 \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-50}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-60}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-3.0}" \
    STEP4E_NORMAL_FOLLOW_MODE="${STEP4E_NORMAL_FOLLOW_MODE:-filtered_live}" \
    STEP4E_NORMAL_FILTER_ALPHA="${STEP4E_NORMAL_FILTER_ALPHA:-0.35}" \
    STEP4E_NORMAL_MIN_FORCE_N="${STEP4E_NORMAL_MIN_FORCE_N:-2.0}" \
    STEP4E_MOTION_LIMIT_M_S="${STEP4E_MOTION_LIMIT_M_S:-0.004}" \
    STEP4E_TOTAL_LINEAR_LIMIT_M_S="${STEP4E_TOTAL_LINEAR_LIMIT_M_S:-0.006}" \
    STEP4E_NORMAL_VELOCITY_LIMIT_M_S="${STEP4E_NORMAL_VELOCITY_LIMIT_M_S:-0.003}" \
    STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-0.060}" \
    STEP5C_QDOT_LIMIT_RAD_S="${STEP5C_QDOT_LIMIT_RAD_S:-0.15}" \
      "${BASE_OPERATOR}" line-bridge
    ;;
  *)
    usage
    exit 2
    ;;
esac
