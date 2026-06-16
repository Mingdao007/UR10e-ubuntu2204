#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
RUN_ROOT="${ROOT}/experiments/tase-contact-reproduction/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${RUN_ROOT}/no_contact_test_${STAMP}"
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

ros2 run ur10e_example_controllers no_contact_cycloid_shadow \
  --output-dir "${RUN_DIR}/no_contact_shadow" \
  | tee "${RUN_DIR}/no_contact_shadow.log"

readiness_failed() {
  rg -n "Could not get configuration package|FATAL|Failed to set the initial state|process has died" \
    "${RUN_DIR}/driver_readiness.log" >/dev/null
}

stop_launch() {
  local pid="$1"
  if kill -0 "${pid}" >/dev/null 2>&1; then
    kill -INT "${pid}" >/dev/null 2>&1 || true
  fi
  wait "${pid}" >/dev/null 2>&1 || true
}

ros2 launch ur10e_bringup ur10e_control.launch.py \
  robot_ip:="${ROBOT_IP}" \
  reverse_ip:="${REVERSE_IP}" \
  headless_mode:=true \
  activate_joint_controller:=false \
  launch_rviz:=false \
  >"${RUN_DIR}/driver_readiness.log" 2>&1 &
READINESS_PID=$!

READINESS_OK=false
READINESS_DEADLINE=$((SECONDS + READINESS_WAIT_S))
while (( SECONDS < READINESS_DEADLINE )); do
  if readiness_failed; then
    break
  fi
  if ! kill -0 "${READINESS_PID}" >/dev/null 2>&1; then
    break
  fi
  if timeout 2s ros2 topic echo --once /joint_states >"${RUN_DIR}/joint_states_once.log" 2>&1; then
    if timeout 2s ros2 control list_controllers --controller-manager /controller_manager \
      >"${RUN_DIR}/controllers_readiness.log" 2>&1; then
      if rg -n "joint_state_broadcaster.*active" "${RUN_DIR}/controllers_readiness.log" >/dev/null; then
        READINESS_OK=true
        break
      fi
    fi
  fi
  sleep 1
done

stop_launch "${READINESS_PID}"
if [[ "${READINESS_OK}" != "true" ]]; then
  echo "5a0 failed; no motion was attempted."
  echo "log=${RUN_DIR}/driver_readiness.log"
  if [[ -f "${RUN_DIR}/controllers_readiness.log" ]]; then
    echo "controllers=${RUN_DIR}/controllers_readiness.log"
  fi
  if [[ -f "${RUN_DIR}/joint_states_once.log" ]]; then
    echo "joint_states=${RUN_DIR}/joint_states_once.log"
  fi
  exit 2
fi

ros2 launch ur10e_bringup ur10e_control.launch.py \
  robot_ip:="${ROBOT_IP}" \
  reverse_ip:="${REVERSE_IP}" \
  headless_mode:=true \
  activate_joint_controller:=true \
  launch_rviz:=false \
  >"${RUN_DIR}/air_motion_launch.log" 2>&1 &
LAUNCH_PID=$!
cleanup() {
  if kill -0 "${LAUNCH_PID}" >/dev/null 2>&1; then
    kill -INT "${LAUNCH_PID}" >/dev/null 2>&1 || true
  fi
  wait "${LAUNCH_PID}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ros2 run ur10e_example_controllers no_contact_motion_probe \
  --execute \
  --robot-ip "${ROBOT_IP}" \
  --max-force-delta-n 8.0 \
  --summary "${RUN_DIR}/no_contact_motion_probe.json" \
  | tee "${RUN_DIR}/no_contact_motion_probe.log"

echo "no_contact_test complete: ${RUN_DIR}"
