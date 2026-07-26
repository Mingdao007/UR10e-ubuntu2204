#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"

case "${1:-}" in
  -h|--help|help)
    exec "${EXP_DIR}/scripts/step4e-line-v17-operator.sh"
    ;;
esac

exec "${EXP_DIR}/scripts/step4e-line-v17-operator.sh" line-autowatch
