#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/../.." && pwd)"

bridge_usage() {
  cat <<'EOF'
Usage: step5d-autotune-v3.sh bridge-live [OPTIONS]

Canonical governed Step5d live bridge launcher. Reuses existing qualification
and TP delivery evidence; campaign preparation and preflight are automatic.

Options:
  --output-root PATH       Per-run evidence directory
  --campaign-root PATH     Campaign state directory
  --delivery-observation PATH
                           Existing governed TP delivery observation
  --launch-profile PATH    Compatibility-only canonical profile path
  --ready-timeout-s SEC    Positive bridge/runner readiness timeout
  --play-timeout-s SEC     Positive TP Play observation timeout
  -h, --help               Show this help without starting any work
EOF
}

usage() {
  cat <<'EOF'
Usage: step5d-autotune-v3.sh bridge-live [OPTIONS]
       step5d-autotune-v3.sh bridge [OPTIONS]
       step5d-autotune-v3.sh status [--json]
       step5d-autotune-v3.sh status --json --assert-state STATE
       step5d-autotune-v3.sh [OPERATOR-CLI-ARGS]

Use "step5d-autotune-v3.sh bridge-live --help" for bridge options.
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
  local explicit_reason_code="${5:-}"
  local prior_enabled="${launch_attempt_enabled}"
  if (( launch_runtime_bootstrap == 1 )); then
    local bootstrap_action="runtime-start"
    local bootstrap_reason="${explicit_reason_code:-LAUNCH_ATTEMPT_FAILED}"
    if [[ "${state}" == "FAILED" ]]; then
      bootstrap_action="runtime-fail"
    elif [[ "${state}" != "STARTED" ]]; then
      echo "bootstrap launch recorder only accepts STARTED or FAILED" >&2
      return 2
    fi
    local bootstrap_command=(
      /usr/bin/python3.10 -B -I
      "${EXPERIMENT_ROOT}/tools/step5d_bridge_authority.py"
      "${bootstrap_action}"
      --authority-root "${BRIDGE_AUTHORITY_ROOT}"
      --attempt-id "${launch_attempt_id}"
      --owner-pid "$$"
      --owner-starttime "${launch_owner_starttime}"
    )
    if [[ "${state}" == "FAILED" ]]; then
      bootstrap_command+=(
        --exit-code "${exit_code}"
        --reason-code "${bootstrap_reason}"
        --detail "${detail}"
      )
    fi
    launch_attempt_enabled=0
    local bootstrap_rc=0
    "${bootstrap_command[@]}" >>"${output_root}/launch-attempt-recorder.log" 2>&1 \
      || bootstrap_rc=$?
    launch_attempt_enabled="${prior_enabled}"
    return "${bootstrap_rc}"
  fi
  local command=(
    "${CONTROL_PYTHON}" -m step5d_autotune_v3.cli
    --experiment-root "${EXPERIMENT_ROOT}"
    --campaign-root "${BRIDGE_AUTHORITY_ROOT}"
    --_launch-attempt-id "${launch_attempt_id}"
    --_launch-attempt-state "${state}"
    --_launch-attempt-phase "${phase}"
    --_launch-attempt-route "${launch_attempt_route}"
    --_launch-repository-head "${launch_repository_head}"
    --_launch-runtime-environment-id "${RUNTIME_BUNDLE_ID}"
    --_launch-campaign-path "${campaign_root}"
    --_launch-output-root "${output_root}"
    --_launch-owner-pid "$$"
    --_launch-owner-starttime "${launch_owner_starttime}"
    --_launch-owner-authority-epoch "${launch_owner_authority_epoch}"
  )
  if [[ -n "${launch_manifest_sha256}" ]]; then
    command+=(--_launch-manifest-sha256 "${launch_manifest_sha256}")
  fi
  if [[ -n "${launch_attempt_route_snapshot}" && -f "${launch_attempt_route_snapshot}" ]]; then
    command+=(--_launch-route-snapshot "${launch_attempt_route_snapshot}")
  fi
  if [[ "${state}" == "FAILED" || "${state}" == "CANCELLED" ]]; then
    local terminal_reason="${explicit_reason_code:-LAUNCH_ATTEMPT_FAILED}"
    if [[ "${state}" == "CANCELLED" && -z "${explicit_reason_code}" ]]; then
      terminal_reason="LAUNCH_ATTEMPT_CANCELLED"
    fi
    command+=(
      --_launch-attempt-exit-code "${exit_code}"
      --_launch-attempt-reason-code "${terminal_reason}"
      --_launch-attempt-detail "${detail}"
    )
  fi
  launch_attempt_enabled=0
  local recorder_rc=0
  "${command[@]}" >>"${output_root}/launch-attempt-recorder.log" 2>&1 \
    || recorder_rc=$?
  launch_attempt_enabled="${prior_enabled}"
  return "${recorder_rc}"
}

bridge_begin_phase() {
  if [[ -n "${launch_attempt_phase}" ]]; then
    bridge_record_launch_attempt PASSED "${launch_attempt_phase}"
  fi
  launch_attempt_phase="$1"
  bridge_record_launch_attempt STARTED "${launch_attempt_phase}"
}

bridge_finish_phase() {
  if [[ -n "${launch_attempt_phase}" ]]; then
    bridge_record_launch_attempt COMPLETED "${launch_attempt_phase}"
    launch_attempt_phase=""
  fi
}

bridge_revoke_authority() {
  local reason="$1"
  if (( launch_authority_active == 0 )); then
    return 0
  fi
  local authority_python="${CONTROL_PYTHON:-/usr/bin/python3.10}"
  "${authority_python}" "${EXPERIMENT_ROOT}/tools/step5d_bridge_authority.py" revoke \
    --authority-root "${BRIDGE_AUTHORITY_ROOT}" \
    --attempt-id "${launch_attempt_id}" \
    --owner-pid "$$" \
    --owner-starttime "${launch_owner_starttime}" \
    --reason "${reason}" \
    >>"${output_root}/bridge-authority.log" 2>&1
  launch_authority_active=0
}

bridge_runtime_fail() {
  local exit_code="$1"
  local reason_code="$2"
  local detail="$3"
  if (( launch_attempt_enabled == 1 )); then
    set +e
    bridge_record_launch_attempt \
      FAILED runtime_gate "${exit_code}" "${detail}" "${reason_code}"
    bridge_revoke_authority failed
    launch_attempt_enabled=0
  fi
  echo "${detail}" >&2
  exit "${exit_code}"
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
    bridge_revoke_authority failed
  fi
  exit "${exit_code}"
}

bridge_cancel_trap() {
  trap - ERR INT TERM
  set +e
  if (( launch_attempt_enabled == 1 )) && [[ -n "${launch_attempt_phase}" ]]; then
    bridge_record_launch_attempt \
      CANCELLED \
      "${launch_attempt_phase}" \
      130 \
      "canonical bridge was cancelled during ${launch_attempt_phase}"
  fi
  bridge_revoke_authority cancelled
  exit 130
}

bridge_mode=0
bridge_live_mode=0
arguments=()
output_root=""
campaign_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3"
delivery_observation=""
canonical_launch_profile="${EXPERIMENT_ROOT}/config/step5/step5d_autotune_v3_launch_profile.json"
runner_args=()
ready_timeout_s="20"
play_timeout_s="900"
launch_attempt_id=""
launch_attempt_phase=""
launch_attempt_enabled=0
launch_attempt_route="UNKNOWN"
launch_attempt_route_snapshot=""
launch_manifest_sha256=""
launch_repository_head=""
launch_owner_starttime=""
launch_owner_authority_epoch=""
launch_authority_active=0
launch_runtime_bootstrap=0
CONTROL_PYTHON=""
BRIDGE_AUTHORITY_ROOT="${EXPERIMENT_ROOT}/runs/step5d_bridge_authority"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "bridge" || "${1:-}" == "bridge-live" ]]; then
  bridge_mode=1
  if [[ "${1}" == "bridge-live" ]]; then
    bridge_live_mode=1
  fi
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
  seen_delivery_observation=0
  seen_launch_profile=0
  seen_ready_timeout=0
  seen_play_timeout=0
  index=0
  while (( index < ${#arguments[@]} )); do
    option="${arguments[index]}"
    option_name="${option%%=*}"
    value=""
    case "${option}" in
      --output-root|--campaign-root|--delivery-observation|--launch-profile|--ready-timeout-s|--play-timeout-s)
        if (( index + 1 >= ${#arguments[@]} )) || [[ "${arguments[index + 1]}" == -* ]]; then
          bridge_argv_error "${option} requires a value"
        fi
        value="${arguments[index + 1]}"
        ((index += 2))
        ;;
      --output-root=*|--campaign-root=*|--delivery-observation=*|--launch-profile=*|--ready-timeout-s=*|--play-timeout-s=*)
        value="${option#*=}"
        if [[ -z "${value}" ]]; then
          bridge_argv_error "${option_name} requires a value"
        fi
        ((index += 1))
        ;;
      --preflight|--preflight=*|--prepare-only|--prepare-only=*|--qualification-endpoints|--qualification-endpoints=*|--experiment-root|--experiment-root=*|--campaign-binding|--campaign-binding=*|--campaign-lease|--campaign-lease=*|--arm-gate|--arm-gate=*|--offline-release-gate|--offline-release-gate=*)
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
      --delivery-observation)
        (( seen_delivery_observation == 0 )) || bridge_argv_error "--delivery-observation may appear only once"
        seen_delivery_observation=1
        delivery_observation="$(readlink -m -- "${value}")"
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
        ready_timeout_s="${value}"
        runner_args+=("${option_name}" "${value}")
        ;;
      --play-timeout-s)
        (( seen_play_timeout == 0 )) || bridge_argv_error "--play-timeout-s may appear only once"
        seen_play_timeout=1
        bridge_require_positive_seconds "${option_name}" "${value}"
        play_timeout_s="${value}"
        runner_args+=("${option_name}" "${value}")
        ;;
    esac
  done
  if (( bridge_live_mode == 1 )) \
    && [[ -z "${delivery_observation}" ]] \
    && [[ -z "${STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" ]] \
    && [[ -z "${STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" ]]
  then
    bridge_argv_error "--delivery-observation is required"
  fi
fi

if [[ "${1:-}" == "status" && "${2:-}" == "--json" ]]; then
  status_command=(
    /usr/bin/python3.10 -B -I
    "${EXPERIMENT_ROOT}/tools/step5d_bridge_status.py"
    --experiment-root "${EXPERIMENT_ROOT}"
  )
  if (( $# == 4 )) && [[ "${3}" == "--assert-state" ]]; then
    status_command+=(--assert-state "${4}")
  elif (( $# != 2 )); then
    echo "status accepts only --json and optional --assert-state STATE" >&2
    exit 64
  fi
  exec "${status_command[@]}"
fi

if (( bridge_mode == 1 )) \
  && [[ -z "${STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" ]] \
  && [[ -z "${STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" ]]
then
  export STEP5D_V3_CANONICAL_LAUNCHER="${SCRIPT_PATH}"
  export STEP5D_V3_SHELL_PID="$$"
  if [[ -z "${output_root}" ]]; then
    output_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/bridge-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  fi
  output_root="$(readlink -m -- "${output_root}")"
  campaign_root="$(readlink -m -- "${campaign_root}")"
  mkdir -p -- "${output_root}" "${campaign_root}" "${BRIDGE_AUTHORITY_ROOT}"
  if [[ -r /proc/sys/kernel/random/uuid ]]; then
    read -r launch_attempt_id </proc/sys/kernel/random/uuid
    launch_attempt_id="${launch_attempt_id//-/}"
  else
    launch_attempt_id="$(/usr/bin/python3.10 -B -I -c 'import secrets; print(secrets.token_hex(16))')"
  fi
  export STEP5D_V3_LAUNCH_ATTEMPT_ID="${launch_attempt_id}"
  launch_repository_head="$(git -C "${REPOSITORY_ROOT}" rev-parse --verify HEAD)"
  shell_proc_stat="$(</proc/$$/stat)"
  shell_proc_fields="${shell_proc_stat##*) }"
  read -r -a shell_proc_values <<<"${shell_proc_fields}"
  launch_owner_starttime="${shell_proc_values[19]:-}"
  if [[ ! "${launch_owner_starttime}" =~ ^[1-9][0-9]*$ ]]; then
    echo "canonical bridge owner starttime is unavailable" >&2
    exit 66
  fi
  launch_authority_epoch_file="${output_root}/bridge-authority-epoch.txt"
  /usr/bin/python3.10 -B -I \
    "${EXPERIMENT_ROOT}/tools/step5d_bridge_authority.py" begin \
    --authority-root "${BRIDGE_AUTHORITY_ROOT}" \
    --attempt-id "${launch_attempt_id}" \
    --owner-pid "$$" \
    --owner-starttime "${launch_owner_starttime}" \
    >"${launch_authority_epoch_file}"
  read -r launch_owner_authority_epoch <"${launch_authority_epoch_file}"
  launch_authority_active=1
  launch_attempt_enabled=1
  launch_runtime_bootstrap=1
  launch_attempt_phase="runtime_gate"
  trap bridge_failure_trap ERR
  trap bridge_cancel_trap INT TERM
  bridge_record_launch_attempt STARTED runtime_gate
fi

RUNTIME_SOURCE="${REPOSITORY_ROOT}/src/ur10e_experiment_runtime"
if [[ ! -d "${RUNTIME_SOURCE}/ur10e_experiment_runtime" ]]; then
  bridge_runtime_fail 66 ACTIVE_SOURCE_CLOSURE_UNRESOLVED \
    "missing ur10e_experiment_runtime source: ${RUNTIME_SOURCE}"
fi
RUNTIME_RESOLVER="${EXPERIMENT_ROOT}/tools/resolve_step5d_autotune_v3_runtime.py"
if [[ ! -f "${RUNTIME_RESOLVER}" || -L "${RUNTIME_RESOLVER}" ]]; then
  bridge_runtime_fail 66 ACTIVE_SOURCE_CLOSURE_UNRESOLVED \
    "missing governed runtime resolver: ${RUNTIME_RESOLVER}"
fi
runtime_binding=""
if ! runtime_binding="$(/usr/bin/python3.10 -B -I "${RUNTIME_RESOLVER}" --shell-binding)"; then
  bridge_runtime_fail 78 RUNTIME_NOT_PROVISIONED \
    "governed runtime unavailable; provision the current uv.lock runtime before bridge delivery"
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
  GOVERNED_GPU_UUID \
  CONTROL_LD_LIBRARY_PATH \
  CONTROL_CUPY_CACHE_DIR <<<"${runtime_binding}"
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
  "${CONTROL_LD_LIBRARY_PATH:-}"
  "${CONTROL_CUPY_CACHE_DIR:-}"
)
if (( ${#runtime_fields[@]} != 11 )); then
  bridge_runtime_fail 78 CONTROL_RUNTIME_INVALID \
    "governed runtime resolver returned an invalid field count"
fi
for field in "${runtime_fields[@]}"; do
  if [[ -z "${field}" || "${field}" == *$'\n'* || "${field}" == *$'\r'* ]]; then
    bridge_runtime_fail 78 CONTROL_RUNTIME_INVALID \
      "governed runtime resolver returned an unsafe field"
  fi
done
if [[ ! -x "${CONTROL_PYTHON}" || ! -x "${OPTIMIZER_PYTHON}" ]]; then
  bridge_runtime_fail 78 CONTROL_RUNTIME_INVALID \
    "governed runtime interpreter is unavailable"
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
  bridge_runtime_fail 66 HOST_CONTRACT_MISMATCH \
    "missing ROS Humble Python runtime for Python ${PYTHON_ABI}"
fi
AMENT_PREFIX_CANDIDATES=("${REPOSITORY_ROOT}/install" "/opt/ros/humble")
AMENT_PREFIXES=()
for candidate in "${AMENT_PREFIX_CANDIDATES[@]}"; do
  if [[ -d "${candidate}/share/ament_index/resource_index" ]]; then
    AMENT_PREFIXES+=("${candidate}")
  fi
done
if (( ${#AMENT_PREFIXES[@]} == 0 )); then
  bridge_runtime_fail 66 HOST_CONTRACT_MISMATCH "missing ROS ament prefix"
fi
RUNTIME_PYTHONPATH="${EXPERIMENT_ROOT}/tools:${RUNTIME_SOURCE}"
for candidate in "${ROS_PYTHON_PATHS[@]}"; do
  RUNTIME_PYTHONPATH="${RUNTIME_PYTHONPATH}:${candidate}"
done
export PYTHONPATH="${RUNTIME_PYTHONPATH}"
AMENT_PREFIX_PATH="$(IFS=:; echo "${AMENT_PREFIXES[*]}")"
export AMENT_PREFIX_PATH
export CUDA_VISIBLE_DEVICES="${GOVERNED_GPU_UUID}"
export CUPY_CACHE_DIR="${CONTROL_CUPY_CACHE_DIR}"
export LD_LIBRARY_PATH="${CONTROL_LD_LIBRARY_PATH}"
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
  launch_runtime_bootstrap=0
  qualification_shell=0
  if [[ -n "${STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" || -n "${STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" ]]; then
    qualification_shell=1
    shell_proc_stat="$(</proc/$$/stat)"
    shell_proc_fields="${shell_proc_stat##*) }"
    read -r -a shell_proc_values <<<"${shell_proc_fields}"
    launch_owner_starttime="${shell_proc_values[19]:-}"
    if [[ ! "${launch_owner_starttime}" =~ ^[1-9][0-9]*$ ]]; then
      echo "qualification shell owner starttime is unavailable" >&2
      exit 66
    fi
  else
    bridge_begin_phase route_resolve
  fi
  route_snapshot="${output_root}/route-snapshot.json"
  launch_attempt_route_snapshot="${route_snapshot}"
  route_resolve_rc=0
  route_resolve_args=(
    --root "${EXPERIMENT_ROOT}"
    --output "${route_snapshot}"
  )
  if [[ -n "${STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" || -n "${STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" ]]; then
    route_resolve_args+=(--robot-host 127.0.0.1)
  fi
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/resolve_step5d_bridge_route.py" \
    "${route_resolve_args[@]}" \
    >"${output_root}/route-resolve.log" 2>&1 || route_resolve_rc=$?
  bridge_route="UNKNOWN"
  resolved_release_sha=""
  route_reason_code="LAUNCH_ATTEMPT_FAILED"
  if [[ -f "${route_snapshot}" && ! -L "${route_snapshot}" ]]; then
    IFS=$'\x1f' read -r bridge_route resolved_release_sha route_reason_code < <(
      "${CONTROL_PYTHON}" -c \
        'import json,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); print(p["route"], p.get("manual_release_manifest_sha256") or p.get("autotune_release_manifest_sha256") or "", p.get("reason_code") or "LAUNCH_ATTEMPT_FAILED", sep="\x1f")' \
        "${route_snapshot}"
    )
  fi
  launch_attempt_route="${bridge_route}"
  launch_manifest_sha256="${resolved_release_sha}"
  if (( route_resolve_rc != 0 )); then
    if (( qualification_shell == 0 )); then
      bridge_record_launch_attempt \
        FAILED \
        route_resolve \
        "${route_resolve_rc}" \
        "canonical route resolution stopped: ${route_reason_code}" \
        "${route_reason_code}"
      bridge_revoke_authority failed
      launch_attempt_enabled=0
    fi
    exit "${route_resolve_rc}"
  fi
  if [[ -n "${STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" ]]; then
    if [[ "${bridge_route}" != "manual_v2" ]]; then
      echo "Manual qualification did not traverse the Manual production route" >&2
      exit 2
    fi
    export STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_PID="$$"
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/step5d_manual_qualification.py" \
      --experiment-root "${EXPERIMENT_ROOT}" \
      --_exec-live-from-shell-contract \
      "${STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_CONTRACT}" &
    qualification_manual_owner_pid=$!
    # shellcheck disable=SC2329  # Invoked by the EXIT trap below.
    qualification_manual_cleanup() {
      if kill -0 "${qualification_manual_owner_pid}" 2>/dev/null; then
        kill -INT "${qualification_manual_owner_pid}" 2>/dev/null || true
        wait "${qualification_manual_owner_pid}" || true
      fi
    }
    trap qualification_manual_cleanup EXIT
    qualification_ready_limit_ticks="$("${CONTROL_PYTHON}" -c \
      'import math,sys; print(math.ceil(float(sys.argv[1]) * 10.0) + 20)' \
      "${ready_timeout_s}")"
    qualification_ready_ticks=0
    while [[ ! -f "${output_root}/bridge_launch.json" ]]; do
      if ! kill -0 "${qualification_manual_owner_pid}" 2>/dev/null; then
        qualification_owner_rc=0
        wait "${qualification_manual_owner_pid}" || qualification_owner_rc=$?
        echo "Manual qualification bridge owner exited before readiness rc=${qualification_owner_rc}" >&2
        exit 2
      fi
      if (( qualification_ready_ticks >= qualification_ready_limit_ticks )); then
        echo "Manual qualification bridge owner readiness timeout" >&2
        exit 2
      fi
      sleep 0.1
      ((qualification_ready_ticks += 1))
    done
    qualification_campaign_rc=0
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/step5d_manual_qualification.py" \
      --experiment-root "${EXPERIMENT_ROOT}" \
      --_exec-campaign-from-shell-contract \
      "${STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_CONTRACT}" \
      || qualification_campaign_rc=$?
    exit "${qualification_campaign_rc}"
  fi
  if [[ -n "${STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" ]]; then
    if [[ "${bridge_route}" != "autotune_v3" ]]; then
      echo "V3 qualification did not traverse the V3 production route" >&2
      exit 2
    fi
    export STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_PID="$$"
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_qualification.py" \
      --_exec-live-from-shell-contract \
      "${STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_CONTRACT}"
    exit 0
  fi
  if [[ "${bridge_route}" == "manual_v2" ]]; then
    manual_release_sha="${resolved_release_sha}"
    manual_campaign_id="manual-v2-${launch_attempt_id}"
    manual_context="${output_root}/manual-bridge-context.json"
    manual_preflight="${output_root}/manual-preflight.json"
    manual_queue="${campaign_root}/control/manual_queue.json"
    manual_state="${campaign_root}/control/manual_runtime_state.json"
    bridge_begin_phase manual_qualification
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/step5d_manual_qualification.py" \
      --experiment-root "${EXPERIMENT_ROOT}" \
      --output-root "${output_root}" \
      >"${output_root}/manual-qualification-command.json"
    bridge_begin_phase manual_context
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/build_step5d_manual_bridge_start_context.py" \
      --root "${EXPERIMENT_ROOT}" \
      --plant-epoch 1 \
      --output "${manual_context}" \
      >"${output_root}/manual-context-result.json"
    bridge_begin_phase manual_preflight
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/preflight_step5d_manual_bridge.py" \
      --mailbox "${output_root}/runtime/command.json" \
      --bridge-start-context "${manual_context}" \
      --output "${manual_preflight}" \
      >"${output_root}/manual-preflight.log"
    bridge_begin_phase manual_bridge_start
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_manual_bridge_live.py" \
      --output-root "${output_root}" \
      --bridge-start-context "${manual_context}" \
      --preflight "${manual_preflight}" \
      --launch-attempt-id "${launch_attempt_id}" \
      --campaign-id "${manual_campaign_id}" \
      --canonical-owner-pid "$$" \
      --canonical-owner-starttime "${launch_owner_starttime}" \
      --ready-timeout-s "${ready_timeout_s}" \
      >"${output_root}/manual-bridge-owner.log" 2>&1 &
    manual_bridge_owner_pid=$!
    # shellcheck disable=SC2329  # Invoked by the EXIT trap below.
    manual_bridge_cleanup() {
      if kill -0 "${manual_bridge_owner_pid}" 2>/dev/null; then
        kill -INT "${manual_bridge_owner_pid}" 2>/dev/null || true
        wait "${manual_bridge_owner_pid}" || true
      fi
      bridge_revoke_authority failed || true
    }
    trap manual_bridge_cleanup EXIT
    manual_ready_limit_ticks="$("${CONTROL_PYTHON}" -c \
      'import math,sys; print(math.ceil(float(sys.argv[1]) * 10.0) + 20)' \
      "${ready_timeout_s}")"
    manual_ready_ticks=0
    while [[ ! -f "${output_root}/bridge_launch.json" ]]; do
      if ! kill -0 "${manual_bridge_owner_pid}" 2>/dev/null; then
        manual_owner_rc=0
        wait "${manual_bridge_owner_pid}" || manual_owner_rc=$?
        if (( manual_owner_rc == 0 )); then
          manual_owner_rc=2
        fi
        bridge_record_launch_attempt \
          FAILED \
          "${launch_attempt_phase}" \
          "${manual_owner_rc}" \
          "manual bridge owner exited before the persistent readiness barrier"
        bridge_revoke_authority failed
        exit "${manual_owner_rc}"
      fi
      if (( manual_ready_ticks >= manual_ready_limit_ticks )); then
        echo "manual bridge owner readiness timeout" >&2
        bridge_record_launch_attempt \
          FAILED \
          "${launch_attempt_phase}" \
          2 \
          "manual bridge owner did not sustain the readiness barrier before timeout"
        bridge_revoke_authority failed
        exit 2
      fi
      sleep 0.1
      ((manual_ready_ticks += 1))
    done
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/step5d_manual_status.py" activate \
      --campaign-root "${campaign_root}" \
      --output-root "${output_root}" \
      --pointer-root "${EXPERIMENT_ROOT}/runs/step5d_autotune_v3" \
      >"${output_root}/manual-active-run.json"
    bridge_begin_phase manual_campaign
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_manual_live_campaign.py" \
      --bridge-output-root "${output_root}" \
      --campaign-root "${campaign_root}" \
      --queue "${manual_queue}" \
      --state "${manual_state}" \
      --campaign-id "${manual_campaign_id}" \
      --release-manifest-sha256 "${manual_release_sha}" \
      --qualification-result "${output_root}/manual-qualification-result.json" \
      --play-timeout-s "${play_timeout_s}"
    bridge_finish_phase
    bridge_revoke_authority completed
    exit 0
  fi
  if [[ -z "${delivery_observation}" ]]; then
    bridge_runtime_fail 64 DELIVERY_OBSERVATION_REQUIRED \
      "V3 bridge requires --delivery-observation from an existing governed TP delivery"
  fi
  bridge_begin_phase campaign_prepare
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py" \
    --prepare-only \
    --canonical-owner-pid "$$" \
    --canonical-owner-starttime "${launch_owner_starttime}" \
    --campaign-root "${campaign_root}" \
    >"${output_root}/campaign-prepare.json"
  preflight="${output_root}/preflight.json"
  bridge_begin_phase preflight
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/preflight_step5d_autotune_v3.py" \
    --mailbox "${output_root}/runtime/command.json" \
    --delivery-observation "${delivery_observation}" \
    --output "${preflight}" \
    --json
  bridge_begin_phase live_handoff
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py" \
    "${runner_args[@]}" --output-root "${output_root}" \
    --canonical-owner-pid "$$" \
    --canonical-owner-starttime "${launch_owner_starttime}" \
    --delivery-observation "${delivery_observation}" \
    --campaign-root "${campaign_root}" \
    --preflight "${preflight}"
  bridge_finish_phase
  bridge_revoke_authority completed
  exit 0
fi
exec "${CONTROL_PYTHON}" -m step5d_autotune_v3.cli --experiment-root "${EXPERIMENT_ROOT}" "$@"
