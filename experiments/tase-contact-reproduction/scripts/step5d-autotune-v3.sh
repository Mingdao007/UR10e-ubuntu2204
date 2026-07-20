#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/../.." && pwd)"
RUNTIME_SOURCE="${REPOSITORY_ROOT}/src/ur10e_experiment_runtime"
if [[ ! -d "${RUNTIME_SOURCE}/ur10e_experiment_runtime" ]]; then
  echo "missing ur10e_experiment_runtime source: ${RUNTIME_SOURCE}" >&2
  exit 66
fi
PYTHON_ABI="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
ROS_PYTHON_PATHS=()
for candidate in \
  "/opt/ros/humble/lib/python${PYTHON_ABI}/site-packages" \
  "/opt/ros/humble/local/lib/python${PYTHON_ABI}/dist-packages"
do
  if [[ -d "${candidate}" ]]; then
    ROS_PYTHON_PATHS+=("${candidate}")
  fi
done
if (( ${#ROS_PYTHON_PATHS[@]} == 0 )); then
  echo "missing ROS Humble Python runtime for Python ${PYTHON_ABI}" >&2
  exit 66
fi
AMENT_PREFIX_CANDIDATES=("${REPOSITORY_ROOT}/install" "/opt/ros/humble")
AMENT_PREFIXES=()
for candidate in "${AMENT_PREFIX_CANDIDATES[@]}"; do
  if [[ -d "${candidate}/share/ament_index/resource_index" ]]; then
    AMENT_PREFIXES+=("${candidate}")
  fi
done
if (( ${#AMENT_PREFIXES[@]} == 0 )); then
  echo "missing ROS ament prefix" >&2
  exit 66
fi
RUNTIME_PYTHONPATH="${EXPERIMENT_ROOT}/tools:${RUNTIME_SOURCE}"
for candidate in "${ROS_PYTHON_PATHS[@]}"; do
  RUNTIME_PYTHONPATH="${RUNTIME_PYTHONPATH}:${candidate}"
done
export PYTHONPATH="${RUNTIME_PYTHONPATH}"
export AMENT_PREFIX_PATH="$(IFS=:; echo "${AMENT_PREFIXES[*]}")"
if [[ "${1:-}" == "bridge" || "${1:-}" == "live" ]]; then
  mode="$1"
  shift
  arguments=("$@")
  output_root=""
  bridge_start_context=""
  campaign_arming_context=""
  launch_profile="${EXPERIMENT_ROOT}/config/step5/step5d_autotune_v3_launch_profile.json"
  index=0
  while (( index < ${#arguments[@]} )); do
    option="${arguments[index]}"
    case "${option}" in
      --output-root|--launch-profile|--bridge-start-context|--campaign-arming-context)
        if (( index + 1 >= ${#arguments[@]} )); then
          echo "${option} requires a value" >&2
          exit 64
        fi
        value="${arguments[index + 1]}"
        if [[ "${option}" == "--output-root" ]]; then
          output_root="${value}"
        elif [[ "${option}" == "--bridge-start-context" ]]; then
          bridge_start_context="${value}"
        elif [[ "${option}" == "--campaign-arming-context" ]]; then
          campaign_arming_context="${value}"
        else
          launch_profile="${value}"
        fi
        ((index += 2))
        ;;
      *)
        ((index += 1))
        ;;
    esac
  done
  if [[ -z "${output_root}" ]]; then
    echo "${mode} requires --output-root" >&2
    exit 64
  fi
  if [[ -z "${bridge_start_context}" ]]; then
    echo "${mode} requires --bridge-start-context" >&2
    exit 64
  fi
  if [[ -z "${campaign_arming_context}" ]]; then
    echo "${mode} requires --campaign-arming-context" >&2
    exit 64
  fi
  output_root="$(readlink -m -- "${output_root}")"
  bridge_start_context="$(readlink -m -- "${bridge_start_context}")"
  campaign_arming_context="$(readlink -m -- "${campaign_arming_context}")"
  preflight="${output_root}/preflight.json"
  python3 "${EXPERIMENT_ROOT}/tools/preflight_step5d_autotune_v3.py" \
    --mailbox "${output_root}/runtime/command.json" \
    --bridge-start-context "${bridge_start_context}" \
    --launch-profile "${launch_profile}" \
    --output "${preflight}" \
    --json
  runner_args=()
  index=0
  while (( index < ${#arguments[@]} )); do
    option="${arguments[index]}"
    case "${option}" in
      --output-root|--launch-profile|--bridge-start-context|--campaign-arming-context)
        ((index += 2))
        ;;
      *)
        runner_args+=("${option}")
        ((index += 1))
        ;;
    esac
  done
  exec python3 "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py" \
    "${runner_args[@]}" --output-root "${output_root}" \
    --launch-profile "${launch_profile}" --preflight "${preflight}" \
    --bridge-start-context "${bridge_start_context}" \
    --campaign-arming-context "${campaign_arming_context}"
fi
exec python3 -m step5d_autotune_v3.cli --experiment-root "${EXPERIMENT_ROOT}" "$@"
