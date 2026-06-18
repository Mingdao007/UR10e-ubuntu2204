#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
RUN_ROOT="${ROOT}/experiments/tase-contact-reproduction/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
COMBO_DIR="${RUN_ROOT}/step5b_live_contact_combo_${STAMP}"
READINESS_CMD="${ROOT}/step5b_zero_policy_check.sh"
LIVE_CMD="${ROOT}/step5b_contact_bridge.sh"

if [[ "${STEP5B_CONFIRM:-}" != "LIVE STEP5B CONTACT RUN" ]]; then
  echo "refusing Step5b combo run: set STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN'"
  exit 40
fi

if [[ ! -x "${READINESS_CMD}" ]]; then
  echo "refusing Step5b combo run: missing executable readiness command ${READINESS_CMD}"
  exit 41
fi
if [[ ! -x "${LIVE_CMD}" ]]; then
  echo "refusing Step5b combo run: missing executable live command ${LIVE_CMD}"
  exit 42
fi

mkdir -p "${COMBO_DIR}"

echo "combo_run_dir=${COMBO_DIR}"
echo "phase=step5b_zero_policy_readiness"
set +e
"${READINESS_CMD}" 2>&1 | tee "${COMBO_DIR}/step5b_zero_policy_check.log"
READINESS_RC="${PIPESTATUS[0]}"
set -e
if [[ "${READINESS_RC}" != "0" ]]; then
  echo "refusing Step5b live contact: zero-policy readiness failed rc=${READINESS_RC}"
  exit "${READINESS_RC}"
fi

READINESS_SUMMARY="$(awk -F= '/^summary=/{print $2}' "${COMBO_DIR}/step5b_zero_policy_check.log" | tail -n 1)"
if [[ -z "${READINESS_SUMMARY}" || ! -f "${READINESS_SUMMARY}" ]]; then
  echo "refusing Step5b live contact: readiness summary path was not found in ${COMBO_DIR}/step5b_zero_policy_check.log"
  exit 43
fi
READINESS_RUN_DIR="$(dirname "${READINESS_SUMMARY}")"

python3 - "$COMBO_DIR/combo_preflight.json" "$READINESS_SUMMARY" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
summary = json.loads(summary_path.read_text(encoding="utf-8"))
payload = {
    "ok": summary.get("ok") is True,
    "role": "step5b_live_contact_combo_preflight",
    "stage_id": "step5_contact_cycloid_baseline_v1",
    "selected_route": "tp_bridge_step5b_v1",
    "readiness_summary": str(summary_path),
    "force_source": "kunwei",
    "zero_policy": {
        "ur_zero_ftsensor_called": False,
        "kunwei_hardware_tare_or_config_written": False,
        "software_baseline_subtraction": True,
    },
    "next_command": "step5b_contact_bridge.sh",
}
out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
if not payload["ok"]:
    raise SystemExit(44)
PY

echo "phase=step5b_contact_bridge"
STEP5B_ZERO_POLICY_RUN_DIR="${READINESS_RUN_DIR}" "${LIVE_CMD}"
