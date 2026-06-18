#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
EXPERIMENT="${ROOT}/experiments/tase-contact-reproduction"
LEDGER="${EXPERIMENT}/config/step5b_authorization_state.json"
AUTH_STATUS="${EXPERIMENT}/tools/step5b_authorization_status.py"
RUNNER="step5b_contact_live_runner"
ROBOT_IP="${ROBOT_IP:-192.168.1.18}"
REVERSE_IP="${REVERSE_IP:-192.168.1.10}"
ACTION_NAME="/scaled_joint_trajectory_controller/follow_joint_trajectory"
DRIVER_READINESS_WAIT_S="${DRIVER_READINESS_WAIT_S:-45}"

usage() {
  cat <<EOF
Usage:
  step5b_ros2_headless_live.sh status
  step5b_ros2_headless_live.sh dry-run
  step5b_ros2_headless_live.sh

Boundary:
  - TEMPORARY LIVE LOCK: default run is disabled after 2026-06-18 table-vibration feedback.
  - Use status/dry-run only until a low-vibration continuous preposition implementation is audited.
  - Current route only: ROS2 Remote Control/headless.
  - Starts the ROS2 UR driver if the trajectory action server is not already present.
  - Uses the locked Step5b specification defaults from the runner/stage table.
  - Does not override target force, path speed, path parameters, force source, or zero policy.
  - No TP/bridge fallback, no URScript send, no zero_ftsensor(), no Kunwei tare/config.
  - Running step5b_ros2_headless_live.sh is the per-run operator final trigger.
  - The per-run operator_final_trigger_received gate is set true only during this script run and restored on exit.
EOF
}

source_ros() {
  set +u
  # shellcheck disable=SC1091
  source /opt/ros/humble/setup.bash
  # shellcheck disable=SC1091
  source "${ROOT}/install/setup.bash"
  set -u
}

run_authorization_status() {
  (cd "${EXPERIMENT}" && python3 "${AUTH_STATUS}" "$@")
}

make_run_dir() {
  local stamp
  stamp="$(date +%Y%m%d_%H%M%S)"
  printf '%s\n' "${EXPERIMENT}/runs/step5b_ros2_headless_live_${stamp}"
}

stop_process_group() {
  local pid="$1"
  if [[ -z "${pid}" ]]; then
    return 0
  fi
  if kill -0 "${pid}" >/dev/null 2>&1; then
    kill -INT "-${pid}" >/dev/null 2>&1 || kill -INT "${pid}" >/dev/null 2>&1 || true
    sleep 1
  fi
  if kill -0 "${pid}" >/dev/null 2>&1; then
    kill -TERM "-${pid}" >/dev/null 2>&1 || kill -TERM "${pid}" >/dev/null 2>&1 || true
    sleep 1
  fi
  if kill -0 "${pid}" >/dev/null 2>&1; then
    kill -KILL "-${pid}" >/dev/null 2>&1 || kill -KILL "${pid}" >/dev/null 2>&1 || true
  fi
  wait "${pid}" >/dev/null 2>&1 || true
}

action_server_available() {
  timeout 5 ros2 action list 2>/dev/null | grep -Fxq "${ACTION_NAME}"
}

print_driver_failure() {
  local run_dir="$1"
  echo "UR ROS2 driver did not become ready." >&2
  for path in \
    "${run_dir}/driver_lifecycle_readiness.log" \
    "${run_dir}/controllers_readiness.log" \
    "${run_dir}/joint_states_once.log" \
    "${run_dir}/ur_driver_launch.log"; do
    if [[ -s "${path}" ]]; then
      echo "--- ${path} (tail) ---" >&2
      tail -80 "${path}" >&2 || true
    fi
  done
}

ensure_driver_ready() {
  local run_dir="$1"
  if action_server_available; then
    echo "ROS2 action server already available: ${ACTION_NAME}"
    return 0
  fi

  echo "Starting UR ROS2 driver: robot_ip=${ROBOT_IP} reverse_ip=${REVERSE_IP}"
  setsid ros2 launch ur10e_bringup ur10e_control.launch.py \
    robot_ip:="${ROBOT_IP}" \
    reverse_ip:="${REVERSE_IP}" \
    headless_mode:=true \
    launch_dashboard_client:=false \
    activate_joint_controller:=true \
    launch_rviz:=false \
    >"${run_dir}/ur_driver_launch.log" 2>&1 &
  DRIVER_LAUNCH_PID=$!

  echo "Waiting for controller/action readiness..."
  if ! ros2 run ur10e_example_controllers step5a_driver_readiness_check \
    --launch-log "${run_dir}/ur_driver_launch.log" \
    --summary "${run_dir}/driver_lifecycle_readiness.json" \
    --controllers-log "${run_dir}/controllers_readiness.log" \
    --joint-states-log "${run_dir}/joint_states_once.log" \
    --run-dir "${run_dir}" \
    --timeout-s "${DRIVER_READINESS_WAIT_S}" \
    | tee "${run_dir}/driver_lifecycle_readiness.log"; then
    print_driver_failure "${run_dir}"
    return 2
  fi

  if ! action_server_available; then
    echo "Driver readiness passed but action server is still absent: ${ACTION_NAME}" >&2
    print_driver_failure "${run_dir}"
    return 2
  fi
  echo "ROS2 action server ready: ${ACTION_NAME}"
}

write_operator_trigger() {
  local value="$1"
  python3 - "${LEDGER}" "${value}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = sys.argv[2].lower() == "true"
payload = json.loads(path.read_text(encoding="utf-8"))
payload.setdefault("open_gates", {})["operator_final_trigger_received"] = value
path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
PY
}

read_operator_trigger() {
  python3 - "${LEDGER}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("true" if payload.get("open_gates", {}).get("operator_final_trigger_received") else "false")
PY
}

require_only_operator_trigger_blocking() {
  local status_json="$1"
  python3 - "${status_json}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
blocking = payload.get("blocking_reasons", [])
allowed = ["operator_final_trigger_received:not_set"]
if payload.get("authorized") or blocking == allowed:
    raise SystemExit(0)
print("Refusing live run; authorization blockers are not limited to operator_final_trigger_received:", file=sys.stderr)
for reason in blocking:
    print(f"  - {reason}", file=sys.stderr)
raise SystemExit(42)
PY
}

require_authorized_after_trigger() {
  local status_json="$1"
  python3 - "${status_json}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("authorized"):
    raise SystemExit(0)
print("Refusing live run; authorization is still false after final trigger:", file=sys.stderr)
for reason in payload.get("blocking_reasons", []):
    print(f"  - {reason}", file=sys.stderr)
raise SystemExit(43)
PY
}

mode="${1:-run}"
case "${mode}" in
  -h|--help|help)
    usage
    exit 0
    ;;
  status)
    run_authorization_status
    ;;
  dry-run)
    source_ros
    ros2 run ur10e_example_controllers "${RUNNER}"
    ;;
  run)
    echo "Step5b ROS2 headless live is temporarily locked after table-vibration feedback." >&2
    echo "Use 'step5b_ros2_headless_live.sh status' or 'step5b_ros2_headless_live.sh dry-run' only." >&2
    echo "Live re-enable requires a new audited low-vibration preposition implementation." >&2
    exit 44

    run_dir="$(make_run_dir)"
    mkdir -p "${run_dir}"
    before_status="${run_dir}/authorization_before_trigger.json"
    after_status="${run_dir}/authorization_after_trigger.json"
    previous_trigger="$(read_operator_trigger)"
    DRIVER_LAUNCH_PID=""

    restore_trigger() {
      write_operator_trigger "${previous_trigger}"
      stop_process_group "${DRIVER_LAUNCH_PID}"
    }
    trap restore_trigger EXIT

    set +e
    run_authorization_status --json >"${before_status}"
    auth_rc=$?
    set -e
    if [[ ${auth_rc} -ne 0 && ${auth_rc} -ne 2 ]]; then
      echo "Authorization status command failed unexpectedly: rc=${auth_rc}" >&2
      exit "${auth_rc}"
    fi
    require_only_operator_trigger_blocking "${before_status}"

    write_operator_trigger true
    run_authorization_status --json >"${after_status}"
    require_authorized_after_trigger "${after_status}"

    source_ros
    ensure_driver_ready "${run_dir}"
    ros2 run ur10e_example_controllers "${RUNNER}" \
      --execute-live-contact \
      --run-dir "${run_dir}"
    echo
    echo "Step5b ROS2 headless live artifacts: ${run_dir}"
    ;;
  *)
    usage
    exit 2
    ;;
esac
