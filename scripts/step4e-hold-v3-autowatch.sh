#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"

case "${1:-}" in
  -h|--help|help)
    exec "${EXP_DIR}/scripts/step4e-line-v3-operator.sh"
    ;;
esac

exec "${EXP_DIR}/scripts/step4e-line-v3-operator.sh" hold-autowatch
