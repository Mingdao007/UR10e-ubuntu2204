#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
EXPERIMENT_ROOT="$(cd -- "$(dirname -- "${SCRIPT_PATH}")/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${EXPERIMENT_ROOT}/../.." && pwd)"

bridge_usage() {
  cat <<'EOF'
Usage: step5d-autotune-v3.sh bridge-live [OPTIONS]

Canonical governed Step5d live bridge launcher. Reuses existing release-contract
and TP delivery evidence; campaign preparation and preflight are automatic.

Options:
  --output-root PATH       Per-run evidence directory
  --campaign-root PATH     Campaign state directory
  --delivery-observation PATH
                           Compatibility-only explicit governed TP delivery
                           observation; current release resolves it by default
  --launch-profile PATH    Compatibility-only canonical profile path
  --ready-timeout-s SEC    Compatibility-only; readiness is event-driven
  -h, --help               Show this help without starting any work
EOF
}

tp_deliver_usage() {
  cat <<'EOF'
Usage: step5d-autotune-v3.sh tp-deliver [OPTIONS]

Canonical TP-local delivery transaction. Uploads the contract-checked triplet, performs
a fresh controller GET, promotes the immutable release, and writes a delivery
receipt. It never sends Dashboard Load or Play.

Options:
  --release-candidate PATH   Required immutable local release candidate
  --release-certificate PATH
                             Required release-contract certificate bound to the candidate

Optional:
  --artifact-dir PATH        TP package directory (default: canonical Step5d)
  --evidence-output PATH     Receipt output under runs/ (default: generated)
  --prior-full-readback-receipt PATH
                             Required migration basis for first no-upload adoption;
                             later releases reuse the tracked current basis
  --publication-plan-output PATH
                             Machine-readable post-promotion Git publication plan
  --publish-and-revalidate  Consume the plan, commit its exact local allowlist,
                             then invoke clean canonical revalidate-current
  --readback-only-existing   Fresh GET and adopt the exact candidate after
                             three-way SHA closure; no upload or Load
  -h, --help                 Show this help without starting any work
EOF
}

runtime_revalidate_usage() {
  cat <<'EOF'
Usage: step5d-autotune-v3.sh revalidate-current [OPTIONS]

Non-promoting post-merge/runtime-FF gate. Recomputes the current release,
runs the sub-3 s release contract, performs a fresh controller GET, and writes
runtime-local delivery plus publication-lineage evidence. It never uploads,
promotes, loads, starts, sends ARM, or moves the robot.

Options:
  --artifact-dir PATH        TP package directory (default: current Step5d)
  --evidence-output PATH     Receipt output under runs/ (default: generated)
  -h, --help                 Show this help without controller access
EOF
}

release_contract_check_usage() {
  cat <<'EOF'
Usage: step5d-autotune-v3.sh release-contract-check [OPTIONS]

Canonical offline/no-motion release contract. Runs the no-network/no-trial
state-machine transition and writes an immutable release-scoped certificate.

Options:
  --release-candidate PATH   Reuse an immutable local release candidate
  --artifact-dir PATH        Required TP package when staging a candidate
                             (default: current immutable release artifacts)

Output:
  JSON includes the exact release_candidate path required by tp-deliver.

  -h, --help                 Show this help without starting any work
EOF
}

usage() {
  cat <<'EOF'
Usage: step5d-autotune-v3.sh bridge-live [OPTIONS]
       step5d-autotune-v3.sh release-contract-check [OPTIONS]
       step5d-autotune-v3.sh tp-deliver [OPTIONS]
       step5d-autotune-v3.sh revalidate-current [OPTIONS]
       step5d-autotune-v3.sh status [--json]
       step5d-autotune-v3.sh status --json --assert-state STATE
       step5d-autotune-v3.sh [OPERATOR-CLI-ARGS]

Use "step5d-autotune-v3.sh bridge-live --help" for bridge options.
Use "step5d-autotune-v3.sh release-contract-check --help" for contract options.
Use "step5d-autotune-v3.sh tp-deliver --help" for delivery options.
Use "step5d-autotune-v3.sh revalidate-current --help" for runtime revalidation.
EOF
}

bridge_argv_error() {
  echo "bridge argv refused: $1" >&2
  echo "use: step5d-autotune-v3.sh bridge-live --help" >&2
  exit 64
}

tp_deliver_argv_error() {
  echo "tp-deliver argv refused: $1" >&2
  echo "use: step5d-autotune-v3.sh tp-deliver --help" >&2
  exit 64
}

release_contract_check_argv_error() {
  echo "release-contract-check argv refused: $1" >&2
  echo "use: step5d-autotune-v3.sh release-contract-check --help" >&2
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
      "${CONTROL_PYTHON}" -B -I
      "${EXPERIMENT_ROOT}/tools/step5d_bridge_authority.py"
      "${bootstrap_action}"
      "${authority_root_args[@]}"
      --resource-id "${authority_resource_id}"
      --attempt-id "${launch_attempt_id}"
      --owner-pid "$$"
      --owner-starttime "${launch_owner_starttime}"
      --worktree-root "${REPOSITORY_ROOT}"
      --repository-head "${launch_repository_head}"
    )
    if [[ -n "${launch_basis_path}" || -n "${launch_basis_sha256}" ]]; then
      if [[ -z "${launch_basis_path}" || -z "${launch_basis_sha256}" ]]; then
        echo "canonical launch basis metadata is incomplete" >&2
        return 2
      fi
      bootstrap_command+=(
        --launch-basis-path "${launch_basis_path}"
        --launch-basis-sha256 "${launch_basis_sha256}"
      )
    fi
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
    --campaign-root "${LAUNCH_ATTEMPT_ROOT}"
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
    "${authority_root_args[@]}" \
    --resource-id "${authority_resource_id}" \
    --attempt-id "${launch_attempt_id}" \
    --owner-pid "$$" \
    --owner-starttime "${launch_owner_starttime}" \
    --reason "${reason}" \
    >>"${output_root}/bridge-authority.log" 2>&1
  launch_authority_active=0
}

bridge_acquire_authority() {
  if (( launch_authority_active == 1 )); then
    return 0
  fi
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
    return 66
  fi
  if [[ -n "${BRIDGE_AUTHORITY_ROOT}" ]]; then
    mkdir -p -- "${BRIDGE_AUTHORITY_ROOT}"
  fi
  launch_authority_runtime_started=0
  launch_runtime_gate_started=0
  launch_basis_path=""
  launch_basis_sha256=""
  launch_authority_epoch_file="${output_root}/bridge-authority-epoch.txt"
  /usr/bin/python3.10 -B -I \
    "${EXPERIMENT_ROOT}/tools/step5d_bridge_authority.py" begin \
    "${authority_root_args[@]}" \
    --resource-id "${authority_resource_id}" \
    --attempt-id "${launch_attempt_id}" \
    --owner-pid "$$" \
    --owner-starttime "${launch_owner_starttime}" \
    --worktree-root "${REPOSITORY_ROOT}" \
    --repository-head "${launch_repository_head}" \
    >"${launch_authority_epoch_file}"
  read -r launch_owner_authority_epoch <"${launch_authority_epoch_file}"
  launch_authority_active=1
  launch_attempt_enabled=1
  launch_attempt_phase="runtime_gate"
  trap bridge_failure_trap ERR
  trap bridge_cancel_trap INT TERM
}

bridge_runtime_fail() {
  local exit_code="$1"
  local reason_code="$2"
  local detail="$3"
  if (( launch_attempt_enabled == 1 )); then
    set +e
    if (( launch_authority_runtime_started == 1 )); then
      launch_runtime_bootstrap=1
      bridge_record_launch_attempt \
        FAILED runtime_gate "${exit_code}" "${detail}" "${reason_code}"
      launch_runtime_bootstrap=0
      if (( launch_runtime_gate_started == 1 )); then
        bridge_record_launch_attempt \
          FAILED runtime_gate "${exit_code}" "${detail}" "${reason_code}"
      fi
      if [[ "${reason_code}" == "LAUNCH_ATTEMPT_CANCELLED" ]]; then
        bridge_revoke_authority cancelled
      else
        bridge_revoke_authority failed
      fi
    else
      if [[ "${reason_code}" == "LAUNCH_ATTEMPT_CANCELLED" ]]; then
        bridge_revoke_authority cancelled
      else
        bridge_revoke_authority failed
      fi
    fi
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
    bridge_runtime_fail \
      "${exit_code}" \
      LAUNCH_ATTEMPT_FAILED \
      "canonical bridge phase ${launch_attempt_phase} exited ${exit_code}"
  fi
  exit "${exit_code}"
}

bridge_cancel_trap() {
  trap - ERR INT TERM
  set +e
  if (( launch_attempt_enabled == 1 )) && [[ -n "${launch_attempt_phase}" ]]; then
    bridge_runtime_fail \
      130 \
      LAUNCH_ATTEMPT_CANCELLED \
      "canonical bridge was cancelled during ${launch_attempt_phase}"
  else
    bridge_revoke_authority cancelled
  fi
  exit 130
}

bridge_mode=0
release_contract_check_mode=0
tp_deliver_mode=0
runtime_revalidate_mode=0
tp_deliver_args=()
release_candidate=""
arguments=()
output_root=""
campaign_root=""
delivery_observation=""
canonical_launch_profile="${EXPERIMENT_ROOT}/config/step5/step5d_autotune_v3_launch_profile.json"
launch_attempt_id=""
launch_attempt_phase=""
launch_attempt_enabled=0
launch_attempt_route="UNKNOWN"
launch_attempt_route_snapshot=""
launch_manifest_sha256=""
LAUNCH_ATTEMPT_ROOT="${EXPERIMENT_ROOT}/runs/step5d_bridge_authority"
launch_basis_path=""
launch_basis_sha256=""
launch_repository_head=""
launch_owner_starttime=""
launch_owner_authority_epoch=""
launch_authority_active=0
launch_authority_runtime_started=0
launch_runtime_gate_started=0
launch_runtime_bootstrap=0
CONTROL_PYTHON=""
BRIDGE_AUTHORITY_ROOT="${STEP5D_V3_AUTHORITY_ROOT:-}"
authority_resource_id="${STEP5D_V3_AUTHORITY_RESOURCE_ID:-step5d-bridge-writer}"
authority_root_args=()
if [[ -n "${BRIDGE_AUTHORITY_ROOT}" ]]; then
  authority_root_args=(--authority-root "${BRIDGE_AUTHORITY_ROOT}")
  export STEP5D_V3_AUTHORITY_ROOT
fi
export STEP5D_V3_AUTHORITY_RESOURCE_ID="${authority_resource_id}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "bridge" ]]; then
  echo "bridge argv refused: compatibility cutoff passed; use bridge-live" >&2
  exit 64
fi

if [[ "${1:-}" == "bridge-live" ]]; then
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
  seen_delivery_observation=0
  seen_launch_profile=0
  seen_ready_timeout=0
  index=0
  while (( index < ${#arguments[@]} )); do
    option="${arguments[index]}"
    option_name="${option%%=*}"
    value=""
    case "${option}" in
      --output-root|--campaign-root|--delivery-observation|--launch-profile|--ready-timeout-s)
        if (( index + 1 >= ${#arguments[@]} )) || [[ "${arguments[index + 1]}" == -* ]]; then
          bridge_argv_error "${option} requires a value"
        fi
        value="${arguments[index + 1]}"
        ((index += 2))
        ;;
      --output-root=*|--campaign-root=*|--delivery-observation=*|--launch-profile=*|--ready-timeout-s=*)
        value="${option#*=}"
        if [[ -z "${value}" ]]; then
          bridge_argv_error "${option_name} requires a value"
        fi
        ((index += 1))
        ;;
      --preflight|--preflight=*|--prepare-only|--prepare-only=*|--experiment-root|--experiment-root=*|--campaign-binding|--campaign-binding=*|--campaign-lease|--campaign-lease=*|--arm-gate|--arm-gate=*)
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
        ;;
    esac
  done
fi

if [[ "${1:-}" == "release-contract-check" ]]; then
  release_contract_check_mode=1
  shift
  arguments=("$@")
  for option in "${arguments[@]}"; do
    if [[ "${option}" == "-h" || "${option}" == "--help" ]]; then
      release_contract_check_usage
      exit 0
    fi
  done

  seen_release_candidate=0
  seen_artifact_dir=0
  artifact_dir=""
  index=0
  while (( index < ${#arguments[@]} )); do
    option="${arguments[index]}"
    option_name="${option%%=*}"
    value=""
    case "${option}" in
      --release-candidate)
        if (( index + 1 >= ${#arguments[@]} )) || [[ "${arguments[index + 1]}" == -* ]]; then
          release_contract_check_argv_error "${option} requires a value"
        fi
        value="${arguments[index + 1]}"
        ((index += 2))
        ;;
      --release-candidate=*)
        value="${option#*=}"
        if [[ -z "${value}" ]]; then
          release_contract_check_argv_error "${option_name} requires a value"
        fi
        ((index += 1))
        ;;
      --artifact-dir)
        if (( index + 1 >= ${#arguments[@]} )) || [[ "${arguments[index + 1]}" == -* ]]; then
          release_contract_check_argv_error "${option} requires a value"
        fi
        value="${arguments[index + 1]}"
        ((index += 2))
        (( seen_artifact_dir == 0 )) \
          || release_contract_check_argv_error "--artifact-dir may appear only once"
        seen_artifact_dir=1
        artifact_dir="$(readlink -m -- "${value}")"
        continue
        ;;
      --artifact-dir=*)
        value="${option#*=}"
        if [[ -z "${value}" ]]; then
          release_contract_check_argv_error "${option_name} requires a value"
        fi
        ((index += 1))
        (( seen_artifact_dir == 0 )) \
          || release_contract_check_argv_error "--artifact-dir may appear only once"
        seen_artifact_dir=1
        artifact_dir="$(readlink -m -- "${value}")"
        continue
        ;;
      *)
        release_contract_check_argv_error "unsupported option or positional argument: ${option}"
        ;;
    esac
    (( seen_release_candidate == 0 )) \
      || release_contract_check_argv_error "--release-candidate may appear only once"
    seen_release_candidate=1
    release_candidate="$(readlink -m -- "${value}")"
  done
fi

if [[ "${1:-}" == "tp-deliver" ]]; then
  tp_deliver_mode=1
  shift
  arguments=("$@")
  for option in "${arguments[@]}"; do
    if [[ "${option}" == "-h" || "${option}" == "--help" ]]; then
      tp_deliver_usage
      exit 0
    fi
  done

  artifact_dir="${EXPERIMENT_ROOT}/programs/step5/step5d"
  release_certificate=""
  delivery_evidence_output=""
  publication_plan_output=""
  publish_and_revalidate=0
  seen_artifact_dir=0
  seen_release_candidate=0
  seen_release_certificate=0
  seen_evidence_output=0
  seen_prior_full_readback=0
  seen_publication_plan_output=0
  seen_readback_only=0
  index=0
  while (( index < ${#arguments[@]} )); do
    option="${arguments[index]}"
    option_name="${option%%=*}"
    value=""
    case "${option}" in
      --artifact-dir|--release-candidate|--release-certificate|--evidence-output|--prior-full-readback-receipt|--publication-plan-output)
        if (( index + 1 >= ${#arguments[@]} )) || [[ "${arguments[index + 1]}" == -* ]]; then
          tp_deliver_argv_error "${option} requires a value"
        fi
        value="${arguments[index + 1]}"
        ((index += 2))
        ;;
      --artifact-dir=*|--release-candidate=*|--release-certificate=*|--evidence-output=*|--prior-full-readback-receipt=*|--publication-plan-output=*)
        value="${option#*=}"
        if [[ -z "${value}" ]]; then
          tp_deliver_argv_error "${option_name} requires a value"
        fi
        ((index += 1))
        ;;
      --publish-and-revalidate)
        ;;
      --readback-only-existing)
        (( seen_readback_only == 0 )) || tp_deliver_argv_error "--readback-only-existing may appear only once"
        seen_readback_only=1
        tp_deliver_args+=(--readback-only-existing)
        ((index += 1))
        continue
        ;;
      *)
        tp_deliver_argv_error "unsupported option or positional argument: ${option}"
        ;;
    esac

    case "${option_name}" in
      --artifact-dir)
        (( seen_artifact_dir == 0 )) || tp_deliver_argv_error "--artifact-dir may appear only once"
        seen_artifact_dir=1
        artifact_dir="$(readlink -m -- "${value}")"
        ;;
      --release-candidate)
        (( seen_release_candidate == 0 )) || tp_deliver_argv_error "--release-candidate may appear only once"
        seen_release_candidate=1
        release_candidate="$(readlink -m -- "${value}")"
        ;;
      --release-certificate)
        (( seen_release_certificate == 0 )) || tp_deliver_argv_error "--release-certificate may appear only once"
        seen_release_certificate=1
        release_certificate="$(readlink -m -- "${value}")"
        ;;
      --evidence-output)
        (( seen_evidence_output == 0 )) || tp_deliver_argv_error "--evidence-output may appear only once"
        seen_evidence_output=1
        delivery_evidence_output="$(readlink -m -- "${value}")"
        ;;
      --prior-full-readback-receipt)
        (( seen_prior_full_readback == 0 )) || tp_deliver_argv_error "--prior-full-readback-receipt may appear only once"
        seen_prior_full_readback=1
        tp_deliver_args+=(
          --prior-full-readback-receipt
          "$(readlink -m -- "${value}")"
        )
        ;;
      --publication-plan-output)
        (( seen_publication_plan_output == 0 )) || tp_deliver_argv_error "--publication-plan-output may appear only once"
        seen_publication_plan_output=1
        publication_plan_output="$(readlink -m -- "${value}")"
        ;;
      --publish-and-revalidate)
        (( publish_and_revalidate == 0 )) || tp_deliver_argv_error "--publish-and-revalidate may appear only once"
        publish_and_revalidate=1
        tp_deliver_args+=(--publish-and-revalidate)
        ((index += 1))
        continue
        ;;
    esac
  done
  if [[ -z "${release_candidate}" ]]; then
    tp_deliver_argv_error "--release-candidate is required"
  fi
  if [[ -z "${release_certificate}" ]]; then
    tp_deliver_argv_error "--release-certificate is required"
  fi
  if [[ -z "${delivery_evidence_output}" ]]; then
    delivery_evidence_output="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/delivery-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
  fi
  if [[ -z "${publication_plan_output}" ]]; then
    publication_plan_output="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/post-promotion-publication-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
  fi
  tp_deliver_args=(
    --root "${EXPERIMENT_ROOT}"
    --artifact-dir "${artifact_dir}"
    --release-candidate "${release_candidate}"
    --release-certificate "${release_certificate}"
    --evidence-output "${delivery_evidence_output}"
    --publication-plan-output "${publication_plan_output}"
    "${tp_deliver_args[@]}"
  )
fi

if [[ "${1:-}" == "revalidate-current" ]]; then
  runtime_revalidate_mode=1
  shift
  arguments=("$@")
  artifact_dir=""
  delivery_evidence_output=""
  seen_artifact_dir=0
  seen_evidence_output=0
  index=0
  while (( index < ${#arguments[@]} )); do
    option="${arguments[index]}"
    option_name="${option%%=*}"
    value=""
    if [[ "${option}" == "-h" || "${option}" == "--help" ]]; then
      runtime_revalidate_usage
      exit 0
    fi
    case "${option}" in
      --artifact-dir|--evidence-output)
        if (( index + 1 >= ${#arguments[@]} )) || [[ "${arguments[index + 1]}" == -* ]]; then
          tp_deliver_argv_error "${option} requires a value"
        fi
        value="${arguments[index + 1]}"
        ((index += 2))
        ;;
      --artifact-dir=*|--evidence-output=*)
        value="${option#*=}"
        [[ -n "${value}" ]] || tp_deliver_argv_error "${option_name} requires a value"
        ((index += 1))
        ;;
      *)
        tp_deliver_argv_error "unsupported revalidate-current option: ${option}"
        ;;
    esac
    case "${option_name}" in
      --artifact-dir)
        (( seen_artifact_dir == 0 )) || tp_deliver_argv_error "--artifact-dir may appear only once"
        seen_artifact_dir=1
        artifact_dir="$(readlink -m -- "${value}")"
        ;;
      --evidence-output)
        (( seen_evidence_output == 0 )) || tp_deliver_argv_error "--evidence-output may appear only once"
        seen_evidence_output=1
        delivery_evidence_output="$(readlink -m -- "${value}")"
        ;;
    esac
  done
  if [[ -z "${delivery_evidence_output}" ]]; then
    delivery_evidence_output="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/revalidate-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
  fi
fi

if (( bridge_mode == 1 )); then
  export STEP5D_V3_CANONICAL_LAUNCHER="${SCRIPT_PATH}"
  export STEP5D_V3_SHELL_PID="$$"
  attempt_suffix="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  if [[ -z "${output_root}" ]]; then
    output_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/bridge-${attempt_suffix}"
  fi
  if [[ -z "${campaign_root}" ]]; then
    campaign_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/parameter-campaign"
  fi
  output_root="$(readlink -m -- "${output_root}")"
  campaign_root="$(readlink -m -- "${campaign_root}")"
  mkdir -p -- "${output_root}" "${campaign_root}"
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
  RUNTIME_BUNDLE_ID \
  RUNTIME_ATTESTATION_SHA256 \
  RUNTIME_CONTRACT_SHA256 \
  RUNTIME_LOCK_SHA256 \
  CONTROL_ENVIRONMENT_ID \
  GOVERNED_GPU_UUID \
  CONTROL_LD_LIBRARY_PATH \
  CONTROL_CUPY_CACHE_DIR <<<"${runtime_binding}"
runtime_fields=(
  "${CONTROL_PYTHON:-}"
  "${RUNTIME_BUNDLE_ID:-}"
  "${RUNTIME_ATTESTATION_SHA256:-}"
  "${RUNTIME_CONTRACT_SHA256:-}"
  "${RUNTIME_LOCK_SHA256:-}"
  "${CONTROL_ENVIRONMENT_ID:-}"
  "${GOVERNED_GPU_UUID:-}"
  "${CONTROL_LD_LIBRARY_PATH:-}"
  "${CONTROL_CUPY_CACHE_DIR:-}"
)
if (( ${#runtime_fields[@]} != 9 )); then
  bridge_runtime_fail 78 CONTROL_RUNTIME_INVALID \
    "governed runtime resolver returned an invalid field count"
fi
for field in "${runtime_fields[@]}"; do
  if [[ -z "${field}" || "${field}" == *$'\n'* || "${field}" == *$'\r'* ]]; then
    bridge_runtime_fail 78 CONTROL_RUNTIME_INVALID \
      "governed runtime resolver returned an unsafe field"
  fi
done
if [[ ! -x "${CONTROL_PYTHON}" ]]; then
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
export STEP5D_V3_RUNTIME_ATTESTATION_SHA256="${RUNTIME_ATTESTATION_SHA256}"
export STEP5D_V3_RUNTIME_BUNDLE_ID="${RUNTIME_BUNDLE_ID}"
if [[ "${1:-}" == "status" && "${2:-}" == "--json" ]]; then
  status_command=(
    "${CONTROL_PYTHON}" -B -I
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
if (( release_contract_check_mode == 1 )); then
  export STEP5D_V3_CANONICAL_LAUNCHER="${SCRIPT_PATH}"
  export STEP5D_V3_SHELL_PID="$$"
  if [[ -z "${release_candidate}" ]]; then
    if [[ -z "${artifact_dir}" ]]; then
      release_contract_check_argv_error \
        "--artifact-dir is required when --release-candidate is omitted"
    fi
    release_candidate="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/release-candidates/$(date -u +%Y%m%dT%H%M%SZ)-$$/candidate.json"
    candidate_command=(
      "${CONTROL_PYTHON}"
      "${EXPERIMENT_ROOT}/tools/promote_step5d_r009_atomic_release.py"
      --root "${EXPERIMENT_ROOT}"
      --stage-local-candidate
      --candidate-output "${release_candidate}"
    )
    candidate_command+=(--artifact-dir "${artifact_dir}")
    "${candidate_command[@]}" >/dev/null
  fi
  "${CONTROL_PYTHON}" \
    "${EXPERIMENT_ROOT}/tools/run_step5d_release_contract.py" \
    --experiment-root "${EXPERIMENT_ROOT}" \
    --output-root "${EXPERIMENT_ROOT}/runs/step5d_autotune_v3" \
    --release-candidate "${release_candidate}"
  exit $?
fi
if (( tp_deliver_mode == 1 )); then
  export STEP5D_V3_CANONICAL_LAUNCHER="${SCRIPT_PATH}"
  export STEP5D_V3_SHELL_PID="$$"
  "${CONTROL_PYTHON}" \
    "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_tp_transaction.py" \
    "${tp_deliver_args[@]}"
  exit $?
fi
if (( runtime_revalidate_mode == 1 )); then
  export STEP5D_V3_CANONICAL_LAUNCHER="${SCRIPT_PATH}"
  export STEP5D_V3_SHELL_PID="$$"
  mkdir -p -- "${EXPERIMENT_ROOT}/runs/step5d_autotune_v3"
  if [[ -z "${artifact_dir}" ]]; then
    artifact_dir="$(
      "${CONTROL_PYTHON}" -c '
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1]).resolve(strict=True)
pointer = json.loads((root / "config/step5d/current.json").read_text(encoding="utf-8"))
manifest = (root / pointer["manifest_path"]).resolve(strict=True)
print(manifest.parent / "programs/step5/step5d")
' "${EXPERIMENT_ROOT}"
    )"
  fi
  "${CONTROL_PYTHON}" \
    "${EXPERIMENT_ROOT}/tools/run_step5d_release_contract.py" \
    --experiment-root "${EXPERIMENT_ROOT}" \
    --output-root "${EXPERIMENT_ROOT}/runs/step5d_autotune_v3" \
    >"${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/revalidate-contract-$$.json"
  revalidate_command=(
    "${CONTROL_PYTHON}"
    "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_tp_transaction.py"
    --root "${EXPERIMENT_ROOT}"
    --evidence-output "${delivery_evidence_output}"
    --readback-only-existing
    --revalidate-current
    --artifact-dir "${artifact_dir}"
  )
  "${revalidate_command[@]}"
  exit $?
fi
if (( bridge_mode == 1 )); then
  export STEP5D_V3_CANONICAL_LAUNCHER="${SCRIPT_PATH}"
  export STEP5D_V3_SHELL_PID="$$"
  if [[ -z "${output_root}" ]]; then
    output_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3/bridge-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  fi
  LAUNCH_ATTEMPT_ROOT="${EXPERIMENT_ROOT}/runs/step5d_bridge_authority"
  output_root="$(readlink -m -- "${output_root}")"
  LAUNCH_ATTEMPT_ROOT="$(readlink -m -- "${LAUNCH_ATTEMPT_ROOT}")"
  campaign_root="$(readlink -m -- "${campaign_root}")"
  mkdir -p -- "${output_root}" "${campaign_root}"
  launch_runtime_bootstrap=0
  route_snapshot="${output_root}/route-snapshot.json"
  launch_attempt_route_snapshot="${route_snapshot}"
  route_resolve_rc=0
  route_resolve_args=(
    --root "${EXPERIMENT_ROOT}"
    --output "${route_snapshot}"
  )
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
    echo "canonical route resolution stopped: ${route_reason_code}" >&2
    exit "${route_resolve_rc}"
  fi
  admission="${output_root}/bridge-admission.json"
  admission_args=(
    --root "${EXPERIMENT_ROOT}"
    --output "${admission}"
  )
  if [[ -n "${delivery_observation}" ]]; then
    admission_args+=(--delivery-observation "${delivery_observation}")
  fi
  admission_rc=0
  "${CONTROL_PYTHON}" \
    "${EXPERIMENT_ROOT}/tools/check_step5d_autotune_v3_bridge_admission.py" \
    "${admission_args[@]}" \
    >"${output_root}/bridge-admission.log" || admission_rc=$?
  if (( admission_rc != 0 )); then
    echo "bridge admission observation failed; see ${admission}" >&2
    exit "${admission_rc}"
  fi
  admission="$(${CONTROL_PYTHON} -c '
import json
import pathlib
import sys
from step5d_autotune_v3.bridge_admission import admission_index_path
root = pathlib.Path(sys.argv[2]).resolve()
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(admission_index_path(root, payload))
' "${admission}" "${EXPERIMENT_ROOT}")"
  delivery_observation="$(
    "${CONTROL_PYTHON}" -c \
      'import json,pathlib,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); print((pathlib.Path(sys.argv[2]) / p["delivery_observation"]["path"]).resolve())' \
      "${admission}" "${EXPERIMENT_ROOT}"
  )"
  bridge_acquire_authority
  preflight="${output_root}/preflight.json"
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_coordinator.py" \
    --experiment-root "${EXPERIMENT_ROOT}" \
    --admission "${admission}" \
    "${authority_root_args[@]}" \
    --authority-resource-id "${authority_resource_id}" \
    --attempt-id "${launch_attempt_id}" \
    --authority-epoch "${launch_owner_authority_epoch}" \
    --owner-pid "$$" \
    --owner-starttime "${launch_owner_starttime}" \
    --output-root "${output_root}" \
    --campaign-root "${campaign_root}" \
    --delivery-observation "${delivery_observation}" \
    --preflight "${preflight}" \
    --launch-basis "${output_root}/launch-basis.json"
  launch_basis_path="${output_root}/launch-basis.json"
  launch_basis_sha256="$(${CONTROL_PYTHON} -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["basis_sha256"])' "${launch_basis_path}")"
  launch_runtime_bootstrap=1
  bridge_record_launch_attempt STARTED runtime_gate
  launch_authority_runtime_started=1
  launch_runtime_bootstrap=0
  bridge_record_launch_attempt STARTED runtime_gate
  launch_runtime_gate_started=1
  bridge_begin_phase campaign_prepare
  bridge_begin_phase preflight
  bridge_begin_phase live_handoff
  "${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py" \
    --output-root "${output_root}" \
    --canonical-owner-pid "$$" \
    --canonical-owner-starttime "${launch_owner_starttime}" \
    --authority-epoch "${launch_owner_authority_epoch}" \
    --delivery-observation "${delivery_observation}" \
    --campaign-root "${campaign_root}" \
    --preflight "${preflight}" \
    --admission "${admission}" \
    --launch-basis "${output_root}/launch-basis.json" \
    --launch-basis-sha256 "${launch_basis_sha256}" \
    --campaign-prepare "${output_root}/campaign-prepare.json"
  bridge_finish_phase
  bridge_revoke_authority completed
  exit 0
fi
exec "${CONTROL_PYTHON}" -m step5d_autotune_v3.cli --experiment-root "${EXPERIMENT_ROOT}" "$@"
