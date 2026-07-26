#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"

if [[ $# -gt 0 ]]; then
    exec "${EXP_DIR}/scripts/step4e-axis-iso-v1-operator.sh" "$@"
fi

exec "${EXP_DIR}/scripts/step4e-axis-iso-v1-operator.sh" axis-autowatch
