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
NO_CONTACT_LIVE_MOTION="${NO_CONTACT_LIVE_MOTION:-false}"
KUNWEI_FORCE_GATE_REQUIRED="${KUNWEI_FORCE_GATE_REQUIRED:-true}"
KUNWEI_SENSOR_IP="${KUNWEI_SENSOR_IP:-192.168.50.25}"
KUNWEI_SENSOR_PORT="${KUNWEI_SENSOR_PORT:-5152}"
KUNWEI_MONITOR_DURATION_S="${KUNWEI_MONITOR_DURATION_S:-2.0}"
KUNWEI_MONITOR_READY_TIMEOUT_S="${KUNWEI_MONITOR_READY_TIMEOUT_S:-3.0}"
KUNWEI_MONITOR_MIN_RECENT_SAMPLES="${KUNWEI_MONITOR_MIN_RECENT_SAMPLES:-20}"
KUNWEI_MONITOR_LATEST_MAX_AGE_S="${KUNWEI_MONITOR_LATEST_MAX_AGE_S:-0.25}"
KUNWEI_MONITOR_WINDOW_S="${KUNWEI_MONITOR_WINDOW_S:-0.5}"
KUNWEI_MAX_FORCE_DELTA_N="${KUNWEI_MAX_FORCE_DELTA_N:-8.0}"
STEP5A_DRIVER_LAUNCH_ATTEMPTS="${STEP5A_DRIVER_LAUNCH_ATTEMPTS:-3}"
STEP5A_DRIVER_RETRY_SLEEP_S="${STEP5A_DRIVER_RETRY_SLEEP_S:-2}"

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
    "${LAUNCH_LOG:-${RUN_DIR}/driver_readiness.log}" >/dev/null
}

readiness_log_passed() {
  rg -n "Successful 'activate' of hardware 'ur10e'" "${LAUNCH_LOG:-${RUN_DIR}/driver_readiness.log}" >/dev/null \
    && rg -n "Configured and activated .*joint_state_broadcaster" "${LAUNCH_LOG:-${RUN_DIR}/driver_readiness.log}" >/dev/null
}

write_log_readiness_artifacts() {
  {
    rg -n "Successful 'activate' of hardware 'ur10e'" "${LAUNCH_LOG:-${RUN_DIR}/driver_readiness.log}" || true
    rg -n "Configured and activated .*joint_state_broadcaster" "${LAUNCH_LOG:-${RUN_DIR}/driver_readiness.log}" || true
  } >"${RUN_DIR}/controllers_readiness.log"
}

print_readiness_failure() {
  echo "5a0 failed; no motion was attempted."
  echo "run_dir=${RUN_DIR}"
  echo "sent_goal=false"
  echo "log=${LAUNCH_LOG:-${RUN_DIR}/driver_readiness.log}"
  if readiness_failed; then
    echo "driver_failure:"
    rg -n "Could not get configuration package|\\[ur_ros2_control_node-[0-9]+\\].*\\[FATAL\\]|Failed to set the initial state|\\[ERROR\\] \\[ur_ros2_control_node-[0-9]+\\]: process has died" \
      "${LAUNCH_LOG:-${RUN_DIR}/driver_readiness.log}" | tail -n 8 || true
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

if [[ "${NO_CONTACT_LIVE_MOTION}" == "true" ]]; then
  LAUNCH_PID=""
  cleanup() {
    if [[ -n "${LAUNCH_PID}" ]]; then
      stop_process_group "${LAUNCH_PID}"
    fi
  }
  trap cleanup EXIT

  DRIVER_READY=false
  for attempt in $(seq 1 "${STEP5A_DRIVER_LAUNCH_ATTEMPTS}"); do
    LAUNCH_LOG="${RUN_DIR}/air_motion_launch_attempt_${attempt}.log"
    READINESS_JSON="${RUN_DIR}/driver_lifecycle_readiness_attempt_${attempt}.json"
    READINESS_LOG="${RUN_DIR}/driver_lifecycle_readiness_attempt_${attempt}.log"
    CONTROLLERS_LOG="${RUN_DIR}/controllers_readiness_attempt_${attempt}.log"
    JOINT_STATES_LOG="${RUN_DIR}/joint_states_once_attempt_${attempt}.log"
    ln -sfn "$(basename "${LAUNCH_LOG}")" "${RUN_DIR}/air_motion_launch.log"
    ln -sfn "$(basename "${READINESS_JSON}")" "${RUN_DIR}/driver_lifecycle_readiness.json"
    ln -sfn "$(basename "${READINESS_LOG}")" "${RUN_DIR}/driver_lifecycle_readiness.log"
    ln -sfn "$(basename "${CONTROLLERS_LOG}")" "${RUN_DIR}/controllers_readiness.log"
    ln -sfn "$(basename "${JOINT_STATES_LOG}")" "${RUN_DIR}/joint_states_once.log"

    echo "5a0 driver launch attempt ${attempt}/${STEP5A_DRIVER_LAUNCH_ATTEMPTS}"
    setsid ros2 launch ur10e_bringup ur10e_control.launch.py \
      robot_ip:="${ROBOT_IP}" \
      reverse_ip:="${REVERSE_IP}" \
      headless_mode:=true \
      launch_dashboard_client:=false \
      activate_joint_controller:=true \
      launch_rviz:=false \
      >"${LAUNCH_LOG}" 2>&1 &
    LAUNCH_PID=$!

    if ros2 run ur10e_example_controllers step5a_driver_readiness_check \
      --launch-log "${LAUNCH_LOG}" \
      --summary "${READINESS_JSON}" \
      --controllers-log "${CONTROLLERS_LOG}" \
      --joint-states-log "${JOINT_STATES_LOG}" \
      --run-dir "${RUN_DIR}" \
      --timeout-s "${READINESS_WAIT_S}" \
      | tee "${READINESS_LOG}"; then
      DRIVER_READY=true
      break
    fi

    stop_process_group "${LAUNCH_PID}"
    LAUNCH_PID=""
    if (( attempt < STEP5A_DRIVER_LAUNCH_ATTEMPTS )) && readiness_failed; then
      echo "5a0 driver startup failed before any motion; retrying after ${STEP5A_DRIVER_RETRY_SLEEP_S}s."
      sleep "${STEP5A_DRIVER_RETRY_SLEEP_S}"
      continue
    fi
    print_readiness_failure
    exit 2
  done

  if [[ "${DRIVER_READY}" != "true" ]]; then
    print_readiness_failure
    exit 2
  fi
  echo "5a0 passed: ${RUN_DIR}"
  echo "log=${LAUNCH_LOG}"
  echo "controllers=${RUN_DIR}/controllers_readiness.log"
  echo "joint_states=${RUN_DIR}/joint_states_once.log"
else
  LAUNCH_LOG="${RUN_DIR}/driver_readiness.log"
  setsid ros2 launch ur10e_bringup ur10e_control.launch.py \
    robot_ip:="${ROBOT_IP}" \
    reverse_ip:="${REVERSE_IP}" \
    headless_mode:=true \
    launch_dashboard_client:=false \
    activate_joint_controller:=false \
    launch_rviz:=false \
    >"${LAUNCH_LOG}" 2>&1 &
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
  echo "log=${LAUNCH_LOG}"
  if [[ -f "${RUN_DIR}/controllers_readiness.log" ]]; then
    echo "controllers=${RUN_DIR}/controllers_readiness.log"
  fi
  if [[ -f "${RUN_DIR}/joint_states_once.log" ]]; then
    echo "joint_states=${RUN_DIR}/joint_states_once.log"
  fi
fi
if [[ "${NO_CONTACT_READINESS_ONLY}" == "true" ]]; then
  echo "readiness-only stop; no live motion was attempted."
  exit 0
fi

if [[ "${KUNWEI_FORCE_GATE_REQUIRED}" == "true" ]]; then
  if [[ "${NO_CONTACT_LIVE_MOTION}" != "true" ]]; then
    if ! ros2 run ur10e_example_controllers kunwei_persistent_gate \
      --sensor-ip "${KUNWEI_SENSOR_IP}" \
      --sensor-port "${KUNWEI_SENSOR_PORT}" \
      --duration-s "${KUNWEI_MONITOR_DURATION_S}" \
      --ready-timeout-s "${KUNWEI_MONITOR_READY_TIMEOUT_S}" \
      --window-s "${KUNWEI_MONITOR_WINDOW_S}" \
      --latest-max-age-s "${KUNWEI_MONITOR_LATEST_MAX_AGE_S}" \
      --min-recent-samples "${KUNWEI_MONITOR_MIN_RECENT_SAMPLES}" \
      --max-force-delta-n "${KUNWEI_MAX_FORCE_DELTA_N}" \
      --summary "${RUN_DIR}/kunwei_persistent_monitor.json" \
      --raw-frames "${RUN_DIR}/kunwei_persistent_monitor_raw_frames.bin" \
      | tee "${RUN_DIR}/kunwei_persistent_monitor.log"; then
      echo "persistent Kunwei monitor failed; no air motion was attempted."
      echo "kunwei=${RUN_DIR}/kunwei_persistent_monitor.json"
      exit 3
    fi
    echo "persistent Kunwei monitor passed; default no-motion stop."
    echo "kunwei=${RUN_DIR}/kunwei_persistent_monitor.json"
    echo "To move the robot in no-contact mode, run: step5a_live_no_contact_test.sh"
    exit 0
  fi
else
  echo "KUNWEI_FORCE_GATE_REQUIRED=false; no air motion was attempted."
  echo "refusing to move without Kunwei persistent monitor."
  exit 3
fi

if [[ "${KUNWEI_FORCE_GATE_REQUIRED}" == "true" ]]; then
  ros2 run ur10e_example_controllers step5a_cartesian_cycloid_motion \
    --execute \
    --robot-ip "${ROBOT_IP}" \
    --kunwei-sensor-ip "${KUNWEI_SENSOR_IP}" \
    --kunwei-sensor-port "${KUNWEI_SENSOR_PORT}" \
    --kunwei-ready-timeout-s "${KUNWEI_MONITOR_READY_TIMEOUT_S}" \
    --kunwei-window-s "${KUNWEI_MONITOR_WINDOW_S}" \
    --kunwei-latest-max-age-s "${KUNWEI_MONITOR_LATEST_MAX_AGE_S}" \
    --kunwei-min-recent-samples "${KUNWEI_MONITOR_MIN_RECENT_SAMPLES}" \
    --kunwei-max-force-delta-n "${KUNWEI_MAX_FORCE_DELTA_N}" \
    --kunwei-summary "${RUN_DIR}/kunwei_persistent_monitor.json" \
    --kunwei-raw-frames "${RUN_DIR}/kunwei_persistent_monitor_raw_frames.bin" \
    --trace "${RUN_DIR}/step5a_cartesian_cycloid_motion_trace.csv" \
    --summary "${RUN_DIR}/step5a_cartesian_cycloid_motion.json" \
    | tee "${RUN_DIR}/step5a_cartesian_cycloid_motion.log"
fi

echo "no_contact_test complete: ${RUN_DIR}"
