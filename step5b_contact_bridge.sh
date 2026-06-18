#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
EXP_DIR="${ROOT}/experiments/tase-contact-reproduction"
RUN_ROOT="${EXP_DIR}/runs"
STAMP="$(date +%Y%m%d_%H%M%S)"
GATE_DIR="${RUN_ROOT}/step5b_live_contact_gate_${STAMP}"
OPERATOR="${EXP_DIR}/scripts/step5b-contact-operator.sh"
READINESS_MAX_AGE_S="${STEP5B_ZERO_POLICY_MAX_AGE_S:-300}"
READINESS_RUN_DIR="${STEP5B_ZERO_POLICY_RUN_DIR:-}"

if [[ "${STEP5B_CONFIRM:-}" != "LIVE STEP5B CONTACT RUN" ]]; then
  echo "refusing live Step5b contact: set STEP5B_CONFIRM='LIVE STEP5B CONTACT RUN'"
  exit 40
fi

if [[ ! -x "${OPERATOR}" ]]; then
  echo "refusing live Step5b contact: missing executable operator ${OPERATOR}"
  exit 41
fi

mkdir -p "${GATE_DIR}"

validate_readiness() {
  python3 - "$RUN_ROOT" "$READINESS_MAX_AGE_S" "$GATE_DIR/preflight_summary.json" "$READINESS_RUN_DIR" <<'PY'
import json
import sys
import time
from pathlib import Path

run_root = Path(sys.argv[1])
max_age_s = float(sys.argv[2])
preflight_path = Path(sys.argv[3])
provided_run_dir = sys.argv[4]

if provided_run_dir:
    candidates = [Path(provided_run_dir) / "summary.json"]
else:
    candidates = sorted(
        run_root.glob("step5b_zero_policy_readiness_*/summary.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
        reverse=True,
    )

failures = []
selected = None
payload = None
now = time.time()
for summary_path in candidates:
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception as exc:
        failures.append(f"{summary_path}: read_error:{exc}")
        continue

    age_s = now - summary_path.stat().st_mtime
    checks = {
        "fresh": 0.0 <= age_s <= max_age_s,
        "ok": data.get("ok") is True,
        "role": data.get("role") == "step5b_zero_policy_readiness_gate",
        "stage": data.get("stage_id") == "step5_contact_cycloid_baseline_v1",
        "force_source": data.get("force_source") == "kunwei_software_baselined_stream",
        "motion_not_authorized": data.get("motion_authorized") is False,
        "contact_not_authorized": data.get("contact_search_authorized") is False,
        "bridge_not_authorized": data.get("bridge_start_authorized") is False,
        "tp_not_authorized": data.get("tp_play_authorized") is False,
    }
    zero = data.get("zero_policy", {})
    checks.update(
        {
            "no_ur_zero": zero.get("ur_zero_ftsensor_called") is False,
            "no_kunwei_hardware_zero": zero.get("kunwei_hardware_tare_or_config_written") is False,
            "software_baseline": zero.get("software_baseline_subtraction") is True,
        }
    )
    if all(checks.values()):
        selected = summary_path.parent
        payload = data
        break
    failed = ",".join(name for name, ok in checks.items() if not ok)
    failures.append(f"{summary_path}: failed_checks:{failed}: age_s={age_s:.1f}")

if selected is None or payload is None:
    preflight = {
        "ok": False,
        "role": "step5b_live_contact_preflight",
        "selected_route": "tp_bridge_step5b_v1",
        "failure_reason": "no_fresh_passing_step5b_zero_policy_readiness_summary",
        "checked_candidates": failures[:20],
        "readiness_max_age_s": max_age_s,
        "contact_motion_authorized": False,
        "bridge_start_authorized": False,
    }
    preflight_path.write_text(json.dumps(preflight, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(preflight, indent=2, sort_keys=True), file=sys.stderr)
    raise SystemExit(42)

preflight = {
    "ok": True,
    "role": "step5b_live_contact_preflight",
    "stage_id": "step5_contact_cycloid_baseline_v1",
    "selected_route": "tp_bridge_step5b_v1",
    "force_source": "kunwei",
    "zero_policy": {
        "ur_zero_ftsensor_called": False,
        "kunwei_hardware_tare_or_config_written": False,
        "software_baseline_subtraction": True,
        "source_summary": str(selected / "summary.json"),
    },
    "target_force_n": 5.0,
    "contact_latch": {
        "force_norm_gt_n": 1.5,
        "normal_force_lte_n": -1.0,
    },
    "hard_guards": {
        "raw_normal_guard_n": 50.0,
        "force_norm_guard_n": 60.0,
        "torque_guard_nm": 3.0,
    },
    "readiness_run_dir": str(selected),
    "readiness_summary_path": str(selected / "summary.json"),
    "readiness_summary_mtime_age_s": now - (selected / "summary.json").stat().st_mtime,
    "readiness_max_age_s": max_age_s,
    "contact_motion_authorized": True,
    "bridge_start_authorized": True,
    "tp_play_or_upload_authorized_by_script": False,
    "operator_must_manually_press_tp_play": True,
    "expected_program": "/programs/andyl/kunwei/step5/step5b_contact_cycloid_baseline_v1.urp",
}
preflight_path.write_text(json.dumps(preflight, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(str(selected))
PY
}

READINESS_RUN_DIR="$(validate_readiness)"
echo "step5b_live_contact_preflight=${GATE_DIR}/preflight_summary.json"
echo "step5b_zero_policy_run_dir=${READINESS_RUN_DIR}"
echo "selected_route=tp_bridge_step5b_v1"
echo "expected_program=/programs/andyl/kunwei/step5/step5b_contact_cycloid_baseline_v1.urp"
echo "boundary=no URScript upload, no TP Play automation, no UR zero, no Kunwei hardware tare/config"

START_EPOCH="$(python3 - <<'PY'
import time
print(time.time())
PY
)"

set +e
STEP5B_ZERO_POLICY_RUN_DIR="${READINESS_RUN_DIR}" "${OPERATOR}" contact-bridge 2>&1 | tee "${GATE_DIR}/step5b_contact_bridge.log"
OPERATOR_RC="${PIPESTATUS[0]}"
set -e

python3 - "$RUN_ROOT" "$GATE_DIR" "$READINESS_RUN_DIR" "$START_EPOCH" "$OPERATOR_RC" <<'PY'
import json
import sys
from pathlib import Path

run_root = Path(sys.argv[1])
gate_dir = Path(sys.argv[2])
readiness_dir = Path(sys.argv[3])
start_epoch = float(sys.argv[4])
operator_rc = int(sys.argv[5])

bridge_dirs = sorted(
    [
        path
        for path in run_root.glob("bridge_step5b_contact_cycloid_baseline_v1_*")
        if path.is_dir() and path.stat().st_mtime >= start_epoch - 1.0
    ],
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
bridge_dir = bridge_dirs[0] if bridge_dirs else None
bridge_csv = bridge_dir / "bridge_rtde_500hz.csv" if bridge_dir else None
metadata = bridge_dir / "metadata.json" if bridge_dir else None
stage_frequency = bridge_dir / "stage_frequency_summary.json" if bridge_dir else None
summary = {
    "ok": operator_rc == 0,
    "role": "step5b_live_contact_run_summary",
    "stage_id": "step5_contact_cycloid_baseline_v1",
    "selected_route": "tp_bridge_step5b_v1",
    "force_source": "kunwei",
    "zero_policy": {
        "ur_zero_ftsensor_called": False,
        "kunwei_hardware_tare_or_config_written": False,
        "software_baseline_subtraction": True,
        "readiness_summary": str(readiness_dir / "summary.json"),
    },
    "target_force_n": 5.0,
    "contact_latch": {
        "force_norm_gt_n": 1.5,
        "normal_force_lte_n": -1.0,
    },
    "hard_guards": {
        "raw_normal_guard_n": 50.0,
        "force_norm_guard_n": 60.0,
        "torque_guard_nm": 3.0,
    },
    "kunwei_monitor_readiness_artifact_path": str(readiness_dir / "summary.json"),
    "contact_trace_path": str(bridge_csv) if bridge_csv and bridge_csv.exists() else None,
    "bridge_metadata_path": str(metadata) if metadata and metadata.exists() else None,
    "stage_frequency_summary_path": str(stage_frequency) if stage_frequency and stage_frequency.exists() else None,
    "contact_motion_authorized": True,
    "bridge_start_authorized": True,
    "sent_goal": False,
    "bridge_started": bool(bridge_dir and (metadata.exists() or bridge_csv.exists())),
    "route_specific_command_authority": {
        "rtde_input_writer_route": True,
        "tp_bridge_operator_entered": True,
        "tp_play_or_upload_by_script": False,
    },
    "stop_reason": "operator_returned_zero" if operator_rc == 0 else f"operator_exit_{operator_rc}",
    "ended_by": "operator_returned" if operator_rc == 0 else "failure_or_operator_abort",
    "final_safety_state": "see_dashboard_or_bridge_metadata",
    "bridge_run_dir": str(bridge_dir) if bridge_dir else None,
    "operator_return_code": operator_rc,
    "operator_log": str(gate_dir / "step5b_contact_bridge.log"),
}
(gate_dir / "step5b_live_contact_run_summary.json").write_text(
    json.dumps(summary, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(f"step5b_live_contact_run_summary={gate_dir / 'step5b_live_contact_run_summary.json'}")
PY

exit "${OPERATOR_RC}"
