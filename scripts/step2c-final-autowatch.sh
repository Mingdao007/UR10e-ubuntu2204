#!/usr/bin/env bash
set -euo pipefail

EXP_DIR="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"

exec "${EXP_DIR}/scripts/step2c-final-operator.sh" autowatch
