#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUN_ROOT="${ROOT}/runs"
BUILD_TOOL="${ROOT}/tools/build_step5d_liveprep.py"
UPLOAD_TOOL="${ROOT}/tools/upload_ur_tp_package.py"
READBACK_GATE="${ROOT}/tools/verify_current_stage_readback.py"
OPERATOR="${SCRIPT_DIR}/step5d-liveprep-operator.sh"
TARGET_DIR="${STEP5D_TARGET_DIR:-/programs/andyl/kunwei/step5}"
DRYRUN_READBACK_ROOT="${STEP5D_DRYRUN_READBACK_ROOT:-/tmp/ur10e_tp_readback_dryrun}"

usage() {
  cat <<EOF
Usage:
  step5d-workflow.sh status
  step5d-workflow.sh dev-loop
  step5d-workflow.sh promote-package
  step5d-workflow.sh prep-long-checks
  STEP5D_CONFIRM='LIVE STEP5D STRICT RNN LIVEPREP' step5d-workflow.sh contact-bridge

Boundary:
  - dev-loop is local-only; it never uploads, updates current_stage, or starts bridge.
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

current_target_dir() {
  python3 - "${ROOT}/config/current_stage.json" "${TARGET_DIR}" <<'PY'
import json
import sys
from pathlib import Path, PurePosixPath

fallback = sys.argv[2]
try:
    current = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    print(fallback)
    raise SystemExit(0)
target = current.get("controller_target")
print(str(PurePosixPath(str(target)).parent) if target else fallback)
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
    python3 "${UPLOAD_TOOL}" "${program}" \
      --target-dir "${TARGET_DIR}" \
      --local-dir "${candidate_dir}" \
      --dry-run \
      --readback-root "${DRYRUN_READBACK_ROOT}"
    run_quick_tests
    echo "local-only candidate verified: ${candidate_dir}"
    echo "not delivered; current_stage unchanged; do not open on Teach Pendant"
    ;;
  promote-package)
    current="$(current_program)"
    program="${STEP5D_VERSION:-${current:-$(builder_program)}}"
    local_dir="${STEP5D_PACKAGE_DIR:-${ROOT}/programs/step5}"
    target_dir="$(current_target_dir)"
    extra_args=()
    if [[ "${STEP5D_PROMOTE_DRY_RUN:-0}" == "1" ]]; then
      extra_args+=(--dry-run --readback-root "${DRYRUN_READBACK_ROOT}")
    fi
    python3 "${UPLOAD_TOOL}" "${program}" \
      --target-dir "${target_dir}" \
      --local-dir "${local_dir}" \
      --allow-local-candidate-promote \
      "${extra_args[@]}"
    if [[ "${STEP5D_PROMOTE_DRY_RUN:-0}" != "1" && -n "${current}" && "${program}" == "${current}" ]]; then
      python3 "${READBACK_GATE}" --root "${ROOT}" --program "${program}" --json
    elif [[ "${STEP5D_PROMOTE_DRY_RUN:-0}" != "1" ]]; then
      echo "promoted ${program}; current_stage still points to ${current:-<missing>}"
      echo "update current_stage before using contact-bridge for this program"
    fi
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
