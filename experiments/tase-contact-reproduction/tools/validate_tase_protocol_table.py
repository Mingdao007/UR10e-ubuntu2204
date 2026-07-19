#!/usr/bin/env python3
"""Validate the canonical TASE protocol table and migrated ledger refs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tase_protocol_table import EXPERIMENT_ROOT, ProtocolTableError, load_protocol_table, resolve_experiment_profile


def _stage_by_id(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(stage.get("id")): stage for stage in table.get("stages", [])}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _require(condition: bool, failures: list[str], message: str) -> None:
    if not condition:
        failures.append(message)


def validate(root: Path = EXPERIMENT_ROOT) -> list[str]:
    failures: list[str] = []
    try:
        table = load_protocol_table(root)
    except Exception as exc:
        return [str(exc)]

    for key in ("paper_shared", "step_profiles", "parameter_profiles", "safety_limits", "experiment_profiles", "evidence"):
        _require(isinstance(table.get(key), dict), failures, f"missing object: {key}")

    for profile_id in (
        "Step5.contact_cycloid",
        "Step5.step5d_rnn",
        "Step5.step5d_rnn_legacy_v27",
        "Step6.no_contact_eight",
        "Step6.contact_eight_v1",
        "Step6.contact_eight",
    ):
        try:
            resolve_experiment_profile(profile_id, root)
        except ProtocolTableError as exc:
            failures.append(str(exc))

    try:
        step5 = resolve_experiment_profile("Step5.step5d_rnn", root)
        flow = step5["flow"]["steps"]
        _require(step5["flow"]["zero_timing"] == "after_far_search_before_near_search", failures, "Step5 zero timing mismatch")
        _require(flow.index("fast_search_down") < flow.index("zero_after_far_search") < flow.index("near_search_down"), failures, "Step5 zero step must be between far and near search")
        _require(step5["parameters"]["zero_hold_s"] == 1.0, failures, "Step5 v32 zero hold mismatch")
        _require(step5["parameters"]["normal_filter_alpha"] == 0.55, failures, "Step5 normal filter alpha mismatch")
        _require(
            step5["safety_limits"]["angular_limit_rad_s"] == 1.0,
            failures,
            "Step5 v32 permissive angular diagnostic limit mismatch",
        )
        legacy = resolve_experiment_profile("Step5.step5d_rnn_legacy_v27", root)
        _require(legacy["parameters"]["zero_hold_s"] == 0.25, failures, "Step5 legacy zero hold mismatch")
        _require(
            legacy["safety_limits"]["angular_limit_rad_s"] == 0.015,
            failures,
            "Step5 legacy angular limit mismatch",
        )
    except Exception as exc:
        failures.append(f"Step5 resolved profile validation failed: {exc}")

    try:
        step6 = resolve_experiment_profile("Step6.contact_eight", root)
        _require(step6["parameters"]["trajectory_duration_s"] == 30.0, failures, "Step6 trajectory duration mismatch")
        _require(step6["safety_limits"]["total_linear_limit_m_s"] == 0.015, failures, "Step6 v2 total linear limit mismatch")
    except Exception as exc:
        failures.append(f"Step6 resolved profile validation failed: {exc}")

    try:
        current = _load_json(root / "config" / "current_stage.json")
        current_step5d_id = (
            str(current.get("program") or current.get("current_stage_id"))
            if str(current.get("program") or current.get("current_stage_id")).startswith(
                "step5d_strict_rnn_ablation_"
            )
            else "step5d_strict_rnn_ablation_v27"
        )
        step5_rows = _stage_by_id(_load_json(root / "config" / "step5_stage_table.json"))
        step6_rows = _stage_by_id(_load_json(root / "config" / "step6_stage_table.json"))
        _require(
            step5_rows["step5_contact_cycloid_baseline_v1"].get("canonical_profile_ref") == "Step5.contact_cycloid",
            failures,
            "Step5b ledger row missing canonical profile ref",
        )
        _require(
            step5_rows[current_step5d_id].get("canonical_profile_ref") == "Step5.step5d_rnn",
            failures,
            f"Step5d {current_step5d_id.rsplit('_', 1)[-1]} ledger row missing canonical profile ref",
        )
        _require(
            step6_rows["step6a_eight_no_contact_v1"].get("canonical_profile_ref") == "Step6.no_contact_eight",
            failures,
            "Step6a ledger row missing canonical profile ref",
        )
        _require(
            step6_rows["step6_contact_eight_baseline_v1"].get("canonical_profile_ref") == "Step6.contact_eight_v1",
            failures,
            "Step6b v1 ledger row missing canonical profile ref",
        )
        _require(
            step6_rows["step6_contact_eight_baseline_v2"].get("canonical_profile_ref") == "Step6.contact_eight",
            failures,
            "Step6b v2 ledger row missing canonical profile ref",
        )
    except Exception as exc:
        failures.append(f"legacy ledger validation failed: {exc}")

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
        print("TASE protocol table validation passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
