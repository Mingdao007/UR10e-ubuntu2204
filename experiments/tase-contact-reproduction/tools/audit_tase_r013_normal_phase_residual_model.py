#!/usr/bin/env python3
"""Test phase-demeaned normal-force model predictability on repeated A traces.

This is an offline closed-loop association audit. It can test whether same-phase
KWR75 force deviations repeat, but it cannot identify physical contact M/K/B
or establish that a command change will improve force tracking.
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


SCHEMA = "tase.r013.normal-phase-residual-model-v1"
FORMAL_START_S = 5.0
FORMAL_END_S = 60.0
FORMAL_BINS = 550
MODEL_FEATURES = ("position", "velocity", "acceleration")


class PhaseResidualAuditError(RuntimeError):
    """A selected repeated trace does not satisfy the input/evidence contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def phase_demean(values: Any) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 2 or array.shape[0] < 2 or not np.isfinite(array).all():
        raise PhaseResidualAuditError("phase-demeaned data must be a finite attempts-by-bins matrix")
    return array - np.mean(array, axis=0, keepdims=True)


def _fit_zero_intercept(features: list[np.ndarray], response: np.ndarray) -> dict[str, Any]:
    y = np.asarray(response, dtype=float)
    if y.ndim != 2 or not np.isfinite(y).all():
        raise PhaseResidualAuditError("response must be a finite attempts-by-bins matrix")
    matrices = [np.asarray(value, dtype=float) for value in features]
    if not matrices or any(value.shape != y.shape or not np.isfinite(value).all() for value in matrices):
        raise PhaseResidualAuditError("model features must match the response matrix")
    design = np.column_stack([value.reshape(-1) for value in matrices])
    target = y.reshape(-1)
    scale = np.std(design, axis=0)
    if np.any(scale <= 1e-15):
        raise PhaseResidualAuditError("a phase-residual model feature has no between-attempt variation")
    standardized = design / scale
    coefficients_standardized = np.linalg.lstsq(standardized, target, rcond=None)[0]
    coefficients = coefficients_standardized / scale
    prediction = design @ coefficients
    denominator = float(target @ target)
    r_squared = 1.0 - float(np.sum((target - prediction) ** 2)) / denominator if denominator > 1e-15 else 0.0
    return {
        "coefficients_physical_units": coefficients.tolist(),
        "r_squared_centered": r_squared,
        "standardized_design_condition_number": float(np.linalg.cond(standardized)),
        "rmse_n": float(np.sqrt(np.mean((target - prediction) ** 2))),
        "zero_prediction_rmse_n": float(np.sqrt(np.mean(target**2))),
    }


def _read_attempt_bins(attempt_dir: Path, expected_sha256: str) -> dict[str, Any]:
    timeline = attempt_dir / "command_timeline.jsonl"
    if not timeline.is_file() or _sha256(timeline) != expected_sha256:
        raise PhaseResidualAuditError(f"source command timeline missing or hash changed: {timeline}")
    bins: list[list[tuple[float, float, float]]] = [[] for _ in range(FORMAL_BINS)]
    path_rows = 0
    with timeline.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PhaseResidualAuditError(f"invalid JSON at {timeline}:{line_number}") from exc
            if row.get("reference_phase") != "path":
                continue
            try:
                time_s = float(row["reference_time_s"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise PhaseResidualAuditError("PATH row lacks a valid reference clock") from exc
            if not math.isfinite(time_s) or not FORMAL_START_S <= time_s < FORMAL_END_S:
                continue
            provenance = row.get("sfc_replay_input_provenance")
            if not isinstance(provenance, Mapping) or provenance.get("control_wrench_tcp_n_nm") != "kunwei_baseline_corrected":
                raise PhaseResidualAuditError("selected PATH row is not bound to baseline-corrected Kunwei input")
            inputs = row.get("sfc_replay_inputs")
            if not isinstance(inputs, Mapping):
                raise PhaseResidualAuditError("formal PATH row lacks synchronized KWR75/RTDE inputs")
            try:
                measured_force = float(row["normal_force_measured_n"])
                z_m = float(inputs["actual_tcp_pose_m_rad"][2])
                vz_m_s = float(inputs["actual_tcp_speed_m_s_rad_s"][2])
            except (KeyError, TypeError, ValueError, OverflowError, IndexError) as exc:
                raise PhaseResidualAuditError("PATH row lacks KWR75 normal force or TCP z motion") from exc
            if not all(math.isfinite(value) for value in (measured_force, z_m, vz_m_s)):
                raise PhaseResidualAuditError("PATH force/position/velocity contains nonfinite data")
            index = int(math.floor((time_s - FORMAL_START_S) * 10.0 + 1e-9))
            if not 0 <= index < FORMAL_BINS:
                continue
            # Positive x/v point into the surface, opposite the approved base-Z approach direction.
            bins[index].append((measured_force, -z_m, -vz_m_s))
            path_rows += 1
    missing = [index for index, values in enumerate(bins) if not values]
    if missing:
        raise PhaseResidualAuditError(f"formal coverage is {FORMAL_BINS-len(missing)}/{FORMAL_BINS} bins")
    mean_by_bin = np.asarray([np.mean(values, axis=0) for values in bins], dtype=float)
    return {
        "attempt_dir": str(attempt_dir),
        "timeline_sha256": expected_sha256,
        "path_rows_formal": path_rows,
        "formal_bins": FORMAL_BINS,
        "force_n": mean_by_bin[:, 0],
        "position_into_surface_m": mean_by_bin[:, 1],
        "velocity_into_surface_m_s": mean_by_bin[:, 2],
    }


def _leave_one_attempt_out(
    force: np.ndarray,
    features: dict[str, np.ndarray],
) -> list[dict[str, Any]]:
    output = []
    for held_out in range(len(force)):
        training_rows = [index for index in range(len(force)) if index != held_out]
        train_force = force[training_rows]
        test_force = force[held_out]
        train_features = {key: value[training_rows] for key, value in features.items()}
        test_features = {key: value[held_out] for key, value in features.items()}
        # Center each train/test phase against training-only phase means. This
        # avoids leaking the held-out curve into the common surface profile.
        mean_force = train_force.mean(axis=0)
        train_force_residual = train_force - mean_force
        test_force_residual = test_force - mean_force
        mean_features = {key: value.mean(axis=0) for key, value in train_features.items()}
        train_residuals = {key: value - mean_features[key] for key, value in train_features.items()}
        test_residuals = {key: value - mean_features[key] for key, value in test_features.items()}
        design = np.column_stack([train_residuals[key].reshape(-1) for key in MODEL_FEATURES])
        target = train_force_residual.reshape(-1)
        scales = np.std(design, axis=0)
        if np.any(scales <= 1e-15):
            raise PhaseResidualAuditError("leave-one-attempt-out training feature has zero variation")
        standardized = design / scales
        beta_scaled = np.linalg.lstsq(standardized, target, rcond=None)[0]
        beta = beta_scaled / scales
        prediction = sum(beta[index] * test_residuals[name] for index, name in enumerate(MODEL_FEATURES))
        baseline_rmse = float(np.sqrt(np.mean(test_force_residual**2)))
        model_rmse = float(np.sqrt(np.mean((test_force_residual - prediction) ** 2)))
        output.append({
            "held_out_index": held_out,
            "held_out_bins": FORMAL_BINS,
            "coefficients_position_velocity_acceleration": beta.tolist(),
            "baseline_phase_mean_rmse_n": baseline_rmse,
            "model_rmse_n": model_rmse,
            "rmse_reduction_vs_phase_mean_fraction": None if baseline_rmse <= 1e-15 else 1.0 - model_rmse / baseline_rmse,
        })
    return output


def analyze_repeated_attempts(attempts: list[Mapping[str, Any]]) -> dict[str, Any]:
    if len(attempts) != 5:
        raise PhaseResidualAuditError("expected exactly five complete A attempts")
    force = np.asarray([row["force_n"] for row in attempts], dtype=float)
    position = np.asarray([row["position_into_surface_m"] for row in attempts], dtype=float)
    velocity = np.asarray([row["velocity_into_surface_m_s"] for row in attempts], dtype=float)
    time_s = FORMAL_START_S + (np.arange(FORMAL_BINS, dtype=float) + 0.5) * 0.1
    acceleration = np.gradient(velocity, time_s, axis=1)
    response = phase_demean(force)
    features = {
        "position": phase_demean(position),
        "velocity": phase_demean(velocity),
        "acceleration": phase_demean(acceleration),
    }
    models = {}
    for name, selected in (
        ("position_only", ("position",)),
        ("velocity_only", ("velocity",)),
        ("acceleration_only", ("acceleration",)),
        ("position_velocity", ("position", "velocity")),
        ("position_velocity_acceleration", MODEL_FEATURES),
    ):
        models[name] = _fit_zero_intercept([features[key] for key in selected], response)
    splits = []
    raw_features = {
        "position": position,
        "velocity": velocity,
        "acceleration": acceleration,
    }
    for low, high in ((0, 275), (275, FORMAL_BINS)):
        selected_force = force[:, low:high]
        selected_features = {
            key: phase_demean(value[:, low:high])
            for key, value in raw_features.items()
        }
        splits.append({
            "formal_bin_indices": [low, high],
            "position_velocity_acceleration": _fit_zero_intercept(
                [selected_features[key] for key in MODEL_FEATURES],
                phase_demean(selected_force),
            ),
        })
    cv = _leave_one_attempt_out(force, {
        "position": position,
        "velocity": velocity,
        "acceleration": acceleration,
    })
    reductions = [float(row["rmse_reduction_vs_phase_mean_fraction"]) for row in cv]
    return {
        "attempt_count": len(attempts),
        "formal_bins_per_attempt": FORMAL_BINS,
        "same_phase_variation_sd": {
            "force_n": float(np.std(response)),
            "position_into_surface_m": float(np.std(features["position"])),
            "velocity_into_surface_m_s": float(np.std(features["velocity"])),
            "acceleration_into_surface_m_s2": float(np.std(features["acceleration"])),
        },
        "in_sample_phase_demeaned_models": models,
        "two_half_models": splits,
        "leave_one_attempt_out": {
            "folds": cv,
            "mean_rmse_reduction_vs_phase_mean_fraction": statistics.fmean(reductions),
            "rmse_reduction_range": [min(reductions), max(reductions)],
        },
        "interpretation_limit": "The regressions predict deviations in a closed-loop repeated trial. Physical M/K/B are not identified because regressors are endogenous, acceleration is differentiated from 100 ms bins, and coefficients are unstable across phase halves.",
    }


def run_audit(source_audit: Path) -> dict[str, Any]:
    source = json.loads(source_audit.read_text(encoding="utf-8"))
    selected = source.get("attempts", [])
    attempts = [
        _read_attempt_bins(
            Path(row["attempt_dir"]),
            expected_sha256=str(row["source_timeline_sha256"]),
        )
        for row in selected
    ]
    analysis = analyze_repeated_attempts(attempts)
    return {
        "schema": SCHEMA,
        "evidence_class": "offline_phase_demeaned_closed_loop_force_model",
        "protocol_id": "figure8_window60_r013_rate460_v1",
        "force_source": "Kunwei KWR75 normal_force_measured_n, equal-weight 100 ms bin means",
        "motion_source": "synchronized RTDE actual_TCP_pose and actual_TCP_speed fields in TASE timeline",
        "positive_coordinate": "into surface = negative base-Z position and speed",
        "force_truth_limit": "KWR75 feedback is not independent task-force truth",
        "source_audit_path": str(source_audit),
        "source_audit_sha256": _sha256(source_audit),
        "attempts": [{
            "candidate_id": row["candidate_id"],
            "attempt_dir": attempt["attempt_dir"],
            "source_timeline_sha256": attempt["timeline_sha256"],
            "path_rows_formal": attempt["path_rows_formal"],
            "formal_bins": attempt["formal_bins"],
            "receipt_formal_mae_n": row["formal_mae_n"],
        } for row, attempt in zip(selected, attempts, strict=True)],
        "analysis": analysis,
        "live_candidate": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = run_audit(args.source_audit.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    cv = result["analysis"]["leave_one_attempt_out"]
    print(json.dumps({
        "out": str(args.out.resolve()),
        "attempts": len(result["attempts"]),
        "cv_rmse_reduction_mean": cv["mean_rmse_reduction_vs_phase_mean_fraction"],
        "cv_rmse_reduction_range": cv["rmse_reduction_range"],
        "in_sample_models": {key: value["r_squared_centered"] for key, value in result["analysis"]["in_sample_phase_demeaned_models"].items()},
    }, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
