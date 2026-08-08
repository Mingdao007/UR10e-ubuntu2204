#!/usr/bin/env bash
# Run PATH ring + integral-limit tests with ROS Humble pinocchio on PATH.
set -eo pipefail
# ROS setup.bash references optional unbound vars under `set -u`.
set +u
source /opt/ros/humble/setup.bash
set -u
EXP="$(cd "$(dirname "$0")/.." && pwd)"
WT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$EXP"
export PYTHONPATH="tools:${WT}/src/ur10e_experiment_runtime:${PYTHONPATH:-}"
exec python3 -m pytest \
  tests/test_step5d_autotune_v4_r008_state25_path_trace.py \
  tests/test_step5d_autotune_v4_r008_integral_limit_wiring.py \
  "$@"
