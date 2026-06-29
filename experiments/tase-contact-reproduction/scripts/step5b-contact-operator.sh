#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"
BASE_OPERATOR="${ROOT}/scripts/step4e-line-v1-operator.sh"
EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/step5b_contact_cycloid_baseline_v1.urp"

usage() {
  cat <<EOF
Usage:
  step5b-contact-operator.sh prep-long-checks
  STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN' step5b-contact-operator.sh contact-bridge
  STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN' step5b-contact-operator.sh guarded-15n-trial

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - Contact motion.
  - Bridge profile: step5b_v1.
  - Force target: 5.0 N default; guarded-15n-trial uses exact 15.0 N.
  - Raw normal guard: 50 N, force norm guard: 60 N, torque guard: 3.0 Nm.
  - guarded-15n-trial outer guards: normal load 50 N, force norm 35 N, torque 1.5 Nm.
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
    STEP4E_VERSION=step5b_v1 "${BASE_OPERATOR}" prep-long-checks
    ;;
  contact-bridge)
    if [[ "${STEP5B_CONFIRM:-}" != "LIVE STEP5B CONTACT RUN" ]]; then
      echo "refusing live Step5b bridge start: set STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN'"
      exit 40
    fi
    STEP4E_VERSION=step5b_v1 \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-50}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-60}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-3.0}" \
    STEP4E_NORMAL_FOLLOW_MODE="${STEP4E_NORMAL_FOLLOW_MODE:-filtered_live}" \
    STEP4E_NORMAL_FILTER_ALPHA="${STEP4E_NORMAL_FILTER_ALPHA:-0.35}" \
    STEP4E_NORMAL_MIN_FORCE_N="${STEP4E_NORMAL_MIN_FORCE_N:-2.0}" \
      "${BASE_OPERATOR}" line-bridge
    ;;
  guarded-15n-trial)
    if [[ "${STEP5B_CONFIRM:-}" != "LIVE STEP5B CONTACT RUN" ]]; then
      echo "refusing live Step5b 15N guarded trial: set STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN'"
      exit 40
    fi
    STEP4E_VERSION=step5b_v1 \
    STEP4E_DISPLAY_NAME="Step5b 15N guarded trial" \
    STEP4E_CONFIRM_PHRASE="START_STEP5B_15N_GUARDED" \
    STEP5B_TRIAL_PROFILE=guarded_15n_sentinel \
    STEP4E_TARGET_FORCE_N=15.0 \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-50}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-35}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-1.5}" \
    STEP4E_NORMAL_FOLLOW_MODE="${STEP4E_NORMAL_FOLLOW_MODE:-filtered_live}" \
    STEP4E_NORMAL_FILTER_ALPHA="${STEP4E_NORMAL_FILTER_ALPHA:-0.45}" \
    STEP4E_NORMAL_MIN_FORCE_N="${STEP4E_NORMAL_MIN_FORCE_N:-2.0}" \
    STEP4E_FORCE_P_GAIN="${STEP4E_FORCE_P_GAIN:-0.00015}" \
    STEP4E_FORCE_I_GAIN="${STEP4E_FORCE_I_GAIN:-0.0}" \
    STEP4E_FORCE_DAMPING="${STEP4E_FORCE_DAMPING:-1.2}" \
    STEP4E_NORMAL_VELOCITY_LIMIT_M_S="${STEP4E_NORMAL_VELOCITY_LIMIT_M_S:-0.0010}" \
    STEP4E_TOTAL_LINEAR_LIMIT_M_S="${STEP4E_TOTAL_LINEAR_LIMIT_M_S:-0.0040}" \
    STEP4E_REACQUIRE_VELOCITY_M_S="${STEP4E_REACQUIRE_VELOCITY_M_S:-0.0004}" \
    STEP4E_INTEGRAL_LIMIT_N_S="${STEP4E_INTEGRAL_LIMIT_N_S:-1.0}" \
      "${BASE_OPERATOR}" line-bridge
    ;;
  *)
    usage
    exit 2
    ;;
esac
