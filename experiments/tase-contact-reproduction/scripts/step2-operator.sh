#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"
RUN_ROOT="${ROOT}/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"

NORMAL_AXIS="${NORMAL_AXIS:-fz}"
NORMAL_SIGN="${NORMAL_SIGN:--1}"
RTDE_HZ="${RTDE_HZ:-125}"

usage() {
  cat <<'USAGE'
Usage:
  step2-operator.sh preflight
  step2-operator.sh bridge-search
  step2-operator.sh bridge-pilot
  step2-operator.sh bridge-final

Step2 teach pendant scripts:
  - /programs/andyl/kunwei/step2a_contact_search.script
  - /programs/andyl/kunwei/step2b_closed_loop_pilot.script
  - /programs/andyl/kunwei/step2c_closed_loop_full.script

Environment overrides after Step2A sign evidence:
  NORMAL_AXIS=fx|fy|fz
  NORMAL_SIGN=1|-1
  RTDE_HZ=125|500
Current Step2A evidence: upward press made zeroed Fz negative, so Step2B/C
should normally use NORMAL_AXIS=fz NORMAL_SIGN=-1 unless a new contact-search
run proves otherwise. The live bridge default is 125 Hz; keep 500 Hz as a
separate benchmark/research path until the reset behavior is resolved.

Boundaries:
  - Ubuntu starts Kunwei stream, software zero, logging, and RTDE input writes.
  - Teach pendant remains the motion authority.
  - No UR TCP/payload write, no UR zero_ftsensor, no Kunwei tare/config write.
USAGE
}

bridge_common() {
  local out_dir="$1"
  local duration_s="$2"
  local target_force_n="$3"
  python3 "${ROOT}/tools/kunwei_rtde_bridge.py" \
    --allow-kunwei-stream-command \
    --write-rtde-inputs \
    --baseline-s 5 \
    --rezero-s 1 \
    --duration-s "${duration_s}" \
    --rtde-hz "${RTDE_HZ}" \
    --target-force-n "${target_force_n}" \
    --normal-axis "${NORMAL_AXIS}" \
    --normal-sign "${NORMAL_SIGN}" \
    --output-dir "${out_dir}"
}

mode="${1:-}"
case "${mode}" in
  preflight)
    out_dir="${RUN_ROOT}/preflight_step2_${STAMP}"
    python3 "${ROOT}/tools/preflight_readonly.py" --output-dir "${out_dir}"
    ;;
  bridge-search)
    cat <<WARNING
STEP2A bridge-search:
This sends Kunwei 48 AA 0D 0A and writes UR RTDE input registers.
It does not send URScript and does not move the robot from Ubuntu.
After this is running, select and Play:
  /programs/andyl/kunwei/step2a_contact_search.script

Current normal mapping for register 24: NORMAL_AXIS=${NORMAL_AXIS}, NORMAL_SIGN=${NORMAL_SIGN}
RTDE write rate: ${RTDE_HZ} Hz.
Step2A contact trigger uses raw mapped normal force/register 24 >= 2 N.
force_norm is only the total-force guard in the teach-pendant script.
You may press Play immediately; the script waits up to 15 s for sensor_ok.
Type START_STEP2_SEARCH to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP2_SEARCH" ]]; then
      echo "aborted"
      exit 2
    fi
    bridge_common "${RUN_ROOT}/bridge_step2a_search_${STAMP}" 120 3
    ;;
  bridge-pilot)
    cat <<WARNING
STEP2B bridge-pilot:
Use only after Step2A confirms the Kunwei normal axis/sign.
This sends Kunwei stream command and writes UR RTDE input registers.
After this is running, select and Play:
  /programs/andyl/kunwei/step2b_closed_loop_pilot.script

Current normal mapping: NORMAL_AXIS=${NORMAL_AXIS}, NORMAL_SIGN=${NORMAL_SIGN}
For the current Step2A evidence, expected pilot mapping is NORMAL_AXIS=fz NORMAL_SIGN=-1.
RTDE write rate: ${RTDE_HZ} Hz.
Target force: 3 N. Tangent speed is hardcoded in the TP script: 5 mm/s.
Closed-loop force correction is PI with light filtering in URScript.
Type START_STEP2_PILOT to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP2_PILOT" ]]; then
      echo "aborted"
      exit 2
    fi
    bridge_common "${RUN_ROOT}/bridge_step2b_pilot_${STAMP}" 160 3
    ;;
  bridge-final)
    cat <<WARNING
STEP2C bridge-final:
Use only after the 3 N pilot is accepted.
This sends Kunwei stream command and writes UR RTDE input registers.
After this is running, select and Play:
  /programs/andyl/kunwei/step2c_closed_loop_full.script

Current normal mapping: NORMAL_AXIS=${NORMAL_AXIS}, NORMAL_SIGN=${NORMAL_SIGN}
For the current Step2A evidence, expected final mapping is NORMAL_AXIS=fz NORMAL_SIGN=-1.
RTDE write rate: ${RTDE_HZ} Hz.
Target force: 5 N. Tangent speed is hardcoded in the TP script: 10 mm/s.
Closed-loop force correction is PI with light filtering in URScript.
Type START_STEP2_FINAL to continue:
WARNING
    read -r confirm
    if [[ "${confirm}" != "START_STEP2_FINAL" ]]; then
      echo "aborted"
      exit 2
    fi
    bridge_common "${RUN_ROOT}/bridge_step2c_final_${STAMP}" 150 5
    ;;
  *)
    usage
    exit 2
    ;;
esac
