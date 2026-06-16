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
READINESS_PROBE_TIMEOUT_S="${READINESS_PROBE_TIMEOUT_S:-2}"
READINESS_PROBE_KILL_AFTER_S="${READINESS_PROBE_KILL_AFTER_S:-1}"
READINESS_USE_ROS_CLI_PROBES="${READINESS_USE_ROS_CLI_PROBES:-false}"
NO_CONTACT_READINESS_ONLY="${NO_CONTACT_READINESS_ONLY:-false}"
KUNWEI_FORCE_GATE_REQUIRED="${KUNWEI_FORCE_GATE_REQUIRED:-true}"
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
  rg -n "Could not get configuration package|\\[ur_ros2_control_node-[0-9]+\\].*\\[FATAL\\]|Failed to set the initial state|\\[ERROR\\] \\[ur_ros2_control_node-[0-9]+\\]: process has died" \
    "${RUN_DIR}/driver_readiness.log" >/dev/null
}

readiness_log_passed() {
  rg -n "Successful 'activate' of hardware 'ur10e'" "${RUN_DIR}/driver_readiness.log" >/dev/null \
    && rg -n "Configured and activated .*joint_state_broadcaster" "${RUN_DIR}/driver_readiness.log" >/dev/null
}

write_log_readiness_artifacts() {
  {
    rg -n "Successful 'activate' of hardware 'ur10e'" "${RUN_DIR}/driver_readiness.log" || true
    rg -n "Configured and activated .*joint_state_broadcaster" "${RUN_DIR}/driver_readiness.log" || true
  } >"${RUN_DIR}/controllers_readiness.log"
}

print_readiness_failure() {
  echo "5a0 failed; no motion was attempted."
  echo "log=${RUN_DIR}/driver_readiness.log"
  if readiness_failed; then
    echo "driver_failure:"
    rg -n "Could not get configuration package|\\[ur_ros2_control_node-[0-9]+\\].*\\[FATAL\\]|Failed to set the initial state|\\[ERROR\\] \\[ur_ros2_control_node-[0-9]+\\]: process has died" \
      "${RUN_DIR}/driver_readiness.log" | tail -n 8 || true
  fi
  if [[ -f "${RUN_DIR}/controllers_readiness.log" ]]; then
    echo "controllers=${RUN_DIR}/controllers_readiness.log"
  fi
  if [[ -f "${RUN_DIR}/joint_states_once.log" ]]; then
    echo "joint_states=${RUN_DIR}/joint_states_once.log"
  fi
}

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

setsid ros2 launch ur10e_bringup ur10e_control.launch.py \
  robot_ip:="${ROBOT_IP}" \
  reverse_ip:="${REVERSE_IP}" \
  headless_mode:=true \
  launch_dashboard_client:=false \
  activate_joint_controller:=false \
  launch_rviz:=false \
  >"${RUN_DIR}/driver_readiness.log" 2>&1 &
READINESS_PID=$!

READINESS_OK=false
JOINT_STATES_OK=false
ACTIVE_PROBE_PID=""
ACTIVE_PROBE_KIND=""
ACTIVE_PROBE_DEADLINE=0
READINESS_DEADLINE=$((SECONDS + READINESS_WAIT_S))
while (( SECONDS < READINESS_DEADLINE )); do
  if readiness_failed; then
    break
  fi
  if ! kill -0 "${READINESS_PID}" >/dev/null 2>&1; then
    break
  fi
  if readiness_log_passed; then
    write_log_readiness_artifacts
    READINESS_OK=true
    break
  fi

  if [[ -n "${ACTIVE_PROBE_PID}" ]]; then
    if kill -0 "${ACTIVE_PROBE_PID}" >/dev/null 2>&1; then
      if (( SECONDS >= ACTIVE_PROBE_DEADLINE )); then
        stop_process_group "${ACTIVE_PROBE_PID}"
      else
        sleep 0.2
        continue
      fi
    else
      PROBE_RC=0
      wait "${ACTIVE_PROBE_PID}" >/dev/null 2>&1 || PROBE_RC=$?
      if [[ "${ACTIVE_PROBE_KIND}" == "joint_states" && "${PROBE_RC}" == "0" ]]; then
        JOINT_STATES_OK=true
      elif [[ "${ACTIVE_PROBE_KIND}" == "controllers" && "${PROBE_RC}" == "0" ]]; then
        if rg -n "joint_state_broadcaster.*active" "${RUN_DIR}/controllers_readiness.log" >/dev/null; then
          READINESS_OK=true
          break
        fi
      fi
      ACTIVE_PROBE_PID=""
      ACTIVE_PROBE_KIND=""
    fi
  fi

  if [[ "${READINESS_USE_ROS_CLI_PROBES}" == "true" && -z "${ACTIVE_PROBE_PID}" ]]; then
    if [[ "${JOINT_STATES_OK}" != "true" ]]; then
      setsid timeout --kill-after="${READINESS_PROBE_KILL_AFTER_S}s" "${READINESS_PROBE_TIMEOUT_S}s" \
        ros2 topic echo --no-daemon --once /joint_states >"${RUN_DIR}/joint_states_once.log" 2>&1 &
      ACTIVE_PROBE_PID=$!
      ACTIVE_PROBE_KIND="joint_states"
    else
      setsid timeout --kill-after="${READINESS_PROBE_KILL_AFTER_S}s" "${READINESS_PROBE_TIMEOUT_S}s" \
        ros2 control list_controllers --controller-manager /controller_manager \
        >"${RUN_DIR}/controllers_readiness.log" 2>&1 &
      ACTIVE_PROBE_PID=$!
      ACTIVE_PROBE_KIND="controllers"
    fi
    ACTIVE_PROBE_DEADLINE=$((SECONDS + READINESS_PROBE_TIMEOUT_S + READINESS_PROBE_KILL_AFTER_S + 1))
  else
    sleep 0.2
  fi
done

if [[ -n "${ACTIVE_PROBE_PID}" ]] && kill -0 "${ACTIVE_PROBE_PID}" >/dev/null 2>&1; then
  stop_process_group "${ACTIVE_PROBE_PID}"
fi
stop_process_group "${READINESS_PID}"
if [[ "${READINESS_OK}" != "true" ]]; then
  print_readiness_failure
  exit 2
fi
echo "5a0 passed: ${RUN_DIR}"
echo "log=${RUN_DIR}/driver_readiness.log"
if [[ -f "${RUN_DIR}/controllers_readiness.log" ]]; then
  echo "controllers=${RUN_DIR}/controllers_readiness.log"
fi
if [[ -f "${RUN_DIR}/joint_states_once.log" ]]; then
  echo "joint_states=${RUN_DIR}/joint_states_once.log"
fi
if [[ "${NO_CONTACT_READINESS_ONLY}" == "true" ]]; then
  echo "readiness-only stop; no live motion was attempted."
  exit 0
fi

if [[ "${KUNWEI_FORCE_GATE_REQUIRED}" == "true" ]]; then
  if ! ros2 run ur10e_example_controllers kunwei_force_gate \
    --sensor-ip "${KUNWEI_SENSOR_IP}" \
    --sensor-port "${KUNWEI_SENSOR_PORT}" \
    --duration-s "${KUNWEI_GATE_DURATION_S}" \
    --min-samples "${KUNWEI_GATE_MIN_SAMPLES}" \
    --summary "${RUN_DIR}/kunwei_force_gate.json" \
    --raw-frames "${RUN_DIR}/kunwei_force_gate_raw_frames.bin" \
    | tee "${RUN_DIR}/kunwei_force_gate.log"; then
    echo "kunwei force gate failed; no air motion was attempted."
    echo "kunwei=${RUN_DIR}/kunwei_force_gate.json"
    exit 3
  fi
  echo "kunwei force gate passed: ${RUN_DIR}/kunwei_force_gate.json"
else
  echo "KUNWEI_FORCE_GATE_REQUIRED=false; no air motion was attempted."
  echo "refusing to treat UR internal force as Kunwei evidence."
  exit 3
fi

setsid ros2 launch ur10e_bringup ur10e_control.launch.py \
  robot_ip:="${ROBOT_IP}" \
  reverse_ip:="${REVERSE_IP}" \
  headless_mode:=true \
  launch_dashboard_client:=false \
  activate_joint_controller:=true \
  launch_rviz:=false \
  >"${RUN_DIR}/air_motion_launch.log" 2>&1 &
LAUNCH_PID=$!
cleanup() {
  stop_process_group "${LAUNCH_PID}"
}
trap cleanup EXIT

ros2 run ur10e_example_controllers no_contact_motion_probe \
  --execute \
  --robot-ip "${ROBOT_IP}" \
  --ur-internal-max-force-delta-n 8.0 \
  --ur-internal-force-readiness-log "${RUN_DIR}/ur_internal_force_readiness.log" \
  --summary "${RUN_DIR}/no_contact_motion_probe.json" \
  | tee "${RUN_DIR}/no_contact_motion_probe.log"

echo "no_contact_test complete: ${RUN_DIR}"
