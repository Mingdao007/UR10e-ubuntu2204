#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
RUN_DIR="${1:-${ROOT}/experiments/tase-contact-reproduction/runs/no_contact_test_20260617_143540}"

source_setup() {
  set +u
  source "$1"
  set -u
}

source_setup /opt/ros/humble/setup.bash
if [[ ! -f "${ROOT}/install/setup.bash" ]]; then
  python3 "${ROOT}/scripts/ur10e_colcon_build.py" ur10e_bringup ur10e_example_controllers
fi
source_setup "${ROOT}/install/setup.bash"
if ! ros2 pkg prefix ur10e_example_controllers >/dev/null 2>&1; then
  python3 "${ROOT}/scripts/ur10e_colcon_build.py" ur10e_bringup ur10e_example_controllers
  source_setup "${ROOT}/install/setup.bash"
fi

ros2 run ur10e_example_controllers step5a_gate_a_audit "${RUN_DIR}"
