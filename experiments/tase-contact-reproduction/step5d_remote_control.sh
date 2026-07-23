#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")" && pwd)"
WORKSPACE_ROOT="$(cd -- "${EXPERIMENT_ROOT}/../.." && pwd)"
STABLE_RUNTIME_PYTHON="/home/andy/.local/share/step5d-remote/r012/control/bin/python"
if [[ -x "${STABLE_RUNTIME_PYTHON}" ]]; then
  DEFAULT_RUNTIME_PYTHON="${STABLE_RUNTIME_PYTHON}"
else
  DEFAULT_RUNTIME_PYTHON="/usr/bin/python3"
fi

source_setup() {
  local script_path=$1
  if [[ ! -f ${script_path} ]]; then
    return
  fi
  set +u
  # shellcheck disable=SC1090
  source "${script_path}"
  set -u
}

if [[ -f /opt/ros/humble/setup.bash ]]; then
  # shellcheck disable=SC1090
  source_setup /opt/ros/humble/setup.bash
fi

if [[ -f "${WORKSPACE_ROOT}/install/setup.bash" ]]; then
  # shellcheck disable=SC1090
  source_setup "${WORKSPACE_ROOT}/install/setup.bash"
fi

export STEP5D_REMOTE_RUN_ROOT="${STEP5D_REMOTE_RUN_ROOT:-${EXPERIMENT_ROOT}/runs/step5d_remote_control}"
export PYTHONPATH="${WORKSPACE_ROOT}/src/ur10e_example_controllers:${EXPERIMENT_ROOT}/tools:${EXPERIMENT_ROOT}:${PYTHONPATH:-}"
PYTHON="${STEP5D_REMOTE_PYTHON:-${DEFAULT_RUNTIME_PYTHON}}"
CONTROL_SITE_PACKAGES="$("${PYTHON}" -I -c 'import site; print(site.getsitepackages()[0])')"
GPU_LIBRARY_PATHS=()
for package in cuda_nvrtc nvjitlink cuda_runtime; do
  candidate="${CONTROL_SITE_PACKAGES}/nvidia/${package}/lib"
  if [[ -d "${candidate}" ]]; then
    GPU_LIBRARY_PATHS+=("${candidate}")
  fi
done
if (( ${#GPU_LIBRARY_PATHS[@]} > 0 )); then
  gpu_library_path="$(IFS=:; echo "${GPU_LIBRARY_PATHS[*]}")"
  export LD_LIBRARY_PATH="${gpu_library_path}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi
export CUPY_CACHE_DIR="${CUPY_CACHE_DIR:-/home/andy/.cache/step5d-remote/r012/cupy}"
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1

usage() {
  cat <<'EOF'
Usage:
  step5d_remote_control.sh status [--params-file PATH]
  step5d_remote_control.sh dry-run [--params-file PATH]
  step5d_remote_control.sh canary --live [--params-file PATH]
  step5d_remote_control.sh run --live [--params-file PATH]
  step5d_remote_control.sh --help
EOF
}

if [[ $# -eq 0 ]]; then
  usage
  exit 1
fi

mode="$1"
shift

if [[ "${mode}" == "--help" || "${mode}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ "${mode}" != "status" && "${mode}" != "dry-run" && "${mode}" != "canary" && "${mode}" != "run" ]]; then
  usage
  exit 64
fi

params_file=""
live="0"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --params-file)
      [[ $# -ge 2 ]] || { echo "--params-file requires a value" >&2; exit 64; }
      params_file="$2"
      shift 2
      ;;
    --live)
      live="1"
      shift
      ;;
    *)
      echo "unsupported argument: ${1}" >&2
      usage
      exit 64
      ;;
  esac
done

if [[ "${mode}" == "status" || "${mode}" == "dry-run" ]]; then
  if [[ "${live}" == "1" ]]; then
    echo "${mode} does not support --live" >&2
    exit 64
  fi
fi

if [[ "${mode}" == "canary" || "${mode}" == "run" ]]; then
  if [[ "${live}" != "1" ]]; then
    echo "${mode} requires --live" >&2
    exit 64
  fi
fi

RUNTIME_ARGS=(
  "${PYTHON}"
  "-m"
  "step5d_remote_control.runtime"
  "${mode}"
  "--experiment-root"
  "${EXPERIMENT_ROOT}"
)
if [[ -n "${params_file}" ]]; then
  RUNTIME_ARGS+=( "--params-file" "${params_file}" )
fi
if [[ "${live}" == "1" ]]; then
  RUNTIME_ARGS+=( "--live" )
fi

if [[ "${live}" == "1" ]]; then
  LOCK_FILE="/tmp/step5d_remote_control.live.lock"
  exec 9>"${LOCK_FILE}"
  flock -n 9 || {
    echo "failed to acquire live lock" >&2
    exit 75
  }
fi

"${RUNTIME_ARGS[@]}"
runtime_rc=$?
exit ${runtime_rc}
