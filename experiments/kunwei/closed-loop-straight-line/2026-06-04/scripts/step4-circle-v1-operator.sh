#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
RUN_ROOT="${ROOT}/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
EXPECTED_PROGRAM="/programs/andyl/kunwei/step2/step4b_circle_contact_paper_attitude_v1.urp"
EXPECTED_BASENAME="step4b_circle_contact_paper_attitude_v1.urp"
ROBOT_HOST="${ROBOT_HOST:-192.168.1.18}"
DASHBOARD_PORT="${DASHBOARD_PORT:-29999}"
WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-45}"
AUTOWATCH_WAIT_FOR_PLAY_S="${AUTOWATCH_WAIT_FOR_PLAY_S:-600}"
DASHBOARD_MISS_LIMIT_S="${DASHBOARD_MISS_LIMIT_S:-5}"
BENCH_GATE="/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/check_ubuntu_network.py"

usage() {
  cat <<'USAGE'
Usage:
  step4-circle-v1-operator.sh bridge
  step4-circle-v1-operator.sh autowatch

Teach Pendant program:
  /programs/andyl/kunwei/step2/step4b_circle_contact_paper_attitude_v1.urp

Bridge lifecycle:
  bridge: starts Kunwei/RTDE bridge, then waits up to 45 s for TP Play
  autowatch: waits for TP Play, then starts Kunwei/RTDE bridge automatically
  stops bridge when the TP program stops, safety is not NORMAL, or Dashboard is unreachable
  sends Kunwei stop-stream quiet command after bridge exit
  writes stage_frequency_summary.json and step4_circle_analysis_summary.json after bridge exit

Motion settings in the TP program:
  scaffold: Step2C v8 bounded high approach, contact search, soft acquisition, short retract, home return
  circle: full circle from the middle-half diameter of the prior contact path
  IK: UR owns IK through speedl Cartesian twist
  search stage: 30 mm/s far, 5 mm/s near, speedl acceleration 500 mm/s^2
  line/circle stage: 5 mm/s tangent, speedl acceleration 300 mm/s^2,
    first 0.10 s at t=10 ms, then t=1 ms
  compliance: signed-Fz Z admittance plus bounded wx/wy attitude compliance proxy
USAGE
}

run_bench_gate() {
  python3 "${BENCH_GATE}" --include-kunwei --json-only
}

ensure_no_existing_bridge() {
  if pgrep -f "${ROOT}/tools/kunwei_rtde_bridge.py" >/dev/null 2>&1; then
    echo "refusing: an existing Kunwei RTDE bridge process is already active"
    pgrep -af "${ROOT}/tools/kunwei_rtde_bridge.py" || true
    exit 3
  fi
}

dashboard_snapshot_py='
import socket
import sys

expected_program = sys.argv[1]
expected_basename = sys.argv[2]
host = sys.argv[3]
port = int(sys.argv[4])

def dash_cmd(cmd, timeout=1.0):
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        try:
            s.recv(4096)
        except socket.timeout:
            pass
        s.sendall((cmd + "\n").encode("ascii"))
        return s.recv(4096).decode("utf-8", errors="replace").strip()

running = dash_cmd("running")
loaded = dash_cmd("get loaded program")
state = dash_cmd("programState")
safety = dash_cmd("safetymode")
print("\n".join([running, loaded, state, safety]))
loaded_ok = expected_program in loaded or expected_basename in loaded
running_ok = "true" in running.lower()
safety_ok = "NORMAL" in safety.upper()
stopped = "STOPPED" in state.upper() or "false" in running.lower()
if not safety_ok:
    raise SystemExit(20)
if not loaded_ok:
    raise SystemExit(21)
if running_ok:
    raise SystemExit(10)
if stopped:
    raise SystemExit(11)
raise SystemExit(12)
'

dashboard_snapshot() {
  python3 -c "${dashboard_snapshot_py}" "${EXPECTED_PROGRAM}" "${EXPECTED_BASENAME}" "${ROBOT_HOST}" "${DASHBOARD_PORT}"
}

wait_for_tp_play_autowatch() {
  echo "[autowatch] Watching for TP Play on:"
  echo "  ${EXPECTED_PROGRAM}"
  local start now
  start="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
  while true; do
    if dashboard_snapshot >/tmp/step4_dash_snapshot.txt 2>&1; then
      true
    else
      rc="$?"
      cat /tmp/step4_dash_snapshot.txt || true
      if [[ "${rc}" == "10" ]]; then
        echo "[autowatch] expected Step4b program is running; starting bridge."
        return 0
      elif [[ "${rc}" == "20" || "${rc}" == "21" ]]; then
        echo "[autowatch] refusing: safety is not NORMAL or loaded program is not Step4b"
        return "${rc}"
      fi
    fi
    now="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
    if python3 - "$start" "$now" "$AUTOWATCH_WAIT_FOR_PLAY_S" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[2]) - float(sys.argv[1]) > float(sys.argv[3]) else 1)
PY
    then
      echo "[autowatch] timed out waiting for TP Play after ${AUTOWATCH_WAIT_FOR_PLAY_S} s"
      return 7
    fi
    sleep 0.25
  done
}

monitor_bridge() {
  local bridge_pid="$1"
  local seen_running="${2:-0}"
  local start
  start="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
  while kill -0 "${bridge_pid}" 2>/dev/null; do
    if dashboard_snapshot >/tmp/step4_dash_snapshot.txt 2>&1; then
      true
    else
      rc="$?"
      cat /tmp/step4_dash_snapshot.txt || true
      if [[ "${rc}" == "10" ]]; then
        seen_running=1
      elif [[ "${rc}" == "11" && "${seen_running}" == "1" ]]; then
        echo "[operator] TP program stopped; stopping bridge"
        kill -INT "${bridge_pid}" 2>/dev/null || true
        return 0
      elif [[ "${rc}" == "20" ]]; then
        echo "[operator] safety mode is not NORMAL; stopping bridge"
        kill -INT "${bridge_pid}" 2>/dev/null || true
        return 0
      fi
    fi
    if [[ "${seen_running}" == "0" ]]; then
      now="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
      if python3 - "$start" "$now" "$WAIT_FOR_PLAY_S" <<'PY'
import sys
raise SystemExit(0 if float(sys.argv[2]) - float(sys.argv[1]) > float(sys.argv[3]) else 1)
PY
      then
        echo "[operator] expected program did not start within ${WAIT_FOR_PLAY_S} s; stopping bridge"
        kill -INT "${bridge_pid}" 2>/dev/null || true
        return 0
      fi
    fi
    sleep 0.25
  done
}

postprocess_run() {
  local out_dir="$1"
  local bridge_csv="${out_dir}/bridge_rtde_500hz.csv"
  if [[ -f "${bridge_csv}" ]]; then
    python3 "${ROOT}/tools/summarize_stage_frequency.py" "${bridge_csv}" --output "${out_dir}/stage_frequency_summary.json" || true
    python3 "${ROOT}/tools/analyze_step2d_circle_run.py" "${bridge_csv}" --target-force-n 5 --output "${out_dir}/step4_circle_analysis_summary.json" || true
  else
    echo "[operator] no bridge CSV found for postprocess: ${bridge_csv}"
  fi
}

run_bridge_for_mode() {
  local out_dir="$1"
  local already_running="$2"
  mkdir -p "${out_dir}"
  local bridge_pid=""
  cleanup() {
    if [[ -n "${bridge_pid}" ]] && kill -0 "${bridge_pid}" 2>/dev/null; then
      kill -INT "${bridge_pid}" 2>/dev/null || true
      sleep 0.5
      kill -TERM "${bridge_pid}" 2>/dev/null || true
    fi
  }
  trap cleanup INT TERM EXIT

  python3 "${ROOT}/tools/kunwei_rtde_bridge.py" \
    --allow-kunwei-stream-command \
    --write-rtde-inputs \
    --baseline-s 5 \
    --rezero-s 1 \
    --duration-s 180 \
    --rtde-hz 500 \
    --socket-timeout-s 0.0 \
    --sensor-stale-s 0.10 \
    --target-force-n 5 \
    --normal-axis fz \
    --normal-sign 1 \
    --max-normal-force-n 20 \
    --max-force-norm-n 50 \
    --output-dir "${out_dir}" &
  bridge_pid="$!"

  monitor_bridge "${bridge_pid}" "${already_running}" || true
  wait "${bridge_pid}" || true
  trap - INT TERM EXIT

  echo "[operator] bridge output: ${out_dir}"
  local quiet_json="${out_dir}/kunwei_quiet_stream.json"
  if python3 "${ROOT}/tools/kunwei_quiet_stream.py" --json-only --output-json "${quiet_json}"; then
    echo "[operator] Kunwei quiet stop passed: ${quiet_json}"
  else
    echo "[operator] Kunwei quiet stop reported issue: ${quiet_json}"
  fi
  [[ -f "${quiet_json}" ]] && cat "${quiet_json}"
  postprocess_run "${out_dir}"
}

mode="${1:-}"
case "${mode}" in
  autowatch)
    cat <<'WARNING'
STEP4b circle contact v1 autowatch.
This mode waits for Teach Pendant Play first.
It does not start Kunwei streaming or write RTDE inputs while waiting.

Open this Teach Pendant program first:
  /programs/andyl/kunwei/step2/step4b_circle_contact_paper_attitude_v1.urp

Then run this mode and press Play on the Teach Pendant.
The bridge will start automatically only after Dashboard reports that exact Step4b program running.
WARNING
    run_bench_gate
    ensure_no_existing_bridge
    wait_for_tp_play_autowatch
    run_bridge_for_mode "${RUN_ROOT}/bridge_step4b_circle_v1_autowatch_contact_paper_attitude_${STAMP}" 1
    ;;
  bridge)
    cat <<'WARNING'
STEP4b circle contact v1 lifecycle bridge.
This sends Kunwei 48 AA 0D 0A and writes UR RTDE input registers.
It does not send URScript from Ubuntu.

Before pressing Play, open this Teach Pendant program:
  /programs/andyl/kunwei/step2/step4b_circle_contact_paper_attitude_v1.urp

Motion:
  full circle from middle-half diameter of the prior contact path
  search = 30 mm/s far, 5 mm/s near, speedl acceleration = 500 mm/s^2
  tangent = 5 mm/s, speedl acceleration = 300 mm/s^2
  Z force admittance = Step2C style signed-Fz P/I correction
  attitude = bounded wx/wy force/torque compliance proxy

Safety:
  target force = 5 N
  raw normal guard = 20 N
  force norm guard = 50 N
  torque norm guard = 0.6 Nm
  sensor stale = 100 ms

Type START_STEP4B_CIRCLE_V1 to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP4B_CIRCLE_V1" ]]; then
      echo "aborted"
      exit 2
    fi
    run_bench_gate
    ensure_no_existing_bridge
    run_bridge_for_mode "${RUN_ROOT}/bridge_step4b_circle_v1_contact_paper_attitude_${STAMP}" 0
    ;;
  *)
    usage
    exit 2
    ;;
esac
