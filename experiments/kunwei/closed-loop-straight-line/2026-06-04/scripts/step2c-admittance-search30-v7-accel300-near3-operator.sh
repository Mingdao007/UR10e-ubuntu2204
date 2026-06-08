#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
RUN_ROOT="${ROOT}/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
EXPECTED_PROGRAM="/programs/andyl/kunwei/step2/step2c_admittance_search30_guard20_search2ms_line1ms_alpha70_v7_accel300_near3.urp"
EXPECTED_BASENAME="step2c_admittance_search30_guard20_search2ms_line1ms_alpha70_v7_accel300_near3.urp"
ROBOT_HOST="${ROBOT_HOST:-192.168.1.18}"
DASHBOARD_PORT="${DASHBOARD_PORT:-29999}"
WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-45}"
AUTOWATCH_WAIT_FOR_PLAY_S="${AUTOWATCH_WAIT_FOR_PLAY_S:-600}"
DASHBOARD_MISS_LIMIT_S="${DASHBOARD_MISS_LIMIT_S:-5}"
BENCH_GATE="/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/check_ubuntu_network.py"
DATA_PYTHON="/home/andy/ur10e_ros2_ws/scripts/ur10e_data_python.sh"

usage() {
  cat <<'USAGE'
Usage:
  step2c-admittance-search30-v7-accel300-near3-operator.sh bridge
  step2c-admittance-search30-v7-accel300-near3-operator.sh autowatch

Teach Pendant program:
  /programs/andyl/kunwei/step2/step2c_admittance_search30_guard20_search2ms_line1ms_alpha70_v7_accel300_near3.urp

Bridge lifecycle:
  bridge: starts Kunwei/RTDE bridge, then waits up to 45 s for TP Play
  autowatch: waits for TP Play, then starts Kunwei/RTDE bridge automatically
  stops bridge when the TP program stops, safety is not NORMAL, or Dashboard is unreachable
  sends Kunwei stop-stream quiet command after bridge exit
  writes stage_frequency_summary.json after bridge exit
  writes fz_tracking.png after bridge exit

Motion settings in the TP program:
  pre-contact: v4-style bounded Z prep into old contact Z + 30..100 mm, with stopl after orientation/XY/Z moves
  far search: 30 mm/s, 300 mm/s^2, speedl t=2 ms
  near search: 3 mm/s inside 10 mm above known contact Z, speedl t=2 ms
  early contact trigger: signed Fz <= -0.5 N in the near zone
  soft acquisition: 2 N velocity admittance, gain 0.0030, vlim 8 mm/s, speedl t=2 ms
  line: 10 mm/s tangent, 500 mm/s^2, first 0.10 s at t=10 ms, then t=1 ms; v4 normal P/I gains
  success exit: retract upward 10 mm, then movel back to TP-start home pose
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

fz_plot() {
  local out_dir="$1"
  "${DATA_PYTHON}" - "$out_dir" <<'FZ_PLOT_PY'
import json
import math
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

out_dir = Path(sys.argv[1])
csv_path = out_dir / "bridge_rtde_500hz.csv"
plot_path = out_dir / "fz_tracking.png"
summary_path = out_dir / "fz_tracking_summary.json"

if not csv_path.exists():
    result = {"ok": False, "issue": "bridge_rtde_500hz.csv not found", "out_dir": str(out_dir)}
    summary_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0)

usecols = [
    "t_monotonic_s",
    "normal_force_n",
    "target_force_n",
    "ur_output_double_register_35",
]
df = pd.read_csv(csv_path, usecols=lambda c: c in usecols)
df = df.dropna(subset=["t_monotonic_s", "normal_force_n"])
if df.empty:
    result = {"ok": False, "issue": "no Fz rows found", "bridge_csv": str(csv_path)}
    summary_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0)

t = df["t_monotonic_s"] - float(df["t_monotonic_s"].iloc[0])
fz = df["normal_force_n"]
stage = df.get("ur_output_double_register_35")

fig, ax = plt.subplots(figsize=(11, 5.5), dpi=160)
if stage is not None:
    bands = [
        (24.0, "#e8f1ff", "search"),
        (24.4, "#fff4d8", "soft acquire"),
        (25.0, "#e7f7ed", "FT line"),
        (26.0, "#f4e8ff", "retract 10mm"),
        (27.0, "#eeeeee", "return home"),
    ]
    label_seen = set()
    for st, color, label in bands:
        mask = (stage - st).abs() < 0.05
        if not mask.any():
            continue
        indices = list(df.index[mask])
        spans = []
        start = prev = indices[0]
        for idx in indices[1:]:
            if idx == prev + 1:
                prev = idx
            else:
                spans.append((start, prev))
                start = prev = idx
        spans.append((start, prev))
        for start, end in spans:
            x0 = float(t.loc[start])
            x1 = float(t.loc[end])
            ax.axvspan(x0, x1, color=color, alpha=0.55, label=label if label not in label_seen else None)
            label_seen.add(label)

ax.plot(t, fz, color="#111827", linewidth=1.1, label="signed Fz / normal force")
if "target_force_n" in df.columns:
    target = -df["target_force_n"].ffill().fillna(5.0)
    ax.plot(t, target, color="#dc2626", linewidth=1.0, linestyle="--", label="-target force")

ax.axhline(0.0, color="#6b7280", linewidth=0.7)
ax.set_title("Step2C v7 v4 accel300 near3 Fz Tracking")
ax.set_xlabel("time since bridge start (s)")
ax.set_ylabel("force (N)")
ax.grid(True, color="#d1d5db", linewidth=0.6, alpha=0.8)
ax.legend(loc="best", fontsize=8)
fig.tight_layout()
fig.savefig(plot_path)
plt.close(fig)

stage25 = df[(stage - 25.0).abs() < 0.05] if stage is not None else df.iloc[0:0]
result = {
    "ok": True,
    "bridge_csv": str(csv_path),
    "plot": str(plot_path),
    "samples": int(len(df)),
    "fz_min_n": float(fz.min()),
    "fz_max_n": float(fz.max()),
    "fz_mean_n": float(fz.mean()),
    "stage25_samples": int(len(stage25)),
    "stage25_fz_mean_n": None if stage25.empty else float(stage25["normal_force_n"].mean()),
    "stage25_fz_min_n": None if stage25.empty else float(stage25["normal_force_n"].min()),
    "stage25_fz_max_n": None if stage25.empty else float(stage25["normal_force_n"].max()),
}
summary_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
print(json.dumps(result, indent=2, sort_keys=True))
FZ_PLOT_PY
}

dashboard_monitor() {
  local bridge_pid="$1"
  local initial_state="${2:-wait_for_play}"
  python3 - "$bridge_pid" "$EXPECTED_PROGRAM" "$EXPECTED_BASENAME" "$ROBOT_HOST" "$DASHBOARD_PORT" "$WAIT_FOR_PLAY_S" "$DASHBOARD_MISS_LIMIT_S" "$initial_state" <<'PY'
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
initial_state = sys.argv[8]

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

if initial_state == 'already_running':
    print('[operator] bridge is running; expected TP program was already playing.', flush=True)
else:
    print('[operator] bridge is running. Now press TP Play for:', flush=True)
    print(f'  {expected_program}', flush=True)
start = time.monotonic()
last_ok = start
seen_running = initial_state == 'already_running'
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

wait_for_tp_play_autowatch() {
  python3 - "$EXPECTED_PROGRAM" "$EXPECTED_BASENAME" "$ROBOT_HOST" "$DASHBOARD_PORT" "$AUTOWATCH_WAIT_FOR_PLAY_S" "$DASHBOARD_MISS_LIMIT_S" <<'PY'
import socket
import sys
import time

expected_program = sys.argv[1]
expected_basename = sys.argv[2]
host = sys.argv[3]
port = int(sys.argv[4])
wait_for_play_s = float(sys.argv[5])
miss_limit_s = float(sys.argv[6])


def dash_cmd(cmd, timeout=1.0):
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        try:
            s.recv(4096)
        except socket.timeout:
            pass
        s.sendall((cmd + "\n").encode("ascii"))
        return s.recv(4096).decode("utf-8", errors="replace").strip()


def snapshot():
    running = dash_cmd("running")
    loaded = dash_cmd("get loaded program")
    state = dash_cmd("programState")
    safety = dash_cmd("safetymode")
    raw = "\n".join([running, loaded, state, safety])
    return {
        "running": "true" in running.lower(),
        "loaded_ok": expected_program in loaded or expected_basename in loaded,
        "safety_normal": "NORMAL" in safety.upper(),
        "raw": raw,
    }


print("[autowatch] Watching for TP Play on:", flush=True)
print(f"  {expected_program}", flush=True)
start = time.monotonic()
last_dashboard_ok = start
while True:
    now = time.monotonic()
    try:
        snap = snapshot()
        last_dashboard_ok = now
    except Exception as exc:
        if now - last_dashboard_ok > miss_limit_s:
            print(f"[autowatch] Dashboard unreachable for >{miss_limit_s:.1f} s: {exc}", flush=True)
            raise SystemExit(4)
        time.sleep(0.25)
        continue

    if not snap["safety_normal"]:
        print(snap["raw"], flush=True)
        print("[autowatch] refusing: safety mode is not NORMAL", flush=True)
        raise SystemExit(5)

    if not snap["loaded_ok"]:
        print(snap["raw"], flush=True)
        print("[autowatch] refusing: loaded program is not the expected v7 .urp", flush=True)
        raise SystemExit(6)

    if snap["running"]:
        print("[autowatch] expected v7 program is running; starting bridge.", flush=True)
        raise SystemExit(0)

    if now - start > wait_for_play_s:
        print(snap["raw"], flush=True)
        print(f"[autowatch] timed out waiting for TP Play after {wait_for_play_s:.1f} s", flush=True)
        raise SystemExit(7)

    time.sleep(0.25)
PY
}

mode="${1:-}"
case "${mode}" in
  autowatch)
    cat <<'WARNING'
STEP2C admittance-search30 v7 v4 accel300 near3 autowatch.
This mode waits for Teach Pendant Play first.
It does not start Kunwei streaming or write RTDE inputs while waiting.

Open this Teach Pendant program first:
  /programs/andyl/kunwei/step2/step2c_admittance_search30_guard20_search2ms_line1ms_alpha70_v7_accel300_near3.urp

Then run this mode and press Play on the Teach Pendant.
The bridge will start automatically only after Dashboard reports that exact v7 program running.
WARNING

    run_bench_gate
    ensure_no_existing_bridge
    wait_for_tp_play_autowatch

    STAMP="$(date +%Y%m%d_%H%M%S)"
    out_dir="${RUN_ROOT}/bridge_step2c_v7_v4_accel300_near3_autowatch_search2ms_line1ms_alpha70_${STAMP}"
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

    dashboard_monitor "${bridge_pid}" already_running || true
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
    fz_plot "${out_dir}"
    ;;
  bridge)
    cat <<'WARNING'
STEP2C admittance-search30 v7 v4 accel300 near3 lifecycle bridge.
This sends Kunwei 48 AA 0D 0A and writes UR RTDE input registers.
It does not send URScript from Ubuntu.

Before pressing Play, open this Teach Pendant program:
  /programs/andyl/kunwei/step2/step2c_admittance_search30_guard20_search2ms_line1ms_alpha70_v7_accel300_near3.urp

Motion:
  far search = 30 mm/s, acceleration = 300 mm/s^2, speedl t = 2 ms
  near search = 3 mm/s, acceleration = 300 mm/s^2, speedl t = 2 ms
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

Type START_STEP2C_ADMITTANCE30_V7_ACCEL300_NEAR3 to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP2C_ADMITTANCE30_V7_ACCEL300_NEAR3" ]]; then
      echo "aborted"
      exit 2
    fi

    out_dir="${RUN_ROOT}/bridge_step2c_v7_v4_accel300_near3_search2ms_line1ms_alpha70_${STAMP}"
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
    fz_plot "${out_dir}"
    ;;
  *)
    usage
    exit 2
    ;;
esac
