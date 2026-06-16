#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
RUN_ROOT="${ROOT}/experiments/tase-contact-reproduction/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${RUN_ROOT}/kunwei_force_check_${STAMP}"
KUNWEI_SENSOR_IP="${KUNWEI_SENSOR_IP:-192.168.50.25}"
KUNWEI_SENSOR_PORT="${KUNWEI_SENSOR_PORT:-5152}"
KUNWEI_GATE_DURATION_S="${KUNWEI_GATE_DURATION_S:-1.0}"
KUNWEI_GATE_MIN_SAMPLES="${KUNWEI_GATE_MIN_SAMPLES:-20}"

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

ros2 run ur10e_example_controllers kunwei_force_gate \
  --sensor-ip "${KUNWEI_SENSOR_IP}" \
  --sensor-port "${KUNWEI_SENSOR_PORT}" \
  --duration-s "${KUNWEI_GATE_DURATION_S}" \
  --min-samples "${KUNWEI_GATE_MIN_SAMPLES}" \
  --summary "${RUN_DIR}/kunwei_force_gate.json" \
  --raw-frames "${RUN_DIR}/kunwei_force_gate_raw_frames.bin" \
  | tee "${RUN_DIR}/kunwei_force_gate.log"

echo "kunwei_force_check complete: ${RUN_DIR}"
echo "summary=${RUN_DIR}/kunwei_force_gate.json"
