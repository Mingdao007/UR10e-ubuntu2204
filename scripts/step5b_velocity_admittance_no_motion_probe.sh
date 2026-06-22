#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)"
RUN_ROOT="${ROOT}/experiments/tase-contact-reproduction/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${RUN_ROOT}/step5b_velocity_admittance_no_motion_probe_${STAMP}"
COMMAND_PERIOD_S="${COMMAND_PERIOD_S:-0.002}"
PROBE_DURATION_S="${PROBE_DURATION_S:-2.0}"

mkdir -p "${RUN_DIR}"
echo "run_dir=${RUN_DIR}"

source_setup() {
  set +u
  source "$1"
  set -u
}

source_setup /opt/ros/humble/setup.bash
if [[ -f "${ROOT}/install/setup.bash" ]]; then
  source_setup "${ROOT}/install/setup.bash"
fi
export PYTHONPATH="${ROOT}/src/ur10e_example_controllers:${PYTHONPATH:-}"

python3 -m ur10e_example_controllers.step5b_velocity_admittance_runner \
  --no-motion-rate-probe \
  --run-dir "${RUN_DIR}" \
  --summary "${RUN_DIR}/summary.json" \
  --trace "${RUN_DIR}/step5b_velocity_admittance_trace.csv" \
  --velocity-command-period-s "${COMMAND_PERIOD_S}" \
  --no-motion-probe-duration-s "${PROBE_DURATION_S}" \
  "$@" | tee "${RUN_DIR}/no_motion_probe.log"

echo "summary=${RUN_DIR}/summary.json"
