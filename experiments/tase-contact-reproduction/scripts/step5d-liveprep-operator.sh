#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction"
BASE_OPERATOR="${ROOT}/scripts/step4e-line-v1-operator.sh"
STEP5D_VERSION="${STEP5D_VERSION:-}"
if [[ -z "${STEP5D_VERSION}" ]]; then
  EXPECTED_PROGRAM="<no current Step5d live-prep package>"
elif [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v1" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v2" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v3" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v4" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v5" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v6" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v7" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v8" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v9" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v10" || "${STEP5D_VERSION}" == "step5d_strict_rnn_liveprep_v11" ]]; then
  EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/step5d/${STEP5D_VERSION}.urp"
else
  EXPECTED_PROGRAM="/programs/andyl/kunwei/step5/${STEP5D_VERSION}.urp"
fi

usage() {
  cat <<EOF
Usage:
  step5d-liveprep-operator.sh prep-long-checks  # manual diagnostics only
  STEP5D_CONFIRM='LIVE STEP5D STRICT RNN LIVEPREP' step5d-liveprep-operator.sh contact-bridge

Teach Pendant target:
  ${EXPECTED_PROGRAM}

Boundary:
  - Contact-capable live-prep package, not a completed reproduction claim.
  - Bridge profile: ${STEP5D_VERSION}.
  - Force target: 5.0 N, Step5/Step6 positive normal-load convention.
  - Stage 25.0: registers 37..42 are qd0..qd5 rad/s; TP executes speedj.
  - qdot cap: v12/v13/v14 default to 0.05 rad/s; retained evidence packages may differ.
  - Raw normal guard: 50 N, force norm guard: 60 N, torque guard: 3.0 Nm.
  - Stage 25.3 runs bridge deadband acquire with Cartesian registers 37..39.
  - Stage 25.3 keeps press recovery on low load and stops outside the 40 N normal-load / 25 N force-norm recovery envelope.
  - Stage 25.3 must hold filtered 2-15 N with force_norm <=25 N for 0.150 s before Stage 25.0 speedj starts.
  - No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.
  - This wrapper never loads a program or presses Play.
  - contact-bridge owns cached long checks; do not run prep-long-checks as a
    separate bridge-start step.
EOF
}

require_current_stage_readback_gate() {
  python3 - "${ROOT}" "${STEP5D_VERSION}" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
version = sys.argv[2]
current_path = root / "config" / "current_stage.json"
current = json.loads(current_path.read_text(encoding="utf-8"))
suffix = version.rsplit("_", 1)[-1]
expected_target_dir = "/programs/andyl/kunwei/step5"
expected_urp = f"{expected_target_dir}/{version}.urp"

def fail(message: str) -> None:
    raise SystemExit(f"refusing live Step5d bridge start: {message}")

if current.get("current_stage_id") != version or current.get("program") != version:
    fail(f"current_stage.json points to {current.get('current_stage_id')}/{current.get('program')}, not {version}")
if current.get("controller_target") != expected_urp:
    fail(f"controller_target is {current.get('controller_target')}, expected {expected_urp}")
if "controller_readback_verified" not in str(current.get("status", "")):
    fail(f"current status is not read-back verified: {current.get('status')}")

evidence = current.get("evidence", {})
if evidence.get(f"{suffix}_controller_readback_verified") is not True:
    fail(f"{suffix}_controller_readback_verified is not true")
manifest_rel = evidence.get(f"{suffix}_controller_readback_manifest")
if not manifest_rel:
    fail(f"{suffix}_controller_readback_manifest is missing")
manifest_path = root / str(manifest_rel)
if not manifest_path.exists():
    fail(f"read-back manifest does not exist: {manifest_rel}")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("status") != "controller read-back verified":
    fail(f"manifest status is {manifest.get('status')}")
validation = manifest.get("validation", {})
if validation.get("program") != version:
    fail(f"manifest program is {validation.get('program')}, expected {version}")
if validation.get("target_dir") != expected_target_dir:
    fail(f"manifest target_dir is {validation.get('target_dir')}, expected {expected_target_dir}")
if validation.get("script_node_path") != f"{expected_target_dir}/{version}.script":
    fail(f"manifest script_node_path is {validation.get('script_node_path')}")

expected_sha = evidence.get("sha256", {})
manifest_sha = manifest.get("sha256", {})
for label, key in ((".script", "script_sha256"), (".txt", "txt_sha256"), (".urp", "urp_sha256")):
    expected = expected_sha.get(label)
    if not expected:
        fail(f"current_stage sha256 {label} is missing")
    if validation.get(key) != expected:
        fail(f"manifest validation {key} does not match current_stage")
    for section in ("local", "controller", "readback"):
        if manifest_sha.get(section, {}).get(label) != expected:
            fail(f"manifest sha256 {section} {label} does not match current_stage")

print(f"[operator] read-back gate passed for {version}: {manifest_rel}")
PY
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  prep-long-checks)
    if [[ -z "${STEP5D_VERSION}" ]]; then
      echo "refusing Step5d prep: no current live-prep package after v14 predicted TCP speed watchdog stop; set STEP5D_VERSION explicitly only for retained evidence diagnostics"
      exit 40
    fi
    STEP4E_VERSION="${STEP5D_VERSION}" "${BASE_OPERATOR}" prep-long-checks
    ;;
  contact-bridge)
    if [[ -z "${STEP5D_VERSION}" ]]; then
      echo "refusing live Step5d bridge start: no current live-prep package after v14 predicted TCP speed watchdog stop; analyze the v14 run and make a new current package or explicitly choose same-version retry first"
      exit 40
    fi
    require_current_stage_readback_gate
    if [[ "${STEP5D_CONFIRM:-}" != "LIVE STEP5D STRICT RNN LIVEPREP" ]]; then
      echo "refusing live Step5d bridge start: set STEP5D_CONFIRM='LIVE STEP5D STRICT RNN LIVEPREP'"
      exit 40
    fi
    STEP4E_VERSION="${STEP5D_VERSION}" \
    BRIDGE_DURATION_S="${BRIDGE_DURATION_S:-180}" \
    MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-50}" \
    MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-60}" \
    MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-3.0}" \
    STEP4E_NORMAL_FOLLOW_MODE="${STEP4E_NORMAL_FOLLOW_MODE:-filtered_live}" \
    STEP4E_NORMAL_FILTER_ALPHA="${STEP4E_NORMAL_FILTER_ALPHA:-0.35}" \
    STEP4E_NORMAL_MIN_FORCE_N="${STEP4E_NORMAL_MIN_FORCE_N:-2.0}" \
      "${BASE_OPERATOR}" line-bridge
    ;;
  *)
    usage
    exit 2
    ;;
esac
