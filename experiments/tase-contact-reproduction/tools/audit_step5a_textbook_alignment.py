#!/usr/bin/env python3
"""Offline audit for Step5a Local Control textbook alignment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[3]
EXPERIMENT = ROOT / "experiments" / "tase-contact-reproduction"
SPEC_PATH = EXPERIMENT / "config" / "step5a_local_control_spec.json"
STAGE_TABLE_PATH = EXPERIMENT / "config" / "step5_stage_table.json"
FLOW_PATH = EXPERIMENT / "STEP5_FLOW.md"
HISTORICAL_CONFIG_PATH = ROOT / "src" / "ur10e_example_controllers" / "config" / "historical_5a_fixed_z.yaml"
REMOTE_CONFIG_PATH = ROOT / "src" / "ur10e_example_controllers" / "config" / "no_contact_cycloid.yaml"
RETURN_SCRIPT_PATH = ROOT / "src" / "ur10e_example_controllers" / "ur10e_example_controllers" / "step5a_return_to_anchor_motion.py"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root is not an object: {path}")
    return payload


def rel_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return ROOT / path


def require(condition: bool, failures: list[str], message: str) -> None:
    if not condition:
        failures.append(message)


def audit(root: Path = ROOT) -> dict[str, Any]:
    failures: list[str] = []
    spec = load_json(root / SPEC_PATH.relative_to(ROOT))
    stage_table = load_json(root / STAGE_TABLE_PATH.relative_to(ROOT))
    historical_config = load_yaml(root / HISTORICAL_CONFIG_PATH.relative_to(ROOT))
    remote_config = load_yaml(root / REMOTE_CONFIG_PATH.relative_to(ROOT))
    flow_text = (root / FLOW_PATH.relative_to(ROOT)).read_text(encoding="utf-8")
    return_script = (root / RETURN_SCRIPT_PATH.relative_to(ROOT)).read_text(encoding="utf-8")

    active = spec["source_scripts"]["active_baseline"]
    active_script_path = root / Path(active["path"])
    active_script = active_script_path.read_text(encoding="utf-8")
    require(active_script_path.exists(), failures, f"active source script missing: {active_script_path}")
    require(active["id"] == "step5a_cycloid_no_contact_v3", failures, "active baseline must be TP v3")
    require(active["version_stamp"] in active_script, failures, "TP v3 script version stamp mismatch")
    require("local amplitude_m = 0.015000000" in active_script, failures, "TP v3 amplitude 0.015 missing")
    require("local path_duration_s = 22.000" in active_script, failures, "TP v3 duration 22 s missing")
    require("local velocity_cap_m_s = 0.009" in active_script, failures, "TP v3 command clamp 0.009 missing")
    require("PHYSICAL_PATH_GATE" in active_script, failures, "TP v3 physical path gate header missing")
    require("affine map exactly matches shifted drag-teach start/mid/end" in active_script, failures, "TP v3 affine map gate missing")

    for retired in spec["source_scripts"]["retired_provenance"]:
        retired_path = root / Path(retired["path"])
        require(retired_path.exists(), failures, f"retired source script missing: {retired_path}")
        require(retired["version_stamp"] in retired_path.read_text(encoding="utf-8"), failures, f"retired script stamp mismatch: {retired_path}")

    stage_ids = {stage["id"]: stage for stage in stage_table["stages"]}
    require("step5a_cycloid_no_contact_v3" in stage_ids, failures, "stage table missing TP v3")
    require(stage_ids["step5a_cycloid_no_contact_v3"]["active"] is True, failures, "TP v3 must remain active baseline in stage table")
    require(stage_ids["step5a_cycloid_no_contact_v3"]["amplitude_m"] == 0.015, failures, "stage table TP v3 amplitude mismatch")
    require(stage_ids["step5a_cycloid_no_contact_v3"]["guard"]["velocity_cap_m_s"] == 0.009, failures, "stage table TP v3 command clamp mismatch")

    spec_rel = "experiments/tase-contact-reproduction/config/step5a_local_control_spec.json"
    require(historical_config.get("local_control_spec_path") == spec_rel, failures, "historical fixed-Z config missing local_control_spec_path")
    require(remote_config.get("local_control_spec_path") == spec_rel, failures, "remote no-contact config missing local_control_spec_path")

    historical_alignment = historical_config.get("local_control_textbook_alignment", {})
    require(historical_alignment.get("frame_map_choice") == "changed_with_reason", failures, "historical config must mark frame map as changed_with_reason")
    require(
        historical_config.get("achieved_speed_policy") == "hard_gate_for_historical_15s_visual_gate",
        failures,
        "historical achieved-speed policy must be explicit",
    )
    require(historical_config.get("achieved_speed_hard_cap_m_s") == 0.015, failures, "historical achieved-speed hard cap must not be TP v3 0.009")

    remote_alignment = remote_config.get("local_control_textbook_alignment", {})
    require(remote_alignment.get("amplitude_m") == "changed_with_reason", failures, "remote no-contact amplitude deviation must be explicit")
    require(remote_alignment.get("frame_map_choice") == "changed_with_reason", failures, "remote no-contact frame map deviation must be explicit")

    require("step5a_local_control_spec.json" in flow_text, failures, "STEP5_FLOW must name the textbook spec JSON")
    require("TP v3 script" in flow_text, failures, "STEP5_FLOW must name TP v3 script as textbook source")
    require("safe-frame" in flow_text, failures, "STEP5_FLOW must name safe-frame evidence")

    first_candidate = 'SOURCE_SUMMARY_CANDIDATES = (\n    "step5a_historical_fixed_z_position.json",'
    require(first_candidate in return_script, failures, "return source priority must prefer pre-cycle positioning summary")

    return {
        "ok": not failures,
        "failures": failures,
        "spec_path": str(root / SPEC_PATH.relative_to(ROOT)),
        "active_baseline": active["id"],
        "checks": {
            "source_scripts": True,
            "stage_table": True,
            "historical_config": True,
            "remote_config": True,
            "flow_doc": True,
            "return_source_priority": True,
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
        print("PASS Step5a Local Control textbook alignment")
    else:
        print("FAIL Step5a Local Control textbook alignment")
        for failure in result["failures"]:
            print(f"- {failure}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
