#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"

export STEP4E_VERSION="v2"
export BRIDGE_DURATION_S="240"

exec "${EXP_DIR}/scripts/step4e-line-v1-operator.sh" "$@"
