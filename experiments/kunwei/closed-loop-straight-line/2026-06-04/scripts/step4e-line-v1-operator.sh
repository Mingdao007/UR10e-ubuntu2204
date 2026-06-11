#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
RUN_ROOT="${ROOT}/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
ROBOT_HOST="${ROBOT_HOST:-192.168.1.18}"
DASHBOARD_PORT="${DASHBOARD_PORT:-29999}"
WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-45}"
AUTOWATCH_WAIT_FOR_PLAY_S="${AUTOWATCH_WAIT_FOR_PLAY_S:-600}"
BENCH_GATE="/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/check_ubuntu_network.py"
STEP4E_VERSION="${STEP4E_VERSION:-v1}"
BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}"
STEP4E_NORMAL_COMMAND_SIGN="${STEP4E_NORMAL_COMMAND_SIGN:-1}"
MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-20}"
MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-0.6}"
STEP4E_ORIENTATION_GAIN="${STEP4E_ORIENTATION_GAIN:-0.20}"
STEP4E_ORIENTATION_WX_SIGN="${STEP4E_ORIENTATION_WX_SIGN:-1}"
STEP4E_ORIENTATION_WY_SIGN="${STEP4E_ORIENTATION_WY_SIGN:-1}"
STEP4E_LINE_SPEED_M_S="${STEP4E_LINE_SPEED_M_S:-0.003}"
STEP4E_LINE_SETTLE_S="${STEP4E_LINE_SETTLE_S:-0.0}"
STEP4E_STAGE25_ONLY="${STEP4E_STAGE25_ONLY:-0}"
STEP4E_NORMAL_VELOCITY_LIMIT_M_S="${STEP4E_NORMAL_VELOCITY_LIMIT_M_S:-0.003}"
STEP4E_FORCE_P_GAIN="${STEP4E_FORCE_P_GAIN:-0.0007}"
STEP4E_FORCE_I_GAIN="${STEP4E_FORCE_I_GAIN:-0.00008}"
STEP4E_FORCE_DAMPING="${STEP4E_FORCE_DAMPING:-0.35}"
STEP4E_INTEGRAL_LIMIT_N_S="${STEP4E_INTEGRAL_LIMIT_N_S:-10.0}"
STEP4E_REACQUIRE_VELOCITY_M_S="${STEP4E_REACQUIRE_VELOCITY_M_S:-0.001}"
STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-0.015}"

PROGRAM_PREVIEW="/programs/andyl/kunwei/step4/step4e_preview_line_${STEP4E_VERSION}.urp"
PROGRAM_HOLD="/programs/andyl/kunwei/step4/step4e_contact_hold_line_${STEP4E_VERSION}.urp"
PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_line_outerloop_${STEP4E_VERSION}.urp"
PROGRAM_GEO="/programs/andyl/kunwei/step4/step4e_ball_first_contact_p0_v1.urp"
PROGRAM_AXIS_ISO="/programs/andyl/kunwei/step4/step4e_attitude_axis_iso_v1.urp"
if [[ "${STEP4E_VERSION}" == "v21" ]]; then
  PROGRAM_LINE="/programs/andyl/kunwei/step4/step4e_detached_movel_minrot_v21.urp"
fi
if [[ "${STEP4E_VERSION}" == "p0_geo_v1" ]]; then
  SEARCH_DESCRIPTION="P0-geo ball-first contact witness: vertical TCP entry, far 15 mm/s until 80 mm depth, then near 3 mm/s until first 1-1.5 N contact or 92 mm max depth; after contact it holds still for visual confirmation, retracts base-Z 2 mm, and never runs attitude, 5N acquisition, or line motion"
elif [[ "${STEP4E_VERSION}" == "v20" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v20 moves directly to entry XY with vertical TCP orientation [pi,0,0], searches far 15 mm/s then near 3 mm/s, latches first-contact normal only, lifts base-Z 2 mm, aligns attitude while detached, reacquires 5 N along the locked normal, then runs the 5 mm/s XY line"
elif [[ "${STEP4E_VERSION}" == "v21" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v21 moves to vertical TCP orientation, searches far 15 mm/s for 80 mm max, then near 3 mm/s to 92 mm max depth, latches first-contact normal only, detaches along the locked normal, then previews the minimal-rotation target without executing contact-posture motion; no 5N acquisition and no XY line"
elif [[ "${STEP4E_VERSION}" == "v19" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v19 first moves TCP orientation to vertical [pi,0,0], then XY entry and downward speedl-search; far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; after contact latch it holds 5 N point contact and aligns TCP z to the contact normal before 5 mm/s XY line motion"
elif [[ "${STEP4E_VERSION}" == "v18" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v18 first moves TCP orientation to vertical [pi,0,0], then XY entry and downward speedl-search; far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; after contact latch it holds 5 N point contact and aligns TCP z to the contact normal before XY line motion"
elif [[ "${STEP4E_VERSION}" == "v17" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v17 first moves TCP orientation to vertical [pi,0,0], then XY entry and downward speedl-search; far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; bridge attitude outer-loop starts after contact latch"
elif [[ "${STEP4E_VERSION}" == "v16" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v16 removes fixed-Z pre-search movel; after XY entry it directly speedl-searches downward, far 15 mm/s for 130 mm then near 3 mm/s up to 150 mm max depth; bridge normal/reacquire limit = 2 mm/s"
elif [[ "${STEP4E_VERSION}" == "v15" ]]; then
  SEARCH_DESCRIPTION="two-stage search: v15 keeps v14 force-safe line settings and adds stage25 command-valid grace to avoid first-cycle RTDE/URScript skew"
elif [[ "${STEP4E_VERSION}" == "v14" ]]; then
  SEARCH_DESCRIPTION="two-stage search: first move to fixed validated search-start z=98.35 mm, then far 15 mm/s for 80 mm, near 3 mm/s for final 12 mm, 92 mm max depth; v14 starts admittance integration only in stage25 and settles normal force before line motion"
elif [[ "${STEP4E_VERSION}" == "v13" ]]; then
  SEARCH_DESCRIPTION="two-stage search: first move to fixed validated search-start z=98.35 mm, then far 15 mm/s for 80 mm, near 3 mm/s for final 12 mm, 92 mm max depth; v13 fixes v12 high-start no-contact miss"
elif [[ "${STEP4E_VERSION}" == "v12" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v12 keeps v11 attitude signs and fixes endpoint success/retract"
elif [[ "${STEP4E_VERSION}" == "v11" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v11 keeps v10 force guards and uses independent attitude signs wx=+1, wy=-1"
elif [[ "${STEP4E_VERSION}" == "v10" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v10 keeps v9 force guards and reverses the attitude outer-loop direction"
elif [[ "${STEP4E_VERSION}" == "v9" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v9 keeps v8 latched-normal line control and raises raw normal guard to 100N / torque guard to 1.0Nm"
elif [[ "${STEP4E_VERSION}" == "v8" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v8 keeps v7 stopl(0.1) and uses latched-normal 5N line control"
elif [[ "${STEP4E_VERSION}" == "v7" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v7 keeps v6 normal command sign/30N guard and uses stopl(0.1)"
elif [[ "${STEP4E_VERSION}" == "v6" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth; v6 flips normal command sign to unload after contact"
elif [[ "${STEP4E_VERSION}" == "v5" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 15 mm/s for 80 mm, then near 3 mm/s for the final 12 mm, 92 mm max depth"
elif [[ "${STEP4E_VERSION}" == "v4" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 10 mm/s for 80 mm, then near 3 mm/s for the final 10 mm, 90 mm max depth"
elif [[ "${STEP4E_VERSION}" == "v3" ]]; then
  SEARCH_DESCRIPTION="two-stage search: far 10 mm/s for 45 mm, then near 3 mm/s until 70 mm max depth"
else
  SEARCH_DESCRIPTION="deterministic 3 mm/s downward search"
fi

usage() {
  cat <<'USAGE'
Usage:
  step4e-line-v1-operator.sh preview-autowatch
  step4e-line-v1-operator.sh hold-autowatch
  step4e-line-v1-operator.sh line-autowatch
  step4e-line-v1-operator.sh axis-autowatch
  step4e-line-v1-operator.sh geo-autowatch
  step4e-line-v1-operator.sh preview-bridge
  step4e-line-v1-operator.sh hold-bridge
  step4e-line-v1-operator.sh line-bridge
  step4e-line-v1-operator.sh axis-bridge
  step4e-line-v1-operator.sh geo-bridge

Teach Pendant programs:
  /programs/andyl/kunwei/step4/step4e_preview_line_${STEP4E_VERSION}.urp
  /programs/andyl/kunwei/step4/step4e_contact_hold_line_${STEP4E_VERSION}.urp
  /programs/andyl/kunwei/step4/step4e_line_outerloop_${STEP4E_VERSION}.urp
  /programs/andyl/kunwei/step4/step4e_ball_first_contact_p0_v1.urp
  /programs/andyl/kunwei/step4/step4e_attitude_axis_iso_v1.urp

Bridge lifecycle:
  * autowatch waits for TP Play, then starts Kunwei/RTDE bridge automatically.
  * bridge starts Kunwei/RTDE bridge immediately, then waits up to 45 s for TP Play.
  * bridge is stopped when TP program stops, safety is not NORMAL, or Dashboard is unreachable.

Step4e motion boundary:
  preview: no robot motion, echo Step4e command registers only.
  geo: P0 ball-first witness; touch once at low threshold, visual dwell, then 2 mm base-Z retract.
  axis: no-contact four-quadrant attitude axis isolation.
  hold: contact search, then 12 s force/orientation hold.
  line: contact search, then straight XY line from the two TP screenshot points.
  force target = 5 N for hold/line only; geo contact witness triggers around 1-1.5 N.
  raw normal guard = ${MAX_NORMAL_FORCE_N} N, force norm guard = 50 N, torque guard = ${MAX_TORQUE_NORM_NM} Nm.
USAGE
}

select_mode() {
  local requested="$1"
  case "${requested}" in
    preview-autowatch|preview-bridge)
      EXPECTED_PROGRAM="${PROGRAM_PREVIEW}"
      EXPECTED_BASENAME="step4e_preview_line_${STEP4E_VERSION}.urp"
      STEP4E_MODE="preview"
      RUN_LABEL="step4e_preview_line_${STEP4E_VERSION}"
      ;;
    hold-autowatch|hold-bridge)
      EXPECTED_PROGRAM="${PROGRAM_HOLD}"
      EXPECTED_BASENAME="step4e_contact_hold_line_${STEP4E_VERSION}.urp"
      STEP4E_MODE="hold"
      RUN_LABEL="step4e_contact_hold_line_${STEP4E_VERSION}"
      ;;
    line-autowatch|line-bridge)
      EXPECTED_PROGRAM="${PROGRAM_LINE}"
      if [[ "${STEP4E_VERSION}" == "v21" ]]; then
        EXPECTED_BASENAME="step4e_detached_movel_minrot_v21.urp"
      else
        EXPECTED_BASENAME="step4e_line_outerloop_${STEP4E_VERSION}.urp"
      fi
      STEP4E_MODE="line"
      if [[ "${STEP4E_VERSION}" == "v21" ]]; then
        RUN_LABEL="step4e_detached_movel_minrot_v21"
      else
        RUN_LABEL="step4e_line_outerloop_${STEP4E_VERSION}"
      fi
      ;;
    axis-autowatch|axis-bridge)
      EXPECTED_PROGRAM="${PROGRAM_AXIS_ISO}"
      EXPECTED_BASENAME="step4e_attitude_axis_iso_v1.urp"
      STEP4E_MODE="axis_iso"
      RUN_LABEL="step4e_attitude_axis_iso_v1"
      CONFIRM_LABEL="axis"
      ;;
    geo-autowatch|geo-bridge)
      EXPECTED_PROGRAM="${PROGRAM_GEO}"
      EXPECTED_BASENAME="step4e_ball_first_contact_p0_v1.urp"
      STEP4E_MODE="off"
      RUN_LABEL="step4e_ball_first_contact_p0_v1"
      CONFIRM_LABEL="geo"
      ;;
    *)
      usage
      exit 2
      ;;
  esac
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

stop_bridge_process() {
  local bridge_pid="$1"
  local reason="$2"
  if ! kill -0 "${bridge_pid}" 2>/dev/null; then
    return 0
  fi

  echo "[operator] stopping bridge: ${reason}"
  kill -INT "${bridge_pid}" 2>/dev/null || true
  local i
  for i in 1 2 3 4; do
    sleep 0.5
    if ! kill -0 "${bridge_pid}" 2>/dev/null; then
      return 0
    fi
  done

  echo "[operator] bridge ignored SIGINT after 2 s; sending SIGTERM"
  kill -TERM "${bridge_pid}" 2>/dev/null || true
  for i in 1 2 3 4; do
    sleep 0.5
    if ! kill -0 "${bridge_pid}" 2>/dev/null; then
      return 0
    fi
  done

  echo "[operator] bridge ignored SIGTERM after 2 s; sending SIGKILL"
  kill -KILL "${bridge_pid}" 2>/dev/null || true
}

wait_for_tp_play_autowatch() {
  echo "[autowatch] Watching for TP Play on:"
  echo "  ${EXPECTED_PROGRAM}"
  local start now rc
  start="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
  while true; do
    if dashboard_snapshot >/tmp/step4e_dash_snapshot.txt 2>&1; then
      true
    else
      rc="$?"
      cat /tmp/step4e_dash_snapshot.txt || true
      if [[ "${rc}" == "10" ]]; then
        echo "[autowatch] expected Step4e ${STEP4E_MODE} program is running; starting bridge."
        return 0
      elif [[ "${rc}" == "20" || "${rc}" == "21" ]]; then
        echo "[autowatch] refusing: safety is not NORMAL or loaded program is not expected Step4e ${STEP4E_MODE}"
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
  local start now rc
  start="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
  while kill -0 "${bridge_pid}" 2>/dev/null; do
    if dashboard_snapshot >/tmp/step4e_dash_snapshot.txt 2>&1; then
      true
    else
      rc="$?"
      cat /tmp/step4e_dash_snapshot.txt || true
      if [[ "${rc}" == "10" ]]; then
        seen_running=1
      elif [[ "${rc}" == "11" && "${seen_running}" == "1" ]]; then
        echo "[operator] TP program stopped"
        stop_bridge_process "${bridge_pid}" "TP program stopped"
        return 0
      elif [[ "${rc}" == "20" ]]; then
        echo "[operator] safety mode is not NORMAL"
        stop_bridge_process "${bridge_pid}" "safety mode is not NORMAL"
        return 0
      elif [[ "${rc}" == "21" ]]; then
        echo "[operator] loaded program is no longer expected Step4e ${STEP4E_MODE}"
        stop_bridge_process "${bridge_pid}" "loaded program mismatch"
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
        echo "[operator] expected program did not start within ${WAIT_FOR_PLAY_S} s"
        stop_bridge_process "${bridge_pid}" "TP Play timeout"
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
  else
    echo "[operator] no bridge CSV found for postprocess: ${bridge_csv}"
  fi
}

run_bridge_for_mode() {
  local out_dir="$1"
  local already_running="$2"
  mkdir -p "${out_dir}"
  local bridge_pid=""
  local stage25_only_args=()
  if [[ "${STEP4E_STAGE25_ONLY}" == "1" ]]; then
    stage25_only_args+=(--step4e-integrate-stage25-only)
  fi
  cleanup() {
    if [[ -n "${bridge_pid}" ]] && kill -0 "${bridge_pid}" 2>/dev/null; then
      stop_bridge_process "${bridge_pid}" "operator cleanup"
    fi
  }
  trap cleanup INT TERM EXIT

  python3 "${ROOT}/tools/kunwei_rtde_bridge.py" \
    --allow-kunwei-stream-command \
    --write-rtde-inputs \
    --baseline-s 5 \
    --rezero-s 1 \
    --duration-s "${BRIDGE_DURATION_S}" \
    --rtde-hz 500 \
    --socket-timeout-s 0.0 \
    --sensor-stale-s 0.10 \
    --target-force-n 5 \
    --normal-axis fz \
    --normal-sign 1 \
    --max-normal-force-n "${MAX_NORMAL_FORCE_N}" \
    --max-force-norm-n 50 \
    --max-torque-norm-nm "${MAX_TORQUE_NORM_NM}" \
    --step4e-mode "${STEP4E_MODE}" \
    --step4e-version "${STEP4E_VERSION}" \
    --step4e-line-speed-m-s "${STEP4E_LINE_SPEED_M_S}" \
    --step4e-line-settle-s "${STEP4E_LINE_SETTLE_S}" \
    "${stage25_only_args[@]}" \
    --step4e-path-p-gain 1.5 \
    --step4e-motion-limit-m-s 0.004 \
    --step4e-total-linear-limit-m-s 0.006 \
    --step4e-normal-velocity-limit-m-s "${STEP4E_NORMAL_VELOCITY_LIMIT_M_S}" \
    --step4e-force-p-gain "${STEP4E_FORCE_P_GAIN}" \
    --step4e-force-i-gain "${STEP4E_FORCE_I_GAIN}" \
    --step4e-force-damping "${STEP4E_FORCE_DAMPING}" \
    --step4e-normal-command-sign "${STEP4E_NORMAL_COMMAND_SIGN}" \
    --step4e-integral-limit-n-s "${STEP4E_INTEGRAL_LIMIT_N_S}" \
    --step4e-min-force-for-control-n 1.0 \
    --step4e-acquire-grace-s 0.25 \
    --step4e-reacquire-velocity-m-s "${STEP4E_REACQUIRE_VELOCITY_M_S}" \
    --step4e-orientation-gain "${STEP4E_ORIENTATION_GAIN}" \
    --step4e-orientation-wx-sign "${STEP4E_ORIENTATION_WX_SIGN}" \
    --step4e-orientation-wy-sign "${STEP4E_ORIENTATION_WY_SIGN}" \
    --step4e-angular-limit-rad-s "${STEP4E_ANGULAR_LIMIT_RAD_S}" \
    --step4e-contact-offset-min-fz-n 1.0 \
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
select_mode "${mode}"
CONFIRM_TOKEN="${CONFIRM_LABEL:-${STEP4E_MODE}}"
CONFIRM_TOKEN="${CONFIRM_TOKEN^^}"

case "${mode}" in
  *-autowatch)
    cat <<WARNING
STEP4e ${STEP4E_MODE} ${STEP4E_VERSION} autowatch.
This mode waits for Teach Pendant Play first.
It does not start Kunwei streaming or write RTDE inputs while waiting.

Open this Teach Pendant program first:
  ${EXPECTED_PROGRAM}

Then run this mode and press Play on the Teach Pendant.
The bridge will start automatically only after Dashboard reports that exact Step4e program running.
WARNING
    run_bench_gate
    ensure_no_existing_bridge
    wait_for_tp_play_autowatch
    run_bridge_for_mode "${RUN_ROOT}/bridge_${RUN_LABEL}_autowatch_${STAMP}" 1
    ;;
  *-bridge)
    cat <<WARNING
STEP4e ${STEP4E_MODE} ${STEP4E_VERSION} lifecycle bridge.
This sends Kunwei 48 AA 0D 0A and writes UR RTDE input registers.
It does not send URScript from Ubuntu.

Before pressing Play, open this Teach Pendant program:
  ${EXPECTED_PROGRAM}

Motion/control:
  preview = no motion, geo/hold/line setup = ${SEARCH_DESCRIPTION}
  line XY speed command = ${STEP4E_LINE_SPEED_M_S} m/s, max Cartesian command = 6 mm/s
  speedl acceleration = 300 mm/s^2, hold time = 2 ms
  force target = 5 N for hold/line only; geo contact witness triggers around 1-1.5 N
  raw normal guard = ${MAX_NORMAL_FORCE_N} N, force norm guard = 50 N, torque guard = ${MAX_TORQUE_NORM_NM} Nm
  attitude proxy = bounded wx/wy velocity command, gain = ${STEP4E_ORIENTATION_GAIN}, angular limit = ${STEP4E_ANGULAR_LIMIT_RAD_S} rad/s, wx sign = ${STEP4E_ORIENTATION_WX_SIGN}, wy sign = ${STEP4E_ORIENTATION_WY_SIGN}, yaw frozen

Type START_STEP4E_${CONFIRM_TOKEN}_${STEP4E_VERSION^^} to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP4E_${CONFIRM_TOKEN}_${STEP4E_VERSION^^}" ]]; then
      echo "aborted"
      exit 2
    fi
    run_bench_gate
    ensure_no_existing_bridge
    run_bridge_for_mode "${RUN_ROOT}/bridge_${RUN_LABEL}_${STAMP}" 0
    ;;
esac
