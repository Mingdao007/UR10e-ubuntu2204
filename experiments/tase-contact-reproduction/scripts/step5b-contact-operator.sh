#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"
BASE_OPERATOR="${ROOT}/scripts/step4e-line-v1-operator.sh"
STEP5B_BRIDGE_VERSION="step5b_v3"
EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/step5b_contact_cycloid_baseline_v3.urp"

usage() {
  cat <<EOF
Usage:
  step5b-contact-operator.sh prep-long-checks
  STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN' step5b-contact-operator.sh contact-bridge
  STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN' step5b-contact-operator.sh guarded-15n-trial
  STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN' step5b-contact-operator.sh ramp-5-to-15-trial

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - Contact motion.
  - Bridge profile: ${STEP5B_BRIDGE_VERSION}.
  - Force target: 12.0 N default for contact-bridge.
  - contact-bridge normal filter alpha: 0.55 default.
  - contact-bridge skips long bench-gate refresh by default; TP/script force guards remain active.
  - Raw normal guard: 50 N, force norm guard: 60 N, torque guard: 3.0 Nm.
  - guarded-15n-trial outer guards: normal load 50 N, force norm 60 N, torque 3.0 Nm.
  - ramp-5-to-15-trial: Stage 25.0 unloads/acquires at 5 N, ramps to 15 N, then enables short XY motion.
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
    STEP4E_VERSION="${STEP5B_BRIDGE_VERSION}" "${BASE_OPERATOR}" prep-long-checks
    ;;
  contact-bridge)
    if [[ "${STEP5B_CONFIRM:-}" != "LIVE STEP5B CONTACT RUN" ]]; then
      echo "refusing live Step5b bridge start: set STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN'"
      exit 40
    fi
    STEP4E_VERSION="${STEP5B_BRIDGE_VERSION}" \
    STEP4E_TARGET_FORCE_N="${STEP4E_TARGET_FORCE_N:-12.0}" \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-50}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-60}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-3.0}" \
    STEP4E_NORMAL_FOLLOW_MODE="${STEP4E_NORMAL_FOLLOW_MODE:-filtered_live}" \
    STEP4E_SKIP_BENCH_GATE="${STEP4E_SKIP_BENCH_GATE:-1}" \
    STEP4E_NORMAL_FILTER_ALPHA="${STEP4E_NORMAL_FILTER_ALPHA:-0.55}" \
    STEP4E_NORMAL_MIN_FORCE_N="${STEP4E_NORMAL_MIN_FORCE_N:-2.0}" \
    STEP4E_FORCE_P_GAIN="${STEP4E_FORCE_P_GAIN:-0.0010}" \
    STEP4E_FORCE_I_GAIN="${STEP4E_FORCE_I_GAIN:-0.00001}" \
    STEP4E_FORCE_DAMPING="${STEP4E_FORCE_DAMPING:-7.0}" \
    STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-0.150}" \
    STEP4E_NORMAL_VELOCITY_LIMIT_M_S="${STEP4E_NORMAL_VELOCITY_LIMIT_M_S:-0.0100}" \
    STEP4E_TOTAL_LINEAR_LIMIT_M_S="${STEP4E_TOTAL_LINEAR_LIMIT_M_S:-0.0040}" \
    STEP4E_REACQUIRE_VELOCITY_M_S="${STEP4E_REACQUIRE_VELOCITY_M_S:-0.0004}" \
    STEP4E_INTEGRAL_LIMIT_N_S="${STEP4E_INTEGRAL_LIMIT_N_S:-1.0}" \
      "${BASE_OPERATOR}" line-bridge
    ;;
  guarded-15n-trial)
    if [[ "${STEP5B_CONFIRM:-}" != "LIVE STEP5B CONTACT RUN" ]]; then
      echo "refusing live Step5b 15N guarded trial: set STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN'"
      exit 40
    fi
    STEP4E_VERSION="${STEP5B_BRIDGE_VERSION}" \
    STEP4E_SKIP_BENCH_GATE="${STEP4E_SKIP_BENCH_GATE:-1}" \
    STEP4E_DISPLAY_NAME="Step5b 15N guarded trial" \
    STEP4E_CONFIRM_PHRASE="START_STEP5B_15N_GUARDED" \
    STEP5B_TRIAL_PROFILE=guarded_15n_sentinel \
    STEP4E_TARGET_FORCE_N=15.0 \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-50}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-60}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-3.0}" \
    STEP4E_NORMAL_FOLLOW_MODE="${STEP4E_NORMAL_FOLLOW_MODE:-filtered_live}" \
    STEP4E_NORMAL_FILTER_ALPHA="${STEP4E_NORMAL_FILTER_ALPHA:-0.55}" \
    STEP4E_NORMAL_MIN_FORCE_N="${STEP4E_NORMAL_MIN_FORCE_N:-2.0}" \
    STEP4E_FORCE_P_GAIN="${STEP4E_FORCE_P_GAIN:-0.0010}" \
    STEP4E_FORCE_I_GAIN="${STEP4E_FORCE_I_GAIN:-0.00001}" \
    STEP4E_FORCE_DAMPING="${STEP4E_FORCE_DAMPING:-7.0}" \
    STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-0.150}" \
    STEP4E_NORMAL_VELOCITY_LIMIT_M_S="${STEP4E_NORMAL_VELOCITY_LIMIT_M_S:-0.0010}" \
    STEP4E_TOTAL_LINEAR_LIMIT_M_S="${STEP4E_TOTAL_LINEAR_LIMIT_M_S:-0.0040}" \
    STEP4E_REACQUIRE_VELOCITY_M_S="${STEP4E_REACQUIRE_VELOCITY_M_S:-0.0004}" \
    STEP4E_INTEGRAL_LIMIT_N_S="${STEP4E_INTEGRAL_LIMIT_N_S:-1.0}" \
      "${BASE_OPERATOR}" line-bridge
    ;;
  ramp-5-to-15-trial)
    if [[ "${STEP5B_CONFIRM:-}" != "LIVE STEP5B CONTACT RUN" ]]; then
      echo "refusing live Step5b ramp 5N to 15N trial: set STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN'"
      exit 40
    fi
    STEP4E_VERSION="${STEP5B_BRIDGE_VERSION}" \
    STEP4E_SKIP_BENCH_GATE="${STEP4E_SKIP_BENCH_GATE:-1}" \
    STEP4E_DISPLAY_NAME="Step5b ramp 5N to 15N trial" \
    STEP4E_CONFIRM_PHRASE="START_STEP5B_RAMP_5_TO_15" \
    STEP5B_TRIAL_PROFILE=ramp_5_to_15_sentinel \
    STEP4E_TARGET_FORCE_N=15.0 \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-50}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-60}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-3.0}" \
    STEP4E_NORMAL_FOLLOW_MODE="${STEP4E_NORMAL_FOLLOW_MODE:-filtered_live}" \
    STEP4E_NORMAL_FILTER_ALPHA="${STEP4E_NORMAL_FILTER_ALPHA:-0.55}" \
    STEP4E_NORMAL_MIN_FORCE_N="${STEP4E_NORMAL_MIN_FORCE_N:-2.0}" \
    STEP4E_FORCE_P_GAIN="${STEP4E_FORCE_P_GAIN:-0.0010}" \
    STEP4E_FORCE_I_GAIN="${STEP4E_FORCE_I_GAIN:-0.00001}" \
    STEP4E_FORCE_DAMPING="${STEP4E_FORCE_DAMPING:-7.0}" \
    STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-0.150}" \
    STEP4E_NORMAL_VELOCITY_LIMIT_M_S="${STEP4E_NORMAL_VELOCITY_LIMIT_M_S:-0.0100}" \
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
