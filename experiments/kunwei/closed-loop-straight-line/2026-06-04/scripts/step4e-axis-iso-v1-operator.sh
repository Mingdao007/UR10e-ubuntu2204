#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"

export STEP4E_VERSION="axis_iso_v1"
export BRIDGE_DURATION_S="120"
export MAX_NORMAL_FORCE_N="20"
export MAX_TORQUE_NORM_NM="3.0"
export STEP4E_ORIENTATION_GAIN="0.20"
export STEP4E_ORIENTATION_WX_SIGN="1"
export STEP4E_ORIENTATION_WY_SIGN="1"
export STEP4E_ANGULAR_LIMIT_RAD_S="0.10"
export STEP4E_LINE_SPEED_M_S="0.0"
export STEP4E_STAGE25_ONLY="1"

exec "${EXP_DIR}/scripts/step4e-line-v1-operator.sh" "$@"
