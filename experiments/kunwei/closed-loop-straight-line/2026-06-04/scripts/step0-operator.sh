#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
RUN_ROOT="${ROOT}/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"

usage() {
  cat <<'USAGE'
Usage:
  step0-operator.sh preflight
  step0-operator.sh bridge-echo
  step0-operator.sh benchmark-freq
  step0-operator.sh bridge-full
  step0-operator.sh bridge-minimal-log

What this script does:
  preflight          Read-only UR Dashboard/RTDE and Kunwei connect-only check.
  bridge-echo        Starts Kunwei stream + writes UR RTDE input registers for
                     kunwei_register_echo.script. No robot motion.
  benchmark-freq     No-motion frequency sweep. Requests 125, 250, 500, and
                     1000 Hz RTDE input writes and measures UR echo rate.
  bridge-full        Starts Kunwei stream + writes UR RTDE input registers for
                     step0_full_no_contact_pipeline.script. It does not move
                     the robot. You still press Play on the teach pendant.
  bridge-minimal-log Starts Kunwei stream and logging only; no RTDE input writes.

Teach pendant remains the motion authority:
  - echo check: programs/kunwei_register_echo.script
  - minimal line: programs/step0_no_contact_straight_10mm.script
  - full pipeline: programs/step0_full_no_contact_pipeline.script
USAGE
}

mode="${1:-}"
case "${mode}" in
  preflight)
    out_dir="${RUN_ROOT}/preflight_${STAMP}"
    python3 "${ROOT}/tools/preflight_readonly.py" --output-dir "${out_dir}"
    ;;
  bridge-echo)
    cat <<'WARNING'
This will send Kunwei 48 AA 0D 0A and write UR RTDE input registers for echo testing.
It will not send URScript and will not move the robot.
Start this first, then run programs/kunwei_register_echo.script on the teach pendant.
Type START_ECHO to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_ECHO" ]]; then
      echo "aborted"
      exit 2
    fi
    out_dir="${RUN_ROOT}/bridge_echo_${STAMP}"
    python3 "${ROOT}/tools/kunwei_rtde_bridge.py" \
      --allow-kunwei-stream-command \
      --write-rtde-inputs \
      --baseline-s 5 \
      --rezero-s 1 \
      --duration-s 20 \
      --target-force-n 3 \
      --normal-axis fz \
      --normal-sign 1 \
      --output-dir "${out_dir}"
    ;;
  benchmark-freq)
    cat <<'WARNING'
This will send Kunwei 48 AA 0D 0A and write UR RTDE input registers.
It will not send URScript and will not move the robot.
Start programs/kunwei_register_echo.script on the teach pendant while this is running.
The sweep requests 125, 250, 500, and 1000 Hz; pass/fail is based on measured UR echo.
Type START_FREQ_BENCH to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_FREQ_BENCH" ]]; then
      echo "aborted"
      exit 2
    fi
    out_dir="${RUN_ROOT}/rtde_frequency_benchmark_${STAMP}"
    python3 "${ROOT}/tools/benchmark_rtde_frequency.py" \
      --allow-kunwei-stream-command \
      --baseline-s 3 \
      --duration-s 8 \
      --rates 125,250,500,1000 \
      --target-force-n 3 \
      --normal-axis fz \
      --normal-sign 1 \
      --output-dir "${out_dir}"
    ;;
  bridge-full)
    cat <<'WARNING'
This will send Kunwei 48 AA 0D 0A and write UR RTDE input registers.
It will not send URScript and will not move the robot.
Start this first, then run programs/step0_full_no_contact_pipeline.script on the teach pendant.
Type START_BRIDGE to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_BRIDGE" ]]; then
      echo "aborted"
      exit 2
    fi
    out_dir="${RUN_ROOT}/bridge_step0_full_${STAMP}"
    python3 "${ROOT}/tools/kunwei_rtde_bridge.py" \
      --allow-kunwei-stream-command \
      --write-rtde-inputs \
      --baseline-s 5 \
      --rezero-s 1 \
      --duration-s 30 \
      --target-force-n 3 \
      --normal-axis fz \
      --normal-sign 1 \
      --output-dir "${out_dir}"
    ;;
  bridge-minimal-log)
    cat <<'WARNING'
This will send Kunwei 48 AA 0D 0A and log sensor data only.
It will not write UR RTDE input registers and will not move the robot.
Type START_LOG to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_LOG" ]]; then
      echo "aborted"
      exit 2
    fi
    out_dir="${RUN_ROOT}/bridge_minimal_log_${STAMP}"
    python3 "${ROOT}/tools/kunwei_rtde_bridge.py" \
      --allow-kunwei-stream-command \
      --baseline-s 5 \
      --duration-s 20 \
      --target-force-n 3 \
      --normal-axis fz \
      --normal-sign 1 \
      --output-dir "${out_dir}"
    ;;
  *)
    usage
    exit 2
    ;;
esac
