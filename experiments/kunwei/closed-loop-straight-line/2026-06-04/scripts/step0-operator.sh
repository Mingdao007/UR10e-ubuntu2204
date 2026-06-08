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
  step0-operator.sh benchmark-freq-500
  step0-operator.sh benchmark-freq-250
  step0-operator.sh step3-0c-pose-math-500
  step0-operator.sh step3-0d-min-speedl-500
  step0-operator.sh bridge-minimal-log

What this script does:
  preflight          Read-only UR Dashboard/RTDE and Kunwei connect-only check.
  bridge-echo        Starts Kunwei stream + writes UR RTDE input registers for
                     step0_register_echo.script. No robot motion.
  benchmark-freq     No-motion frequency sweep. Requests 125, 250, 500, and
                     1000 Hz RTDE input writes and measures UR echo rate.
  step3-0c-pose-math-500
                     No-motion 500 Hz isolation for
                     step3_0c_pose_math_500hz_v1.urp: URScript reads registers,
                     calls get_actual_tcp_pose(), runs filter/control math, and
                     echoes output registers. No robot motion.
  step3-0d-min-speedl-500
                     Minimal 500 Hz speedl isolation for
                     step3_0d_min_speedl_500hz_v1.urp: speedl t=2 ms, 2 mm/s,
                     100 mm/s^2, about 1 mm +X then 1 mm back. No contact or
                     force-control law.
  bridge-minimal-log Starts Kunwei stream and logging only; no RTDE input writes.

Teach pendant remains the motion authority:
  - step0 echo check: /programs/andyl/kunwei/step0_register_echo.script
  - minimal line: /programs/andyl/kunwei/step0_no_contact_straight_10mm.script
USAGE
}

run_step3_0c_pose_math_500() {
  cat <<'WARNING'
This will send Kunwei 48 AA 0D 0A and write UR RTDE input registers at 500 Hz.
It will not send URScript from Ubuntu and will not move the robot.
Open /programs/andyl/kunwei/step3_0c_pose_math_500hz_v1.urp on the teach pendant.
After typing START_STEP3_0C here, press Play on the teach pendant during the 3 s baseline window.
The program itself does no speedl, no movel, no stopl, no zero, and no tare.
Pass/fail is based on measured UR echo heartbeat transitions, not requested rate.
Type START_STEP3_0C to continue:
WARNING
  read -r confirm
  if [[ "${confirm}" != "START_STEP3_0C" ]]; then
    echo "aborted"
    exit 2
  fi
  out_dir="${RUN_ROOT}/step3_0c_pose_math_500hz_${STAMP}"
  python3 "${ROOT}/tools/benchmark_rtde_frequency.py" \
    --allow-kunwei-stream-command \
    --baseline-s 3 \
    --duration-s 8 \
    --rates 500 \
    --target-force-n 5 \
    --normal-axis fz \
    --normal-sign 1 \
    --max-normal-force-n 20 \
    --max-force-norm-n 50 \
    --sensor-stale-s 0.10 \
    --output-dir "${out_dir}"
}

run_step3_0d_min_speedl_500() {
  cat <<'WARNING'
This will send Kunwei 48 AA 0D 0A and write UR RTDE input registers at 500 Hz.
It will not send URScript from Ubuntu.
Open /programs/andyl/kunwei/step3_0d_min_speedl_500hz_v1.urp on the teach pendant.
After typing START_STEP3_0D here, press Play on the teach pendant during the 3 s baseline window.
The program uses speedl(t=0.002) with a small no-contact XY motion:
  speed = 2 mm/s, acceleration = 100 mm/s^2, about 1 mm +X then 1 mm back.
It does no contact search, no force-control law, no zero, and no tare.
Pass/fail is based on measured UR echo heartbeat transitions, not requested rate.
Type START_STEP3_0D to continue:
WARNING
  read -r confirm
  if [[ "${confirm}" != "START_STEP3_0D" ]]; then
    echo "aborted"
    exit 2
  fi
  out_dir="${RUN_ROOT}/step3_0d_min_speedl_500hz_${STAMP}"
  python3 "${ROOT}/tools/benchmark_rtde_frequency.py" \
    --allow-kunwei-stream-command \
    --baseline-s 3 \
    --duration-s 8 \
    --rates 500 \
    --target-force-n 5 \
    --normal-axis fz \
    --normal-sign 1 \
    --max-normal-force-n 20 \
    --max-force-norm-n 50 \
    --sensor-stale-s 0.10 \
    --output-dir "${out_dir}"
}

run_benchmark_rate() {
  local rate_hz="$1"
  local confirm_phrase="START_FREQ_${rate_hz}"
  cat <<WARNING
This will send Kunwei 48 AA 0D 0A and write UR RTDE input registers at ${rate_hz} Hz.
It will not send URScript and will not move the robot.
Start /programs/andyl/kunwei/step0_register_echo.script on the teach pendant while this is running.
Pass/fail is based on measured UR echo heartbeat transitions, not requested rate.
Type ${confirm_phrase} to continue:
WARNING
  read -r confirm
  if [[ "${confirm}" != "${confirm_phrase}" ]]; then
    echo "aborted"
    exit 2
  fi
  out_dir="${RUN_ROOT}/rtde_frequency_benchmark_${rate_hz}hz_${STAMP}"
  python3 "${ROOT}/tools/benchmark_rtde_frequency.py" \
    --allow-kunwei-stream-command \
    --baseline-s 3 \
    --duration-s 8 \
    --rates "${rate_hz}" \
    --target-force-n 3 \
    --normal-axis fz \
    --normal-sign 1 \
    --output-dir "${out_dir}"
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
Start this first, then run /programs/andyl/kunwei/step0_register_echo.script on the teach pendant.
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
Start /programs/andyl/kunwei/step0_register_echo.script on the teach pendant while this is running.
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
  benchmark-freq-500)
    run_benchmark_rate 500
    ;;
  benchmark-freq-250)
    run_benchmark_rate 250
    ;;
  step3-0c-pose-math-500)
    run_step3_0c_pose_math_500
    ;;
  step3-0d-min-speedl-500)
    run_step3_0d_min_speedl_500
    ;;
  bridge-full|bridge-step1)
    echo "step1 is separated now. Use: ${ROOT}/scripts/step1-operator.sh bridge" >&2
    exit 2
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
