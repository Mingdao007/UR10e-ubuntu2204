#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LIVEPREP_OPERATOR="${SCRIPT_DIR}/step5d-liveprep-operator.sh"
BRIDGE_OPERATOR="${SCRIPT_DIR}/bridge-line-operator.sh"
P0_VERIFIER="${ROOT}/tools/verify_step5d_no_contact_p0.py"
STAGE_ENV_EXPORTER="${ROOT}/tools/export_stage_env.py"
P0_PROFILE="step5d_strict_rnn_no_contact_p0_v7"
P0_CONFIRM_TOKEN="LIVE STEP5D STRICT RNN NO CONTACT P0"
LATEST_RUN_POINTER="${ROOT}/runs/latest_run_pointer.json"

usage() {
  cat <<EOF
Usage:
  step5d-strict-rnn-p0.sh status
  step5d-strict-rnn-p0.sh live-ready
  step5d-strict-rnn-p0.sh capture-ready
  STEP5D_P0_CONFIRM='LIVE STEP5D STRICT RNN NO CONTACT P0' step5d-strict-rnn-p0.sh capture-bridge
  step5d-strict-rnn-p0.sh validate-run RUN_DIR_OR_CSV [verifier args...]

Boundary:
  - status/live-ready only report current Step5d runtime readiness.
  - capture-ready reports the dedicated no-contact P0 capture profile readiness.
  - capture-bridge starts the dedicated no-contact P0 bridge before Teach
    Pendant Play after STEP5D_P0_CONFIRM is set exactly. Open the exact v7
    program on the Teach Pendant and keep it STOPPED; press Play only after
    the operator prints: [operator] P0 bridge armed: press TP Play now.
    it does not mark P0 passed.
  - validate-run only checks an already captured bridge_rtde_500hz.csv artifact.
  - This wrapper never loads a program, presses Play, zeroes/tares force sensing,
    writes TCP/payload, or changes the full live authorization gate.
EOF
}

p0_table_preflight() {
  python3 - "${ROOT}/config/current_stage.json" "${ROOT}/config/step5_stage_table.json" "${P0_PROFILE}" <<'PY' || exit 24
import json
import sys
from pathlib import Path
from pathlib import PurePosixPath

current_path, table_path, profile = sys.argv[1:4]
current = json.loads(open(current_path, encoding="utf-8").read())
table = json.loads(open(table_path, encoding="utf-8").read())
capture = current.get("bridge_trigger", {}).get("no_contact_p0_capture", {})
if capture.get("profile") != profile:
    raise SystemExit(
        "refusing no-contact P0: current capture profile is "
        f"{capture.get('profile')}, expected {profile}\\n"
        "next: stop/reopen exact v7, rerun capture-bridge, then press TP Play after '[operator] P0 bridge armed: press TP Play now'"
    )
row = next((row for row in table.get("stages", []) if row.get("id") == profile), None)
if row is None:
    raise SystemExit(
        f"refusing no-contact P0: missing stage table row {profile}\\n"
        "next: stop/reopen exact v7, rerun capture-bridge, then press TP Play after '[operator] P0 bridge armed: press TP Play now'"
    )
delivery = row.get("package_delivery", {})
guard = row.get("guard", {})
target = capture.get("controller_target")
controller_dir = str(PurePosixPath(str(target)).parent) if target else None
manifest_rel = capture.get("controller_readback_manifest")
if not manifest_rel:
    raise SystemExit(
        "refusing no-contact P0: capture missing controller_readback_manifest\\n"
        "next: stop/reopen exact v7, rerun capture-bridge, then press TP Play after '[operator] P0 bridge armed: press TP Play now'"
    )
manifest_path = Path(current_path).resolve().parent.parent / manifest_rel
if not manifest_path.is_file():
    raise SystemExit(
        f"refusing no-contact P0: manifest missing: {manifest_rel}\\n"
        "next: stop/reopen exact v7, rerun capture-bridge, then press TP Play after '[operator] P0 bridge armed: press TP Play now'"
    )
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
validation = manifest.get("validation", {})
checks = {
    "controller_target": delivery.get("controller_target") == target,
    "controller_dir": delivery.get("controller_dir") == controller_dir,
    "readback_manifest": delivery.get("controller_readback_manifest") == capture.get("controller_readback_manifest"),
}
for ext in (".script", ".txt", ".urp"):
    checks[f"sha256 {ext}"] = delivery.get("sha256", {}).get(ext) == capture.get("sha256", {}).get(ext)
for section in ("local", "controller", "readback"):
    for ext in (".script", ".txt", ".urp"):
        checks[f"manifest {section} sha256 {ext}"] = (
            manifest.get("sha256", {}).get(section, {}).get(ext) == capture.get("sha256", {}).get(ext)
        )
checks["validation program"] = validation.get("program") == profile
checks["validation target_dir"] = validation.get("target_dir") == controller_dir
checks["validation script_node_path"] = validation.get("script_node_path") == f"{controller_dir}/{profile}.script"
failed = [key for key, ok in checks.items() if not ok]
if failed:
    raise SystemExit(
        f"refusing no-contact P0: table/current mismatch: {failed}\\n"
        "next: stop/reopen exact v7, rerun capture-bridge, then press TP Play after '[operator] P0 bridge armed: press TP Play now'"
    )
print(f"[operator] P0 table preflight: program={profile}")
print(f"[operator] P0 table preflight: controller_dir={controller_dir} controller_target={target}")
print(
    "[operator] P0 table preflight: "
    f"duration_s={row.get('duration_s')} "
    f"stage25_success_target_s={guard.get('stage25_success_target_s')} "
    f"stage25_runtime_limit_s={guard.get('stage25_runtime_limit_s')}"
)
sha = capture.get("sha256", {})
print(
    "[operator] P0 table preflight: "
    f"sha256 .script={sha.get('.script')} .txt={sha.get('.txt')} .urp={sha.get('.urp')}"
)
PY
}

load_p0_stage_env() {
  local stage_env
  if ! stage_env="$(python3 "${STAGE_ENV_EXPORTER}" "${P0_PROFILE}")"; then
    echo "refusing no-contact P0: failed to export stage env for ${P0_PROFILE}" >&2
    return 24
  fi
  set -a
  eval "${stage_env}"
  set +a
}

latest_pointer_run_dir() {
  local start_epoch="$1"
  python3 - "${LATEST_RUN_POINTER}" "${P0_PROFILE}" "${start_epoch}" <<'PY'
import json
import sys
from pathlib import Path

pointer_path = Path(sys.argv[1])
profile = sys.argv[2]
start_epoch = float(sys.argv[3])
if not pointer_path.is_file():
    raise SystemExit(1)
pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
if pointer.get("profile") != profile:
    raise SystemExit(1)
if float(pointer.get("started_at_epoch_s", 0.0)) < start_epoch:
    raise SystemExit(1)
run_dir = Path(str(pointer.get("run_dir", "")))
manifest_path = Path(str(pointer.get("manifest", run_dir / "bridge_run_manifest.json")))
if not run_dir.is_dir() or not manifest_path.is_file():
    raise SystemExit(1)
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("profile") != profile:
    raise SystemExit(1)
if Path(str(manifest.get("run_dir", ""))) != run_dir:
    raise SystemExit(1)
print(run_dir)
PY
}

stdout_run_dir_fallback() {
  local log_path="$1"
  python3 - "${log_path}" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
matches = re.findall(r"^\[operator\] bridge output:\s*(\S+)\s*$", text, flags=re.M)
if matches:
    print(matches[-1])
PY
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  status|live-ready)
    echo "Step5d strict RNN P0 status only: no live run is started and no-contact P0 is not marked passed by this command." >&2
    STEP5D_STAGE25_CONTROL_MODE=speedj_rnn_live "${LIVEPREP_OPERATOR}" live-ready
    ;;
  capture-ready)
    echo "Step5d strict RNN no-contact P0 capture readiness: no bridge is started by this command." >&2
    p0_table_preflight
    load_p0_stage_env
    "${BRIDGE_OPERATOR}" live-ready
    echo "Teach Pendant target: /programs/andyl/kunwei/step5/${P0_PROFILE}.urp"
    echo "Open that exact v7 program on the Teach Pendant and keep it STOPPED."
    echo "Then run capture-bridge and press TP Play only after: [operator] P0 bridge armed: press TP Play now"
    ;;
  capture-bridge)
    if [[ "${STEP5D_P0_CONFIRM:-}" != "${P0_CONFIRM_TOKEN}" ]]; then
      echo "refusing no-contact P0 bridge start: set STEP5D_P0_CONFIRM='${P0_CONFIRM_TOKEN}'"
      exit 40
    fi
    p0_table_preflight
    tmp_log="$(mktemp)"
    bridge_start_epoch="$(python3 - <<'PY'
import time
print(f"{time.time():.6f}")
PY
)"
    cleanup() {
      rm -f "${tmp_log}"
    }
    trap cleanup EXIT
    load_p0_stage_env
    export BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE=1
    export BRIDGE_REQUIRE_PREPLAY_STOPPED=1
    export BRIDGE_STAGE25_ONLY=1
    "${BRIDGE_OPERATOR}" prep-long-checks
    "${BRIDGE_OPERATOR}" line-bridge-fast | tee "${tmp_log}"
    run_dir="$(latest_pointer_run_dir "${bridge_start_epoch}" || true)"
    if [[ -z "${run_dir}" ]]; then
      run_dir="$(stdout_run_dir_fallback "${tmp_log}")"
    fi
    if [[ -z "${run_dir}" ]]; then
      echo "refusing to validate no-contact P0: bridge output run dir was not found"
      echo "next: keep capture-bridge alive, check for runtime output path in logs, and rerun capture-bridge if needed"
      exit 24
    fi
    python3 "${P0_VERIFIER}" "${run_dir}" --output "${run_dir}/step5d_no_contact_p0_summary.json"
    echo "[operator] Step5d no-contact P0 summary: ${run_dir}/step5d_no_contact_p0_summary.json"
    ;;
  validate-run)
    if [[ $# -lt 2 ]]; then
      echo "usage error: validate-run requires RUN_DIR_OR_CSV"
      exit 2
    fi
    run_path="$2"
    shift 2
    python3 "${P0_VERIFIER}" "${run_path}" "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
