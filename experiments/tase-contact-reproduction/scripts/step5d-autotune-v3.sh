#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")/.." && pwd)"
export PYTHONPATH="${EXPERIMENT_ROOT}/tools${PYTHONPATH:+:${PYTHONPATH}}"
if [[ "${1:-}" == "live" ]]; then
  shift
  arguments=("$@")
  output_root=""
  launch_profile="${EXPERIMENT_ROOT}/config/step5/step5d_autotune_v3_launch_profile.json"
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
        if [[ "${option}" == "--output-root" ]]; then
          output_root="${value}"
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
    echo "live requires --output-root" >&2
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
  exec python3 "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py" \
    "${runner_args[@]}" --output-root "${output_root}" \
    --launch-profile "${launch_profile}" --preflight "${preflight}"
fi
exec python3 -m step5d_autotune_v3.cli --experiment-root "${EXPERIMENT_ROOT}" "$@"
