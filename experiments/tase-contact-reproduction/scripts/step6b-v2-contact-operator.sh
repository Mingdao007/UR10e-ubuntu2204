#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"
BASE_OPERATOR="${ROOT}/scripts/step4e-line-v1-operator.sh"
EXPECTED_PROGRAM="/programs/andyl/kunwei/step6/step6b_contact_eight_baseline_v2.urp"
TASE_PROTOCOL_DEFAULTS="$(python3 "${ROOT}/tools/tase_protocol_table.py" operator-env step6b-v2-contact)"
eval "${TASE_PROTOCOL_DEFAULTS}"

usage() {
  cat <<EOF
Usage:
  step6b-v2-contact-operator.sh prep-long-checks
  STEP6B_CONFIRM='LIVE STEP6B CONTACT RUN' step6b-v2-contact-operator.sh contact-bridge

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - Contact motion.
  - Bridge profile: step6b_v2, path shape: eight.
  - Force target: 5.0 N.
  - Active path: Step6 five-point safe frame, 30 s.
  - Bridge caps: 15 mm/s path, 15 mm/s total linear, 3 mm/s normal reserve, 0.060 rad/s attitude.
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
    STEP4E_VERSION=step6b_v2 \
    STEP4E_MOTION_LIMIT_M_S="${STEP4E_MOTION_LIMIT_M_S:-${TASE_STEP6B_MOTION_LIMIT_M_S}}" \
    STEP4E_TOTAL_LINEAR_LIMIT_M_S="${STEP4E_TOTAL_LINEAR_LIMIT_M_S:-${TASE_STEP6B_TOTAL_LINEAR_LIMIT_M_S}}" \
    STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-${TASE_STEP6B_ANGULAR_LIMIT_RAD_S}}" \
      "${BASE_OPERATOR}" prep-long-checks
    ;;
  contact-bridge)
    if [[ "${STEP6B_CONFIRM:-}" != "LIVE STEP6B CONTACT RUN" ]]; then
      echo "refusing live Step6b v2 bridge start: set STEP6B_CONFIRM='LIVE STEP6B CONTACT RUN'"
      exit 40
    fi
    STEP4E_VERSION=step6b_v2 \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-${TASE_STEP6B_BRIDGE_DURATION_S}}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-${TASE_STEP6B_MAX_NORMAL_FORCE_N}}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-${TASE_STEP6B_MAX_FORCE_NORM_N}}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-${TASE_STEP6B_MAX_TORQUE_NORM_NM}}" \
    STEP4E_MOTION_LIMIT_M_S="${STEP4E_MOTION_LIMIT_M_S:-${TASE_STEP6B_MOTION_LIMIT_M_S}}" \
    STEP4E_TOTAL_LINEAR_LIMIT_M_S="${STEP4E_TOTAL_LINEAR_LIMIT_M_S:-${TASE_STEP6B_TOTAL_LINEAR_LIMIT_M_S}}" \
    STEP4E_NORMAL_VELOCITY_LIMIT_M_S="${STEP4E_NORMAL_VELOCITY_LIMIT_M_S:-${TASE_STEP6B_NORMAL_VELOCITY_LIMIT_M_S}}" \
    STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-${TASE_STEP6B_ANGULAR_LIMIT_RAD_S}}" \
    STEP4E_NORMAL_FOLLOW_MODE="${STEP4E_NORMAL_FOLLOW_MODE:-filtered_live}" \
    STEP4E_NORMAL_FILTER_ALPHA="${STEP4E_NORMAL_FILTER_ALPHA:-${TASE_STEP6B_NORMAL_FILTER_ALPHA}}" \
    STEP4E_NORMAL_MIN_FORCE_N="${STEP4E_NORMAL_MIN_FORCE_N:-${TASE_STEP6B_NORMAL_MIN_FORCE_N}}" \
      "${BASE_OPERATOR}" line-bridge
    ;;
  *)
    usage
    exit 2
    ;;
esac
