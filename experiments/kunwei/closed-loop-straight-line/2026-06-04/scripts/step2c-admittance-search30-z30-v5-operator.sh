#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
RUN_ROOT="${ROOT}/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
EXPECTED_PROGRAM="/programs/andyl/kunwei/step2/step2c_admittance_search30_z30_nostop_guard20_search2ms_line1ms_alpha70_v5.urp"
EXPECTED_BASENAME="step2c_admittance_search30_z30_nostop_guard20_search2ms_line1ms_alpha70_v5.urp"
ROBOT_HOST="${ROBOT_HOST:-192.168.1.18}"
DASHBOARD_PORT="${DASHBOARD_PORT:-29999}"
WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-45}"
DASHBOARD_MISS_LIMIT_S="${DASHBOARD_MISS_LIMIT_S:-5}"

usage() {
  cat <<'USAGE'
Usage:
  step2c-admittance-search30-z30-v5-operator.sh bridge

Teach Pendant program:
  /programs/andyl/kunwei/step2/step2c_admittance_search30_z30_nostop_guard20_search2ms_line1ms_alpha70_v5.urp

Bridge lifecycle:
  starts Kunwei/RTDE bridge, then waits up to 45 s for TP Play
  stops bridge when the TP program stops, safety is not NORMAL, or Dashboard is unreachable
  sends Kunwei stop-stream quiet command after bridge exit
  writes stage_frequency_summary.json after bridge exit

Motion settings in the TP program:
  pre-contact: direct Z prep to old contact Z + 30 mm, with no explicit stopl after orientation, XY, or Z prep
  far search: 30 mm/s, 500 mm/s^2, speedl t=2 ms
  near search: 5 mm/s inside 10 mm above known contact Z, speedl t=2 ms
  soft acquisition: 2 N velocity admittance, gain 0.0030, vlim 8 mm/s, speedl t=2 ms
  line: 10 mm/s tangent, 500 mm/s^2, first 0.10 s at t=10 ms, then t=1 ms
  success exit: retract upward 10 mm, then movel back to TP-start home pose
USAGE
}

stage_summary() {
  local out_dir="$1"
  python3 - "$out_dir" <<'PY'
import csv
import json
import math
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])
csv_path = out_dir / 'bridge_rtde_500hz.csv'
summary_path = out_dir / 'stage_frequency_summary.json'
if not csv_path.exists():
    result = {'ok': False, 'issue': 'bridge_rtde_500hz.csv not found', 'out_dir': str(out_dir)}
    summary_path.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    raise SystemExit(0)

rows = []
with csv_path.open(newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        try:
            row['_t'] = float(row['t_monotonic_s'])
            row['_stage'] = float(row.get('ur_output_double_register_35') or 'nan')
            row['_echo'] = float(row.get('ur_output_double_register_26') or 'nan')
        except ValueError:
            continue
        rows.append(row)

def rate_for(stage):
    rs = [r for r in rows if math.isfinite(r['_stage']) and abs(r['_stage'] - stage) < 0.05]
    if len(rs) < 2:
        return {'stage': stage, 'duration_s': 0.0, 'rtde_rows': len(rs), 'rtde_row_rate_hz': None, 'echo_transitions': 0, 'echo_rate_hz': None}
    duration = rs[-1]['_t'] - rs[0]['_t']
    transitions = 0
    prev = rs[0]['_echo']
    for r in rs[1:]:
        cur = r['_echo']
        if math.isfinite(cur) and cur != prev:
            transitions += 1
            prev = cur
    return {
        'stage': stage,
        'duration_s': duration,
        'rtde_rows': len(rs),
        'rtde_row_rate_hz': (len(rs) - 1) / duration if duration > 0 else None,
        'echo_transitions': transitions,
        'echo_rate_hz': transitions / duration if duration > 0 else None,
    }

if len(rows) >= 2:
    total_duration = rows[-1]['_t'] - rows[0]['_t']
else:
    total_duration = 0.0
result = {
    'ok': True,
    'bridge_csv': str(csv_path),
    'total_rows': len(rows),
    'bridge_write_rate_hz': (len(rows) - 1) / total_duration if total_duration > 0 else None,
    'rtde_output_logging_rate_hz': (len(rows) - 1) / total_duration if total_duration > 0 else None,
    'stage24_search_echo_rate': rate_for(24.0),
    'stage24_4_soft_acquisition_echo_rate': rate_for(24.4),
    'stage25_ft_line_control_echo_rate': rate_for(25.0),
    'stage26_unload_echo_rate': rate_for(26.0),
    'stage27_retract_echo_rate': rate_for(27.0),
    'frequency_contract_note': 'FT control phase is output_double_register_35 == 25.0; echo rate is heartbeat transition rate from ur_output_double_register_26.',
}
summary_path.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
PY
}

dashboard_monitor() {
  local bridge_pid="$1"
  python3 - "$bridge_pid" "$EXPECTED_PROGRAM" "$EXPECTED_BASENAME" "$ROBOT_HOST" "$DASHBOARD_PORT" "$WAIT_FOR_PLAY_S" "$DASHBOARD_MISS_LIMIT_S" <<'PY'
import os
import socket
import sys
import time

bridge_pid = int(sys.argv[1])
expected_program = sys.argv[2]
expected_basename = sys.argv[3]
host = sys.argv[4]
port = int(sys.argv[5])
wait_for_play_s = float(sys.argv[6])
miss_limit_s = float(sys.argv[7])

def bridge_alive():
    try:
        os.kill(bridge_pid, 0)
        return True
    except OSError:
        return False

def dash_cmd(cmd, timeout=1.0):
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        try:
            s.recv(4096)
        except socket.timeout:
            pass
        s.sendall((cmd + '\n').encode('ascii'))
        data = s.recv(4096).decode('utf-8', errors='replace').strip()
        return data

def snapshot():
    running = dash_cmd('running')
    loaded = dash_cmd('get loaded program')
    state = dash_cmd('programState')
    safety = dash_cmd('safetymode')
    text = '\n'.join([running, loaded, state, safety])
    return {
        'running': 'true' in running.lower(),
        'loaded_ok': expected_program in loaded or expected_basename in loaded,
        'stopped': 'STOPPED' in state.upper() or 'false' in running.lower(),
        'safety_normal': 'NORMAL' in safety.upper(),
        'raw': text,
    }

def stop_bridge(reason):
    print(f'[operator] stopping bridge: {reason}', flush=True)
    try:
        os.kill(bridge_pid, 2)
    except OSError:
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and bridge_alive():
        time.sleep(0.1)
    if bridge_alive():
        try:
            os.kill(bridge_pid, 15)
        except OSError:
            pass

print('[operator] bridge is running. Now press TP Play for:', flush=True)
print(f'  {expected_program}', flush=True)
start = time.monotonic()
last_ok = start
seen_running = False
while bridge_alive():
    now = time.monotonic()
    try:
        snap = snapshot()
        last_ok = now
    except Exception as exc:
        if now - last_ok > miss_limit_s:
            stop_bridge(f'Dashboard unreachable for >{miss_limit_s:.1f} s')
            raise SystemExit(0)
        time.sleep(0.25)
        continue

    if not snap['safety_normal']:
        print(snap['raw'], flush=True)
        stop_bridge('safety mode is not NORMAL')
        raise SystemExit(0)

    if not seen_running:
        if snap['running'] and snap['loaded_ok']:
            seen_running = True
            print('[operator] expected TP program is running; monitoring for stop.', flush=True)
        elif now - start > wait_for_play_s:
            print(snap['raw'], flush=True)
            stop_bridge(f'expected program did not start within {wait_for_play_s:.1f} s')
            raise SystemExit(0)
    else:
        if snap['stopped']:
            stop_bridge('TP program stopped')
            raise SystemExit(0)

    time.sleep(0.25)
PY
}

mode="${1:-}"
case "${mode}" in
  bridge)
    cat <<'WARNING'
STEP2C admittance-search30 z30 v5 lifecycle bridge.
This sends Kunwei 48 AA 0D 0A and writes UR RTDE input registers.
It does not send URScript from Ubuntu.

Before pressing Play, open this Teach Pendant program:
  /programs/andyl/kunwei/step2/step2c_admittance_search30_z30_nostop_guard20_search2ms_line1ms_alpha70_v5.urp

Motion:
  far search = 30 mm/s, acceleration = 500 mm/s^2, speedl t = 2 ms
  near search = 5 mm/s, acceleration = 500 mm/s^2, speedl t = 2 ms
  soft acquisition = 2 N, gain = 0.0030, velocity limit = 8 mm/s, speedl t = 2 ms
  line tangent = 10 mm/s, acceleration = 500 mm/s^2
  line hold = first 0.10 s at 10 ms, then 1 ms

Safety:
  target force = 5 N
  raw normal guard = 20 N
  force norm guard = 50 N
  torque norm guard = 0.6 Nm
  sensor stale = 100 ms

The operator will stop the bridge if the TP program does not start within 45 s,
when the TP program stops, when safety is not NORMAL, or when Dashboard is lost.
After bridge exit it sends repeated Kunwei stop-stream commands and probes quiet output.

Type START_STEP2C_ADMITTANCE30_Z30_V5 to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP2C_ADMITTANCE30_Z30_V5" ]]; then
      echo "aborted"
      exit 2
    fi

    out_dir="${RUN_ROOT}/bridge_step2c_admittance_search30_z30_v5_search2ms_line1ms_alpha70_${STAMP}"
    mkdir -p "${out_dir}"

    bridge_pid=""
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

    dashboard_monitor "${bridge_pid}" || true
    wait "${bridge_pid}" || true
    trap - INT TERM EXIT

    echo "[operator] bridge output: ${out_dir}"

    quiet_json="${out_dir}/kunwei_quiet_stream.json"
    quiet_rc=0
    if python3 "${ROOT}/tools/kunwei_quiet_stream.py" --json-only --output-json "${quiet_json}"; then
      echo "[operator] Kunwei quiet stop passed: ${quiet_json}"
    else
      quiet_rc="$?"
      echo "[operator] Kunwei quiet stop reported issue rc=${quiet_rc}: ${quiet_json}"
    fi
    if [[ -f "${quiet_json}" ]]; then
      cat "${quiet_json}"
    fi

    stage_summary "${out_dir}"
    ;;
  *)
    usage
    exit 2
    ;;
esac
