#!/usr/bin/env python3
"""Audit filtered, measured, and fused KWR75 normal-force signals in sealed attempts.

This read-only diagnostic checks the live force-source selection rule and
quantifies when the controller's fused signal disagrees with the filtered
signal used by the formal score. It does not infer causality or independent
contact-force truth.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from analyze_tase_resident_evidence import reduce_attempt


SCHEMA = "tase.kwr75-force-control-signal-selection-audit-v1"
TARGET_FORCE_N = 5.0
FORMAL_START_S = 5.0
PATH_END_S = 60.0
BIN_WIDTH_S = 0.1
CONTROL_RULE_TOLERANCE_N = 1e-9


class SignalAuditError(RuntimeError):
    """A sealed trace does not meet this force-signal audit contract."""


def _quantiles(values: np.ndarray) -> dict[str, float]:
    if values.size == 0 or not np.isfinite(values).all():
        raise SignalAuditError("force-signal window has no finite samples")
    return {
        "mean": float(np.mean(values)),
        "median": float(np.median(values)),
        "p95": float(np.quantile(values, 0.95)),
        "max": float(np.max(values)),
    }


def summarize_window(rows: list[dict[str, Any]], *, start_s: float, end_s: float) -> dict[str, Any]:
    selected = [
        row for row in rows
        if start_s <= float(row["reference_time_s"]) < end_s
    ]
    if not selected:
        raise SignalAuditError(f"no controller frames in [{start_s},{end_s})")
    filtered = np.asarray([float(row["normal_force_filtered_n"]) for row in selected])
    measured = np.asarray([float(row["normal_force_measured_n"]) for row in selected])
    control = np.asarray([float(row["normal_force_control_n"]) for row in selected])
    target = np.asarray([float(row.get("force_target_n", TARGET_FORCE_N)) for row in selected])
    error_trace = np.asarray([float(row["force_error_n"]) for row in selected])
    if not all(np.isfinite(values).all() for values in (filtered, measured, control, target, error_trace)):
        raise SignalAuditError("force signal contains a nonfinite sample")
    if not np.allclose(target, TARGET_FORCE_N, rtol=0.0, atol=1e-9):
        raise SignalAuditError("attempt force target differs from the audited 5 N protocol")

    expected_control = np.maximum(filtered, measured)
    control_rule_error = control - expected_control
    control_error = target - control
    filtered_error = target - filtered
    measured_error = target - measured
    error_feedback_error = error_trace - control_error
    rule_violations = int(np.count_nonzero(np.abs(control_rule_error) > CONTROL_RULE_TOLERANCE_N))

    bin_count = int(round((end_s - start_s) / BIN_WIDTH_S))
    if not math.isclose((end_s - start_s) / BIN_WIDTH_S, bin_count, rel_tol=0.0, abs_tol=1e-12):
        raise SignalAuditError("window does not contain an integral number of 100 ms bins")
    totals = {name: np.zeros(bin_count, dtype=float) for name in ("filtered", "measured", "control")}
    counts = np.zeros(bin_count, dtype=int)
    for row, values in zip(selected, zip(filtered, measured, control, strict=True), strict=True):
        index = int(math.floor((float(row["reference_time_s"]) - start_s) / BIN_WIDTH_S + 1e-9))
        if 0 <= index < bin_count:
            for name, value in zip(totals, values, strict=True):
                totals[name][index] += float(value)
            counts[index] += 1
    nonempty = counts > 0
    bin_means = {name: values[nonempty] / counts[nonempty] for name, values in totals.items()}
    filtered_bin_error = TARGET_FORCE_N - bin_means["filtered"]
    control_bin_error = TARGET_FORCE_N - bin_means["control"]
    measured_bin_error = TARGET_FORCE_N - bin_means["measured"]
    sample_sign_disagreement = (filtered_error > 0.0) != (control_error > 0.0)
    bin_sign_disagreement = (filtered_bin_error > 0.0) != (control_bin_error > 0.0)

    return {
        "window_s": [float(start_s), float(end_s)],
        "sample_count": int(filtered.size),
        "bin_width_s": BIN_WIDTH_S,
        "nonempty_bins": int(np.count_nonzero(nonempty)),
        "required_bins": bin_count,
        "complete_bin_coverage": int(np.count_nonzero(nonempty)) == bin_count,
        "signals": {
            "filtered_normal_force_n": {
                "sample_mae_n": float(np.mean(np.abs(filtered_error))),
                "sample_rmse_n": float(np.sqrt(np.mean(filtered_error ** 2))),
                "sample_bias_measured_minus_target_n": float(np.mean(filtered - target)),
                "bin_mean_mae_n": float(np.mean(np.abs(filtered_bin_error))),
                "bin_mean_rmse_n": float(np.sqrt(np.mean(filtered_bin_error ** 2))),
            },
            "measured_normal_force_n": {
                "sample_mae_n": float(np.mean(np.abs(measured_error))),
                "sample_rmse_n": float(np.sqrt(np.mean(measured_error ** 2))),
                "sample_bias_measured_minus_target_n": float(np.mean(measured - target)),
                "bin_mean_mae_n": float(np.mean(np.abs(measured_bin_error))),
                "bin_mean_rmse_n": float(np.sqrt(np.mean(measured_bin_error ** 2))),
            },
            "control_normal_force_n": {
                "sample_mae_n": float(np.mean(np.abs(control_error))),
                "sample_rmse_n": float(np.sqrt(np.mean(control_error ** 2))),
                "sample_bias_measured_minus_target_n": float(np.mean(control - target)),
                "bin_mean_mae_n": float(np.mean(np.abs(control_bin_error))),
                "bin_mean_rmse_n": float(np.sqrt(np.mean(control_bin_error ** 2))),
            },
        },
        "signal_selection": {
            "declared_live_rule": "control_normal=max(filtered_normal, measured_normal)",
            "measured_dominates_filtered_sample_fraction": float(np.mean(measured > filtered + CONTROL_RULE_TOLERANCE_N)),
            "mean_control_minus_filtered_n": float(np.mean(control - filtered)),
            "control_minus_filtered_sample_quantiles_n": _quantiles(control - filtered),
            "control_rule_violation_samples": rule_violations,
            "force_error_vs_target_minus_control_max_abs_difference_n": float(np.max(np.abs(error_feedback_error))),
            "official_filtered_under_target_but_control_over_target_fraction": float(np.mean((filtered < target) & (control > target))),
            "filtered_and_control_error_sign_disagreement_fraction": float(np.mean(sample_sign_disagreement)),
            "filtered_and_control_bin_error_sign_disagreement_fraction": float(np.mean(bin_sign_disagreement)),
            "bin_mean_control_minus_filtered_n": float(np.mean(bin_means["control"] - bin_means["filtered"])),
        },
    }


def audit_attempt(attempt_dir: Path) -> dict[str, Any]:
    attempt_dir = Path(attempt_dir).resolve(strict=True)
    reduced = reduce_attempt(attempt_dir, verify_seal=True)
    if reduced.get("protocol_id") != "figure8_window60_r013_rate460_v1":
        raise SignalAuditError(f"attempt is not the rate460 60 s protocol: {attempt_dir}")
    if reduced.get("path_complete") is not True or reduced.get("home_verified") is not True:
        raise SignalAuditError(f"attempt lacks complete PATH/Home evidence: {attempt_dir}")
    timeline_path = attempt_dir / "command_timeline.jsonl"
    rows = []
    with timeline_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("reference_phase") == "path":
                rows.append(row)
    windows = {
        "window_0_60_s": summarize_window(rows, start_s=0.0, end_s=PATH_END_S),
        "window_5_60_s": summarize_window(rows, start_s=FORMAL_START_S, end_s=PATH_END_S),
    }
    return {
        "attempt_dir": str(attempt_dir),
        "candidate_id": reduced.get("candidate_id"),
        "protocol_id": reduced.get("protocol_id"),
        "seal_verified": reduced.get("seal_verified"),
        "path_complete": reduced.get("path_complete"),
        "home_verified": reduced.get("home_verified"),
        "evidence_eligible": reduced.get("evidence_eligible"),
        "receipt_official_formal_mae_n": reduced.get("formal_bin_mean_mae_n"),
        "receipt_formal_bins": reduced.get("formal_bins"),
        "cadence": reduced.get("cadence"),
        "windows": windows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {
        "schema": SCHEMA,
        "evidence_class": "sealed_attempt_sensor_signal_selection_diagnostic",
        "force_source": "Kunwei KWR75 measured and filtered normal-load channels",
        "independent_force_truth": False,
        "selection_rule_source": "tools/tase_contact_provider.py:_installation_force_binding and path command; control_normal=max(filtered, measured)",
        "attempts": [audit_attempt(path) for path in args.attempt_dir],
        "interpretation_limit": "Signal disagreement quantifies a feedback/score mismatch only. It does not establish that signal selection caused force tracking error or predict an alternative controller's force score.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output.resolve()),
        "attempts": len(result["attempts"]),
        "evidence_class": result["evidence_class"],
    }, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
