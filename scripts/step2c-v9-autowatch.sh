#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/2026-06-04"

exec "${EXP_DIR}/scripts/step2c-admittance-search15-v9-v4copy-far15-operator.sh" autowatch
