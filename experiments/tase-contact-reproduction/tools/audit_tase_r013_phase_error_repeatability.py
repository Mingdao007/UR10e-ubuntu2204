#!/usr/bin/env python3
"""Measure repeatability of filtered KWR75 force error across sealed A traces.

This is an offline phase-profile predictability audit, not an ILC controller or
a claim of improved closed-loop force tracking.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping

import numpy as np


SCHEMA = "tase.r013.phase-error-repeatability-audit-v1"
FORMAL_START_S = 5.0
FORMAL_END_S = 60.0
FORMAL_BINS = 550


class PhaseAuditError(RuntimeError):
    """A source trace is incomplete or does not contain the expected signal."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bin_filtered_force(rows: list[Mapping[str, Any]], *, bin_count: int = FORMAL_BINS) -> np.ndarray:
    bins: list[list[float]] = [[] for _ in range(bin_count)]
    for row in rows:
        if row.get("reference_phase") != "path":
            continue
        try:
            time_s = float(row["reference_time_s"])
            force_n = float(row["normal_force_filtered_n"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise PhaseAuditError("PATH row lacks finite reference time or filtered force") from exc
        if not math.isfinite(time_s) or not math.isfinite(force_n):
            raise PhaseAuditError("PATH row has nonfinite time or filtered force")
        if FORMAL_START_S <= time_s < FORMAL_END_S:
            index = int(math.floor((time_s - FORMAL_START_S) * 10.0 + 1e-9))
            if 0 <= index < bin_count:
                bins[index].append(force_n)
    missing = [index for index, values in enumerate(bins) if not values]
    if missing:
        raise PhaseAuditError(f"formal filtered-force coverage is {bin_count-len(missing)}/{bin_count} bins")
    return np.asarray([statistics.fmean(values) for values in bins], dtype=float)


def phase_profile_cv(curves: Any) -> dict[str, Any]:
    values = np.asarray(curves, dtype=float)
    if values.ndim != 2 or values.shape[0] < 2 or not np.isfinite(values).all():
        raise PhaseAuditError("phase curves must be a finite attempts-by-bins matrix")
    pairwise = np.corrcoef(values)
    upper = pairwise[np.triu_indices(values.shape[0], k=1)]
    folds = []
    for held_out in range(values.shape[0]):
        training = np.delete(values, held_out, axis=0)
        phase_prediction = training.mean(axis=0)
        constant_prediction = np.full(values.shape[1], training.mean())
        truth = values[held_out]
        phase_rmse = float(np.sqrt(np.mean((truth - phase_prediction) ** 2)))
        constant_rmse = float(np.sqrt(np.mean((truth - constant_prediction) ** 2)))
        folds.append({
            "held_out_index": held_out,
            "phase_profile_rmse_n": phase_rmse,
            "constant_bias_rmse_n": constant_rmse,
            "phase_profile_reduction_vs_constant_fraction": (
                None if constant_rmse <= 1e-12 else 1.0 - phase_rmse / constant_rmse
            ),
        })
    reductions = [
        float(row["phase_profile_reduction_vs_constant_fraction"])
        for row in folds
        if row["phase_profile_reduction_vs_constant_fraction"] is not None
    ]
    return {
        "attempt_count": int(values.shape[0]),
        "bin_count_per_attempt": int(values.shape[1]),
        "mean_pairwise_pearson_error_curve": float(np.mean(upper)),
        "pairwise_pearson_error_curve_range": [float(np.min(upper)), float(np.max(upper))],
        "between_attempt_error_sd_per_bin_median_n": float(np.median(values.std(axis=0, ddof=1))),
        "between_attempt_error_sd_per_bin_p95_n": float(np.percentile(values.std(axis=0, ddof=1), 95.0)),
        "leave_one_attempt_out": {
            "mean_phase_profile_rmse_n": statistics.fmean(row["phase_profile_rmse_n"] for row in folds),
            "mean_constant_bias_rmse_n": statistics.fmean(row["constant_bias_rmse_n"] for row in folds),
            "mean_error_rmse_reduction_vs_constant_fraction": statistics.fmean(reductions),
            "fold_error_rmse_reduction_range": [min(reductions), max(reductions)],
            "folds": folds,
        },
        "interpretation_limit": "Repeatability of sensor-feedback error shape only; not causal plant identification, independent force truth, or predicted improvement from an ILC command.",
    }


def run_audit(source_audit: Path) -> dict[str, Any]:
    source = json.loads(source_audit.read_text(encoding="utf-8"))
    attempts = []
    curves = []
    for item in source.get("attempts", []):
        attempt_dir = Path(item["attempt_dir"])
        timeline = attempt_dir / "command_timeline.jsonl"
        actual_sha = _sha256(timeline)
        if actual_sha != item.get("source_timeline_sha256"):
            raise PhaseAuditError(f"source timeline hash changed: {timeline}")
        rows = []
        with timeline.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise PhaseAuditError(f"invalid JSON at {timeline}:{line_number}") from exc
        curve = bin_filtered_force(rows)
        reconstructed_mae = float(np.mean(np.abs(5.0 - curve)))
        attempts.append({
            "candidate_id": item["candidate_id"],
            "attempt_dir": str(attempt_dir),
            "timeline_sha256": actual_sha,
            "formal_bins": len(curve),
            "receipt_formal_mae_n": item["formal_mae_n"],
            "reconstructed_filtered_bin_mae_n": reconstructed_mae,
            "reconstructed_minus_receipt_mae_n": reconstructed_mae - float(item["formal_mae_n"]),
            "mean_filtered_force_n": float(np.mean(curve)),
            "mean_signed_error_n": float(np.mean(5.0 - curve)),
        })
        curves.append(5.0 - curve)
    if len(attempts) != 5:
        raise PhaseAuditError("expected five eligible rate460 A traces")
    result = phase_profile_cv(curves)
    return {
        "schema": SCHEMA,
        "evidence_class": "offline_sensor_feedback_repeatability",
        "protocol_id": "figure8_window60_r013_rate460_v1",
        "metric_signal": "normal_force_filtered_n from baseline-corrected Kunwei KWR75 feedback",
        "force_truth_limit": "KWR75 feedback is not independent task-force ground truth",
        "window_s": [FORMAL_START_S, FORMAL_END_S],
        "bin_count": FORMAL_BINS,
        "source_audit": str(source_audit),
        "source_audit_sha256": _sha256(source_audit),
        "attempts": attempts,
        "repeatability": result,
        "candidate_status": "no ILC command or live controller was evaluated",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = run_audit(args.source_audit.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"schema": result["schema"], "attempts": len(result["attempts"]), "repeatability": {k: v for k, v in result["repeatability"].items() if k != "leave_one_attempt_out"}, "out": str(args.out.resolve())}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
