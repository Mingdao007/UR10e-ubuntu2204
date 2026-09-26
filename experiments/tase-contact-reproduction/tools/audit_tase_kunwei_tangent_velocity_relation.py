#!/usr/bin/env python3
"""Describe the relation between sealed KWR75 tangent feedback and TCP motion.

This is an offline association audit. It does not identify friction, bias, or
external disturbance as a cause, and it never reads UR internal force fields.
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

from step5d_paper_outer_loop import rotvec_to_matrix


SCHEMA = "tase.kunwei-tangent-velocity-relation-audit-v1"


class AuditError(RuntimeError):
    """A selected sealed trace is missing valid synchronized measurements."""


def _finite_vector(value: Any, width: int, label: str) -> np.ndarray:
    try:
        vector = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AuditError(f"{label} is not a numeric vector") from exc
    if vector.shape != (width,) or not np.isfinite(vector).all():
        raise AuditError(f"{label} must contain {width} finite values")
    return vector.copy()


def tangent_vectors_base(
    *,
    force_tcp_n: Any,
    tcp_pose_m_rad: Any,
    velocity_base_m_s: Any,
    normal_base: Any,
) -> tuple[np.ndarray, np.ndarray]:
    """Rotate the sensor force to base and project force/velocity tangent."""
    force_tcp = _finite_vector(force_tcp_n, 3, "force_tcp_n")
    pose = _finite_vector(tcp_pose_m_rad, 6, "tcp_pose_m_rad")
    velocity = _finite_vector(velocity_base_m_s, 3, "velocity_base_m_s")
    normal = _finite_vector(normal_base, 3, "normal_base")
    norm = float(np.linalg.norm(normal))
    if norm <= 1e-12:
        raise AuditError("normal_base is degenerate")
    normal /= norm
    projector = np.eye(3) - np.outer(normal, normal)
    rotation = np.asarray(rotvec_to_matrix(pose[3:]), dtype=float)
    if rotation.shape != (3, 3) or not np.isfinite(rotation).all():
        raise AuditError("TCP rotvec did not produce a finite rotation")
    return projector @ (rotation @ force_tcp), projector @ velocity


def _r_squared(features: np.ndarray, response: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    design = np.column_stack((np.ones(len(features)), features))
    coefficients = np.linalg.lstsq(design, response, rcond=None)[0]
    predicted = design @ coefficients
    residual_ss = np.sum((response - predicted) ** 2, axis=0)
    total_ss = np.sum((response - response.mean(axis=0)) ** 2, axis=0)
    r_squared = np.divide(
        total_ss - residual_ss,
        total_ss,
        out=np.zeros_like(total_ss),
        where=total_ss > 1e-15,
    )
    return coefficients, r_squared


def _velocity_agreement(left: np.ndarray, right: np.ndarray) -> dict[str, float]:
    left = np.asarray(left, dtype=float)
    right = np.asarray(right, dtype=float)
    if (
        left.ndim != 2 or right.shape != left.shape or left.shape[1] != 2
        or not np.isfinite(left).all() or not np.isfinite(right).all()
    ):
        raise AuditError("velocity agreement requires paired finite Nx2 vectors")
    left_norm = np.linalg.norm(left, axis=1)
    right_norm = np.linalg.norm(right, axis=1)
    valid = (left_norm > 1e-9) & (right_norm > 1e-9)
    if not np.any(valid):
        raise AuditError("velocity agreement has no nonzero paired rows")
    cosine = np.sum(left[valid] * right[valid], axis=1) / (
        left_norm[valid] * right_norm[valid]
    )
    ratios = left_norm[valid] / right_norm[valid]
    difference = left[valid] - right[valid]
    return {
        "valid_bins": int(np.count_nonzero(valid)),
        "positive_direction_fraction": float(np.mean(cosine > 0.0)),
        "mean_cosine": float(np.mean(cosine)),
        "median_speed_ratio": float(np.median(ratios)),
        "rms_component_difference_m_s": float(np.sqrt(np.mean(difference**2))),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_attempt(attempt_dir: Path, *, expected_timeline_sha256: str) -> dict[str, Any]:
    timeline = attempt_dir / "command_timeline.jsonl"
    if not timeline.is_file():
        raise AuditError(f"missing command timeline: {timeline}")
    actual_sha256 = _sha256(timeline)
    if actual_sha256 != expected_timeline_sha256:
        raise AuditError(f"timeline hash changed for {attempt_dir}")

    path_rows: list[tuple[float, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
    with timeline.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditError(f"invalid JSON at {timeline}:{line_number}") from exc
            if row.get("reference_phase") != "path":
                continue
            time_s = float(row.get("reference_time_s"))
            if not 5.0 <= time_s < 60.0:
                continue
            inputs = row.get("sfc_replay_inputs")
            if not isinstance(inputs, Mapping):
                raise AuditError("formal PATH row lacks synchronized replay inputs")
            pose = _finite_vector(inputs.get("actual_tcp_pose_m_rad"), 6, "actual_tcp_pose_m_rad")
            force, actual_velocity = tangent_vectors_base(
                force_tcp_n=_finite_vector(
                    inputs.get("control_wrench_tcp_n_nm"), 6, "control_wrench_tcp_n_nm"
                )[:3],
                tcp_pose_m_rad=pose,
                velocity_base_m_s=_finite_vector(
                    inputs.get("actual_tcp_speed_m_s_rad_s"), 6,
                    "actual_tcp_speed_m_s_rad_s",
                )[:3],
                normal_base=inputs.get("control_reaction_normal_base"),
            )
            _force_ref, reference_velocity = tangent_vectors_base(
                force_tcp_n=(0.0, 0.0, 0.0),
                tcp_pose_m_rad=pose,
                velocity_base_m_s=inputs.get("reference_velocity_base_m_s"),
                normal_base=inputs.get("control_reaction_normal_base"),
            )
            path_rows.append((time_s, force[:2], actual_velocity[:2], reference_velocity[:2], pose[:2]))

    bins: dict[int, list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]] = {
        index: [] for index in range(550)
    }
    for time_s, force, actual_velocity, reference_velocity, position in path_rows:
        index = int(math.floor((time_s - 5.0) * 10.0 + 1e-9))
        if 0 <= index < 550:
            bins[index].append((force, actual_velocity, reference_velocity, position))
    missing = [index for index, values in bins.items() if not values]
    if missing:
        raise AuditError(f"formal coverage incomplete: {550 - len(missing)}/550 bins")

    force_bins = np.asarray([np.mean([row[0] for row in bins[i]], axis=0) for i in range(550)])
    actual_velocity_bins = np.asarray(
        [np.mean([row[1] for row in bins[i]], axis=0) for i in range(550)]
    )
    reference_velocity_bins = np.asarray(
        [np.mean([row[2] for row in bins[i]], axis=0) for i in range(550)]
    )
    position_bins = np.asarray([np.mean([row[3] for row in bins[i]], axis=0) for i in range(550)])
    bin_times = 5.05 + np.arange(550, dtype=float) * 0.1
    pose_finite_difference_velocity_bins = np.gradient(position_bins, bin_times, axis=0)
    actual_acceleration_bins = np.gradient(actual_velocity_bins, bin_times, axis=0)
    velocity_coefficients, velocity_r2 = _r_squared(actual_velocity_bins, force_bins)
    velocity_accel_coefficients, velocity_accel_r2 = _r_squared(
        np.column_stack((actual_velocity_bins, actual_acceleration_bins)), force_bins
    )

    def alignment(velocity: np.ndarray) -> dict[str, float]:
        force_norm = np.linalg.norm(force_bins, axis=1)
        velocity_norm = np.linalg.norm(velocity, axis=1)
        valid = (force_norm > 1e-9) & (velocity_norm > 1e-9)
        cosine = np.sum(force_bins[valid] * velocity[valid], axis=1) / (
            force_norm[valid] * velocity_norm[valid]
        )
        projection = np.sum(force_bins * velocity, axis=1) / np.maximum(velocity_norm, 1e-9)
        return {
            "valid_bins": int(np.count_nonzero(valid)),
            "mean_cosine": float(np.mean(cosine)),
            "negative_cosine_fraction": float(np.mean(cosine < 0.0)),
            "mean_force_projection_on_velocity_n": float(np.mean(projection)),
            "median_velocity_m_s": float(np.median(velocity_norm)),
            "p90_velocity_m_s": float(np.percentile(velocity_norm, 90.0)),
        }

    return {
        "attempt_dir": str(attempt_dir),
        "source_timeline_sha256": actual_sha256,
        "path_rows_formal": len(path_rows),
        "formal_bins": 550,
        "force_source": "baseline-corrected Kunwei KWR75 control_wrench_tcp_n_nm",
        "actual_velocity_source": "RTDE actual_TCP_speed captured in synchronized replay inputs",
        "actual_tcp_velocity_alignment": alignment(actual_velocity_bins),
        "reference_velocity_alignment": alignment(reference_velocity_bins),
        "actual_tcp_speed_vs_pose_difference_check": _velocity_agreement(
            actual_velocity_bins, pose_finite_difference_velocity_bins
        ),
        "binned_force_regression": {
            "response": "base-frame tangent KWR75 force x/y, equal-weight 100 ms bins in [5,60)",
            "velocity_only": {
                "intercept_n_xy": velocity_coefficients[0].tolist(),
                "slope_n_s_per_m_xy": velocity_coefficients[1:3].T.tolist(),
                "r_squared_xy": velocity_r2.tolist(),
            },
            "velocity_and_finite_difference_acceleration": {
                "intercept_n_xy": velocity_accel_coefficients[0].tolist(),
                "coefficients_by_force_axis_n_xy": velocity_accel_coefficients[1:].T.tolist(),
                "r_squared_xy": velocity_accel_r2.tolist(),
                "acceleration_method": "finite difference of 100 ms-bin mean RTDE TCP velocity",
            },
        },
        "_regression_data": {
            "force_bins_xy_n": force_bins.tolist(),
            "actual_velocity_bins_xy_m_s": actual_velocity_bins.tolist(),
        },
    }


def _leave_one_attempt_out_velocity_fit(
    attempt_data: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Fit force from actual velocity on other attempts and score held-out rows."""
    if len(attempt_data) < 2:
        raise AuditError("cross-validation requires at least two complete attempts")
    arrays = []
    for row in attempt_data:
        force = _finite_vector_matrix(row.get("force_bins_xy_n"), 2, "force_bins_xy_n")
        velocity = _finite_vector_matrix(
            row.get("actual_velocity_bins_xy_m_s"), 2, "actual_velocity_bins_xy_m_s"
        )
        if force.shape != velocity.shape or len(force) == 0:
            raise AuditError("cross-validation force and velocity bins differ")
        arrays.append((force, velocity))
    output = []
    for held_out_index, (test_force, test_velocity) in enumerate(arrays):
        train_force = np.concatenate([value[0] for i, value in enumerate(arrays) if i != held_out_index])
        train_velocity = np.concatenate([value[1] for i, value in enumerate(arrays) if i != held_out_index])
        design = np.column_stack((np.ones(len(train_velocity)), train_velocity))
        coefficients = np.linalg.lstsq(design, train_force, rcond=None)[0]
        prediction = np.column_stack((np.ones(len(test_velocity)), test_velocity)) @ coefficients
        constant_prediction = np.broadcast_to(train_force.mean(axis=0), test_force.shape)
        velocity_rmse = float(np.sqrt(np.mean((test_force - prediction) ** 2)))
        constant_rmse = float(np.sqrt(np.mean((test_force - constant_prediction) ** 2)))
        output.append({
            "held_out_attempt_index": held_out_index,
            "held_out_bins": len(test_force),
            "zero_force_rmse_n": float(np.sqrt(np.mean(test_force**2))),
            "constant_force_model_rmse_n": constant_rmse,
            "velocity_model_rmse_n": velocity_rmse,
            "velocity_model_reduction_vs_constant_fraction": (
                None if constant_rmse <= 1e-12 else 1.0 - velocity_rmse / constant_rmse
            ),
            "training_velocity_coefficients_by_force_axis": coefficients[1:].T.tolist(),
            "training_intercept_n_xy": coefficients[0].tolist(),
        })
    return output


def _finite_vector_matrix(value: Any, width: int, label: str) -> np.ndarray:
    try:
        matrix = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AuditError(f"{label} is not a numeric matrix") from exc
    if matrix.ndim != 2 or matrix.shape[1] != width or not np.isfinite(matrix).all():
        raise AuditError(f"{label} must be a finite Nx{width} matrix")
    return matrix


def run_audit(audit_path: Path) -> dict[str, Any]:
    source = json.loads(audit_path.read_text(encoding="utf-8"))
    attempts = []
    for source_attempt in source.get("attempts", []):
        attempt_dir = Path(source_attempt["attempt_dir"])
        attempts.append(
            audit_attempt(
                attempt_dir,
                expected_timeline_sha256=str(source_attempt["source_timeline_sha256"]),
            )
        )
    if len(attempts) != int(source.get("eligible_attempts", 0)) or len(attempts) != 5:
        raise AuditError("expected exactly five eligible complete rate460 A traces")
    actual_negative = [row["actual_tcp_velocity_alignment"]["negative_cosine_fraction"] for row in attempts]
    actual_cosine = [row["actual_tcp_velocity_alignment"]["mean_cosine"] for row in attempts]
    cross_validation = _leave_one_attempt_out_velocity_fit(
        [row["_regression_data"] for row in attempts]
    )
    for row in attempts:
        del row["_regression_data"]
    return {
        "schema": SCHEMA,
        "evidence_class": "offline_descriptive_same_trace_association",
        "source_audit": str(audit_path),
        "source_audit_sha256": _sha256(audit_path),
        "attempt_count": len(attempts),
        "summary": {
            "actual_velocity_negative_alignment_fraction_min": min(actual_negative),
            "actual_velocity_negative_alignment_fraction_mean": statistics.fmean(actual_negative),
            "actual_velocity_negative_alignment_fraction_max": max(actual_negative),
            "actual_velocity_mean_cosine_mean": statistics.fmean(actual_cosine),
            "tcp_speed_vs_pose_difference_positive_direction_fraction_range": [
                min(row["actual_tcp_speed_vs_pose_difference_check"]["positive_direction_fraction"] for row in attempts),
                max(row["actual_tcp_speed_vs_pose_difference_check"]["positive_direction_fraction"] for row in attempts),
            ],
            "tcp_speed_vs_pose_difference_mean_cosine_range": [
                min(row["actual_tcp_speed_vs_pose_difference_check"]["mean_cosine"] for row in attempts),
                max(row["actual_tcp_speed_vs_pose_difference_check"]["mean_cosine"] for row in attempts),
            ],
            "velocity_only_r_squared_xy_range": [
                min(value for row in attempts for value in row["binned_force_regression"]["velocity_only"]["r_squared_xy"]),
                max(value for row in attempts for value in row["binned_force_regression"]["velocity_only"]["r_squared_xy"]),
            ],
            "velocity_plus_acceleration_r_squared_xy_range": [
                min(value for row in attempts for value in row["binned_force_regression"]["velocity_and_finite_difference_acceleration"]["r_squared_xy"]),
                max(value for row in attempts for value in row["binned_force_regression"]["velocity_and_finite_difference_acceleration"]["r_squared_xy"]),
            ],
            "leave_one_attempt_out_velocity_model_reduction_vs_constant_fraction_mean": statistics.fmean(
                float(row["velocity_model_reduction_vs_constant_fraction"])
                for row in cross_validation
                if row["velocity_model_reduction_vs_constant_fraction"] is not None
            ),
            "leave_one_attempt_out_velocity_model_reduction_vs_constant_fraction_range": [
                min(float(row["velocity_model_reduction_vs_constant_fraction"]) for row in cross_validation if row["velocity_model_reduction_vs_constant_fraction"] is not None),
                max(float(row["velocity_model_reduction_vs_constant_fraction"]) for row in cross_validation if row["velocity_model_reduction_vs_constant_fraction"] is not None),
            ],
        },
        "leave_one_attempt_out_velocity_model": {
            "fit": "force_xy = intercept_xy + B_xy @ actual_tcp_velocity_xy",
            "cross_validation_unit": "hold out one whole Figure-eight attempt; train on the other four attempts",
            "folds": cross_validation,
            "interpretation_limit": "descriptive predictive repeatability only; no causal or external-force identification",
        },
        "causal_interpretation": "not identified; velocity-correlated KWR75 feedback can include contact friction/reaction, residual baseline, sensor/frame effects, and closed-loop coupling",
        "independent_task_force_ground_truth": False,
        "attempts": attempts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = run_audit(args.source_audit.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"schema": report["schema"], "attempt_count": report["attempt_count"], "summary": report["summary"], "out": str(args.out.resolve())}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
