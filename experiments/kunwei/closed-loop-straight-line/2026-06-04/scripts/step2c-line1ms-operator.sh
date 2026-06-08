#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
RUN_ROOT="${ROOT}/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"

usage() {
  cat <<'USAGE'
Usage:
  step2c-line1ms-operator.sh bridge

Teach Pendant program:
  /programs/andyl/kunwei/00_current/step2c_aggressive_search5_guard20_line1ms_alpha70_vlim5_v1.urp

Bridge settings:
  RTDE writes: 500 Hz
  Kunwei socket timeout: nonblocking
  sensor stale: 100 ms
  normal axis/sign: fz, +1
  target force: 5 N
  raw normal hard guard: 20 N

Motion settings in the TP program:
  search: 5 mm/s, 500 mm/s^2, speedl t=10 ms
  line: 10 mm/s tangent, 500 mm/s^2, first 0.10 s at t=10 ms, then t=1 ms
USAGE
}

mode="${1:-}"
case "${mode}" in
  bridge)
    cat <<'WARNING'
STEP2C line1ms bridge:
This sends Kunwei 48 AA 0D 0A and writes UR RTDE input registers.
It does not send URScript from Ubuntu.
Open and Play on the Teach Pendant:
  /programs/andyl/kunwei/00_current/step2c_aggressive_search5_guard20_line1ms_alpha70_vlim5_v1.urp

Program motion:
  search = 5 mm/s, acceleration = 500 mm/s^2, speedl t=10 ms
  line tangent = 10 mm/s, acceleration = 500 mm/s^2
  line fast hold = 1 ms after the first 0.10 s

Safety:
  target force = 5 N
  raw normal guard = 20 N
  force norm guard = 50 N
  torque norm guard = 0.6 Nm
  sensor stale = 100 ms

Type START_STEP2C_LINE1MS to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP2C_LINE1MS" ]]; then
      echo "aborted"
      exit 2
    fi
    out_dir="${RUN_ROOT}/bridge_step2c_line1ms_search5_guard20_alpha70_vlim5_${STAMP}"
    python3 "${ROOT}/tools/kunwei_rtde_bridge.py" \
      --allow-kunwei-stream-command \
      --write-rtde-inputs \
      --baseline-s 5 \
      --rezero-s 1 \
      --duration-s 150 \
      --rtde-hz 500 \
      --socket-timeout-s 0.0 \
      --sensor-stale-s 0.10 \
      --target-force-n 5 \
      --normal-axis fz \
      --normal-sign 1 \
      --max-normal-force-n 20 \
      --max-force-norm-n 50 \
      --output-dir "${out_dir}"
    ;;
  *)
    usage
    exit 2
    ;;
esac
