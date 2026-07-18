#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")/.." && pwd)"
MODE="${1:-}"
if [[ -z "${MODE}" ]]; then
  echo "usage: $(basename -- "$0") {check|run} [options]" >&2
  exit 64
fi
shift

case "${MODE}" in
  check)
    exec python3 "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_hil_hold.py" \
      --check "$@"
    ;;
  run)
    output_root=""
    launch_profile="${EXPERIMENT_ROOT}/config/step5/step5d_autotune_v3_launch_profile.json"
    arguments=("$@")
    index=0
    while (( index < ${#arguments[@]} )); do
      option="${arguments[index]}"
      case "${option}" in
        --output-root|--launch-profile)
          if (( index + 1 >= ${#arguments[@]} )); then
            echo "${option} requires a value" >&2
            exit 64
          fi
          value="${arguments[index + 1]}"
          case "${option}" in
            --output-root) output_root="${value}" ;;
            --launch-profile) launch_profile="${value}" ;;
          esac
          ((index += 2))
          ;;
        *)
          ((index += 1))
          ;;
      esac
    done
    if [[ -z "${output_root}" ]]; then
      echo "run requires --output-root" >&2
      exit 64
    fi
    output_root="$(readlink -m -- "${output_root}")"
    preflight="${output_root}/preflight.json"
    python3 "${EXPERIMENT_ROOT}/tools/preflight_step5d_autotune_v3.py" \
      --mailbox "${output_root}/runtime/command.json" \
      --launch-profile "${launch_profile}" \
      --output "${preflight}" \
      --json
    runner_args=()
    index=0
    while (( index < ${#arguments[@]} )); do
      option="${arguments[index]}"
      case "${option}" in
        --output-root|--launch-profile)
          ((index += 2))
          ;;
        *)
          runner_args+=("${option}")
          ((index += 1))
          ;;
      esac
    done
    exec python3 "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_hil_hold.py" \
      "${runner_args[@]}" --output-root "${output_root}" \
      --launch-profile "${launch_profile}" --preflight "${preflight}"
    ;;
  *)
    echo "unknown mode: ${MODE}; expected check or run" >&2
    exit 64
    ;;
esac
