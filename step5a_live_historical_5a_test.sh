#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
RUN_ROOT="${ROOT}/experiments/tase-contact-reproduction/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${RUN_ROOT}/step5a_historical_fixed_z_no_contact_${STAMP}"
ROBOT_IP="${ROBOT_IP:-192.168.1.18}"
REVERSE_IP="${REVERSE_IP:-192.168.1.10}"
READINESS_WAIT_S="${READINESS_WAIT_S:-30}"
STEP5A_DRIVER_LAUNCH_ATTEMPTS="${STEP5A_DRIVER_LAUNCH_ATTEMPTS:-3}"
STEP5A_DRIVER_RETRY_SLEEP_S="${STEP5A_DRIVER_RETRY_SLEEP_S:-2}"
KUNWEI_SENSOR_IP="${KUNWEI_SENSOR_IP:-192.168.50.25}"
KUNWEI_SENSOR_PORT="${KUNWEI_SENSOR_PORT:-5152}"
KUNWEI_MONITOR_READY_TIMEOUT_S="${KUNWEI_MONITOR_READY_TIMEOUT_S:-3.0}"
KUNWEI_MONITOR_MIN_RECENT_SAMPLES="${KUNWEI_MONITOR_MIN_RECENT_SAMPLES:-20}"
KUNWEI_MONITOR_LATEST_MAX_AGE_S="${KUNWEI_MONITOR_LATEST_MAX_AGE_S:-0.25}"
KUNWEI_MONITOR_WINDOW_S="${KUNWEI_MONITOR_WINDOW_S:-0.5}"
KUNWEI_MAX_FORCE_DELTA_N="${KUNWEI_MAX_FORCE_DELTA_N:-2.0}"
STEP5A_POSITION_TOLERANCE_M="${STEP5A_POSITION_TOLERANCE_M:-0.003}"

mkdir -p "${RUN_DIR}"
echo "run_dir=${RUN_DIR}"

POSITION_GATE_MESSAGE="$(python3 - "${RUN_ROOT}" <<'PY'
import json
import sys
from pathlib import Path

run_root = Path(sys.argv[1])
runs = sorted(run_root.glob("step5a_historical_fixed_z_position_*"))
if not runs:
    print("NO_POSITION_RUN")
    raise SystemExit(2)
latest = runs[-1]
summary_path = latest / "step5a_historical_fixed_z_position.json"
try:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"BAD_POSITION_SUMMARY {summary_path}: {exc}")
    raise SystemExit(2)
allowed_revisions = {
    "active_tcp_10mm_clearance_v2",
    "active_tcp_10mm_clearance_15s_visual_v3",
}
if (
    summary.get("ok") is True
    and summary.get("stage_revision") in allowed_revisions
    and summary.get("motion_kind") == "fixed_z_start_positioning"
):
    print(f"POSITION_READY {latest}")
    raise SystemExit(0)
print(
    "POSITION_NOT_READY "
    f"{latest} ok={summary.get('ok')} "
    f"stage_revision={summary.get('stage_revision')} "
    f"error={summary.get('error')}"
)
raise SystemExit(2)
PY
)" || {
  echo "historical fixed-Z path is disabled until a corrected position run succeeds."
  echo "${POSITION_GATE_MESSAGE}"
  echo "Run first: step5a_live_historical_5a_position.sh"
  echo "Only continue to this test after you visually confirm about 10 mm air gap and the position summary has ok=true."
  exit 2
}
echo "${POSITION_GATE_MESSAGE}"

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

readiness_failed() {
  rg -n "Could not get configuration package|\\[ur_ros2_control_node-[0-9]+\\].*\\[FATAL\\]|Failed to set the initial state|\\[ERROR\\] \\[ur_ros2_control_node-[0-9]+\\]: process has died" \
    "${LAUNCH_LOG}" >/dev/null
}

print_readiness_failure() {
  echo "historical fixed-Z 5a readiness failed; no path motion was attempted."
  echo "run_dir=${RUN_DIR}"
  echo "sent_goal=false"
  echo "log=${LAUNCH_LOG}"
  if readiness_failed; then
    echo "driver_failure:"
    rg -n "Could not get configuration package|\\[ur_ros2_control_node-[0-9]+\\].*\\[FATAL\\]|Failed to set the initial state|\\[ERROR\\] \\[ur_ros2_control_node-[0-9]+\\]: process has died" \
      "${LAUNCH_LOG}" | tail -n 8 || true
  fi
}

LAUNCH_PID=""
cleanup() {
  if [[ -n "${LAUNCH_PID}" ]]; then
    stop_process_group "${LAUNCH_PID}"
  fi
}
trap cleanup EXIT

DRIVER_READY=false
for attempt in $(seq 1 "${STEP5A_DRIVER_LAUNCH_ATTEMPTS}"); do
  LAUNCH_LOG="${RUN_DIR}/historical_5a_launch_attempt_${attempt}.log"
  READINESS_JSON="${RUN_DIR}/driver_lifecycle_readiness_attempt_${attempt}.json"
  READINESS_LOG="${RUN_DIR}/driver_lifecycle_readiness_attempt_${attempt}.log"
  CONTROLLERS_LOG="${RUN_DIR}/controllers_readiness_attempt_${attempt}.log"
  JOINT_STATES_LOG="${RUN_DIR}/joint_states_once_attempt_${attempt}.log"
  ln -sfn "$(basename "${LAUNCH_LOG}")" "${RUN_DIR}/historical_5a_launch.log"
  ln -sfn "$(basename "${READINESS_JSON}")" "${RUN_DIR}/driver_lifecycle_readiness.json"
  ln -sfn "$(basename "${READINESS_LOG}")" "${RUN_DIR}/driver_lifecycle_readiness.log"
  ln -sfn "$(basename "${CONTROLLERS_LOG}")" "${RUN_DIR}/controllers_readiness.log"
  ln -sfn "$(basename "${JOINT_STATES_LOG}")" "${RUN_DIR}/joint_states_once.log"

  echo "historical fixed-Z 5a driver launch attempt ${attempt}/${STEP5A_DRIVER_LAUNCH_ATTEMPTS}"
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
    --role "step5a_historical_fixed_z_path_driver_readiness_gate" \
    | tee "${READINESS_LOG}"; then
    DRIVER_READY=true
    break
  fi

  stop_process_group "${LAUNCH_PID}"
  LAUNCH_PID=""
  if (( attempt < STEP5A_DRIVER_LAUNCH_ATTEMPTS )) && readiness_failed; then
    echo "driver startup failed before historical fixed-Z path; retrying after ${STEP5A_DRIVER_RETRY_SLEEP_S}s."
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

ros2 run ur10e_example_controllers step5a_historical_fixed_z_motion \
  --execute \
  --mode path \
  --robot-ip "${ROBOT_IP}" \
  --position-tolerance-m "${STEP5A_POSITION_TOLERANCE_M}" \
  --kunwei-sensor-ip "${KUNWEI_SENSOR_IP}" \
  --kunwei-sensor-port "${KUNWEI_SENSOR_PORT}" \
  --kunwei-ready-timeout-s "${KUNWEI_MONITOR_READY_TIMEOUT_S}" \
  --kunwei-window-s "${KUNWEI_MONITOR_WINDOW_S}" \
  --kunwei-latest-max-age-s "${KUNWEI_MONITOR_LATEST_MAX_AGE_S}" \
  --kunwei-min-recent-samples "${KUNWEI_MONITOR_MIN_RECENT_SAMPLES}" \
  --kunwei-max-force-delta-n "${KUNWEI_MAX_FORCE_DELTA_N}" \
  --kunwei-summary "${RUN_DIR}/kunwei_persistent_monitor.json" \
  --kunwei-raw-frames "${RUN_DIR}/kunwei_persistent_monitor_raw_frames.bin" \
  --trace "${RUN_DIR}/step5a_historical_fixed_z_trace.csv" \
  --summary "${RUN_DIR}/step5a_historical_fixed_z.json" \
  | tee "${RUN_DIR}/step5a_historical_fixed_z.log"

echo "step5a historical fixed-Z no-contact complete: ${RUN_DIR}"
