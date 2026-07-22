#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/../.." && pwd)"

bridge_usage() {
  cat <<'EOF'
Usage: step5d-autotune-v3.sh bridge [OPTIONS]

Canonical governed Step5d bridge launcher. Qualification, TP delivery,
preflight, and campaign preparation are automatic.

Options:
  --output-root PATH       Per-run evidence directory
  --campaign-root PATH     Campaign state directory
  --launch-profile PATH    Compatibility-only canonical profile path
  --ready-timeout-s SEC    Positive bridge/runner readiness timeout
  --play-timeout-s SEC     Positive TP Play observation timeout
  -h, --help               Show this help without starting any work
EOF
}

usage() {
  cat <<'EOF'
Usage: step5d-autotune-v3.sh bridge [OPTIONS]
       step5d-autotune-v3.sh status [--json]
       step5d-autotune-v3.sh [OPERATOR-CLI-ARGS]

Use "step5d-autotune-v3.sh bridge --help" for bridge options.
EOF
}

bridge_argv_error() {
  echo "bridge argv refused: $1" >&2
  echo "use: step5d-autotune-v3.sh bridge --help" >&2
  exit 64
}

bridge_require_positive_seconds() {
  local option="$1"
  local value="$2"
  local significant=""
  if [[ ! "${value}" =~ ^([0-9]+([.][0-9]+)?|[.][0-9]+)$ ]]; then
    bridge_argv_error "${option} requires a finite positive decimal value"
  fi
  significant="${value//[0.]/}"
  if [[ -z "${significant}" ]]; then
    bridge_argv_error "${option} requires a finite positive decimal value"
  fi
}

bridge_record_launch_attempt() {
  local state="$1"
  local phase="$2"
  local exit_code="${3:-}"
  local detail="${4:-}"
  local prior_enabled="${launch_attempt_enabled}"
  local command=(
    "${CONTROL_PYTHON}" -m step5d_autotune_v3.cli
    --experiment-root "${EXPERIMENT_ROOT}"
    --campaign-root "${campaign_root}"
    --_launch-attempt-id "${launch_attempt_id}"
    --_launch-attempt-state "${state}"
    --_launch-attempt-phase "${phase}"
  )
  if [[ "${state}" == "FAILED" ]]; then
    command+=(
      --_launch-attempt-exit-code "${exit_code}"
      --_launch-attempt-reason-code LAUNCH_ATTEMPT_FAILED
      --_launch-attempt-detail "${detail}"
    )
  fi
  launch_attempt_enabled=0
  "${command[@]}" >>"${output_root}/launch-attempt-recorder.log" 2>&1
  launch_attempt_enabled="${prior_enabled}"
}

bridge_begin_phase() {
  launch_attempt_phase="$1"
  bridge_record_launch_attempt STARTED "${launch_attempt_phase}"
}

bridge_failure_trap() {
  local exit_code=$?
  trap - ERR
  if (( launch_attempt_enabled == 1 )) && [[ -n "${launch_attempt_phase}" ]]; then
    set +e
    bridge_record_launch_attempt \
      FAILED \
      "${launch_attempt_phase}" \
      "${exit_code}" \
      "canonical bridge phase ${launch_attempt_phase} exited ${exit_code}"
  fi
  exit "${exit_code}"
}

bridge_mode=0
arguments=()
output_root=""
campaign_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3"
canonical_launch_profile="${EXPERIMENT_ROOT}/config/step5/step5d_autotune_v3_launch_profile.json"
runner_args=()
launch_attempt_id=""
launch_attempt_phase=""
launch_attempt_enabled=0

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "bridge" ]]; then
  bridge_mode=1
  shift
  arguments=("$@")
  for option in "${arguments[@]}"; do
    if [[ "${option}" == "-h" || "${option}" == "--help" ]]; then
      bridge_usage
      exit 0
    fi
  done

  seen_output_root=0
  seen_campaign_root=0
  seen_launch_profile=0
  seen_ready_timeout=0
  seen_play_timeout=0
  index=0
  while (( index < ${#arguments[@]} )); do
    option="${arguments[index]}"
    option_name="${option%%=*}"
    value=""
    case "${option}" in
      --output-root|--campaign-root|--launch-profile|--ready-timeout-s|--play-timeout-s)
        if (( index + 1 >= ${#arguments[@]} )) || [[ "${arguments[index + 1]}" == -* ]]; then
          bridge_argv_error "${option} requires a value"
        fi
        value="${arguments[index + 1]}"
        ((index += 2))
        ;;
      --output-root=*|--campaign-root=*|--launch-profile=*|--ready-timeout-s=*|--play-timeout-s=*)
        value="${option#*=}"
        if [[ -z "${value}" ]]; then
          bridge_argv_error "${option_name} requires a value"
        fi
        ((index += 1))
        ;;
      --preflight|--preflight=*|--delivery-observation|--delivery-observation=*|--prepare-only|--prepare-only=*|--qualification-endpoints|--qualification-endpoints=*|--experiment-root|--experiment-root=*|--campaign-binding|--campaign-binding=*|--campaign-lease|--campaign-lease=*|--authorization-file|--authorization-file=*|--arm-gate|--arm-gate=*|--offline-release-gate|--offline-release-gate=*)
        bridge_argv_error "${option_name} is an internal worker option"
        ;;
      *)
        bridge_argv_error "unsupported option or positional argument: ${option}"
        ;;
    esac
    if [[ -z "${value}" ]]; then
      bridge_argv_error "${option_name} requires a value"
    fi

    case "${option_name}" in
      --output-root)
        (( seen_output_root == 0 )) || bridge_argv_error "--output-root may appear only once"
        seen_output_root=1
        output_root="${value}"
        ;;
      --campaign-root)
        (( seen_campaign_root == 0 )) || bridge_argv_error "--campaign-root may appear only once"
        seen_campaign_root=1
        campaign_root="${value}"
        ;;
      --launch-profile)
        (( seen_launch_profile == 0 )) || bridge_argv_error "--launch-profile may appear only once"
        seen_launch_profile=1
        if [[ "$(readlink -m -- "${value}")" != "${canonical_launch_profile}" ]]; then
          bridge_argv_error "--launch-profile overrides are retired; the immutable release selects the profile"
        fi
        ;;
      --ready-timeout-s)
        (( seen_ready_timeout == 0 )) || bridge_argv_error "--ready-timeout-s may appear only once"
        seen_ready_timeout=1
        bridge_require_positive_seconds "${option_name}" "${value}"
        runner_args+=("${option_name}" "${value}")
        ;;
      --play-timeout-s)
        (( seen_play_timeout == 0 )) || bridge_argv_error "--play-timeout-s may appear only once"
        seen_play_timeout=1
        bridge_require_positive_seconds "${option_name}" "${value}"
        runner_args+=("${option_name}" "${value}")
        ;;
    esac
  done
fi

RUNTIME_SOURCE="${REPOSITORY_ROOT}/src/ur10e_experiment_runtime"
if [[ ! -d "${RUNTIME_SOURCE}/ur10e_experiment_runtime" ]]; then
  echo "missing ur10e_experiment_runtime source: ${RUNTIME_SOURCE}" >&2
  exit 66
fi
RUNTIME_RESOLVER="${EXPERIMENT_ROOT}/tools/resolve_step5d_autotune_v3_runtime.py"
if [[ ! -f "${RUNTIME_RESOLVER}" || -L "${RUNTIME_RESOLVER}" ]]; then
  echo "missing governed runtime resolver: ${RUNTIME_RESOLVER}" >&2
  exit 66
fi
runtime_binding=""
if ! runtime_binding="$(/usr/bin/python3.10 -B -I "${RUNTIME_RESOLVER}" --shell-binding)"; then
  if [[ "${1:-}" == "status" && "${2:-}" == "--json" && $# -eq 2 ]]; then
    exec /usr/bin/python3.10 -B -I "${RUNTIME_RESOLVER}" --status-json
  fi
  echo "governed runtime unavailable: ${runtime_binding}" >&2
  echo "next action: provision the current uv.lock runtime before bridge delivery" >&2
  exit 78
fi
IFS=$'\t' read -r \
  CONTROL_PYTHON \
  OPTIMIZER_PYTHON \
  RUNTIME_BUNDLE_ID \
  RUNTIME_ATTESTATION_SHA256 \
  RUNTIME_CONTRACT_SHA256 \
  RUNTIME_LOCK_SHA256 \
  CONTROL_ENVIRONMENT_ID \
  OPTIMIZER_ENVIRONMENT_ID \
  GOVERNED_GPU_UUID <<<"${runtime_binding}"
runtime_fields=(
  "${CONTROL_PYTHON:-}"
  "${OPTIMIZER_PYTHON:-}"
  "${RUNTIME_BUNDLE_ID:-}"
  "${RUNTIME_ATTESTATION_SHA256:-}"
  "${RUNTIME_CONTRACT_SHA256:-}"
  "${RUNTIME_LOCK_SHA256:-}"
  "${CONTROL_ENVIRONMENT_ID:-}"
  "${OPTIMIZER_ENVIRONMENT_ID:-}"
  "${GOVERNED_GPU_UUID:-}"
)
if (( ${#runtime_fields[@]} != 9 )); then
  echo "governed runtime resolver returned an invalid field count" >&2
  exit 78
fi
for field in "${runtime_fields[@]}"; do
  if [[ -z "${field}" || "${field}" == *$'\n'* || "${field}" == *$'\r'* ]]; then
    echo "governed runtime resolver returned an unsafe field" >&2
    exit 78
  fi
done
if [[ ! -x "${CONTROL_PYTHON}" || ! -x "${OPTIMIZER_PYTHON}" ]]; then
  echo "governed runtime interpreter is unavailable" >&2
  exit 78
fi
PYTHON_ABI="3.10"
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
export CUDA_VISIBLE_DEVICES="${GOVERNED_GPU_UUID}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export STEP5D_V3_CONTROL_ENVIRONMENT_ID="${CONTROL_ENVIRONMENT_ID}"
export STEP5D_V3_CONTROL_PYTHON="${CONTROL_PYTHON}"
export STEP5D_V3_GPU_UUID="${GOVERNED_GPU_UUID}"
export STEP5D_V3_OPTIMIZER_ENVIRONMENT_ID="${OPTIMIZER_ENVIRONMENT_ID}"
export STEP5D_V3_OPTIMIZER_PYTHON="${OPTIMIZER_PYTHON}"
export STEP5D_V3_RUNTIME_ATTESTATION_SHA256="${RUNTIME_ATTESTATION_SHA256}"
export STEP5D_V3_RUNTIME_BUNDLE_ID="${RUNTIME_BUNDLE_ID}"
if (( bridge_mode == 1 )); then
  export STEP5D_V3_CANONICAL_LAUNCHER="${SCRIPT_PATH}"
  export STEP5D_V3_SHELL_PID="$$"
  if [[ -z "${output_root}" ]]; then
    output_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/bridge-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  fi
  output_root="$(readlink -m -- "${output_root}")"
  campaign_root="$(readlink -m -- "${campaign_root}")"
  mkdir -p -- "${output_root}" "${campaign_root}"
  if [[ -n "${STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" ]]; then
    export STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_PID="$$"
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_qualification.py" \
      --_exec-live-from-shell-contract \
      "${STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_CONTRACT}"
    exit 0
  fi
  if [[ -r /proc/sys/kernel/random/uuid ]]; then
    read -r launch_attempt_id </proc/sys/kernel/random/uuid
    launch_attempt_id="${launch_attempt_id//-/}"
  else
    launch_attempt_id="$("${CONTROL_PYTHON}" -c 'import uuid; print(uuid.uuid4().hex)')"
  fi
  export STEP5D_V3_LAUNCH_ATTEMPT_ID="${launch_attempt_id}"
  launch_attempt_enabled=1
  trap bridge_failure_trap ERR
  bridge_begin_phase runtime_gate
  bridge_begin_phase status_before
  "${CONTROL_PYTHON}" -m step5d_autotune_v3.cli \
    --experiment-root "${EXPERIMENT_ROOT}" \
    --campaign-root "${campaign_root}" \
    status --json >"${output_root}/status-before.json"
  delivery_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/delivery-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  bridge_begin_phase tp_build
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/build_step5d_autotune_tp_v3.py" \
    --output-dir "${delivery_root}" \
    >"${output_root}/tp-build.json"
  bridge_begin_phase release_candidate
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/promote_step5d_r009_atomic_release.py" \
    --root "${EXPERIMENT_ROOT}" \
    --artifact-dir "${delivery_root}" \
    --stage-local-candidate \
    >"${output_root}/local-release-candidate.json"
  bridge_begin_phase qualification
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_qualification.py" \
    --experiment-root "${EXPERIMENT_ROOT}" \
    --output-root "${campaign_root}" \
    --release-candidate "${output_root}/local-release-candidate.json" \
    >"${output_root}/qualification.json"
  bridge_begin_phase tp_delivery
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_tp_transaction.py" \
    --root "${EXPERIMENT_ROOT}" \
    --artifact-dir "${delivery_root}" \
    --release-candidate "${output_root}/local-release-candidate.json" \
    --qualification-result "${output_root}/qualification.json" \
    --evidence-output "${output_root}/delivery-observation.json" \
    >"${output_root}/tp-transaction.log"
  bridge_begin_phase status_after_delivery
  "${CONTROL_PYTHON}" -m step5d_autotune_v3.cli \
    --experiment-root "${EXPERIMENT_ROOT}" \
    --campaign-root "${campaign_root}" \
    status --json >"${output_root}/status-after-delivery.json"
  bridge_begin_phase campaign_prepare
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py" \
    --prepare-only \
    --campaign-root "${campaign_root}" \
    >"${output_root}/campaign-prepare.json"
  preflight="${output_root}/preflight.json"
  bridge_begin_phase preflight
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/preflight_step5d_autotune_v3.py" \
    --mailbox "${output_root}/runtime/command.json" \
    --delivery-observation "${output_root}/delivery-observation.json" \
    --output "${preflight}" \
    --json
  bridge_begin_phase live_handoff
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py" \
    "${runner_args[@]}" --output-root "${output_root}" \
    --delivery-observation "${output_root}/delivery-observation.json" \
    --campaign-root "${campaign_root}" \
    --preflight "${preflight}"
  exit 0
fi
exec "${CONTROL_PYTHON}" -m step5d_autotune_v3.cli --experiment-root "${EXPERIMENT_ROOT}" "$@"
