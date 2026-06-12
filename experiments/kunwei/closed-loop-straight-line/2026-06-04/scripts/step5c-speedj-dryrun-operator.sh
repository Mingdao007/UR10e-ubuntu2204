#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
BASE_OPERATOR="${ROOT}/scripts/step4e-line-v1-operator.sh"
EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/step5c_speedj_dryrun_v1.urp"

usage() {
  cat <<EOF
Usage:
  step5c-speedj-dryrun-operator.sh prep-long-checks
  STEP5C_CONFIRM='LIVE STEP5C SPEEDJ DRY RUN' step5c-speedj-dryrun-operator.sh joint-bridge

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - No-contact speedj dry-run.
  - Bridge profile: step5c_speedj_dryrun_v1.
  - qdot cap: 0.10 rad/s.
  - No contact search, no UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.
  - This wrapper never loads a program or presses Play.
EOF
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  prep-long-checks)
    STEP4E_VERSION=step5c_speedj_dryrun_v1 "${BASE_OPERATOR}" prep-long-checks
    ;;
  joint-bridge)
    if [[ "${STEP5C_CONFIRM:-}" != "LIVE STEP5C SPEEDJ DRY RUN" ]]; then
      echo "refusing live Step5c speedj dry-run bridge start: set STEP5C_CONFIRM='LIVE STEP5C SPEEDJ DRY RUN'"
      exit 40
    fi
    STEP4E_VERSION=step5c_speedj_dryrun_v1 \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-45}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-12}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-20}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-1.0}" \
    STEP4E_MOTION_LIMIT_M_S="${STEP4E_MOTION_LIMIT_M_S:-0.004}" \
    STEP4E_TOTAL_LINEAR_LIMIT_M_S="${STEP4E_TOTAL_LINEAR_LIMIT_M_S:-0.004}" \
    STEP4E_NORMAL_VELOCITY_LIMIT_M_S="${STEP4E_NORMAL_VELOCITY_LIMIT_M_S:-0.0}" \
    STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-0.0}" \
    STEP5C_QDOT_LIMIT_RAD_S="${STEP5C_QDOT_LIMIT_RAD_S:-0.10}" \
      "${BASE_OPERATOR}" line-bridge
    ;;
  *)
    usage
    exit 2
    ;;
esac
