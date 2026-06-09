#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"

export STEP4E_VERSION="v5"
export BRIDGE_DURATION_S="240"

exec "${EXP_DIR}/scripts/step4e-line-v1-operator.sh" "$@"
