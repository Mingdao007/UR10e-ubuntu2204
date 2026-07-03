#!/usr/bin/env python3
"""Validate normalized cross-step stage-table maintenance fields."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dotted_get(payload: dict[str, Any], dotted: str) -> Any:
    value: Any = payload
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(dotted)
        value = value[part]
    return value


def stage_by_id(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("id")): row for row in table.get("stages", [])}


def ref_exists(root: Path, table: dict[str, Any], ref: str) -> bool:
    if not ref:
        return False
    if "#" in ref:
        path_text, anchor = ref.split("#", 1)
        path = root / path_text
        if not path.is_file():
            return False
        if path.suffix == ".json":
            try:
                payload = load_json(path)
            except Exception:
                return False
            if anchor in payload:
                return True
            if "." in anchor:
                try:
                    dotted_get(payload, anchor)
                    return True
                except KeyError:
                    return False
            return True
        return True
    if ref in table:
        return True
    if "." in ref:
        try:
            dotted_get(table, ref)
            return True
        except KeyError:
            pass
    return (root / ref).is_file()


def validate(root: Path = EXPERIMENT_ROOT) -> list[str]:
    failures: list[str] = []
    step5 = load_json(root / "config" / "step5_stage_table.json")
    step6 = load_json(root / "config" / "step6_stage_table.json")
    current = load_json(root / "config" / "current_stage.json")
    step5_rows = stage_by_id(step5)
    step6_rows = stage_by_id(step6)
    current_stage_id = current.get("current_stage_id")
    current_program = current.get("program")
    current_target = current.get("controller_target")

    profile_ref = step5.get("bridge_startup_policy", {}).get("startup_gate_profile_ref")
    if profile_ref != "startup_gate_profiles.prepared_fast_bridge_v1":
        failures.append("bridge_startup_policy.startup_gate_profile_ref must use prepared_fast_bridge_v1")
    try:
        profile = dotted_get(step5, str(profile_ref))
    except KeyError:
        failures.append(f"startup gate profile is missing: {profile_ref}")
        profile = {}
    for gate in profile.get("gates", []):
        if gate.get("liveness_required") is True and gate.get("cacheable") is True:
            failures.append(f"live gate must not be cacheable: {gate.get('id')}")

    required_stage_ids = {
        "step5_contact_cycloid_baseline_v1",
        "step5d_strict_rnn_ablation_v25",
        "step5d_strict_rnn_ablation_v26",
        "step6_contact_eight_baseline_v2",
    }
    applies_to = set(step5.get("bridge_startup_policy", {}).get("applies_to_stage_ids", []))
    missing_applies = sorted(required_stage_ids - applies_to)
    if missing_applies:
        failures.append("bridge_startup_policy.applies_to_stage_ids missing: " + ", ".join(missing_applies))

    for table_name, table, rows in (("step5", step5, step5_rows), ("step6", step6, step6_rows)):
        for row_id, row in rows.items():
            refs = row.get("policy_refs")
            if not refs:
                continue
            for key, ref in refs.items():
                if not ref_exists(root, table, str(ref)):
                    failures.append(f"{table_name}:{row_id} policy_refs.{key} does not resolve: {ref}")
            if row.get("bridge") is True and refs.get("bridge_startup_policy") is None:
                failures.append(f"{table_name}:{row_id} bridge row missing bridge_startup_policy ref")

    current_row = step5_rows.get(str(current_stage_id))
    if current_row is None:
        failures.append(f"current stage row is missing from Step5 table: {current_stage_id}")
    else:
        binding = current_row.get("current_binding", {})
        delivery = current_row.get("package_delivery", {})
        if binding.get("is_current") is not True:
            failures.append(f"current row {current_stage_id} must have current_binding.is_current=true")
        if binding.get("stage_id") != current_stage_id:
            failures.append(f"current row binding stage_id mismatch: {binding.get('stage_id')} != {current_stage_id}")
        if binding.get("program") != current_program:
            failures.append(f"current row binding program mismatch: {binding.get('program')} != {current_program}")
        if binding.get("controller_target") != current_target:
            failures.append("current row binding controller_target does not match current_stage.json")
        if delivery.get("program_basename") != current_program:
            failures.append("current row package_delivery.program_basename does not match current_stage.json")
        if delivery.get("controller_target") != current_target:
            failures.append("current row package_delivery.controller_target does not match current_stage.json")
        if delivery.get("controller_readback_manifest") != current.get("controller_readback_manifest"):
            failures.append("current row controller readback manifest does not match current_stage.json")
        current_sha = current.get("sha256") or {}
        delivery_sha = delivery.get("sha256") or {}
        for ext in (".script", ".txt", ".urp"):
            if delivery_sha.get(ext) != current_sha.get(ext):
                failures.append(f"current row package sha mismatch for {ext}")

    for table_name, rows in (("step5", step5_rows), ("step6", step6_rows)):
        for row_id, row in rows.items():
            binding = row.get("current_binding")
            if binding and binding.get("is_current") is True and row_id != current_stage_id:
                failures.append(f"{table_name}:{row_id} incorrectly claims global current binding")

    step6_v2 = step6_rows.get("step6_contact_eight_baseline_v2")
    if not step6_v2:
        failures.append("Step6b v2 row is missing")
    else:
        if step6_v2.get("current_binding", {}).get("is_current") is not False:
            failures.append("Step6b v2 must be retained evidence, not the global current pointer")
        if step6_v2.get("package_delivery", {}).get("controller_readback_status") != "verified_retained":
            failures.append("Step6b v2 package delivery must be marked verified_retained")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    failures = validate(args.root)
    payload = {"ok": not failures, "failures": failures}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif failures:
        for failure in failures:
            print(f"FAIL: {failure}")
    else:
        print("cross-step parameter table validation passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
