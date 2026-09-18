#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")/.." && pwd)"
VENV_PATH="${EXPERIMENT_ROOT}/.venv-contact-six"
PYTHON_PATH="/usr/bin/python3.10"
PREFLIGHT_PATH="${EXPERIMENT_ROOT}/tools/contact_benchmark_preflight.py"
RECEIPT_PATH="${EXPERIMENT_ROOT}/runs/contact-six/provision.json"
ROS_PREFIX="/opt/ros/humble"

usage() {
  cat <<'EOF'
Usage: contact-six.sh <status|provision>

status      Run the fresh-process CPU-only preflight. It never provisions.
provision   Create/update only the worktree-local .venv-contact-six using
            the locked contact-control dependency group, then run status.
EOF
}

managed_env() {
  env \
    -u VIRTUAL_ENV \
    -u PYTHONPATH \
    -u PYTHONHOME \
    PYTHONNOUSERSITE=1 \
    AMENT_PREFIX_PATH="${ROS_PREFIX}" \
    "$@"
}

status() {
  local interpreter="${VENV_PATH}/bin/python"
  if [[ ! -x "${interpreter}" ]]; then
    # The system interpreter is used only to emit the explicit provision
    # action. It must not import the contact dependencies or run a prewarm.
    managed_env "${PYTHON_PATH}" -B -I "${PREFLIGHT_PATH}" status \
      --experiment-root "${EXPERIMENT_ROOT}" \
      --venv "${VENV_PATH}" \
      --receipt "${RECEIPT_PATH}"
    return $?
  fi
  managed_env "${interpreter}" -B -I "${PREFLIGHT_PATH}" status \
    --experiment-root "${EXPERIMENT_ROOT}" \
    --venv "${VENV_PATH}" \
    --receipt "${RECEIPT_PATH}"
}

provision() {
  command -v uv >/dev/null 2>&1 || {
    echo "contact-six.sh: uv is required for the managed provision" >&2
    return 69
  }
  [[ -x "${PYTHON_PATH}" ]] || {
    echo "contact-six.sh: required interpreter is missing: ${PYTHON_PATH}" >&2
    return 69
  }
  (
    cd -- "${EXPERIMENT_ROOT}"
    managed_env \
      UV_PROJECT_ENVIRONMENT="${VENV_PATH}" \
      uv sync \
        --locked \
        --group contact-control \
        --python "${PYTHON_PATH}"
  )
  if [[ ! -f "${EXPERIMENT_ROOT}/build/contact-qp/libcontact_qp.so" ]]; then
    managed_env "${VENV_PATH}/bin/python" -B -I \
      "${EXPERIMENT_ROOT}/tools/build_contact_qp.py" \
      --output "${EXPERIMENT_ROOT}/build/contact-qp"
  fi
  managed_env "${VENV_PATH}/bin/python" -B -I "${PREFLIGHT_PATH}" record-provision \
    --experiment-root "${EXPERIMENT_ROOT}" \
    --venv "${VENV_PATH}" \
    --receipt "${RECEIPT_PATH}"
  status
}

case "${1:-}" in
  status)
    [[ "$#" -eq 1 ]] || { echo "contact-six.sh: status takes no options" >&2; exit 64; }
    status
    ;;
  provision)
    [[ "$#" -eq 1 ]] || { echo "contact-six.sh: provision takes no options" >&2; exit 64; }
    provision
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac
