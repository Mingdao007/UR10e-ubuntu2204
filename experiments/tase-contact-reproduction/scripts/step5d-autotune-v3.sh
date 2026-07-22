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
       step5d-autotune-v3.sh status --json --assert-state STATE
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
  local explicit_reason_code="${5:-}"
  local prior_enabled="${launch_attempt_enabled}"
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
    --_launch-capabilities-json "${launch_attempt_capabilities_json}"
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
  "${command[@]}" >>"${output_root}/launch-attempt-recorder.log" 2>&1
  launch_attempt_enabled="${prior_enabled}"
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
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/step5d_bridge_authority.py" revoke \
    --authority-root "${BRIDGE_AUTHORITY_ROOT}" \
    --attempt-id "${launch_attempt_id}" \
    --owner-pid "$$" \
    --owner-starttime "${launch_owner_starttime}" \
    --reason "${reason}" \
    >>"${output_root}/bridge-authority.log" 2>&1
  launch_authority_active=0
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
arguments=()
output_root=""
campaign_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3"
canonical_launch_profile="${EXPERIMENT_ROOT}/config/step5/step5d_autotune_v3_launch_profile.json"
runner_args=()
ready_timeout_s="20"
play_timeout_s="900"
launch_attempt_id=""
launch_attempt_phase=""
launch_attempt_enabled=0
launch_attempt_route="UNKNOWN"
launch_attempt_route_snapshot=""
launch_attempt_capabilities_json='{"bridge":true,"play":false,"arm":false,"motion":false,"zero":false,"tare":false}'
launch_manifest_sha256=""
launch_repository_head=""
launch_owner_starttime=""
launch_owner_authority_epoch=""
launch_authority_active=0
BRIDGE_AUTHORITY_ROOT="${EXPERIMENT_ROOT}/runs/step5d_bridge_authority"

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
if [[ "${1:-}" == "status" && "${2:-}" == "--json" ]]; then
  status_command=(
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/step5d_bridge_status.py"
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
if (( bridge_mode == 1 )); then
  export STEP5D_V3_CANONICAL_LAUNCHER="${SCRIPT_PATH}"
  export STEP5D_V3_SHELL_PID="$$"
  if [[ -z "${output_root}" ]]; then
    output_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/bridge-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  fi
  output_root="$(readlink -m -- "${output_root}")"
  campaign_root="$(readlink -m -- "${campaign_root}")"
  mkdir -p -- "${output_root}" "${campaign_root}"
  if [[ -n "${STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_CONTRACT:-}" ]]; then
    export STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_PID="$$"
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/step5d_manual_qualification.py" \
      --experiment-root "${EXPERIMENT_ROOT}" \
      --_exec-live-from-shell-contract \
      "${STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_CONTRACT}"
    exit 0
  fi
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
  launch_repository_head="$(git -C "${REPOSITORY_ROOT}" rev-parse --verify HEAD)"
  launch_owner_starttime="$("${CONTROL_PYTHON}" -c \
    'from pathlib import Path; import os; print((Path("/proc") / str(os.getppid()) / "stat").read_text().split()[21])')"
  mkdir -p -- "${BRIDGE_AUTHORITY_ROOT}"
  launch_owner_authority_epoch="$(
    "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/step5d_bridge_authority.py" begin \
      --authority-root "${BRIDGE_AUTHORITY_ROOT}" \
      --attempt-id "${launch_attempt_id}" \
      --owner-pid "$$" \
      --owner-starttime "${launch_owner_starttime}"
  )"
  launch_authority_active=1
  launch_attempt_enabled=1
  trap bridge_failure_trap ERR
  trap bridge_cancel_trap INT TERM
  bridge_begin_phase runtime_gate
  bridge_begin_phase route_resolve
  route_snapshot="${output_root}/route-snapshot.json"
  launch_attempt_route_snapshot="${route_snapshot}"
  route_resolve_rc=0
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/resolve_step5d_bridge_route.py" \
    --root "${EXPERIMENT_ROOT}" \
    --output "${route_snapshot}" \
    >"${output_root}/route-resolve.log" 2>&1 || route_resolve_rc=$?
  bridge_route="UNKNOWN"
  resolved_release_sha=""
  route_reason_code="LAUNCH_ATTEMPT_FAILED"
  if [[ -f "${route_snapshot}" && ! -L "${route_snapshot}" ]]; then
    IFS=$'\t' read -r bridge_route resolved_release_sha route_reason_code < <(
      "${CONTROL_PYTHON}" -c \
        'import json,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); print(p["route"], p.get("manual_release_manifest_sha256") or p.get("autotune_release_manifest_sha256") or "", p.get("reason_code") or "LAUNCH_ATTEMPT_FAILED", sep="\t")' \
        "${route_snapshot}"
    )
  fi
  launch_attempt_route="${bridge_route}"
  launch_manifest_sha256="${resolved_release_sha}"
  if (( route_resolve_rc != 0 )); then
    bridge_record_launch_attempt \
      FAILED \
      route_resolve \
      "${route_resolve_rc}" \
      "canonical route resolution stopped: ${route_reason_code}" \
      "${route_reason_code}"
    bridge_revoke_authority failed
    launch_attempt_enabled=0
    exit "${route_resolve_rc}"
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
      --authorization-file "${campaign_root}/control/manual_capability_authorization.json" \
      --play-timeout-s "${play_timeout_s}"
    bridge_finish_phase
    bridge_revoke_authority completed
    exit 0
  fi
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
  launch_manifest_sha256="$(
    "${CONTROL_PYTHON}" -c \
      'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["release_manifest_sha256"])' \
      "${output_root}/delivery-observation.json"
  )"
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
  bridge_finish_phase
  bridge_revoke_authority completed
  exit 0
fi
exec "${CONTROL_PYTHON}" -m step5d_autotune_v3.cli --experiment-root "${EXPERIMENT_ROOT}" "$@"
