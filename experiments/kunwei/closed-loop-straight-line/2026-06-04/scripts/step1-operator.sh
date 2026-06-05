#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"
RUN_ROOT="${ROOT}/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"

usage() {
  cat <<'USAGE'
Usage:
  step1-operator.sh preflight
  step1-operator.sh bridge
  step1-operator.sh bridge-highrate-500
  step1-operator.sh bridge-highrate-250

Step1 is the full no-contact pipeline:
  - Ubuntu starts Kunwei stream, software zero, logging, and RTDE input writes.
  - Teach pendant runs /programs/andyl/kunwei/step1_full_no_contact_pipeline.script.
  - Robot moves to reference XY start at current Z, descends 50 mm in base Z,
    runs the full no-contact reference line, then retracts +50 mm.
  - No contact search, force-control correction, TCP/payload write, UR zero, or
    Kunwei tare/config write.

High-rate no-contact variants:
  - Run step0-operator.sh benchmark-freq-500 before bridge-highrate-500.
  - If 500 Hz fails, run step0-operator.sh benchmark-freq-250 before bridge-highrate-250.
  - Teach pendant scripts are:
    /programs/andyl/kunwei/step1_full_no_contact_pipeline_500hz.script
    /programs/andyl/kunwei/step1_full_no_contact_pipeline_250hz.script
USAGE
}

bridge_common() {
  local out_dir="$1"
  local rtde_hz="$2"
  local duration_s="$3"
  local socket_timeout_s="$4"
  python3 "${ROOT}/tools/kunwei_rtde_bridge.py" \
    --allow-kunwei-stream-command \
    --write-rtde-inputs \
    --baseline-s 5 \
    --rezero-s 1 \
    --duration-s "${duration_s}" \
    --rtde-hz "${rtde_hz}" \
    --socket-timeout-s "${socket_timeout_s}" \
    --target-force-n 3 \
    --normal-axis fz \
    --normal-sign 1 \
    --max-force-norm-n 15 \
    --output-dir "${out_dir}"
}

mode="${1:-}"
case "${mode}" in
  preflight)
    out_dir="${RUN_ROOT}/preflight_step1_${STAMP}"
    python3 "${ROOT}/tools/preflight_readonly.py" --output-dir "${out_dir}"
    ;;
  bridge)
    cat <<'WARNING'
STEP1 bridge:
This will send Kunwei 48 AA 0D 0A and write UR RTDE input registers.
It will not send URScript and will not move the robot.
Start this first, then run /programs/andyl/kunwei/step1_full_no_contact_pipeline.script on the teach pendant.
Type START_STEP1_BRIDGE to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP1_BRIDGE" ]]; then
      echo "aborted"
      exit 2
    fi
    out_dir="${RUN_ROOT}/bridge_step1_full_${STAMP}"
    bridge_common "${out_dir}" 125 60 0.01
    ;;
  bridge-highrate-500)
    cat <<'WARNING'
STEP1 high-rate 500 Hz bridge:
Use only after step0-operator.sh benchmark-freq-500 passes.
This will send Kunwei 48 AA 0D 0A and write UR RTDE input registers at 500 Hz.
It will not send URScript and will not move the robot.
Start this first, then run /programs/andyl/kunwei/step1_full_no_contact_pipeline_500hz.script on the teach pendant.
Type START_STEP1_500_BRIDGE to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP1_500_BRIDGE" ]]; then
      echo "aborted"
      exit 2
    fi
    out_dir="${RUN_ROOT}/bridge_step1_highrate_500hz_${STAMP}"
    bridge_common "${out_dir}" 500 90 0.0
    ;;
  bridge-highrate-250)
    cat <<'WARNING'
STEP1 high-rate 250 Hz bridge:
Use after 500 Hz fails or if you are explicitly validating the 250 Hz fallback.
This will send Kunwei 48 AA 0D 0A and write UR RTDE input registers at 250 Hz.
It will not send URScript and will not move the robot.
Start this first, then run /programs/andyl/kunwei/step1_full_no_contact_pipeline_250hz.script on the teach pendant.
Type START_STEP1_250_BRIDGE to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP1_250_BRIDGE" ]]; then
      echo "aborted"
      exit 2
    fi
    out_dir="${RUN_ROOT}/bridge_step1_highrate_250hz_${STAMP}"
    bridge_common "${out_dir}" 250 90 0.0
    ;;
  *)
    usage
    exit 2
    ;;
esac
