#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"

if [[ $# -gt 0 ]]; then
    exec "${EXP_DIR}/scripts/step4e-line-v22-operator.sh" "$@"
fi

exec "${EXP_DIR}/scripts/step4e-line-v22-operator.sh" line-autowatch
