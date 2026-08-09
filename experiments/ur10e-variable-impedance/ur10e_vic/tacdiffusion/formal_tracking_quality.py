"""Behavioral eligibility for complete formal contact-path motion."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


FORMAL_TRACKING_QUALITY_SCHEMA_V1 = "ur10e_tacdiffusion_formal_tracking_quality/v1"
FORMAL_TRACKING_QUALITY_SCHEMA_V2 = "ur10e_tacdiffusion_formal_tracking_quality/v2"
FORMAL_PLANNED_TARGET_LOADS_N = (3.0, 5.0, 8.0)


def _rotation_matrix_from_rotvec(rotvec: Sequence[float]) -> np.ndarray:
    vector = np.asarray(tuple(float(value) for value in rotvec), dtype=float)
    if vector.shape != (3,) or not np.isfinite(vector).all():
        raise ValueError("rotation vector must contain three finite values")
    angle = float(np.linalg.norm(vector))
    if angle < 1.0e-12:
        return np.eye(3)
    axis = vector / angle
    skew = np.asarray(
        (
            (0.0, -axis[2], axis[1]),
            (axis[2], 0.0, -axis[0]),
            (-axis[1], axis[0], 0.0),
        ),
        dtype=float,
    )
    return np.eye(3) + math.sin(angle) * skew + (1.0 - math.cos(angle)) * (skew @ skew)


@dataclass(frozen=True)
class FormalTrackingQualityContractV1:
    """Frozen thresholds that distinguish a traced path from a static hold."""

    minimum_desired_excursion_m: float = 0.0025
    minimum_actual_excursion_m: float = 0.0018
    minimum_excursion_ratio: float = 0.60
    minimum_path_length_ratio: float = 0.50
    maximum_path_length_ratio: float = 2.00
    maximum_tracking_rmse_m: float = 0.0015
    minimum_displacement_correlation: float = 0.80
    maximum_mean_normal_load_error_n: float = 1.0
    maximum_p95_normal_load_error_n: float = 2.0
    maximum_tracking_error_m: float = 0.0015
    tail_fraction: float = 0.20
    maximum_tail_tracking_error_m: float = 0.0015
    maximum_tail_tracking_rmse_m: float = 0.0015
    minimum_tail_path_length_ratio: float = 0.50
    schema_version: str = FORMAL_TRACKING_QUALITY_SCHEMA_V2

    def __post_init__(self) -> None:
        if self.schema_version != FORMAL_TRACKING_QUALITY_SCHEMA_V2:
            raise ValueError("unsupported formal tracking quality schema")
        expected = (
            0.0025,
            0.0018,
            0.60,
            0.50,
            2.00,
            0.0015,
            0.80,
            1.0,
            2.0,
            0.0015,
            0.20,
            0.0015,
            0.0015,
            0.50,
        )
        actual = (
            self.minimum_desired_excursion_m,
            self.minimum_actual_excursion_m,
            self.minimum_excursion_ratio,
            self.minimum_path_length_ratio,
            self.maximum_path_length_ratio,
            self.maximum_tracking_rmse_m,
            self.minimum_displacement_correlation,
            self.maximum_mean_normal_load_error_n,
            self.maximum_p95_normal_load_error_n,
            self.maximum_tracking_error_m,
            self.tail_fraction,
            self.maximum_tail_tracking_error_m,
            self.maximum_tail_tracking_rmse_m,
            self.minimum_tail_path_length_ratio,
        )
        if actual != expected:
            raise ValueError("formal tracking quality thresholds are frozen")

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "minimum_desired_excursion_m": self.minimum_desired_excursion_m,
            "minimum_actual_excursion_m": self.minimum_actual_excursion_m,
            "minimum_excursion_ratio": self.minimum_excursion_ratio,
            "minimum_path_length_ratio": self.minimum_path_length_ratio,
            "maximum_path_length_ratio": self.maximum_path_length_ratio,
            "maximum_tracking_rmse_m": self.maximum_tracking_rmse_m,
            "minimum_displacement_correlation": self.minimum_displacement_correlation,
            "maximum_mean_normal_load_error_n": self.maximum_mean_normal_load_error_n,
            "maximum_p95_normal_load_error_n": self.maximum_p95_normal_load_error_n,
            "maximum_tracking_error_m": self.maximum_tracking_error_m,
            "tail_fraction": self.tail_fraction,
            "maximum_tail_tracking_error_m": self.maximum_tail_tracking_error_m,
            "maximum_tail_tracking_rmse_m": self.maximum_tail_tracking_rmse_m,
            "minimum_tail_path_length_ratio": self.minimum_tail_path_length_ratio,
        }


FormalTrackingQualityContractV2 = FormalTrackingQualityContractV1


def _payload(row: object) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    as_json = getattr(row, "as_json", None)
    if callable(as_json):
        value = as_json()
        if isinstance(value, Mapping):
            return value
    raise ValueError("formal tracking row is not serializable")


def _block_mean_path(values: np.ndarray, *, target_samples: int = 81) -> np.ndarray:
    """Suppress high-rate encoder jitter before geometric path integration."""

    if values.ndim != 2 or values.shape[0] < 2:
        return values
    block = max(1, values.shape[0] // max(2, int(target_samples) - 1))
    return np.vstack(
        [np.mean(values[start : start + block], axis=0) for start in range(0, values.shape[0], block)]
    )


def _path_diameter(values: np.ndarray) -> float:
    if values.ndim != 2 or values.shape[0] < 2:
        return 0.0
    return float(np.max(np.linalg.norm(values[:, None, :] - values[None, :, :], axis=2)))


def _path_length(values: np.ndarray) -> float:
    if values.ndim != 2 or values.shape[0] < 2:
        return 0.0
    return float(np.sum(np.linalg.norm(np.diff(values, axis=0), axis=1)))


def evaluate_formal_path_geometry(
    desired_xyz: np.ndarray,
    actual_xyz: np.ndarray,
    *,
    contract: FormalTrackingQualityContractV1 = FormalTrackingQualityContractV1(),
) -> dict[str, object]:
    """Shared geometric tracking predicates for formal and no-contact gates."""

    desired_raw = np.asarray(desired_xyz, dtype=float)
    actual_raw = np.asarray(actual_xyz, dtype=float)
    if desired_raw.ndim != 2 or actual_raw.ndim != 2 or desired_raw.shape != actual_raw.shape:
        raise ValueError("formal path geometry arrays must share shape (N, 3)")
    if desired_raw.shape[1] != 3:
        raise ValueError("formal path geometry requires xyz columns")
    row_count = int(desired_raw.shape[0])
    if row_count < 2:
        metrics = {
            "row_count": row_count,
            "desired_excursion_m": 0.0,
            "actual_excursion_m": 0.0,
            "excursion_ratio": 0.0,
            "desired_path_length_m": 0.0,
            "actual_path_length_m": 0.0,
            "path_length_ratio": 0.0,
            "tracking_rmse_m": 1.0e9,
            "tracking_error_m": 1.0e9,
            "displacement_correlation": 0.0,
            "tail_row_count": 0,
            "tail_sample_count": 0,
            "desired_tail_path_length_m": 0.0,
            "actual_tail_path_length_m": 0.0,
            "tail_path_length_ratio": 0.0,
            "tail_tracking_error_m": 1.0e9,
            "tail_tracking_rmse_m": 1.0e9,
        }
    else:
        per_row_errors = np.linalg.norm(desired_raw - actual_raw, axis=1)
        tracking_error_m = float(np.max(per_row_errors))
        desired = _block_mean_path(desired_raw)
        actual = _block_mean_path(actual_raw)
        desired_displacement = desired - desired[0]
        actual_displacement = actual - actual[0]
        desired_excursion = _path_diameter(desired)
        actual_excursion = _path_diameter(actual)
        desired_length = _path_length(desired)
        actual_length = _path_length(actual)
        desired_flat = desired_displacement.reshape(-1)
        actual_flat = actual_displacement.reshape(-1)
        if float(np.std(desired_flat)) <= 1.0e-12 or float(np.std(actual_flat)) <= 1.0e-12:
            correlation = 0.0
        else:
            correlation = float(np.corrcoef(desired_flat, actual_flat)[0, 1])
        tail_start_raw = max(0, int(math.ceil((1.0 - contract.tail_fraction) * (row_count - 1))))
        desired_tail_raw = desired_raw[tail_start_raw:]
        actual_tail_raw = actual_raw[tail_start_raw:]
        tail_tracking_error_m = float(
            np.max(np.linalg.norm(desired_tail_raw - actual_tail_raw, axis=1))
        )
        tail_start_block = max(0, int(math.ceil((1.0 - contract.tail_fraction) * (desired.shape[0] - 1))))
        desired_tail = desired[tail_start_block:]
        actual_tail = actual[tail_start_block:]
        desired_tail_length = _path_length(desired_tail)
        actual_tail_length = _path_length(actual_tail)
        if desired_tail_length <= 1.0e-12:
            tail_path_length_ratio = 1.0
        else:
            tail_path_length_ratio = actual_tail_length / desired_tail_length
        if desired_tail.shape[0] < 2:
            tail_tracking_rmse_m = float(
                np.sqrt(np.mean(np.sum((desired_tail - actual_tail) ** 2, axis=1)))
            )
        else:
            tail_tracking_rmse_m = float(
                np.sqrt(np.mean(np.sum((desired_tail - actual_tail) ** 2, axis=1)))
            )
        metrics = {
            "row_count": row_count,
            "desired_excursion_m": desired_excursion,
            "actual_excursion_m": actual_excursion,
            "excursion_ratio": actual_excursion / desired_excursion if desired_excursion > 0.0 else 0.0,
            "desired_path_length_m": desired_length,
            "actual_path_length_m": actual_length,
            "path_length_ratio": actual_length / desired_length if desired_length > 0.0 else 0.0,
            "tracking_rmse_m": float(np.sqrt(np.mean(np.sum((desired - actual) ** 2, axis=1)))),
            "tracking_error_m": tracking_error_m,
            "displacement_correlation": correlation,
            "tail_row_count": int(desired_tail_raw.shape[0]),
            "tail_sample_count": int(desired_tail.shape[0]),
            "desired_tail_path_length_m": desired_tail_length,
            "actual_tail_path_length_m": actual_tail_length,
            "tail_path_length_ratio": float(tail_path_length_ratio),
            "tail_tracking_error_m": tail_tracking_error_m,
            "tail_tracking_rmse_m": float(tail_tracking_rmse_m),
        }
    predicates = {
        "desired_path_complete": metrics["desired_excursion_m"] >= contract.minimum_desired_excursion_m,
        "actual_path_moved": metrics["actual_excursion_m"] >= contract.minimum_actual_excursion_m,
        "actual_excursion_ratio": metrics["excursion_ratio"] >= contract.minimum_excursion_ratio,
        "actual_path_length_ratio": (
            contract.minimum_path_length_ratio
            <= metrics["path_length_ratio"]
            <= contract.maximum_path_length_ratio
        ),
        "tracking_rmse_bounded": metrics["tracking_rmse_m"] <= contract.maximum_tracking_rmse_m,
        "tracking_error_bounded": metrics["tracking_error_m"] <= contract.maximum_tracking_error_m,
        "displacement_correlated": (
            metrics["displacement_correlation"] >= contract.minimum_displacement_correlation
        ),
        "tail_tracking_error_bounded": (
            metrics["tail_tracking_error_m"] <= contract.maximum_tail_tracking_error_m
        ),
        "tail_tracking_rmse_bounded": (
            metrics["tail_tracking_rmse_m"] <= contract.maximum_tail_tracking_rmse_m
        ),
        "tail_path_length_ratio_ok": (
            metrics["desired_tail_path_length_m"] <= 1.0e-12
            or metrics["tail_path_length_ratio"] >= contract.minimum_tail_path_length_ratio
        ),
    }
    return {
        "schema_version": FORMAL_TRACKING_QUALITY_SCHEMA_V2,
        "contract": contract.as_json(),
        "metrics": metrics,
        "predicates": predicates,
        "passed": bool(all(predicates.values())),
    }


def evaluate_formal_tracking_quality(
    frames: Iterable[object],
    *,
    planned_target_load_n: float,
    contract: FormalTrackingQualityContractV1 = FormalTrackingQualityContractV1(),
) -> dict[str, object]:
    """Evaluate measured TCP path and planned-bound Kunwei/applied loads.

    In each 42D observation slice, indices 18:24 are the desired pose and
    36:42 are ``desired - actual``.  Therefore the measured pose is recovered
    without introducing an unsealed side channel.  Kunwei is indices 0:6.
    """

    planned = float(planned_target_load_n)
    if not math.isfinite(planned) or planned not in FORMAL_PLANNED_TARGET_LOADS_N:
        raise ValueError("planned_target_load_n must be one of 3/5/8 N")

    rows = tuple(_payload(frame) for frame in frames)
    desired_positions: list[np.ndarray] = []
    actual_positions: list[np.ndarray] = []
    applied_vs_planned: list[float] = []
    kunwei_vs_planned: list[float] = []
    for row in rows:
        observation = np.asarray(row.get("observation_84d", ()), dtype=float)
        desired_pose = np.asarray(row.get("desired_pose_6d", ()), dtype=float)
        expert_action = np.asarray(row.get("expert_action_12d", ()), dtype=float)
        applied_action = np.asarray(row.get("applied_action_12d", ()), dtype=float)
        if (
            observation.shape != (84,)
            or desired_pose.shape != (6,)
            or expert_action.shape != (12,)
            or applied_action.shape != (12,)
        ):
            raise ValueError("formal tracking row dimensions are invalid")
        if (
            not np.isfinite(observation).all()
            or not np.isfinite(desired_pose).all()
            or not np.isfinite(expert_action).all()
            or not np.isfinite(applied_action).all()
        ):
            raise ValueError("formal tracking row contains non-finite values")
        tracking_error = observation[36:42]
        actual_pose = desired_pose - tracking_error
        desired_positions.append(desired_pose[:3])
        actual_positions.append(actual_pose[:3])
        rotation = _rotation_matrix_from_rotvec(desired_pose[3:])
        measured_wrench_base = rotation @ observation[0:3]
        applied_force_base = rotation @ applied_action[0:3]
        kunwei_normal = max(0.0, float(measured_wrench_base[2]))
        applied_normal = max(0.0, -float(applied_force_base[2]))
        applied_vs_planned.append(abs(applied_normal - planned))
        kunwei_vs_planned.append(abs(kunwei_normal - planned))

    if not desired_positions:
        geometry = evaluate_formal_path_geometry(
            np.zeros((0, 3)),
            np.zeros((0, 3)),
            contract=contract,
        )
    else:
        geometry = evaluate_formal_path_geometry(
            np.vstack(desired_positions),
            np.vstack(actual_positions),
            contract=contract,
        )

    if not applied_vs_planned:
        mean_applied = 1.0e9
        p95_applied = 1.0e9
        mean_kunwei = 1.0e9
        p95_kunwei = 1.0e9
    else:
        mean_applied = float(np.mean(applied_vs_planned))
        p95_applied = float(np.percentile(applied_vs_planned, 95))
        mean_kunwei = float(np.mean(kunwei_vs_planned))
        p95_kunwei = float(np.percentile(kunwei_vs_planned, 95))

    metrics = dict(geometry["metrics"])
    metrics.update(
        {
            "planned_target_load_n": planned,
            "mean_applied_vs_planned_load_error_n": mean_applied,
            "p95_applied_vs_planned_load_error_n": p95_applied,
            "mean_kunwei_vs_planned_load_error_n": mean_kunwei,
            "p95_kunwei_vs_planned_load_error_n": p95_kunwei,
            # Compatibility aliases: planned-bound Kunwei error is the formal load gate.
            "mean_normal_load_error_n": mean_kunwei,
            "p95_normal_load_error_n": p95_kunwei,
        }
    )
    predicates = dict(geometry["predicates"])
    predicates.update(
        {
            "planned_target_load_valid": planned in FORMAL_PLANNED_TARGET_LOADS_N,
            "mean_applied_vs_planned_load_bounded": (
                mean_applied <= contract.maximum_mean_normal_load_error_n
            ),
            "p95_applied_vs_planned_load_bounded": (
                p95_applied <= contract.maximum_p95_normal_load_error_n
            ),
            "mean_kunwei_vs_planned_load_bounded": (
                mean_kunwei <= contract.maximum_mean_normal_load_error_n
            ),
            "p95_kunwei_vs_planned_load_bounded": (
                p95_kunwei <= contract.maximum_p95_normal_load_error_n
            ),
            "mean_normal_load_error_bounded": mean_kunwei
            <= contract.maximum_mean_normal_load_error_n,
            "p95_normal_load_error_bounded": p95_kunwei
            <= contract.maximum_p95_normal_load_error_n,
        }
    )
    return {
        "schema_version": FORMAL_TRACKING_QUALITY_SCHEMA_V2,
        "contract": contract.as_json(),
        "metrics": metrics,
        "predicates": predicates,
        "passed": bool(all(predicates.values())),
    }


__all__ = [
    "FORMAL_PLANNED_TARGET_LOADS_N",
    "FORMAL_TRACKING_QUALITY_SCHEMA_V1",
    "FORMAL_TRACKING_QUALITY_SCHEMA_V2",
    "FormalTrackingQualityContractV1",
    "FormalTrackingQualityContractV2",
    "evaluate_formal_path_geometry",
    "evaluate_formal_tracking_quality",
]
