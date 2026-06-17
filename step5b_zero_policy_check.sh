#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
RUN_ROOT="${ROOT}/experiments/tase-contact-reproduction/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${RUN_ROOT}/step5b_zero_policy_readiness_${STAMP}"
KUNWEI_SENSOR_IP="${KUNWEI_SENSOR_IP:-192.168.50.25}"
KUNWEI_SENSOR_PORT="${KUNWEI_SENSOR_PORT:-5152}"
STEP5B_ZERO_POLICY_SAMPLE_DURATION_S="${STEP5B_ZERO_POLICY_SAMPLE_DURATION_S:-2.0}"
STEP5B_ZERO_POLICY_MIN_SAMPLES="${STEP5B_ZERO_POLICY_MIN_SAMPLES:-1000}"
STEP5B_ZERO_POLICY_RECENT_RATE_HZ_MIN="${STEP5B_ZERO_POLICY_RECENT_RATE_HZ_MIN:-200.0}"
STEP5B_ZERO_POLICY_LATEST_MAX_AGE_S="${STEP5B_ZERO_POLICY_LATEST_MAX_AGE_S:-0.25}"

mkdir -p "${RUN_DIR}"
echo "run_dir=${RUN_DIR}"

source_setup() {
  set +u
  source "$1"
  set -u
}

source_setup /opt/ros/humble/setup.bash
if [[ ! -f "${ROOT}/install/setup.bash" ]]; then
  colcon build --packages-select ur10e_example_controllers --symlink-install
fi
source_setup "${ROOT}/install/setup.bash"
if ! ros2 pkg prefix ur10e_example_controllers >/dev/null 2>&1; then
  colcon build --packages-select ur10e_example_controllers --symlink-install
  source_setup "${ROOT}/install/setup.bash"
fi

ros2 run ur10e_example_controllers step5b_zero_policy_readiness \
  --sensor-ip "${KUNWEI_SENSOR_IP}" \
  --sensor-port "${KUNWEI_SENSOR_PORT}" \
  --sample-duration-s "${STEP5B_ZERO_POLICY_SAMPLE_DURATION_S}" \
  --min-samples "${STEP5B_ZERO_POLICY_MIN_SAMPLES}" \
  --recent-rate-hz-min "${STEP5B_ZERO_POLICY_RECENT_RATE_HZ_MIN}" \
  --latest-sample-max-age-s "${STEP5B_ZERO_POLICY_LATEST_MAX_AGE_S}" \
  --output-dir "${RUN_DIR}" \
  | tee "${RUN_DIR}/step5b_zero_policy_readiness.log"

echo "step5b_zero_policy_check complete: ${RUN_DIR}"
echo "summary=${RUN_DIR}/summary.json"
