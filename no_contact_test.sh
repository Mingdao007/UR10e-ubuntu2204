#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ROOT="${ROOT}/experiments/tase-contact-reproduction/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${RUN_ROOT}/no_contact_test_${STAMP}"
ROBOT_IP="${ROBOT_IP:-192.168.1.18}"
REVERSE_IP="${REVERSE_IP:-192.168.1.10}"

mkdir -p "${RUN_DIR}"
echo "run_dir=${RUN_DIR}"

source /opt/ros/humble/setup.bash
if [[ ! -f "${ROOT}/install/setup.bash" ]]; then
  colcon build --packages-select ur10e_bringup ur10e_example_controllers --symlink-install
fi
source "${ROOT}/install/setup.bash"
if ! ros2 pkg prefix ur10e_example_controllers >/dev/null 2>&1; then
  colcon build --packages-select ur10e_bringup ur10e_example_controllers --symlink-install
  source "${ROOT}/install/setup.bash"
fi

ros2 run ur10e_example_controllers no_contact_cycloid_shadow \
  --output-dir "${RUN_DIR}/no_contact_shadow" \
  | tee "${RUN_DIR}/no_contact_shadow.log"

set +e
timeout --signal=INT 20s ros2 launch ur10e_bringup ur10e_control.launch.py \
  robot_ip:="${ROBOT_IP}" \
  reverse_ip:="${REVERSE_IP}" \
  headless_mode:=true \
  activate_joint_controller:=false \
  launch_rviz:=false \
  >"${RUN_DIR}/driver_readiness.log" 2>&1
READINESS_RC=$?
set -e

if rg -n "Could not get configuration package|FATAL|Failed to set the initial state|process has died" "${RUN_DIR}/driver_readiness.log" >/dev/null; then
  echo "5a0 failed; no motion was attempted."
  echo "log=${RUN_DIR}/driver_readiness.log"
  exit 2
fi
if [[ "${READINESS_RC}" != "0" && "${READINESS_RC}" != "124" ]]; then
  echo "5a0 launch exited unexpectedly: rc=${READINESS_RC}"
  echo "log=${RUN_DIR}/driver_readiness.log"
  exit "${READINESS_RC}"
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
    wait "${LAUNCH_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

ros2 run ur10e_example_controllers no_contact_motion_probe \
  --execute \
  --robot-ip "${ROBOT_IP}" \
  --summary "${RUN_DIR}/no_contact_motion_probe.json" \
  | tee "${RUN_DIR}/no_contact_motion_probe.log"

echo "no_contact_test complete: ${RUN_DIR}"
