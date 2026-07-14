#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUN_ROOT="${ROOT}/runs"
BUILD_TOOL="${ROOT}/tools/build_step5d_liveprep.py"
UPLOAD_TOOL="${ROOT}/tools/upload_ur_tp_package.py"
READBACK_GATE="${ROOT}/tools/verify_step5d_current_binding.py"
PROMOTE_TOOL="${ROOT}/tools/promote_step5d_current.py"
TP_COORDINATOR="${ROOT}/tools/run_step5d_tp_transaction.py"
PUBLISH_GATE="${ROOT}/tools/verify_step5d_publish_gate.py"
PARALLEL_TOOL="${ROOT}/tools/run_step5d_parallel_workflow.py"
OPERATOR="${SCRIPT_DIR}/step5d-liveprep-operator.sh"
DRYRUN_READBACK_ROOT="${STEP5D_DRYRUN_READBACK_ROOT:-/tmp/ur10e_tp_readback_dryrun}"
LATEST_CANDIDATE_INDEX="${RUN_ROOT}/local_tp_packages/.latest_step5d_candidate.json"

usage() {
  cat <<EOF
Usage:
  step5d-workflow.sh status
  step5d-workflow.sh dev-loop
  step5d-workflow.sh parallel-check
  step5d-workflow.sh offline-functional
  step5d-workflow.sh formal-timing
  step5d-workflow.sh offline-all
  step5d-workflow.sh postprocess <immutable-run-dir>
  step5d-workflow.sh promote-package
  step5d-workflow.sh prep-long-checks
  STEP5D_CONFIRM='LIVE STEP5D STRICT RNN LIVEPREP' step5d-workflow.sh contact-bridge

Boundary:
  - dev-loop is local-only; it never uploads, updates current_stage, or starts bridge.
  - offline-functional uses short feature-bearing RNN windows and is diagnostic_only.
  - formal-timing is the only full timing acceptance mode and takes the exclusive throughput lock.
  - postprocess requires a closed immutable capture and writes only to a separate derived tree.
  - promote-package is controller file delivery plus read-back; it never starts bridge or motion.
  - contact-bridge delegates to the live-gated Step5d operator; it never rebuilds or uploads.
EOF
}

builder_program() {
  PYTHONPATH="${ROOT}/tools:${PYTHONPATH:-}" python3 - <<'PY'
import build_step5d_liveprep as builder
print(builder.PROGRAM_NAME)
PY
}

current_program() {
  python3 - "${ROOT}/config/current_stage.json" <<'PY'
import json
import sys
from pathlib import Path

try:
    current = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
program = current.get("program") or current.get("current_stage_id") or ""
if program:
    print(program)
PY
}

record_latest_candidate() {
  python3 - "$1" "${LATEST_CANDIDATE_INDEX}" <<'PY'
import json
import sys
from pathlib import Path

candidate_dir = Path(sys.argv[1]).resolve()
index_path = Path(sys.argv[2])
marker_path = candidate_dir / ".local_tp_candidate.json"
marker = json.loads(marker_path.read_text(encoding="utf-8"))
if marker.get("local_only") is not True or marker.get("not_delivered") is not True:
    raise SystemExit(f"refusing to index non-local candidate marker: {marker_path}")
payload = {
    "candidate_dir": str(candidate_dir),
    "marker": str(marker_path),
    "program": marker["program"],
    "target_dir": marker["target_dir"],
    "semantic_fingerprint": marker.get("semantic_fingerprint"),
    "stamp": marker.get("stamp"),
}
index_path.parent.mkdir(parents=True, exist_ok=True)
tmp = index_path.with_suffix(index_path.suffix + ".tmp")
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(index_path)
PY
}

latest_candidate_exports() {
  python3 - "${LATEST_CANDIDATE_INDEX}" "$(builder_program)" <<'PY'
import json
import shlex
import sys
from pathlib import Path

index_path = Path(sys.argv[1])
builder_program = sys.argv[2]
if not index_path.is_file():
    raise SystemExit(1)
index = json.loads(index_path.read_text(encoding="utf-8"))
candidate_dir = Path(index.get("candidate_dir", ""))
marker_path = Path(index.get("marker", ""))
if not candidate_dir.is_dir() or not marker_path.is_file():
    raise SystemExit(1)
marker = json.loads(marker_path.read_text(encoding="utf-8"))
if marker.get("local_only") is not True or marker.get("not_delivered") is not True:
    raise SystemExit(1)
if marker.get("program") != index.get("program") or marker.get("target_dir") != index.get("target_dir"):
    raise SystemExit(1)
if marker.get("program") != builder_program:
    raise SystemExit(1)
for ext in (".script", ".txt", ".urp"):
    if not (candidate_dir / f"{marker['program']}{ext}").is_file():
        raise SystemExit(1)
values = {
    "program": marker["program"],
    "local_dir": str(candidate_dir),
}
for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
}

cache_status() {
  python3 - "${RUN_ROOT}/.bridge_long_checks_cache.json" "${LONG_CHECK_TTL_S:-7200}" <<'PY'
import json
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
ttl = float(sys.argv[2])
if not path.is_file():
    print("long_check_cache=missing")
    raise SystemExit(0)
payload = json.loads(path.read_text(encoding="utf-8"))
age = time.time() - float(payload.get("checked_at_epoch", 0.0))
state = "fresh" if 0 <= age <= ttl else "stale"
print(f"long_check_cache={state} age_s={age:.1f} ttl_s={ttl:.1f} path={path}")
PY
}

run_quick_tests() {
  (
    cd "${ROOT}"
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_step5d_full_chain_sanity.py'
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_bridge_operator_startup_policy.py'
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_upload_ur_tp_package_reuse.py'
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_current_stage_readback_gate.py'
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_step5d_current_promotion.py'
    python3 "${PUBLISH_GATE}" --root "${ROOT}" --json
  )
}

mode="${1:-}"
case "${mode}" in
  status)
    program="$(current_program)"
    echo "current_program=${program:-<missing>}"
    if [[ -n "${program}" ]]; then
      python3 "${READBACK_GATE}" --root "${ROOT}" --program "${program}" --json
    fi
    cache_status
    ;;
  dev-loop)
    program="$(builder_program)"
    candidate_dir="${STEP5D_CANDIDATE_DIR:-${RUN_ROOT}/local_tp_packages/${program}_$(date +%Y%m%d_%H%M%S)}"
    python3 "${BUILD_TOOL}" --local-only --output-dir "${candidate_dir}"
    record_latest_candidate "${candidate_dir}"
    python3 "${UPLOAD_TOOL}" "${program}" \
      --local-dir "${candidate_dir}" \
      --dry-run \
      --readback-root "${DRYRUN_READBACK_ROOT}"
    run_quick_tests
    echo "local-only candidate verified: ${candidate_dir}"
    echo "not delivered; current_stage unchanged; do not open on Teach Pendant"
    ;;
  parallel-check|offline-functional|formal-timing|offline-all)
    python3 "${PARALLEL_TOOL}" "${mode}"
    ;;
  postprocess)
    run_dir="${2:-}"
    if [[ -z "${run_dir}" ]]; then
      echo "postprocess requires <immutable-run-dir>" >&2
      exit 2
    fi
    derived_root="${STEP5D_DERIVED_ROOT:-${RUN_ROOT}/derived}"
    output_root="${derived_root}/$(basename "$(readlink -f "${run_dir}")")_$(date +%Y%m%d_%H%M%S)"
    python3 "${PARALLEL_TOOL}" postprocess "${run_dir}" --output-root "${output_root}"
    ;;
  promote-package)
    current="$(current_program)"
    if [[ -n "${STEP5D_PACKAGE_DIR:-}" ]]; then
      program="${STEP5D_VERSION:-${current:-$(builder_program)}}"
      local_dir="${STEP5D_PACKAGE_DIR}"
    elif [[ -z "${STEP5D_VERSION:-}" && "${STEP5D_USE_LATEST_CANDIDATE:-1}" == "1" ]] && latest_env="$(latest_candidate_exports)"; then
      eval "${latest_env}"
      echo "promoting latest local-only candidate: ${program} from ${local_dir}"
    else
      program="${STEP5D_VERSION:-${current:-$(builder_program)}}"
      local_dir="${ROOT}/programs/step5"
    fi
    transaction_args=()
    if [[ "${STEP5D_PROMOTE_DRY_RUN:-0}" == "1" ]]; then
      transaction_args+=(--dry-run --readback-root "${DRYRUN_READBACK_ROOT}")
    fi
    python3 "${TP_COORDINATOR}" "${program}" --root "${ROOT}" --local-dir "${local_dir}" "${transaction_args[@]}"
    ;;
  prep-long-checks)
    "${OPERATOR}" prep-long-checks
    ;;
  contact-bridge)
    "${OPERATOR}" contact-bridge
    ;;
  *)
    usage
    exit 2
    ;;
esac
