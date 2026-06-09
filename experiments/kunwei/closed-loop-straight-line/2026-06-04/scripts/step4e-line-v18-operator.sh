#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"

export STEP4E_VERSION="v18"
export BRIDGE_DURATION_S="240"
export STEP4E_NORMAL_COMMAND_SIGN="-1"
export MAX_NORMAL_FORCE_N="50"
export MAX_TORQUE_NORM_NM="3.0"
export STEP4E_ORIENTATION_GAIN="0.20"
export STEP4E_ORIENTATION_WX_SIGN="1"
export STEP4E_ORIENTATION_WY_SIGN="-1"
export STEP4E_ANGULAR_LIMIT_RAD_S="0.10"
export STEP4E_LINE_SPEED_M_S="0.003"
export STEP4E_LINE_SETTLE_S="0.0"
export STEP4E_STAGE25_ONLY="1"
export STEP4E_NORMAL_VELOCITY_LIMIT_M_S="0.002"
export STEP4E_FORCE_P_GAIN="0.0005"
export STEP4E_FORCE_I_GAIN="0.00002"
export STEP4E_FORCE_DAMPING="0.50"
export STEP4E_INTEGRAL_LIMIT_N_S="2.0"
export STEP4E_REACQUIRE_VELOCITY_M_S="0.002"

exec "${EXP_DIR}/scripts/step4e-line-v1-operator.sh" "$@"
