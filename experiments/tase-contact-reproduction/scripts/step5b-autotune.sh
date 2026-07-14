#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")/.." && pwd)"
VENV="${ROOT}/.venv-step5b-autotune"
SUPERVISOR="${ROOT}/tools/step5b_autotune_supervisor.py"
REQUIREMENTS="${ROOT}/requirements-step5b-autotune.txt"

usage() {
  cat <<'EOF'
Usage:
  step5b-autotune.sh install
  step5b-autotune.sh preflight [--offline]
  STEP5B_AUTOTUNE_CONFIRM='LIVE STEP5B AUTOTUNE SESSION' step5b-autotune.sh start
  step5b-autotune.sh status
  step5b-autotune.sh stop
  step5b-autotune.sh resume
  step5b-autotune.sh build-package
  step5b-autotune.sh numeric-sanity

Live boundary:
  start is a TP Local contact-motion supervisor. It never loads a program and
  never presses TP Play. Open the controller-read-back-verified package and
  press Play manually only after the live gate is accepted.
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

command_name="$1"
shift

case "${command_name}" in
  install)
    if [[ $# -ne 0 ]]; then
      usage
      exit 2
    fi
    if ! python3 -m venv --clear --system-site-packages "${VENV}"; then
      echo "system venv/ensurepip unavailable; using user-level virtualenv fallback"
      python3 -m virtualenv --clear --system-site-packages "${VENV}"
    fi
    "${VENV}/bin/python" -m pip install --upgrade pip
    "${VENV}/bin/python" -m pip install -r "${REQUIREMENTS}"
    "${VENV}/bin/python" -c 'import torch,botorch,gpytorch; assert torch.cuda.is_available(), "CUDA unavailable"; print(torch.__version__, botorch.__version__, gpytorch.__version__, torch.cuda.get_device_name(0))'
    ;;
  preflight)
    python_exec="python3"
    if [[ -x "${VENV}/bin/python" ]]; then
      python_exec="${VENV}/bin/python"
    fi
    exec "${python_exec}" "${SUPERVISOR}" preflight "$@"
    ;;
  start)
    if [[ $# -ne 0 ]]; then
      usage
      exit 2
    fi
    if [[ ! -x "${VENV}/bin/python" ]]; then
      echo "refusing start: run step5b-autotune.sh install first"
      exit 40
    fi
    exec "${VENV}/bin/python" "${SUPERVISOR}" start
    ;;
  status|stop|resume)
    if [[ $# -ne 0 ]]; then
      usage
      exit 2
    fi
    exec python3 "${SUPERVISOR}" "${command_name}"
    ;;
  build-package)
    if [[ $# -ne 0 ]]; then
      usage
      exit 2
    fi
    exec python3 "${ROOT}/tools/build_step5b_autotune_package.py"
    ;;
  numeric-sanity)
    if [[ $# -ne 0 ]]; then
      usage
      exit 2
    fi
    exec python3 "${ROOT}/tools/step5b_autotune_numeric_sanity.py"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
