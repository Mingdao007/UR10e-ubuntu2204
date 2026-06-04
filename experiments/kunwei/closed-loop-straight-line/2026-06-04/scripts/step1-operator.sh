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

Step1 is the full no-contact pipeline:
  - Ubuntu starts Kunwei stream, software zero, logging, and RTDE input writes.
  - Teach pendant runs /programs/andyl/kunwei/step1_full_no_contact_pipeline.script.
  - Robot moves to reference XY start at current Z, runs the full no-contact
    reference line, then retracts +5 mm.
  - No contact search, force-control correction, TCP/payload write, UR zero, or
    Kunwei tare/config write.
USAGE
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
  *)
    usage
    exit 2
    ;;
esac
