#!/usr/bin/env bash
set -euo pipefail

# Cold-start / overnight-ready three-stream drift launcher.
#
# Workflow:
#   1. Start this script in a real terminal or tmux before powering/starting the bench.
#   2. The launcher waits until UR Dashboard, UR RTDE, and OnRobot TCP DAQ are reachable.
#   3. It waits until a UR no-motion program is running and the OnRobot URCap
#      RTDE registers show live activity.
#   4. It starts the interruptible logger:
#        - UR actual_TCP_force via RTDE at 500 Hz
#        - OnRobot URCap variables via output_double_register_24..29 at ~125 Hz
#        - OnRobot Compute Box UDP raw with SPEED=2 at ~500 Hz
#   5. Press Ctrl+C to stop early. The Python logger catches SIGINT/SIGTERM,
#      sends final UDP STOP, summarizes the partial data, and writes plots.

ROBOT_HOST="${ROBOT_HOST:-192.168.1.18}"
COMPUTE_BOX_HOST="${COMPUTE_BOX_HOST:-192.168.1.1}"
CAP_SECONDS="${CAP_SECONDS:-86400}"               # 24 h cap; change if needed.
CHECKPOINT_INTERVAL_S="${CHECKPOINT_INTERVAL_S:-300}" # 5 min checkpoint.
POLL_INTERVAL_S="${POLL_INTERVAL_S:-5}"
REQUIRE_PROGRAM_RUNNING="${REQUIRE_PROGRAM_RUNNING:-1}"
REQUIRE_URCAP_ACTIVITY="${REQUIRE_URCAP_ACTIVITY:-1}"
PREFIX="${PREFIX:-three_stream_coldstart_drift}"
OUT_ROOT="${OUT_ROOT:-/home/andy/ur10e_ros2_ws/experiments/$(date +%Y%m%d)_onrobot_three_stream_coldstart_drift}"
STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${OUT_ROOT}/run_${STAMP}"
PY_CAPTURE="/home/andy/ur10e_ros2_ws/scripts/capture_onrobot_ur_three_stream_interruptible.py"

mkdir -p "${RUN_DIR}"
LOG="${RUN_DIR}/launcher_${STAMP}.log"
exec > >(tee -a "${LOG}") 2>&1

echo "[launcher] run_dir=${RUN_DIR}"
echo "[launcher] log=${LOG}"
echo "[launcher] cap_seconds=${CAP_SECONDS}"
echo "[launcher] checkpoint_interval_s=${CHECKPOINT_INTERVAL_S}"
echo "[launcher] robot_host=${ROBOT_HOST}"
echo "[launcher] compute_box_host=${COMPUTE_BOX_HOST}"
echo "[launcher] require_program_running=${REQUIRE_PROGRAM_RUNNING}"
echo "[launcher] require_urcap_activity=${REQUIRE_URCAP_ACTIVITY}"
echo "[launcher] start_time=$(date --iso-8601=seconds)"
echo
echo "[launcher] Waiting for UR Dashboard/RTDE and OnRobot Compute Box TCP DAQ."
echo "[launcher] You can start/power the bench now. Press Ctrl+C before capture starts to abort."

python3 - <<PY
import socket
import time
from datetime import datetime

robot = "${ROBOT_HOST}"
compute = "${COMPUTE_BOX_HOST}"
poll_s = float("${POLL_INTERVAL_S}")
targets = [
    ("UR Dashboard", robot, 29999),
    ("UR RTDE", robot, 30004),
    ("OnRobot TCP DAQ", compute, 49151),
]

def can_connect(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

while True:
    states = [(name, host, port, can_connect(host, port)) for name, host, port in targets]
    print(
        datetime.now().isoformat(timespec="seconds"),
        " ".join(f"{name}={'OK' if ok else 'WAIT'}" for name, _, _, ok in states),
        flush=True,
    )
    if all(ok for _, _, _, ok in states):
        break
    time.sleep(poll_s)
PY

if [[ "${REQUIRE_PROGRAM_RUNNING}" == "1" || "${REQUIRE_URCAP_ACTIVITY}" == "1" ]]; then
  echo
  echo "[launcher] Waiting for no-motion URCap export program/register activity."
  echo "[launcher] Keep the wait/export loop running for the whole capture."
  python3 - <<PY
import socket
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts")
from _ur_common import RTDEClient, dashboard_exchange

robot = "${ROBOT_HOST}"
poll_s = float("${POLL_INTERVAL_S}")
require_program = "${REQUIRE_PROGRAM_RUNNING}" == "1"
require_activity = "${REQUIRE_URCAP_ACTIVITY}" == "1"
fields = [f"output_double_register_{i}" for i in range(24, 30)]

def program_running():
    try:
        res = dashboard_exchange(
            robot,
            ["robotmode", "safetymode", "running", "programState"],
            port=29999,
            timeout=2.0,
        )
        running = res.get("running", "").strip() == "Program running: true"
        normal = "NORMAL" in res.get("safetymode", "")
        return running and normal, res
    except Exception as exc:
        return False, {"error": f"{type(exc).__name__}: {exc}"}

def urcap_register_activity():
    try:
        samples = []
        with RTDEClient(robot, port=30004, timeout=3.0) as client:
            client.negotiate()
            recipe_id, type_names = client.setup_outputs(125.0, fields)
            client.start()
            start = time.perf_counter()
            while time.perf_counter() - start < 1.25:
                values = client.recv_recipe_sample(recipe_id, type_names)
                samples.append(tuple(round(float(v), 9) for v in values))
        if len(samples) < 20:
            return False, {"samples": len(samples), "reason": "too_few_samples"}
        distinct = len(set(samples))
        all_zero = all(all(abs(v) < 1e-12 for v in sample) for sample in samples)
        return (distinct >= 2 and not all_zero), {
            "samples": len(samples),
            "distinct": distinct,
            "first": samples[0],
            "last": samples[-1],
            "all_zero": all_zero,
        }
    except Exception as exc:
        return False, {"error": f"{type(exc).__name__}: {exc}"}

while True:
    program_ok, program_info = program_running()
    activity_ok, activity_info = urcap_register_activity()
    ok = (program_ok or not require_program) and (activity_ok or not require_activity)
    print(
        datetime.now().isoformat(timespec="seconds"),
        f"program={'OK' if program_ok else 'WAIT'}",
        f"urcap_register_activity={'OK' if activity_ok else 'WAIT'}",
        f"program_info={program_info}",
        f"activity_info={activity_info}",
        flush=True,
    )
    if ok:
        break
    time.sleep(poll_s)
PY
fi

echo
echo "[launcher] Network endpoints are reachable. Starting live capture."
echo "[launcher] This sends OnRobot UDP SPEED=2, START, and final STOP."
echo "[launcher] Stop command: press Ctrl+C in this terminal."
echo

python3 "${PY_CAPTURE}" \
  --seconds "${CAP_SECONDS}" \
  --checkpoint-interval-s "${CHECKPOINT_INTERVAL_S}" \
  --prefix "${PREFIX}" \
  --output-dir "${RUN_DIR}" \
  --confirm INTERRUPTIBLE_THREE_STREAM_SPEED2

echo
echo "[launcher] capture_finished=$(date --iso-8601=seconds)"
echo "[launcher] run_dir=${RUN_DIR}"
