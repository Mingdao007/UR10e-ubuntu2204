#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"

export STEP4E_VERSION="p0_ball_vs_cyl_v1"
export BRIDGE_DURATION_S="180"
export MAX_NORMAL_FORCE_N="35"
export MAX_TORQUE_NORM_NM="3.0"
export STEP4E_LINE_SPEED_M_S="0.0"
export STEP4E_STAGE25_ONLY="1"

exec "${EXP_DIR}/scripts/step4e-line-v1-operator.sh" "$@"
