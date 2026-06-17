#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
RUN_ROOT="${ROOT}/experiments/tase-contact-reproduction/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${RUN_ROOT}/step5a_driver_lifecycle_check_${STAMP}"
ROBOT_IP="${ROBOT_IP:-192.168.1.18}"
REVERSE_IP="${REVERSE_IP:-192.168.1.10}"
READINESS_WAIT_S="${READINESS_WAIT_S:-30}"

mkdir -p "${RUN_DIR}"
echo "run_dir=${RUN_DIR}"

source_setup() {
  set +u
  source "$1"
  set -u
}

source_setup /opt/ros/humble/setup.bash
if [[ ! -f "${ROOT}/install/setup.bash" ]]; then
  colcon build --packages-select ur10e_bringup ur10e_example_controllers --symlink-install
fi
source_setup "${ROOT}/install/setup.bash"
if ! ros2 pkg prefix ur10e_example_controllers >/dev/null 2>&1; then
  colcon build --packages-select ur10e_bringup ur10e_example_controllers --symlink-install
  source_setup "${ROOT}/install/setup.bash"
fi

stop_process_group() {
  local pid="$1"
  if kill -0 "${pid}" >/dev/null 2>&1; then
    kill -INT "-${pid}" >/dev/null 2>&1 || kill -INT "${pid}" >/dev/null 2>&1 || true
    local deadline=$((SECONDS + 3))
    while kill -0 "${pid}" >/dev/null 2>&1 && (( SECONDS < deadline )); do
      sleep 0.1
    done
    if kill -0 "${pid}" >/dev/null 2>&1; then
      kill -TERM "-${pid}" >/dev/null 2>&1 || kill -TERM "${pid}" >/dev/null 2>&1 || true
      sleep 0.2
    fi
    if kill -0 "${pid}" >/dev/null 2>&1; then
      kill -KILL "-${pid}" >/dev/null 2>&1 || kill -KILL "${pid}" >/dev/null 2>&1 || true
    fi
  fi
  wait "${pid}" >/dev/null 2>&1 || true
}

LAUNCH_LOG="${RUN_DIR}/driver_lifecycle_launch.log"
setsid ros2 launch ur10e_bringup ur10e_control.launch.py \
  robot_ip:="${ROBOT_IP}" \
  reverse_ip:="${REVERSE_IP}" \
  headless_mode:=true \
  launch_dashboard_client:=false \
  activate_joint_controller:=true \
  launch_rviz:=false \
  >"${LAUNCH_LOG}" 2>&1 &
LAUNCH_PID=$!
cleanup() {
  stop_process_group "${LAUNCH_PID}"
}
trap cleanup EXIT

ros2 run ur10e_example_controllers step5a_driver_readiness_check \
  --launch-log "${LAUNCH_LOG}" \
  --summary "${RUN_DIR}/driver_lifecycle_readiness.json" \
  --controllers-log "${RUN_DIR}/controllers_readiness.log" \
  --joint-states-log "${RUN_DIR}/joint_states_once.log" \
  --run-dir "${RUN_DIR}" \
  --timeout-s "${READINESS_WAIT_S}" \
  --role driver_lifecycle_diagnostic_not_step5a_acceptance \
  | tee "${RUN_DIR}/driver_lifecycle_readiness.log"

echo "driver lifecycle diagnostic complete: ${RUN_DIR}"
echo "role=driver_lifecycle_diagnostic_not_step5a_acceptance"
echo "sent_goal=false"
