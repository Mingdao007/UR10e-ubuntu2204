#!/usr/bin/env python3
"""Offline audit for Step5/Step6 Local Control textbook alignment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT = ROOT / "experiments" / "tase-contact-reproduction"
SPEC_PATH = EXPERIMENT / "config" / "local_control_textbook_spec.json"
STEP5_TABLE_PATH = EXPERIMENT / "config" / "step5_stage_table.json"
STEP6_TABLE_PATH = EXPERIMENT / "config" / "step6_stage_table.json"
STEP5_SAFE_FRAME_PATH = EXPERIMENT / "config" / "step5_safe_frame.json"
STEP6_SAFE_FRAME_PATH = EXPERIMENT / "config" / "step6_eight_safe_frame.json"
STEP5_FLOW_PATH = EXPERIMENT / "STEP5_FLOW.md"
STEP6_FLOW_PATH = EXPERIMENT / "STEP6_FLOW.md"

EXPECTED_STAGES = (
    "step5a_cycloid_no_contact_v3",
    "step5_contact_cycloid_baseline_v1",
    "step6a_eight_no_contact_v1",
    "step6_contact_eight_baseline_v2",
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition: bool, failures: list[str], message: str) -> None:
    if not condition:
        failures.append(message)


def rel(root: Path, path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return root / path


def stage_by_id(stage_table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {stage["id"]: stage for stage in stage_table["stages"]}


def read_required_source(root: Path, path_text: str, failures: list[str]) -> str:
    path = rel(root, path_text)
    if not path.exists():
        failures.append(f"textbook source missing: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def assert_no_contact_script_policy(
    text: str,
    failures: list[str],
    stage_id: str,
) -> None:
    code_text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    forbidden = (
        "read_input_float_register",
        "force_mode(",
        "zero_ftsensor(",
        "set_tcp(",
        "set_payload(",
    )
    for token in forbidden:
        require(token not in code_text, failures, f"{stage_id} no-contact script must not call {token}")


def audit_step5a(root: Path, spec: dict[str, Any], stage5: dict[str, Any], failures: list[str]) -> None:
    stage_spec = spec["stage_specs"]["step5a_cycloid_no_contact_v3"]
    script_info = stage_spec["source_scripts"]["active_baseline"]
    script = read_required_source(root, script_info["path"], failures)
    stage = stage5.get("step5a_cycloid_no_contact_v3", {})

    require(script_info["version_stamp"] in script, failures, "Step5a TP v3 version stamp missing")
    require("local amplitude_m = 0.015000000" in script, failures, "Step5a amplitude 0.015 missing in script")
    require("local path_duration_s = 22.000" in script, failures, "Step5a TP v3 22 s duration missing")
    require("local velocity_cap_m_s = 0.009" in script, failures, "Step5a TP v3 command clamp 0.009 missing")
    require("affine map exactly matches shifted drag-teach start/mid/end" in script, failures, "Step5a affine map correction missing")
    require(stage.get("active") is True, failures, "Step5a TP v3 must remain active in Step5 stage table")
    require(stage.get("amplitude_m") == stage_spec["task_space"]["amplitude_m"], failures, "Step5a stage table amplitude mismatch")
    require(stage.get("guard", {}).get("velocity_cap_m_s") == 0.009, failures, "Step5a stage table command clamp mismatch")
    assert_no_contact_script_policy(script, failures, "Step5a")

    sys.path.insert(0, str(EXPERIMENT / "tools"))
    try:
        import audit_step5a_textbook_alignment as step5a_audit  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - defensive audit reporting
        failures.append(f"Step5a detailed audit import failed: {exc}")
        return
    result = step5a_audit.audit(root)
    require(result["ok"], failures, f"Step5a detailed audit failed: {result['failures']}")


def audit_step5b(root: Path, spec: dict[str, Any], stage5: dict[str, Any], failures: list[str]) -> None:
    stage_spec = spec["stage_specs"]["step5_contact_cycloid_baseline_v1"]
    script_info = stage_spec["source_scripts"]["active_baseline"]
    script = read_required_source(root, script_info["path"], failures)
    package_text = read_required_source(root, script_info["package_text_path"], failures)
    stage = stage5.get("step5_contact_cycloid_baseline_v1", {})

    require(script_info["version_stamp"] in script, failures, "Step5b version stamp missing")
    require("STEP5_STAGE_ID: step5_contact_cycloid_baseline_v1" in script, failures, "Step5b stage id missing")
    require("TP_ROLE: executor_and_guard_only" in script, failures, "Step5b TP role missing")
    require("--target-force-n 5.0" in package_text, failures, "Step5b contact target missing")
    require("step4e-normal-follow-mode=filtered_live" in script, failures, "Step5b filtered-live normal correction missing")
    require("x=0.015 * (0.1t - sin(0.1t))" in script, failures, "Step5b cycloid formula missing")
    require("movel(entry_xy_pose, a=0.060, v=0.040, r=0.0)" in script, failures, "Step5b fast entry movel missing")
    require("movel(lift_pose, a=0.060, v=0.040, r=0.0)" in script, failures, "Step5b fast lift movel missing")
    require("local short_retract_speed_m_s = 0.040" in script, failures, "Step5b fast retract speed missing")
    require("zero_ftsensor(" not in script, failures, "Step5b must not call zero_ftsensor")
    require("set_tcp(" not in script and "set_payload(" not in script, failures, "Step5b must not write TCP/payload")

    require(stage.get("contact") is True, failures, "Step5b stage table contact flag mismatch")
    require(stage.get("bridge") is True, failures, "Step5b stage table bridge flag mismatch")
    require(stage.get("duration_s") == 60.0, failures, "Step5b duration mismatch")
    require(stage.get("amplitude_m") == 0.015, failures, "Step5b amplitude mismatch")
    require(stage.get("phase_law", {}).get("omega_rad_s") == 0.1, failures, "Step5b omega mismatch")
    require(stage.get("filter_policy", {}).get("alpha") == 0.35, failures, "Step5b normal filter alpha mismatch")
    require(stage.get("filter_policy", {}).get("min_force_n") == 2.0, failures, "Step5b normal filter min force mismatch")
    require(stage.get("contact_policy", {}).get("tp_role") == "executor_and_guard_only", failures, "Step5b TP role mismatch in stage table")


def audit_step6a(
    root: Path,
    spec: dict[str, Any],
    stage6: dict[str, Any],
    safe_frame: dict[str, Any],
    failures: list[str],
) -> None:
    stage_spec = spec["stage_specs"]["step6a_eight_no_contact_v1"]
    script_info = stage_spec["source_scripts"]["active_baseline"]
    script = read_required_source(root, script_info["path"], failures)
    stage = stage6.get("step6a_eight_no_contact_v1", {})
    expected_z = safe_frame["fixed_z"]["fixed_base_z_m"]

    require(script_info["version_stamp"] in script, failures, "Step6a version stamp missing")
    require("STEP6_STAGE_ID: step6a_eight_no_contact_v1" in script, failures, "Step6a stage id missing")
    require("NO_CONTACT_POLICY: fixed base Z, no force control, no contact search, no Kunwei/bridge requirement" in script, failures, "Step6a no-contact policy missing")
    require("along=0.04*sin(0.2t), lateral=0.01*sin(0.4t), duration=30s" in script, failures, "Step6a formula missing")
    require("local fixed_base_z_m = 0.030311079" in script, failures, "Step6a fixed-Z script value missing")
    assert_no_contact_script_policy(script, failures, "Step6a")

    require(stage.get("contact") is False, failures, "Step6a stage table contact flag mismatch")
    require(stage.get("bridge") is False, failures, "Step6a stage table bridge flag mismatch")
    require(stage.get("duration_s") == 30.0, failures, "Step6a duration mismatch")
    require(stage.get("path", {}).get("along_amplitude_m") == 0.04, failures, "Step6a along amplitude mismatch")
    require(stage.get("path", {}).get("lateral_amplitude_m") == 0.01, failures, "Step6a lateral amplitude mismatch")
    require(stage.get("path", {}).get("omega_rad_s") == 0.2, failures, "Step6a omega mismatch")
    require(stage.get("cadence", {}).get("path_clock") == "get_steptime", failures, "Step6a path clock mismatch")
    require(stage.get("guard", {}).get("velocity_cap_m_s") == 0.009, failures, "Step6a velocity cap mismatch")
    require(abs(stage.get("fixed_base_z_m", 0.0) - expected_z) < 1e-12, failures, "Step6a stage table fixed-Z must match safe-frame fixed-Z")
    require(abs(stage_spec["task_space"]["fixed_base_z_m"] - expected_z) < 1e-12, failures, "Step6a textbook fixed-Z must match safe-frame fixed-Z")


def audit_step6b(
    root: Path,
    spec: dict[str, Any],
    stage6: dict[str, Any],
    safe_frame: dict[str, Any],
    failures: list[str],
) -> None:
    stage_spec = spec["stage_specs"]["step6_contact_eight_baseline_v2"]
    script_info = stage_spec["source_scripts"]["active_baseline"]
    script = read_required_source(root, script_info["path"], failures)
    package_text = read_required_source(root, script_info["package_text_path"], failures)
    stage = stage6.get("step6_contact_eight_baseline_v2", {})
    retired = stage6.get("step6_contact_eight_baseline_v1", {})

    require(script_info["version_stamp"] in script, failures, "Step6b v2 version stamp missing")
    require("STEP6_STAGE_ID: step6_contact_eight_baseline_v2" in script, failures, "Step6b v2 stage id missing")
    require("CONTACT_TARGET: --target-force-n 5.0" in script or "--target-force-n 5.0" in package_text, failures, "Step6b contact target missing")
    require("BRIDGE_LIMITS: step4e-motion-limit-m-s=0.015, step4e-total-linear-limit-m-s=0.015, step4e-normal-velocity-limit-m-s=0.003, step4e-angular-limit-rad-s=0.060" in script, failures, "Step6b v2 bridge limits missing")
    require("OFFLINE_FEASIBILITY: ref_max=8.944mm/s, total_with_3.0mm/s_normal=9.434mm/s" in script, failures, "Step6b feasibility correction missing")
    require("step4e-normal-follow-mode=filtered_live" in script, failures, "Step6b filtered-live normal correction missing")
    require("zero_ftsensor(" not in script, failures, "Step6b must not call zero_ftsensor")
    require("set_tcp(" not in script and "set_payload(" not in script, failures, "Step6b must not write TCP/payload")

    require(stage.get("active") is True, failures, "Step6b v2 must be active in stage table")
    require(stage.get("contact") is True, failures, "Step6b v2 stage table contact flag mismatch")
    require(stage.get("bridge") is True, failures, "Step6b v2 stage table bridge flag mismatch")
    require(stage.get("duration_s") == 30.0, failures, "Step6b duration mismatch")
    require(stage.get("bridge_profile", {}).get("step4e_version") == "step6b_v2", failures, "Step6b bridge profile mismatch")
    require(stage.get("bridge_profile", {}).get("step4e_path_shape") == "eight", failures, "Step6b bridge path shape mismatch")
    require(stage.get("bridge_profile", {}).get("target_force_n") == 5.0, failures, "Step6b target force mismatch")
    require(stage.get("bridge_limits", {}).get("motion_limit_m_s") == 0.015, failures, "Step6b motion limit mismatch")
    require(stage.get("bridge_limits", {}).get("total_linear_limit_m_s") == 0.015, failures, "Step6b total linear limit mismatch")
    require(stage.get("bridge_limits", {}).get("normal_velocity_limit_m_s") == 0.003, failures, "Step6b normal velocity limit mismatch")
    require(stage.get("bridge_limits", {}).get("angular_limit_rad_s") == 0.06, failures, "Step6b angular limit mismatch")
    require(stage.get("offline_feasibility", {}).get("reference_speed_max_m_s") == 0.00894427191, failures, "Step6b reference speed feasibility mismatch")
    require(stage.get("offline_feasibility", {}).get("required_total_with_normal_reserve_m_s") == 0.00943398113, failures, "Step6b total speed feasibility mismatch")
    require(retired.get("active") is False and retired.get("retained_evidence") is True, failures, "Step6b v1 must remain retained evidence, not active")
    require(safe_frame.get("residual_gate_passed") is True, failures, "Step6 safe-frame residual gate must pass")
    require(safe_frame.get("guard", {}).get("passed") is True, failures, "Step6 safe-frame X guard must pass")


def audit_flow_docs(root: Path, failures: list[str]) -> None:
    step5_flow = (root / STEP5_FLOW_PATH.relative_to(ROOT)).read_text(encoding="utf-8")
    step6_flow = (root / STEP6_FLOW_PATH.relative_to(ROOT)).read_text(encoding="utf-8")
    for token in (
        "local_control_textbook_spec.json",
        "step5a_local_control_spec.json",
        "step5b_contact_cycloid_baseline_v1.script",
        "Local Control Textbook Alignment",
    ):
        require(token in step5_flow, failures, f"STEP5_FLOW missing {token}")
    for token in (
        "local_control_textbook_spec.json",
        "step6a_eight_no_contact_v1.script",
        "step6b_contact_eight_baseline_v2.script",
        "Local Control Textbook Alignment",
    ):
        require(token in step6_flow, failures, f"STEP6_FLOW missing {token}")


def audit(root: Path = ROOT) -> dict[str, Any]:
    failures: list[str] = []
    spec = load_json(root / SPEC_PATH.relative_to(ROOT))
    step5_table = load_json(root / STEP5_TABLE_PATH.relative_to(ROOT))
    step6_table = load_json(root / STEP6_TABLE_PATH.relative_to(ROOT))
    step5_safe_frame = load_json(root / STEP5_SAFE_FRAME_PATH.relative_to(ROOT))
    step6_safe_frame = load_json(root / STEP6_SAFE_FRAME_PATH.relative_to(ROOT))

    stages = spec.get("stage_specs", {})
    require(tuple(stages.keys()) == EXPECTED_STAGES, failures, "textbook spec must cover Step5a, Step5b, Step6a, and Step6b exactly")
    require(spec.get("global_boundaries", {}).get("live_robot_command_authorized") is False, failures, "global no-live boundary missing")
    require(spec.get("global_boundaries", {}).get("zero_ftsensor_authorized") is False, failures, "global zero_ftsensor boundary missing")

    for stage_id in EXPECTED_STAGES:
        stage_spec = stages.get(stage_id, {})
        for source in stage_spec.get("required_textbook_sources", []):
            if source.endswith("/"):
                require(Path(source).exists(), failures, f"textbook source directory missing: {source}")
            else:
                require(rel(root, source).exists(), failures, f"textbook source missing: {source}")

    stage5 = stage_by_id(step5_table)
    stage6 = stage_by_id(step6_table)

    require(step5_safe_frame.get("guard", {}).get("passed") is True, failures, "Step5 safe-frame X guard must pass")
    audit_step5a(root, spec, stage5, failures)
    audit_step5b(root, spec, stage5, failures)
    audit_step6a(root, spec, stage6, step6_safe_frame, failures)
    audit_step6b(root, spec, stage6, step6_safe_frame, failures)
    audit_flow_docs(root, failures)

    return {
        "ok": not failures,
        "failures": failures,
        "spec_path": str(root / SPEC_PATH.relative_to(ROOT)),
        "covered_stages": list(EXPECTED_STAGES),
        "checks": {
            "source_scripts": True,
            "stage_tables": True,
            "safe_frames": True,
            "flow_docs": True,
            "step5a_detailed_audit": True,
            "operator_boundaries": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print full JSON audit output.")
    args = parser.parse_args(argv)

    result = audit()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["ok"]:
        print("PASS Step5/Step6 Local Control textbook alignment")
    else:
        print("FAIL Step5/Step6 Local Control textbook alignment")
        for failure in result["failures"]:
            print(f"- {failure}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
