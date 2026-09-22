#!/usr/bin/env bash
set -euo pipefail
SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd)"
MAIN_VENV="/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction/.venv-contact-six/bin/python"
LOCAL_VENV="${EXPERIMENT_ROOT}/.venv-contact-six/bin/python"
if [[ -x "${LOCAL_VENV}" ]]; then
  interpreter="${LOCAL_VENV}"
elif [[ -x "${MAIN_VENV}" ]]; then
  interpreter="${MAIN_VENV}"
else
  echo "Run scripts/contact-six.sh provision for the locked CPU environment." >&2
  exit 69
fi
usage() {
  cat <<'EOF'
Usage: contact-yield-live.sh <status|supervise|qualify|pilot|stop> [options]

status     Software/registry/package identity. No devices.
supervise  Continuously observe Load/Play, resident-check or writer, and Stop.
qualify    Open the mature writer, ARM, qualification attempt, stop/Home.
pilot      Open, ARM, PATH --method TASE_RNN_MATURE|SFC --duration 2|10|r013_60|full.
stop       Signal the bound owner process and read its physical stop receipt.

Native parameters are seeds, not physical qualification.
Short 2s/10s rungs are diagnostic. r013_60 is the explicit 60 s R013-compatible
measurement window; full period remains 62.831853 s.
Hardware admission uses run-dir receipts; this script does not invent them.
EOF
}
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -eq 0 ]]; then
  usage
  exit 0
fi
entry="${EXPERIMENT_ROOT}/tools/contact_yield_live.py"
case "$1" in
  supervise)
    if [[ "${TASE_CAMPAIGN_REUSE:-0}" != "1" ]]; then
      "${EXPERIMENT_ROOT}/scripts/contact-six.sh" status >&2
    fi
    entry="${EXPERIMENT_ROOT}/tools/contact_yield_supervisor.py"
    shift ;;
  qualify|pilot) "${EXPERIMENT_ROOT}/scripts/contact-six.sh" status >&2 ;;
esac
exec env -u VIRTUAL_ENV -u PYTHONHOME \
  PYTHONPATH="${EXPERIMENT_ROOT}/tools:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages" \
  PYTHONNOUSERSITE=1 AMENT_PREFIX_PATH="/opt/ros/humble" \
  OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
  "$interpreter" -B "$entry" "$@"
