#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"

if [[ $# -gt 0 ]]; then
    exec "${EXP_DIR}/scripts/step4e-ball-vs-cyl-p0-v1-operator.sh" "$@"
fi

exec "${EXP_DIR}/scripts/step4e-ball-vs-cyl-p0-v1-operator.sh" witness-autowatch
