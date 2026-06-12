#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
BASE_OPERATOR="${ROOT}/scripts/step4e-line-v1-operator.sh"
EXPECTED_PROGRAM="/programs/andyl/kunwei/step6/step6b_contact_eight_baseline_v1.urp"

usage() {
  cat <<EOF
Usage:
  step6b-contact-operator.sh prep-long-checks
  STEP6B_CONFIRM='LIVE STEP6B CONTACT RUN' step6b-contact-operator.sh contact-bridge

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - Contact motion.
  - Bridge profile: step6b_v1, path shape: eight.
  - Force target: 5.0 N.
  - Active path: Step6 five-point safe frame, 30 s.
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
    STEP4E_VERSION=step6b_v1 "${BASE_OPERATOR}" prep-long-checks
    ;;
  contact-bridge)
    if [[ "${STEP6B_CONFIRM:-}" != "LIVE STEP6B CONTACT RUN" ]]; then
      echo "refusing live Step6b bridge start: set STEP6B_CONFIRM='LIVE STEP6B CONTACT RUN'"
      exit 40
    fi
    STEP4E_VERSION=step6b_v1 \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-50}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-60}" \
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
