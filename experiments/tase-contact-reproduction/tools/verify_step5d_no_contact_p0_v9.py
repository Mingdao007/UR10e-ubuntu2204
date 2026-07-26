#!/usr/bin/env python3
"""Verify one direct 60 s P0 v9 canonical free-space canary."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any


PROFILE = "step5d_strict_rnn_no_contact_p0_v9"
REQUIRED_DURATION_S = 60.0
MIN_ALONG_ENDPOINT_M = 0.090
MIN_LATERAL_PEAK_M = 0.025
MIN_Z_ENDPOINT_M = 0.018


def _finite(row: dict[str, str], field: str) -> float:
    try:
        value = float(row.get(field, "nan"))
    except (TypeError, ValueError):
        return math.nan
    return value if math.isfinite(value) else math.nan


def _csv_path(run: Path) -> Path:
    if run.is_file():
        return run
    return run / "bridge_rtde_500hz.csv"


def verify(run: Path, *, phase_s: float = REQUIRED_DURATION_S) -> dict[str, Any]:
    blockers: list[str] = []
    if not math.isclose(float(phase_s), REQUIRED_DURATION_S, abs_tol=1e-9):
        blockers.append("phase_must_equal_60s")
    csv_path = _csv_path(run)
    if not csv_path.is_file():
        return {"schema_version": "step5d_no_contact_p0_v9_verification_v2", "ok": False, "blockers": ["bridge_csv_missing"]}
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        blockers.append("bridge_csv_empty")
        return {"schema_version": "step5d_no_contact_p0_v9_verification_v2", "ok": False, "blockers": blockers}

    qualified = [row for row in rows if _finite(row, "_step5d_p0_v9_qualified") >= 0.5]
    terminal = [row for row in rows if _finite(row, "_step5d_p0_v9_canary_stop_active") >= 0.5]
    max_qualified_s = max((_finite(row, "_step5d_p0_v9_qualified_s") for row in rows), default=0.0)
    if max_qualified_s < REQUIRED_DURATION_S:
        blockers.append("continuous_qualified_duration_short")
    if any(_finite(row, "_step5d_p0_rnn_accepted") < 0.5 for row in qualified):
        blockers.append("host_rejection_in_qualified_window")
    if any(not math.isclose(_finite(row, "_step5d_stage25_echo_layout_tag"), 524.0, abs_tol=1e-3) for row in qualified):
        blockers.append("layout_echo_mismatch")
    if any(_finite(row, "_step5d_stage25_echo_cmd_valid") < 0.5 for row in qualified):
        blockers.append("cmd_valid_echo_mismatch")
    if any(_finite(row, "_step5d_stage25_echo_consumed") < 0.5 for row in qualified):
        blockers.append("tp_consumption_gap")
    if any(int(round(_finite(row, "_step5d_rnn_inner_iterations"))) != 512 for row in qualified):
        blockers.append("rnn_inner_iterations_not_512")

    actual_components = {
        component: [
            _finite(row, f"_step5d_p0_v9_actual_{component}_displacement_m")
            for row in qualified
        ]
        for component in ("along", "lateral", "z")
    }
    target_components = {
        component: [
            _finite(row, f"_step5d_p0_v9_target_{component}_displacement_m")
            for row in qualified
        ]
        for component in ("along", "lateral", "z")
    }
    for values in (*actual_components.values(), *target_components.values()):
        values[:] = [value for value in values if math.isfinite(value)]
    along_endpoint_m = actual_components["along"][-1] if actual_components["along"] else math.nan
    lateral_peak_m = max(actual_components["lateral"], default=math.nan)
    z_endpoint_m = actual_components["z"][-1] if actual_components["z"] else math.nan
    motion_observed = any(
        math.isfinite(value) and abs(value) > 0.0
        for values in actual_components.values()
        for value in values
    )
    if not math.isfinite(along_endpoint_m) or along_endpoint_m < MIN_ALONG_ENDPOINT_M:
        blockers.append("canonical_along_endpoint_below_90mm")
    if not math.isfinite(lateral_peak_m) or lateral_peak_m < MIN_LATERAL_PEAK_M:
        blockers.append("canonical_lateral_peak_below_25mm")
    if not math.isfinite(z_endpoint_m) or z_endpoint_m < MIN_Z_ENDPOINT_M:
        blockers.append("relative_base_z_endpoint_below_18mm")
    xyz_tracking_errors = [
        _finite(row, "_step5d_p0_v9_xyz_tracking_error_norm_m") for row in qualified
    ]
    xyz_tracking_errors = [value for value in xyz_tracking_errors if math.isfinite(value)]
    max_xyz_tracking_error_m = max(xyz_tracking_errors, default=math.nan)

    normal_displacements = [abs(_finite(row, "_step5d_p0_v9_anchor_normal_displacement_m")) for row in rows]
    max_normal_displacement_m = max((value for value in normal_displacements if math.isfinite(value)), default=math.inf)
    if not terminal:
        blockers.append("terminal_zero_stop_packet_missing")
    else:
        last = terminal[-1]
        if any(abs(_finite(last, f"step4e_cmd_v{axis}_m_s")) > 1e-12 for axis in ("x", "y", "z")):
            blockers.append("terminal_linear_command_nonzero")
        if any(abs(_finite(last, f"step4e_cmd_w{axis}_rad_s")) > 1e-12 for axis in ("x", "y", "z")):
            blockers.append("terminal_angular_command_nonzero")
        if not math.isclose(_finite(last, "step4e_controller_state"), 524.0, abs_tol=1e-3):
            blockers.append("terminal_layout_not_524")
        if _finite(last, "step4e_cmd_valid") < 0.5 or _finite(last, "stop_request") < 0.5:
            blockers.append("terminal_valid_stop_request_missing")
    tp_ack = any(
        math.isclose(_finite(row, "ur_output_double_register_35"), 26.0, abs_tol=0.05)
        and _finite(row, "ur_output_double_register_28") > 0.5
        for row in rows
    )
    if not tp_ack:
        blockers.append("tp_terminal_stop_ack_missing")
    blockers = sorted(set(blockers))
    return {
        "schema_version": "step5d_no_contact_p0_v9_verification_v2",
        "ok": not blockers,
        "profile": PROFILE,
        "phase_s": phase_s,
        "blockers": blockers,
        "metrics": {
            "rows": len(rows),
            "qualified_rows": len(qualified),
            "max_continuous_qualified_s": max_qualified_s,
            "motion_observed": motion_observed,
            "actual_along_endpoint_m": along_endpoint_m,
            "actual_lateral_peak_m": lateral_peak_m,
            "actual_z_endpoint_m": z_endpoint_m,
            "max_xyz_tracking_error_m": max_xyz_tracking_error_m,
            "max_normal_displacement_m": max_normal_displacement_m,
            "terminal_rows": len(terminal),
            "tp_terminal_stop_acknowledged": tp_ack,
        },
        "claim_boundary": {
            "offline_tooling_accepted": not blockers,
            "no_contact_live_accepted": not blockers,
            "contact_live_accepted": False,
            "reproduction_complete": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--phase-s", type=float, default=REQUIRED_DURATION_S)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = verify(args.run, phase_s=args.phase_s)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["ok"] else 24


if __name__ == "__main__":
    raise SystemExit(main())
