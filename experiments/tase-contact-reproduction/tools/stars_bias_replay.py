#!/usr/bin/env python3
"""Offline-only STARS-lite bias replay for Step5d-v3 CSV artifacts.

The tool reads completed run directories and writes new artifacts elsewhere.
It has no robot, dashboard, RTDE, Kunwei socket, or controller write path.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import numpy as np

from contact_mode_estimator import ContactModeObserver, EstimationMode, ModeConfig, Step5dV3Adapter


SCHEMA = "ur10e.stars-lite/replay-v1"
MANIFEST_SCHEMA = "ur10e.stars-lite/dataset-manifest-v1"
AXES = ("fx_n", "fy_n", "fz_n", "mx_nm", "my_nm", "mz_nm")
ZEROED_FIELDS = tuple(f"{axis}_zeroed" for axis in AXES)
BASELINE_FIELDS = tuple(f"bias_est_{axis}" for axis in AXES)
BRIDGE_REQUIRED = {
    "t_monotonic_s",
    "baseline_ready",
    "zero_event_id",
    "ur_output_double_register_35",
    "step4e_cmd_valid",
    "_step4e_path_time_s",
    *(f"ur_actual_TCP_speed_{idx}" for idx in range(6)),
}
SENSOR_REQUIRED = {"t_monotonic_s", "zero_event_id", *ZEROED_FIELDS, *BASELINE_FIELDS}


def _json_load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _json_dump(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(row: Mapping[str, Any], field: str, default: float = math.nan) -> float:
    try:
        value = float(row.get(field, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _headers(path: Path) -> set[str]:
    with path.open(newline="", encoding="utf-8") as stream:
        return set(next(csv.reader(stream)))


def _csv_row_count(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as stream:
        return sum(1 for _ in csv.DictReader(stream))


def _resolve_run(run_dir: Path) -> tuple[Path, Path, Path]:
    run_dir = run_dir.resolve()
    bridge = run_dir / "bridge_rtde_500hz.csv"
    sensor = run_dir / "kunwei_sensor_1khz.csv"
    metadata = run_dir / "metadata.json"
    missing = [str(path) for path in (bridge, sensor, metadata) if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"run is missing required files: {missing}")
    bridge_missing = sorted(BRIDGE_REQUIRED - _headers(bridge))
    sensor_missing = sorted(SENSOR_REQUIRED - _headers(sensor))
    if bridge_missing or sensor_missing:
        raise ValueError(
            f"ineligible run {run_dir}: bridge_missing={bridge_missing}, sensor_missing={sensor_missing}"
        )
    return bridge, sensor, metadata


def _prepare_output(path: Path) -> Path:
    path = path.resolve()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _require_outside_source(output_path: Path, source_root: Path) -> None:
    output = output_path.resolve()
    source = source_root.resolve()
    try:
        output.relative_to(source)
    except ValueError:
        return
    raise ValueError(f"output must be outside the read-only source tree: {source}")


def _causal_rows(bridge_path: Path, sensor_path: Path) -> Iterator[tuple[int, dict[str, str], int, dict[str, str]]]:
    """Join the latest sensor row at or before each bridge timestamp."""
    with bridge_path.open(newline="", encoding="utf-8") as bridge_stream, sensor_path.open(
        newline="", encoding="utf-8"
    ) as sensor_stream:
        bridges = csv.DictReader(bridge_stream)
        sensors = enumerate(csv.DictReader(sensor_stream), start=1)
        pending = next(sensors, None)
        latest: tuple[int, dict[str, str]] | None = None
        for bridge_index, bridge_row in enumerate(bridges, start=1):
            bridge_t = _finite(bridge_row, "t_monotonic_s")
            while pending is not None and _finite(pending[1], "t_monotonic_s") <= bridge_t:
                latest = pending
                pending = next(sensors, None)
            if latest is not None:
                yield bridge_index, bridge_row, latest[0], latest[1]


@dataclass(frozen=True)
class ReferenceTrace:
    run_dir: Path
    source_sha256: dict[str, str]
    profile: str
    path_shape: str
    phase: np.ndarray
    wrench: np.ndarray
    pose: np.ndarray
    tcp_speed: np.ndarray
    tcp_acceleration: np.ndarray
    wrench_covariance: np.ndarray

    @classmethod
    def from_run(cls, run_dir: Path, config: Mapping[str, Any]) -> "ReferenceTrace":
        bridge, sensor, metadata_path = _resolve_run(run_dir)
        metadata = _json_load(metadata_path)
        args = metadata.get("args", {})
        if not isinstance(args, Mapping):
            raise ValueError("reference metadata.args must be an object")
        profile = str(args.get("bridge_profile") or args.get("step4e_version") or "")
        required_token = str(config["required_profile_token"])
        if required_token not in profile:
            raise ValueError(
                f"reference profile must contain {required_token!r}, got {profile!r}"
            )
        path_shape = str(args.get("bridge_path_shape") or args.get("step4e_path_shape") or "")
        samples: dict[float, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        masked_rows = 0
        for _, bridge_row, _, sensor_row in _causal_rows(bridge, sensor):
            stage = _finite(bridge_row, "ur_output_double_register_35")
            phase = _finite(bridge_row, "_step4e_path_time_s")
            wrench = np.array([_finite(sensor_row, field) for field in ZEROED_FIELDS])
            pose = np.array([_finite(bridge_row, f"ur_actual_TCP_pose_{idx}") for idx in range(6)])
            speed = np.array([_finite(bridge_row, f"ur_actual_TCP_speed_{idx}") for idx in range(6)])
            acceleration = np.array(
                [_finite(bridge_row, f"ur_actual_TCP_accel_{idx}") for idx in range(6)]
            )
            if abs(stage - 25.0) < 0.05 and math.isfinite(phase):
                if _finite(sensor_row, "bias_estimation_contact_mask", 0.0) > 0.5:
                    masked_rows += 1
                if all(
                    np.all(np.isfinite(values))
                    for values in (wrench, pose, speed, acceleration)
                ):
                    samples[phase] = (wrench, pose, speed, acceleration)
        minimum_samples = int(config["minimum_samples"])
        if len(samples) < minimum_samples:
            raise ValueError(
                f"reference run has {len(samples)} usable stage25 samples; minimum is {minimum_samples}"
            )
        if masked_rows:
            raise ValueError(
                f"reference run contains {masked_rows} stage25 contact-masked rows"
            )
        ordered = sorted(samples.items())
        wrench = np.vstack([item[1][0] for item in ordered])
        covariance = np.cov(wrench, rowvar=False, ddof=1)
        covariance = np.atleast_2d(covariance)
        covariance = 0.5 * (covariance + covariance.T)
        if covariance.shape != (6, 6) or not np.all(np.isfinite(covariance)):
            raise ValueError("reference wrench covariance is not finite 6x6")
        covariance_min_eigenvalue = float(np.linalg.eigvalsh(covariance).min())
        if covariance_min_eigenvalue < -1.0e-12:
            raise ValueError(
                f"reference wrench covariance is not PSD: {covariance_min_eigenvalue}"
            )
        diagonal = np.diag(covariance)
        if np.any(diagonal[:3] > float(config["maximum_force_variance_n2"])):
            raise ValueError(f"reference force variance exceeds limit: {diagonal[:3].tolist()}")
        if np.any(diagonal[3:] > float(config["maximum_torque_variance_nm2"])):
            raise ValueError(f"reference torque variance exceeds limit: {diagonal[3:].tolist()}")
        return cls(
            run_dir=Path(run_dir).resolve(),
            source_sha256={
                "bridge_csv": _sha256(bridge),
                "sensor_csv": _sha256(sensor),
                "metadata": _sha256(metadata_path),
            },
            profile=profile,
            path_shape=path_shape,
            phase=np.array([item[0] for item in ordered]),
            wrench=wrench,
            pose=np.vstack([item[1][1] for item in ordered]),
            tcp_speed=np.vstack([item[1][2] for item in ordered]),
            tcp_acceleration=np.vstack([item[1][3] for item in ordered]),
            wrench_covariance=covariance,
        )

    def contains_phase(self, phase: float) -> bool:
        return math.isfinite(phase) and self.phase[0] <= phase <= self.phase[-1]

    def interpolate(self, values: np.ndarray, phase: float) -> np.ndarray:
        if not self.contains_phase(phase):
            raise ValueError(
                f"phase {phase} lies outside reference interval [{self.phase[0]}, {self.phase[-1]}]"
            )
        return np.array([np.interp(phase, self.phase, values[:, axis]) for axis in range(values.shape[1])])

    def wrench_at(self, phase: float) -> np.ndarray:
        return self.interpolate(self.wrench, phase)


def _rotvec_matrix(rotvec: np.ndarray) -> np.ndarray:
    angle = float(np.linalg.norm(rotvec))
    if angle < 1.0e-12:
        return np.eye(3)
    axis = rotvec / angle
    skew = np.array(
        [
            [0.0, -axis[2], axis[1]],
            [axis[2], 0.0, -axis[0]],
            [-axis[1], axis[0], 0.0],
        ]
    )
    return np.eye(3) + math.sin(angle) * skew + (1.0 - math.cos(angle)) * (skew @ skew)


def _rotation_distance(first: np.ndarray, second: np.ndarray) -> float:
    relative = _rotvec_matrix(first).T @ _rotvec_matrix(second)
    cosine = float(np.clip((np.trace(relative) - 1.0) / 2.0, -1.0, 1.0))
    return math.acos(cosine)


def validate_reference_pair(
    current_run_dir: Path,
    reference: ReferenceTrace,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    bridge, sensor, metadata_path = _resolve_run(current_run_dir)
    metadata = _json_load(metadata_path)
    args = metadata.get("args", {})
    current_shape = str(args.get("bridge_path_shape") or args.get("step4e_path_shape") or "")
    if not current_shape or current_shape != reference.path_shape:
        raise ValueError(
            f"reference path-shape mismatch: current={current_shape!r}, reference={reference.path_shape!r}"
        )
    stage25_rows = 0
    covered_rows = 0
    xy_errors: list[float] = []
    orientation_errors: list[float] = []
    linear_speed_errors: list[float] = []
    angular_speed_errors: list[float] = []
    linear_acceleration_errors: list[float] = []
    angular_acceleration_errors: list[float] = []
    z_offsets: list[float] = []
    for _, bridge_row, _, _ in _causal_rows(bridge, sensor):
        stage = _finite(bridge_row, "ur_output_double_register_35")
        phase = _finite(bridge_row, "_step4e_path_time_s")
        if abs(stage - 25.0) >= 0.05 or not math.isfinite(phase):
            continue
        stage25_rows += 1
        if phase < reference.phase[0] or phase > reference.phase[-1]:
            continue
        pose = np.array([_finite(bridge_row, f"ur_actual_TCP_pose_{idx}") for idx in range(6)])
        speed = np.array([_finite(bridge_row, f"ur_actual_TCP_speed_{idx}") for idx in range(6)])
        acceleration = np.array(
            [_finite(bridge_row, f"ur_actual_TCP_accel_{idx}") for idx in range(6)]
        )
        reference_pose = reference.interpolate(reference.pose, phase)
        reference_speed = reference.interpolate(reference.tcp_speed, phase)
        reference_acceleration = reference.interpolate(reference.tcp_acceleration, phase)
        if not all(
            np.all(np.isfinite(values)) for values in (pose, speed, acceleration)
        ):
            continue
        covered_rows += 1
        xy_errors.append(float(np.linalg.norm(pose[:2] - reference_pose[:2])))
        orientation_errors.append(_rotation_distance(pose[3:], reference_pose[3:]))
        linear_speed_errors.append(float(np.linalg.norm(speed[:3] - reference_speed[:3])))
        angular_speed_errors.append(float(np.linalg.norm(speed[3:] - reference_speed[3:])))
        linear_acceleration_errors.append(
            float(np.linalg.norm(acceleration[:3] - reference_acceleration[:3]))
        )
        angular_acceleration_errors.append(
            float(np.linalg.norm(acceleration[3:] - reference_acceleration[3:]))
        )
        z_offsets.append(float(pose[2] - reference_pose[2]))
    if stage25_rows == 0 or covered_rows == 0:
        raise ValueError("current/reference pair has no comparable stage25 geometry")
    coverage = covered_rows / stage25_rows
    rms = lambda values: math.sqrt(statistics.fmean(value * value for value in values))
    xy_rms = rms(xy_errors)
    orientation_rms = rms(orientation_errors)
    linear_speed_rms = rms(linear_speed_errors)
    angular_speed_rms = rms(angular_speed_errors)
    linear_acceleration_rms = rms(linear_acceleration_errors)
    angular_acceleration_rms = rms(angular_acceleration_errors)
    mean_offset = statistics.fmean(z_offsets)
    expected_offset = float(config["expected_current_minus_reference_z_m"])
    offset_errors = [value - expected_offset for value in z_offsets]
    offset_rms = rms(offset_errors)
    offset_max_deviation = max(abs(value) for value in offset_errors)
    checks = {
        "phase_coverage": coverage >= float(config["minimum_phase_coverage"]),
        "xy_rms": xy_rms <= float(config["xy_rms_tolerance_m"]),
        "orientation_rotvec_rms": orientation_rms <= float(config["orientation_rms_tolerance_rad"]),
        "linear_speed_rms": linear_speed_rms
        <= float(config["linear_speed_rms_tolerance_m_s"]),
        "angular_speed_rms": angular_speed_rms
        <= float(config["angular_speed_rms_tolerance_rad_s"]),
        "linear_acceleration_rms": linear_acceleration_rms
        <= float(config["linear_acceleration_rms_tolerance_m_s2"]),
        "angular_acceleration_rms": angular_acceleration_rms
        <= float(config["angular_acceleration_rms_tolerance_rad_s2"]),
        "signed_z_offset_mean": abs(mean_offset - expected_offset)
        <= float(config["z_offset_mean_tolerance_m"]),
        "z_offset_rms": offset_rms <= float(config["z_offset_rms_tolerance_m"]),
        "z_offset_max_deviation": offset_max_deviation
        <= float(config["z_offset_max_deviation_m"]),
    }
    result = {
        "status": "pass" if all(checks.values()) else "fail",
        "reference_run_dir": str(reference.run_dir),
        "reference_source_sha256": reference.source_sha256,
        "reference_profile": reference.profile,
        "path_shape": current_shape,
        "stage25_rows": stage25_rows,
        "covered_rows": covered_rows,
        "phase_coverage": coverage,
        "xy_rms_m": xy_rms,
        "orientation_rotvec_rms_rad": orientation_rms,
        "linear_speed_rms_m_s": linear_speed_rms,
        "angular_speed_rms_rad_s": angular_speed_rms,
        "linear_acceleration_rms_m_s2": linear_acceleration_rms,
        "angular_acceleration_rms_rad_s2": angular_acceleration_rms,
        "expected_current_minus_reference_z_m": expected_offset,
        "mean_z_offset_m": mean_offset,
        "z_offset_error_rms_m": offset_rms,
        "z_offset_max_deviation_m": offset_max_deviation,
        "reference_wrench_covariance": reference.wrench_covariance.tolist(),
        "reference_wrench_covariance_diagonal": np.diag(
            reference.wrench_covariance
        ).tolist(),
        "checks": checks,
    }
    if result["status"] != "pass":
        raise ValueError(f"reference geometry validation failed: {result}")
    return result


@dataclass
class GatedEma:
    tau: np.ndarray
    rate_limit: np.ndarray
    bias: np.ndarray

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "GatedEma":
        tau = np.array([float(config["force_tau_s"])] * 3 + [float(config["torque_tau_s"])] * 3)
        rate = np.array(
            [float(config["force_rate_limit_n_per_s"])] * 3
            + [float(config["torque_rate_limit_nm_per_s"])] * 3
        )
        if np.any(tau <= 0.0) or np.any(rate <= 0.0):
            raise ValueError("EMA tau and rate limits must be positive")
        return cls(tau=tau, rate_limit=rate, bias=np.zeros(6, dtype=float))

    def step(self, measurement: np.ndarray, dt_s: float, update_allowed: bool) -> np.ndarray:
        if update_allowed and dt_s > 0.0:
            alpha = 1.0 - np.exp(-dt_s / self.tau)
            delta = alpha * (measurement - self.bias)
            delta = np.clip(delta, -self.rate_limit * dt_s, self.rate_limit * dt_s)
            self.bias = self.bias + delta
        return self.bias.copy()


@dataclass
class BiasRateKalman:
    state: np.ndarray
    covariance: np.ndarray
    measurement_variance: np.ndarray
    process_variance: np.ndarray

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        q_multiplier: float = 1.0,
        measurement_variance: np.ndarray | None = None,
    ) -> "BiasRateKalman":
        bias_std = np.array(
            [float(config["initial_force_bias_std_n"])] * 3
            + [float(config["initial_torque_bias_std_nm"])] * 3
        )
        rate_std = np.array(
            [float(config["initial_force_rate_std_n_per_s"])] * 3
            + [float(config["initial_torque_rate_std_nm_per_s"])] * 3
        )
        variance_floor = np.array(
            [float(config["measurement_variance_floor_force_n2"])] * 3
            + [float(config["measurement_variance_floor_torque_nm2"])] * 3
        )
        if measurement_variance is None:
            measurement_variance = variance_floor
        else:
            measurement_variance = np.maximum(
                np.asarray(measurement_variance, dtype=float), variance_floor
            )
            if measurement_variance.shape != (6,) or not np.all(
                np.isfinite(measurement_variance)
            ):
                raise ValueError("measurement_variance must be a finite six-vector")
        process_variance = q_multiplier * np.array(
            [float(config["process_variance_floor_force_n2_per_s3"])] * 3
            + [float(config["process_variance_floor_torque_nm2_per_s3"])] * 3
        )
        covariance = np.diag(np.concatenate((bias_std**2, rate_std**2)))
        return cls(np.zeros(12), covariance, measurement_variance, process_variance)

    def step(
        self, measurement: np.ndarray, dt_s: float, update_allowed: bool
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float | None, float]:
        dt = max(float(dt_s), 0.0)
        transition = np.eye(12)
        transition[:6, 6:] = np.eye(6) * dt
        process = np.zeros((12, 12))
        for axis, variance in enumerate(self.process_variance):
            process[axis, axis] = variance * dt**3 / 3.0
            process[axis, 6 + axis] = variance * dt**2 / 2.0
            process[6 + axis, axis] = variance * dt**2 / 2.0
            process[6 + axis, 6 + axis] = variance * dt
        predicted_state = transition @ self.state
        predicted_covariance = transition @ self.covariance @ transition.T + process
        innovation = np.full(6, math.nan)
        nis: float | None = None
        observation = np.hstack((np.eye(6), np.zeros((6, 6))))
        residual_covariance = observation @ predicted_covariance @ observation.T + np.diag(
            self.measurement_variance
        )
        measurement_finite = np.all(np.isfinite(measurement))
        if measurement_finite:
            innovation = measurement - observation @ predicted_state
            nis = float(innovation @ np.linalg.solve(residual_covariance, innovation))
        if update_allowed:
            if not measurement_finite:
                raise ValueError("Kalman update requested with non-finite measurement")
            gain = np.linalg.solve(residual_covariance, observation @ predicted_covariance).T
            self.state = predicted_state + gain @ innovation
            identity = np.eye(12)
            joseph = identity - gain @ observation
            self.covariance = (
                joseph @ predicted_covariance @ joseph.T
                + gain @ np.diag(self.measurement_variance) @ gain.T
            )
            self.covariance = 0.5 * (self.covariance + self.covariance.T)
        else:
            self.state = predicted_state
            self.covariance = predicted_covariance
        min_eigenvalue = float(np.linalg.eigvalsh(self.covariance).min())
        return predicted_state[:6], self.state[:6].copy(), innovation, nis, min_eigenvalue


def _output_fields() -> list[str]:
    fields = [
        "bridge_row_index",
        "sensor_row_index",
        "bridge_t_monotonic_s",
        "sensor_t_monotonic_s",
        "sensor_age_s",
        "mode",
        "mode_confidence",
        "transition_id",
        "contact_mask",
        "update_allowed",
        "freeze_reason",
        "phase_s",
        "reference_domain_status",
        "reference_applied",
    ]
    for prefix in ("raw_zeroed", "logged_baseline", "reference_wrench", "reference_corrected", "ema_bias", "ema_corrected", "kf_pred_bias", "kf_post_bias", "kf_rate", "kf_corrected", "innovation", "kf_cov_diag"):
        fields.extend(f"{prefix}_{axis}" for axis in AXES)
    fields.extend(("nis", "covariance_min_eigenvalue"))
    return fields


def replay_run(
    run_dir: Path,
    output_dir: Path | None,
    config_path: Path,
    *,
    q_multiplier: float = 1.0,
    emit_csv: bool = True,
    reference_run_dir: Path | None = None,
    measurement_variance: np.ndarray | None = None,
) -> dict[str, Any]:
    bridge_path, sensor_path, metadata_path = _resolve_run(run_dir)
    config = _json_load(config_path)
    if config.get("schema") != "ur10e.stars-lite/config-v1":
        raise ValueError("unsupported STARS-lite config schema")
    metadata = _json_load(metadata_path)
    mode_config = ModeConfig.from_mapping(config["mode"])
    adapter = Step5dV3Adapter.from_metadata(
        metadata,
        base_config=mode_config,
        max_sensor_age_s=float(config["alignment"]["max_sensor_age_s"]),
    )
    observer = ContactModeObserver(adapter.mode_config)
    ema = GatedEma.from_config(config["ema"])
    kalman = BiasRateKalman.from_config(
        config["kalman"],
        q_multiplier=q_multiplier,
        measurement_variance=measurement_variance,
    )
    reference = (
        ReferenceTrace.from_run(reference_run_dir, config["reference"])
        if reference_run_dir is not None
        else None
    )
    reference_validation = (
        validate_reference_pair(run_dir, reference, config["reference"])
        if reference is not None
        else {"status": "not_evaluated"}
    )
    mode_counts: Counter[str] = Counter()
    freeze_counts: Counter[str] = Counter()
    nis_by_mode: dict[str, list[float]] = {}
    frozen_nis_values: list[float] = []
    innovation_norm_by_mode: dict[str, list[float]] = {}
    total_bridge_rows = _csv_row_count(bridge_path)
    row_count = 0
    valid_row_count = 0
    update_count = 0
    forbidden_update_count = 0
    future_sample_count = 0
    min_covariance_eigenvalue = math.inf
    static_force_errors: list[float] = []
    static_torque_errors: list[float] = []
    contact_retained_ratios: list[float] = []
    contact_direction_cosines: list[float] = []
    reference_applied_rows = 0
    reference_out_of_domain_rows = 0
    previous_bridge_t: float | None = None
    output_stream = None
    writer = None
    if emit_csv:
        if output_dir is None:
            raise ValueError("output_dir is required when emit_csv is true")
        _require_outside_source(output_dir, Path(run_dir))
        output_dir = _prepare_output(output_dir)
        output_stream = (output_dir / "corrected_wrench.csv").open("w", newline="", encoding="utf-8")
        writer = csv.DictWriter(output_stream, fieldnames=_output_fields())
        writer.writeheader()
    try:
        for bridge_index, bridge_row, sensor_index, sensor_row in _causal_rows(bridge_path, sensor_path):
            observation = adapter.observation(
                bridge_row, sensor_row, previous_bridge_t_s=previous_bridge_t
            )
            previous_bridge_t = observation.t_monotonic_s
            decision = observer.decide(observation)
            raw_measurement = np.array([_finite(sensor_row, field) for field in ZEROED_FIELDS])
            baseline = np.array([_finite(sensor_row, field) for field in BASELINE_FIELDS])
            reference_domain_status = "not_configured"
            reference_wrench = np.full(6, math.nan)
            if reference is not None:
                stage25 = abs(observation.stage - 25.0) < adapter.mode_config.stage_tolerance
                if not stage25:
                    reference_domain_status = "not_stage25"
                elif not reference.contains_phase(observation.phase_s):
                    reference_domain_status = "outside_validated_phase"
                    reference_out_of_domain_rows += 1
                else:
                    reference_domain_status = "applied"
                    reference_wrench = reference.wrench_at(observation.phase_s)
                    reference_applied_rows += 1
            measurement = (
                raw_measurement - reference_wrench
                if np.all(np.isfinite(reference_wrench))
                else raw_measurement
            )
            update_allowed = bool(decision.update_allowed)
            ema_bias = ema.step(measurement, observation.dt_s, update_allowed)
            predicted_bias, posterior_bias, innovation, nis, min_eigenvalue = kalman.step(
                measurement, observation.dt_s, update_allowed
            )
            corrected_ema = measurement - ema_bias
            corrected_kf = measurement - posterior_bias
            raw_norm = float(np.linalg.norm(measurement[:3]))
            corrected_norm = float(np.linalg.norm(corrected_kf[:3]))
            confirmed_contact = decision.mode in {
                EstimationMode.IMPACT,
                EstimationMode.CONTACT_TRACK,
            }
            minimum_contact_norm = float(config["promotion"]["minimum_contact_force_norm_n"])
            if confirmed_contact and raw_norm >= minimum_contact_norm:
                contact_retained_ratios.append(corrected_norm / raw_norm)
                if corrected_norm > 1.0e-12:
                    contact_direction_cosines.append(
                        float(np.dot(measurement[:3], corrected_kf[:3]) / (raw_norm * corrected_norm))
                    )
            if decision.mode == EstimationMode.FREE_STATIC:
                static_force_errors.extend(corrected_kf[:3].tolist())
                static_torque_errors.extend(corrected_kf[3:].tolist())
            mode_counts[decision.mode.value] += 1
            if decision.mode != EstimationMode.INVALID:
                valid_row_count += 1
            if nis is not None:
                nis_by_mode.setdefault(decision.mode.value, []).append(nis)
                if not update_allowed:
                    frozen_nis_values.append(nis)
            if np.all(np.isfinite(innovation)):
                innovation_norm_by_mode.setdefault(decision.mode.value, []).append(
                    float(np.linalg.norm(innovation))
                )
            if decision.freeze_reason:
                freeze_counts[decision.freeze_reason] += 1
            row_count += 1
            update_count += decision.update_allowed
            if decision.update_allowed and decision.mode != EstimationMode.FREE_STATIC:
                forbidden_update_count += 1
            if _finite(sensor_row, "t_monotonic_s") > observation.t_monotonic_s + 1.0e-12:
                future_sample_count += 1
            min_covariance_eigenvalue = min(min_covariance_eigenvalue, min_eigenvalue)
            if writer is not None:
                output: dict[str, Any] = {
                    "bridge_row_index": bridge_index,
                    "sensor_row_index": sensor_index,
                    "bridge_t_monotonic_s": f"{observation.t_monotonic_s:.12g}",
                    "sensor_t_monotonic_s": f"{_finite(sensor_row, 't_monotonic_s'):.12g}",
                    "sensor_age_s": f"{observation.t_monotonic_s - _finite(sensor_row, 't_monotonic_s'):.12g}",
                    "mode": decision.mode.value,
                    "mode_confidence": decision.confidence,
                    "transition_id": decision.transition_id,
                    "contact_mask": decision.contact_mask,
                    "update_allowed": decision.update_allowed,
                    "freeze_reason": decision.freeze_reason,
                    "phase_s": "" if decision.phase_s is None else f"{decision.phase_s:.12g}",
                    "reference_domain_status": reference_domain_status,
                    "reference_applied": int(reference_domain_status == "applied"),
                    "nis": "" if nis is None else f"{nis:.12g}",
                    "covariance_min_eigenvalue": f"{min_eigenvalue:.12g}",
                }
                vectors = {
                    "raw_zeroed": raw_measurement,
                    "logged_baseline": baseline,
                    "reference_wrench": reference_wrench,
                    "reference_corrected": measurement,
                    "ema_bias": ema_bias,
                    "ema_corrected": corrected_ema,
                    "kf_pred_bias": predicted_bias,
                    "kf_post_bias": posterior_bias,
                    "kf_rate": kalman.state[6:],
                    "kf_corrected": corrected_kf,
                    "innovation": innovation,
                    "kf_cov_diag": np.diag(kalman.covariance)[:6],
                }
                for prefix, vector in vectors.items():
                    for axis, value in zip(AXES, vector):
                        output[f"{prefix}_{axis}"] = "" if not math.isfinite(float(value)) else f"{float(value):.12g}"
                writer.writerow(output)
    finally:
        if output_stream is not None:
            output_stream.close()
    force_rmse = math.sqrt(statistics.fmean(value * value for value in static_force_errors)) if static_force_errors else None
    torque_rmse = math.sqrt(statistics.fmean(value * value for value in static_torque_errors)) if static_torque_errors else None
    retention = statistics.fmean(contact_retained_ratios) if contact_retained_ratios else None
    retention_min = min(contact_retained_ratios) if contact_retained_ratios else None
    retention_max = max(contact_retained_ratios) if contact_retained_ratios else None
    direction_min = min(contact_direction_cosines) if contact_direction_cosines else None
    free_static_nis_values = nis_by_mode.get(EstimationMode.FREE_STATIC.value, [])
    free_static_nis_p95 = (
        float(np.percentile(free_static_nis_values, 95))
        if free_static_nis_values
        else None
    )
    frozen_nis_p95 = (
        float(np.percentile(frozen_nis_values, 95)) if frozen_nis_values else None
    )
    promotion_eligible_contact_profile = "no_contact" not in adapter.bridge_profile
    reported_min_covariance_eigenvalue = (
        min_covariance_eigenvalue if math.isfinite(min_covariance_eigenvalue) else None
    )
    causal_coverage = row_count / total_bridge_rows if total_bridge_rows else 0.0
    alignment_config = config["alignment"]
    promotion_config = config["promotion"]
    software_pass = (
        total_bridge_rows > 0
        and row_count > 0
        and causal_coverage >= float(alignment_config["minimum_causal_coverage"])
        and valid_row_count >= int(alignment_config["minimum_valid_rows"])
        and forbidden_update_count == 0
        and future_sample_count == 0
        and math.isfinite(min_covariance_eigenvalue)
        and min_covariance_eigenvalue >= -1.0e-12
    )
    promotion_pass = (
        software_pass
        and promotion_eligible_contact_profile
        and force_rmse is not None
        and torque_rmse is not None
        and force_rmse <= float(promotion_config["static_force_rmse_max_n"])
        and torque_rmse <= float(promotion_config["static_torque_rmse_max_nm"])
        and len(free_static_nis_values)
        >= int(promotion_config["minimum_free_static_nis_rows"])
        and free_static_nis_p95 is not None
        and free_static_nis_p95
        <= float(promotion_config["free_static_nis_p95_max"])
        and len(contact_retained_ratios)
        >= int(promotion_config["minimum_confirmed_contact_rows"])
        and retention_min is not None
        and retention_min >= float(promotion_config["contact_force_retention_ratio_min"])
        and retention_max is not None
        and retention_max <= float(promotion_config["contact_force_retention_ratio_max"])
        and direction_min is not None
        and direction_min >= float(promotion_config["contact_force_direction_cosine_min"])
    )
    mode_diagnostics = {}
    for mode in sorted(set(nis_by_mode) | set(innovation_norm_by_mode)):
        nis_values = nis_by_mode.get(mode, [])
        innovation_values = innovation_norm_by_mode.get(mode, [])
        mode_diagnostics[mode] = {
            "nis_count": len(nis_values),
            "nis_mean": statistics.fmean(nis_values) if nis_values else None,
            "nis_p95": float(np.percentile(nis_values, 95)) if nis_values else None,
            "nis_max": max(nis_values) if nis_values else None,
            "innovation_norm_count": len(innovation_values),
            "innovation_norm_p95": float(np.percentile(innovation_values, 95))
            if innovation_values
            else None,
        }
    summary: dict[str, Any] = {
        "schema": SCHEMA,
        "run_dir": str(Path(run_dir).resolve()),
        "source": {
            "bridge_csv": {"path": str(bridge_path), "sha256": _sha256(bridge_path)},
            "sensor_csv": {"path": str(sensor_path), "sha256": _sha256(sensor_path)},
            "metadata": {"path": str(metadata_path), "sha256": _sha256(metadata_path)},
            "config": {"path": str(config_path.resolve()), "sha256": _sha256(config_path)},
        },
        "reference": reference_validation,
        "alignment": {
            "policy": "latest_sensor_at_or_before_bridge",
            "bridge_rows": total_bridge_rows,
            "causally_joined_rows": row_count,
            "causal_coverage": causal_coverage,
            "valid_rows": valid_row_count,
            "future_sample_count": future_sample_count,
        },
        "estimator_origin": "logged Kunwei channel coordinates; physical wrench origin unverified; no tip-wrench claim",
        "q_multiplier": q_multiplier,
        "rows": row_count,
        "updates": update_count,
        "forbidden_update_count": forbidden_update_count,
        "mode_counts": dict(sorted(mode_counts.items())),
        "freeze_reason_counts": dict(sorted(freeze_counts.items())),
        "mode_diagnostics": mode_diagnostics,
        "reference_application": {
            "applied_rows": reference_applied_rows,
            "out_of_domain_stage25_rows": reference_out_of_domain_rows,
        },
        "metrics": {
            "static_force_rmse_n": force_rmse,
            "static_torque_rmse_nm": torque_rmse,
            "free_static_nis_count": len(free_static_nis_values),
            "free_static_nis_p95": free_static_nis_p95,
            "frozen_nis_count": len(frozen_nis_values),
            "frozen_nis_p95": frozen_nis_p95,
            "contact_force_retention_ratio": retention,
            "contact_force_retention_ratio_min": retention_min,
            "contact_force_retention_ratio_max": retention_max,
            "contact_force_direction_cosine_min": direction_min,
            "confirmed_contact_rows": len(contact_retained_ratios),
            "promotion_eligible_contact_profile": promotion_eligible_contact_profile,
            "covariance_min_eigenvalue": reported_min_covariance_eigenvalue,
        },
        "acceptance": {
            "software": "pass" if software_pass else "fail",
            "kalman_promotion": "pass" if promotion_pass else "not_promoted",
            "reference_physical_validation": reference_validation["status"],
        },
        "scope": "offline_no_motion_only",
    }
    if emit_csv and output_dir is not None:
        _json_dump(output_dir / "summary.json", summary)
    return summary


def _static_measurement_differences(run_dir: Path, config_path: Path) -> np.ndarray:
    bridge_path, sensor_path, metadata_path = _resolve_run(run_dir)
    config = _json_load(config_path)
    metadata = _json_load(metadata_path)
    adapter = Step5dV3Adapter.from_metadata(
        metadata,
        base_config=ModeConfig.from_mapping(config["mode"]),
        max_sensor_age_s=float(config["alignment"]["max_sensor_age_s"]),
    )
    observer = ContactModeObserver(adapter.mode_config)
    previous_bridge_t: float | None = None
    previous_static: np.ndarray | None = None
    differences: list[np.ndarray] = []
    for _, bridge_row, _, sensor_row in _causal_rows(bridge_path, sensor_path):
        observation = adapter.observation(
            bridge_row, sensor_row, previous_bridge_t_s=previous_bridge_t
        )
        previous_bridge_t = observation.t_monotonic_s
        decision = observer.decide(observation)
        measurement = np.array([_finite(sensor_row, field) for field in ZEROED_FIELDS])
        if decision.mode == EstimationMode.FREE_STATIC and np.all(np.isfinite(measurement)):
            if previous_static is not None:
                differences.append(measurement - previous_static)
            previous_static = measurement
        else:
            previous_static = None
    return np.vstack(differences) if differences else np.empty((0, 6))


def fit_measurement_noise(
    records: Sequence[Mapping[str, Any]],
    runs_root: Path,
    config_path: Path,
    jobs: int,
) -> dict[str, Any]:
    train_records = [record for record in records if record.get("split") == "train"]
    if not train_records:
        raise ValueError("manifest has no train records")

    def collect(record: Mapping[str, Any]) -> np.ndarray:
        return _static_measurement_differences(
            runs_root / str(record["relative_run_dir"]), config_path
        )

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        chunks = list(pool.map(collect, train_records))
    nonempty = [chunk for chunk in chunks if chunk.size]
    differences = np.vstack(nonempty) if nonempty else np.empty((0, 6))
    config = _json_load(config_path)
    minimum = int(config["training"]["minimum_static_differences"])
    if len(differences) < minimum:
        raise ValueError(
            f"training produced {len(differences)} static differences; minimum is {minimum}"
        )
    variance_floor = np.array(
        [float(config["kalman"]["measurement_variance_floor_force_n2"])] * 3
        + [float(config["kalman"]["measurement_variance_floor_torque_nm2"])] * 3
    )
    fitted = np.maximum(np.var(differences, axis=0, ddof=1) / 2.0, variance_floor)
    return {
        "method": "train_split_contiguous_free_static_first_difference_variance_over_two",
        "train_runs": len(train_records),
        "contributing_train_runs": sum(bool(chunk.size) for chunk in chunks),
        "static_difference_count": len(differences),
        "measurement_variance": fitted.tolist(),
    }


def _canonical_run_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    reference = dict(summary["reference"])
    reference.pop("reference_run_dir", None)
    return {
        "schema": summary["schema"],
        "source_sha256": {
            key: value["sha256"] for key, value in summary["source"].items()
        },
        "reference": reference,
        "alignment": summary["alignment"],
        "estimator_origin": summary["estimator_origin"],
        "q_multiplier": summary["q_multiplier"],
        "rows": summary["rows"],
        "updates": summary["updates"],
        "forbidden_update_count": summary["forbidden_update_count"],
        "mode_counts": summary["mode_counts"],
        "freeze_reason_counts": summary["freeze_reason_counts"],
        "mode_diagnostics": summary["mode_diagnostics"],
        "reference_application": summary["reference_application"],
        "metrics": summary["metrics"],
        "acceptance": summary["acceptance"],
        "scope": summary["scope"],
    }


def _run_data_quality(
    run_dir: Path,
    config: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    bridge_path, sensor_path, metadata_path = _resolve_run(run_dir)
    total_bridge_rows = _csv_row_count(bridge_path)
    adapter = Step5dV3Adapter.from_metadata(
        _json_load(metadata_path),
        base_config=ModeConfig.from_mapping(config["mode"]),
        max_sensor_age_s=float(config["alignment"]["max_sensor_age_s"]),
    )
    observer = ContactModeObserver(adapter.mode_config)
    joined_rows = 0
    valid_rows = 0
    previous_bridge_t: float | None = None
    for _, bridge_row, _, sensor_row in _causal_rows(bridge_path, sensor_path):
        observation = adapter.observation(
            bridge_row, sensor_row, previous_bridge_t_s=previous_bridge_t
        )
        previous_bridge_t = observation.t_monotonic_s
        decision = observer.decide(observation)
        joined_rows += 1
        if decision.mode != EstimationMode.INVALID:
            valid_rows += 1
    causal_coverage = joined_rows / total_bridge_rows if total_bridge_rows else 0.0
    checks = {
        "nonempty": total_bridge_rows > 0 and joined_rows > 0,
        "causal_coverage": causal_coverage
        >= float(config["alignment"]["minimum_causal_coverage"]),
        "minimum_valid_rows": valid_rows
        >= int(config["alignment"]["minimum_valid_rows"]),
    }
    return all(checks.values()), {
        "bridge_rows": total_bridge_rows,
        "causally_joined_rows": joined_rows,
        "causal_coverage": causal_coverage,
        "valid_rows": valid_rows,
        "checks": checks,
    }


def inventory_runs(
    runs_root: Path,
    output_path: Path,
    config_path: Path,
) -> dict[str, Any]:
    runs_root = runs_root.resolve()
    output_path = output_path.resolve()
    _require_outside_source(output_path, runs_root)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_path}")
    config = _json_load(config_path)
    records: list[dict[str, Any]] = []
    analysis_excluded: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        try:
            bridge, sensor, metadata = _resolve_run(run_dir)
            metadata_payload = _json_load(metadata)
            profile = str(metadata_payload.get("args", {}).get("bridge_profile", ""))
            if not profile:
                raise ValueError("missing bridge profile")
            relative_run_dir = str(run_dir.relative_to(runs_root))
            files = {
                "bridge_sha256": _sha256(bridge),
                "sensor_sha256": _sha256(sensor),
                "metadata_sha256": _sha256(metadata),
            }
            identity = hashlib.sha256(
                (
                    relative_run_dir
                    + files["metadata_sha256"]
                    + files["bridge_sha256"]
                    + files["sensor_sha256"]
                ).encode()
            ).hexdigest()
            quality_ok, quality = _run_data_quality(run_dir, config)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            skipped[type(exc).__name__] += 1
            continue
        if not quality_ok:
            analysis_excluded.append(
                {
                    "relative_run_dir": relative_run_dir,
                    "profile": profile,
                    "identity_sha256": identity,
                    "files": files,
                    "data_quality": quality,
                }
            )
            continue
        scenario = "no_contact" if "no_contact" in profile else "contact_capable"
        records.append(
            {
                "relative_run_dir": relative_run_dir,
                "profile": profile,
                "scenario": scenario,
                "identity_sha256": identity,
                "files": files,
            }
        )
    for scenario in sorted({record["scenario"] for record in records}):
        group = sorted((record for record in records if record["scenario"] == scenario), key=lambda item: item["identity_sha256"])
        train_end = round(len(group) * 0.60)
        tune_end = train_end + round(len(group) * 0.20)
        for index, record in enumerate(group):
            record["split"] = "train" if index < train_end else "tune" if index < tune_end else "holdout"
    records.sort(key=lambda item: item["relative_run_dir"])
    counts = Counter((record["scenario"], record["split"]) for record in records)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "inventory_implementation_sha256": _sha256(Path(__file__)),
        "eligibility_config_sha256": _sha256(config_path),
        "schema_eligible_count": len(records) + len(analysis_excluded),
        "eligible_count": len(records),
        "records": records,
        "counts": {f"{scenario}.{split}": count for (scenario, split), count in sorted(counts.items())},
        "skipped": dict(sorted(skipped.items())),
        "analysis_excluded": analysis_excluded,
        "split_policy": "sha256-stratified 60/20/20 by scenario",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _json_dump(output_path, manifest)
    return manifest


def _validated_manifest_records(
    manifest: Mapping[str, Any], runs_root: Path
) -> list[dict[str, Any]]:
    raw_records = manifest.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("manifest has no records")
    raw_excluded = manifest.get("analysis_excluded")
    if not isinstance(raw_excluded, list):
        raise ValueError("manifest analysis_excluded must be a list")
    if manifest.get("eligible_count") != len(raw_records):
        raise ValueError("manifest eligible_count does not match records")
    if manifest.get("schema_eligible_count") != len(raw_records) + len(raw_excluded):
        raise ValueError("manifest schema_eligible_count does not match records and exclusions")
    records: list[dict[str, Any]] = []
    seen_relative: set[str] = set()
    seen_identity: set[str] = set()

    def validate_source(raw_record: Any) -> tuple[str, str, str]:
        if not isinstance(raw_record, Mapping):
            raise ValueError("manifest record must be an object")
        relative = str(raw_record.get("relative_run_dir", ""))
        relative_path = Path(relative)
        if not relative or relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(f"unsafe manifest relative_run_dir: {relative!r}")
        run_dir = (runs_root / relative_path).resolve()
        try:
            run_dir.relative_to(runs_root)
        except ValueError as exc:
            raise ValueError(f"manifest run escapes runs root: {relative!r}") from exc
        if relative in seen_relative:
            raise ValueError(f"duplicate manifest relative_run_dir: {relative}")
        seen_relative.add(relative)
        bridge, sensor, metadata = _resolve_run(run_dir)
        actual_files = {
            "bridge_sha256": _sha256(bridge),
            "sensor_sha256": _sha256(sensor),
            "metadata_sha256": _sha256(metadata),
        }
        expected_files = raw_record.get("files")
        if not isinstance(expected_files, Mapping) or any(
            expected_files.get(key) != value for key, value in actual_files.items()
        ):
            raise ValueError(f"manifest file hash mismatch: {relative}")
        actual_identity = hashlib.sha256(
            (
                relative
                + actual_files["metadata_sha256"]
                + actual_files["bridge_sha256"]
                + actual_files["sensor_sha256"]
            ).encode()
        ).hexdigest()
        if raw_record.get("identity_sha256") != actual_identity:
            raise ValueError(f"manifest identity hash mismatch: {relative}")
        if actual_identity in seen_identity:
            raise ValueError(f"duplicate manifest identity_sha256: {actual_identity}")
        seen_identity.add(actual_identity)
        metadata_payload = _json_load(metadata)
        metadata_args = metadata_payload.get("args", {})
        if not isinstance(metadata_args, Mapping):
            raise ValueError(f"manifest source metadata.args is invalid: {relative}")
        profile = str(metadata_args.get("bridge_profile", ""))
        if raw_record.get("profile") != profile:
            raise ValueError(f"manifest profile mismatch: {relative}")
        return relative, actual_identity, profile

    for raw_record in raw_records:
        relative, _, profile = validate_source(raw_record)
        scenario = "no_contact" if "no_contact" in profile else "contact_capable"
        if raw_record.get("scenario") != scenario:
            raise ValueError(f"manifest scenario mismatch: {relative}")
        if raw_record.get("split") not in {"train", "tune", "holdout"}:
            raise ValueError(f"manifest split is invalid: {relative}")
        records.append(dict(raw_record))
    for raw_record in raw_excluded:
        relative, _, _ = validate_source(raw_record)
        if not isinstance(raw_record.get("data_quality"), Mapping):
            raise ValueError(f"manifest exclusion data_quality is invalid: {relative}")
    return records


def evaluate_manifest(
    manifest_path: Path,
    runs_root: Path,
    output_dir: Path,
    config_path: Path,
    jobs: int,
) -> dict[str, Any]:
    manifest = _json_load(manifest_path)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("unsupported dataset manifest schema")
    if manifest.get("eligibility_config_sha256") != _sha256(config_path):
        raise ValueError("manifest eligibility config SHA does not match evaluation config")
    runs_root = runs_root.resolve()
    _require_outside_source(output_dir, runs_root)
    records = _validated_manifest_records(manifest, runs_root)
    output_dir = _prepare_output(output_dir)
    config = _json_load(config_path)
    q_values = [float(value) for value in config["kalman"]["q_sweep_multipliers"]]
    training = fit_measurement_noise(records, runs_root, config_path, jobs)
    measurement_variance = np.array(training["measurement_variance"])
    started_at = datetime.now(timezone.utc).isoformat()

    def score(record: Mapping[str, Any], q_value: float) -> tuple[str, float, dict[str, Any]]:
        summary = replay_run(
            runs_root / str(record["relative_run_dir"]),
            None,
            config_path,
            q_multiplier=q_value,
            emit_csv=False,
            measurement_variance=measurement_variance,
        )
        metrics = summary["metrics"]
        force = metrics["static_force_rmse_n"]
        torque = metrics["static_torque_rmse_nm"]
        value = math.inf if force is None or torque is None else float(force) + 10.0 * float(torque)
        return str(record["relative_run_dir"]), value, summary

    tune_records = [record for record in records if record.get("split") == "tune"]
    sweep: dict[str, Any] = {}
    for q_value in q_values:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(lambda record: score(record, q_value), tune_records))
        finite_scores = [value for _, value, _ in results if math.isfinite(value)]
        sweep[str(q_value)] = {
            "mean_score": statistics.fmean(finite_scores) if finite_scores else None,
            "scored_runs": len(finite_scores),
        }
    if not any(payload["mean_score"] is not None for payload in sweep.values()):
        raise ValueError("Q sweep has no tune runs with static RMSE")
    selected_q = min(
        q_values,
        key=lambda value: math.inf if sweep[str(value)]["mean_score"] is None else sweep[str(value)]["mean_score"],
    )
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        evaluated = list(pool.map(lambda record: score(record, selected_q), records))
    by_relative = {relative: _canonical_run_summary(summary) for relative, _, summary in evaluated}
    run_summaries = [
        {
            "relative_run_dir": record["relative_run_dir"],
            "scenario": record["scenario"],
            "split": record["split"],
            "summary": by_relative[record["relative_run_dir"]],
        }
        for record in records
    ]
    holdout = [item["summary"] for item in run_summaries if item["split"] == "holdout"]
    all_summaries = [item["summary"] for item in run_summaries]
    software_pass = bool(all_summaries) and all(
        item["acceptance"]["software"] == "pass" for item in all_summaries
    )
    promotion_pass = bool(holdout) and all(
        item["acceptance"]["kalman_promotion"] == "pass" for item in holdout
    )
    report = {
        "schema": "ur10e.stars-lite/evaluation-v1",
        "implementation_sha256": _sha256(Path(__file__)),
        "manifest_sha256": _sha256(manifest_path),
        "config_sha256": _sha256(config_path),
        "training": training,
        "q_sweep": sweep,
        "selected_q_multiplier": selected_q,
        "run_count": len(run_summaries),
        "holdout_count": len(holdout),
        "acceptance": {
            "offline_software": "pass" if software_pass else "fail",
            "kalman_promotion": "pass" if promotion_pass else "not_promoted",
            "reference_physical_validation": "not_evaluated",
        },
        "runs": run_summaries,
        "scope": "offline_no_motion_only",
    }
    _json_dump(output_dir / "evaluation.json", report)
    _json_dump(
        output_dir / "parallel_run_manifest.json",
        {
            "schema": "ur10e.parallel-run-manifest/v1",
            "contract_id": "ur10e_concurrency_contract_v1",
            "task": "stars_lite_evaluate",
            "resource_lane": "cpu_throughput",
            "claim_class": "offline_tooling_only",
            "dependencies": ["inventory", "train_measurement_noise", "q_sweep", "holdout"],
            "started_at": started_at,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": 0,
            "cpu_workers": jobs,
            "blas_threads_per_worker": 1,
            "output_paths": ["evaluation.json"],
        },
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    inventory = subcommands.add_parser("inventory", help="freeze eligible historical runs")
    inventory.add_argument("--runs-root", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)
    inventory.add_argument("--config", type=Path, required=True)
    replay = subcommands.add_parser("replay", help="replay one completed run")
    replay.add_argument("--run-dir", type=Path, required=True)
    replay.add_argument("--output-dir", type=Path, required=True)
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--q-multiplier", type=float, default=1.0)
    replay.add_argument("--reference-run-dir", type=Path)
    evaluate = subcommands.add_parser("evaluate", help="tune and evaluate a frozen manifest")
    evaluate.add_argument("--manifest", type=Path, required=True)
    evaluate.add_argument("--runs-root", type=Path, required=True)
    evaluate.add_argument("--output-dir", type=Path, required=True)
    evaluate.add_argument("--config", type=Path, required=True)
    evaluate.add_argument("--jobs", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "inventory":
        result = inventory_runs(args.runs_root, args.output, args.config)
    elif args.command == "replay":
        if not math.isfinite(args.q_multiplier) or args.q_multiplier <= 0.0:
            raise ValueError("q multiplier must be finite and positive")
        result = replay_run(
            args.run_dir,
            args.output_dir,
            args.config,
            q_multiplier=args.q_multiplier,
            reference_run_dir=args.reference_run_dir,
        )
    else:
        if not 1 <= args.jobs <= 14:
            raise ValueError("jobs must be between 1 and the approved 14 CPU-token limit")
        result = evaluate_manifest(
            args.manifest,
            args.runs_root,
            args.output_dir,
            args.config,
            args.jobs,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, FileExistsError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
