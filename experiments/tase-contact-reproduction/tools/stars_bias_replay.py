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
    path_shape: str
    phase: np.ndarray
    wrench: np.ndarray
    pose: np.ndarray
    linear_speed: np.ndarray

    @classmethod
    def from_run(cls, run_dir: Path) -> "ReferenceTrace":
        bridge, sensor, metadata_path = _resolve_run(run_dir)
        metadata = _json_load(metadata_path)
        args = metadata.get("args", {})
        path_shape = str(args.get("bridge_path_shape") or args.get("step4e_path_shape") or "")
        samples: dict[float, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for _, bridge_row, _, sensor_row in _causal_rows(bridge, sensor):
            stage = _finite(bridge_row, "ur_output_double_register_35")
            phase = _finite(bridge_row, "_step4e_path_time_s")
            wrench = np.array([_finite(sensor_row, field) for field in ZEROED_FIELDS])
            pose = np.array([_finite(bridge_row, f"ur_actual_TCP_pose_{idx}") for idx in range(6)])
            speed = np.array([_finite(bridge_row, f"ur_actual_TCP_speed_{idx}") for idx in range(3)])
            if abs(stage - 25.0) < 0.05 and math.isfinite(phase) and np.all(np.isfinite(wrench)):
                samples[phase] = (wrench, pose, speed)
        if len(samples) < 2:
            raise ValueError(f"reference run has fewer than two phase-indexed stage25 samples: {run_dir}")
        ordered = sorted(samples.items())
        return cls(
            run_dir=Path(run_dir).resolve(),
            path_shape=path_shape,
            phase=np.array([item[0] for item in ordered]),
            wrench=np.vstack([item[1][0] for item in ordered]),
            pose=np.vstack([item[1][1] for item in ordered]),
            linear_speed=np.vstack([item[1][2] for item in ordered]),
        )

    def interpolate(self, values: np.ndarray, phase: float) -> np.ndarray:
        return np.array([np.interp(phase, self.phase, values[:, axis]) for axis in range(values.shape[1])])

    def wrench_at(self, phase: float) -> np.ndarray:
        return self.interpolate(self.wrench, phase)


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
    speed_errors: list[float] = []
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
        speed = np.array([_finite(bridge_row, f"ur_actual_TCP_speed_{idx}") for idx in range(3)])
        reference_pose = reference.interpolate(reference.pose, phase)
        reference_speed = reference.interpolate(reference.linear_speed, phase)
        if not np.all(np.isfinite(pose)) or not np.all(np.isfinite(speed)):
            continue
        covered_rows += 1
        xy_errors.append(float(np.linalg.norm(pose[:2] - reference_pose[:2])))
        orientation_errors.append(float(np.linalg.norm(pose[3:] - reference_pose[3:])))
        speed_errors.append(float(np.linalg.norm(speed - reference_speed)))
        z_offsets.append(float(pose[2] - reference_pose[2]))
    if stage25_rows == 0 or covered_rows == 0:
        raise ValueError("current/reference pair has no comparable stage25 geometry")
    coverage = covered_rows / stage25_rows
    rms = lambda values: math.sqrt(statistics.fmean(value * value for value in values))
    xy_rms = rms(xy_errors)
    orientation_rms = rms(orientation_errors)
    speed_rms = rms(speed_errors)
    mean_offset = statistics.fmean(z_offsets)
    checks = {
        "phase_coverage": coverage >= float(config["minimum_phase_coverage"]),
        "xy_rms": xy_rms <= float(config["xy_rms_tolerance_m"]),
        "orientation_rotvec_rms": orientation_rms <= float(config["orientation_rms_tolerance_rad"]),
        "linear_speed_rms": speed_rms <= float(config["linear_speed_rms_tolerance_m_s"]),
        "z_offset": abs(abs(mean_offset) - float(config["nominal_offset_m"]))
        <= float(config["offset_tolerance_m"]),
    }
    result = {
        "status": "pass" if all(checks.values()) else "fail",
        "reference_run_dir": str(reference.run_dir),
        "path_shape": current_shape,
        "stage25_rows": stage25_rows,
        "covered_rows": covered_rows,
        "phase_coverage": coverage,
        "xy_rms_m": xy_rms,
        "orientation_rotvec_rms_rad": orientation_rms,
        "linear_speed_rms_m_s": speed_rms,
        "mean_z_offset_m": mean_offset,
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
    def from_config(cls, config: Mapping[str, Any], q_multiplier: float = 1.0) -> "BiasRateKalman":
        bias_std = np.array(
            [float(config["initial_force_bias_std_n"])] * 3
            + [float(config["initial_torque_bias_std_nm"])] * 3
        )
        rate_std = np.array(
            [float(config["initial_force_rate_std_n_per_s"])] * 3
            + [float(config["initial_torque_rate_std_nm_per_s"])] * 3
        )
        measurement_variance = np.array(
            [float(config["measurement_variance_floor_force_n2"])] * 3
            + [float(config["measurement_variance_floor_torque_nm2"])] * 3
        )
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
        if update_allowed:
            observation = np.hstack((np.eye(6), np.zeros((6, 6))))
            innovation = measurement - observation @ predicted_state
            residual_covariance = observation @ predicted_covariance @ observation.T + np.diag(
                self.measurement_variance
            )
            gain = np.linalg.solve(residual_covariance, observation @ predicted_covariance).T
            self.state = predicted_state + gain @ innovation
            identity = np.eye(12)
            joseph = identity - gain @ observation
            self.covariance = (
                joseph @ predicted_covariance @ joseph.T
                + gain @ np.diag(self.measurement_variance) @ gain.T
            )
            self.covariance = 0.5 * (self.covariance + self.covariance.T)
            nis = float(innovation @ np.linalg.solve(residual_covariance, innovation))
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
    kalman = BiasRateKalman.from_config(config["kalman"], q_multiplier=q_multiplier)
    reference = ReferenceTrace.from_run(reference_run_dir) if reference_run_dir is not None else None
    reference_validation = (
        validate_reference_pair(run_dir, reference, config["reference"])
        if reference is not None
        else {"status": "not_evaluated"}
    )
    mode_counts: Counter[str] = Counter()
    freeze_counts: Counter[str] = Counter()
    row_count = 0
    update_count = 0
    forbidden_update_count = 0
    future_sample_count = 0
    min_covariance_eigenvalue = math.inf
    static_force_errors: list[float] = []
    static_torque_errors: list[float] = []
    contact_retained_ratios: list[float] = []
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
            reference_wrench = (
                reference.wrench_at(observation.phase_s)
                if reference is not None and math.isfinite(observation.phase_s)
                else np.full(6, math.nan)
            )
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
            if decision.contact_mask and raw_norm > 1.0e-12:
                contact_retained_ratios.append(corrected_norm / raw_norm)
            if decision.mode == EstimationMode.FREE_STATIC:
                static_force_errors.extend(corrected_kf[:3].tolist())
                static_torque_errors.extend(corrected_kf[3:].tolist())
            mode_counts[decision.mode.value] += 1
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
    software_pass = forbidden_update_count == 0 and future_sample_count == 0 and min_covariance_eigenvalue >= -1.0e-12
    promotion_pass = (
        software_pass
        and force_rmse is not None
        and torque_rmse is not None
        and force_rmse <= 0.10
        and torque_rmse <= 0.01
        and (retention is None or retention >= 0.95)
    )
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
        "alignment": {"policy": "latest_sensor_at_or_before_bridge", "future_sample_count": future_sample_count},
        "estimator_origin": "logged sensor frame/origin; no tip-wrench claim",
        "q_multiplier": q_multiplier,
        "rows": row_count,
        "updates": update_count,
        "forbidden_update_count": forbidden_update_count,
        "mode_counts": dict(sorted(mode_counts.items())),
        "freeze_reason_counts": dict(sorted(freeze_counts.items())),
        "metrics": {
            "static_force_rmse_n": force_rmse,
            "static_torque_rmse_nm": torque_rmse,
            "contact_force_retention_ratio": retention,
            "covariance_min_eigenvalue": min_covariance_eigenvalue,
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


def inventory_runs(runs_root: Path, output_path: Path) -> dict[str, Any]:
    runs_root = runs_root.resolve()
    output_path = output_path.resolve()
    _require_outside_source(output_path, runs_root)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_path}")
    records: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        try:
            bridge, sensor, metadata = _resolve_run(run_dir)
            metadata_payload = _json_load(metadata)
            profile = str(metadata_payload.get("args", {}).get("bridge_profile", ""))
            if not profile:
                raise ValueError("missing bridge profile")
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            skipped[type(exc).__name__] += 1
            continue
        scenario = "no_contact" if "no_contact" in profile else "contact_capable"
        identity = hashlib.sha256(
            (str(run_dir.relative_to(runs_root)) + _sha256(metadata) + _sha256(bridge) + _sha256(sensor)).encode()
        ).hexdigest()
        records.append(
            {
                "run_dir": str(run_dir),
                "relative_run_dir": str(run_dir.relative_to(runs_root)),
                "profile": profile,
                "scenario": scenario,
                "identity_sha256": identity,
                "files": {
                    "bridge_sha256": _sha256(bridge),
                    "sensor_sha256": _sha256(sensor),
                    "metadata_sha256": _sha256(metadata),
                },
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
        "runs_root": str(runs_root),
        "eligible_count": len(records),
        "records": records,
        "counts": {f"{scenario}.{split}": count for (scenario, split), count in sorted(counts.items())},
        "skipped": dict(sorted(skipped.items())),
        "split_policy": "sha256-stratified 60/20/20 by scenario",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _json_dump(output_path, manifest)
    return manifest


def evaluate_manifest(
    manifest_path: Path,
    output_dir: Path,
    config_path: Path,
    jobs: int,
) -> dict[str, Any]:
    manifest = _json_load(manifest_path)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("unsupported dataset manifest schema")
    _require_outside_source(output_dir, Path(manifest["runs_root"]))
    output_dir = _prepare_output(output_dir)
    records = list(manifest.get("records", []))
    q_values = [float(value) for value in _json_load(config_path)["kalman"]["q_sweep_multipliers"]]

    def score(record: Mapping[str, Any], q_value: float) -> tuple[str, float, dict[str, Any]]:
        summary = replay_run(Path(record["run_dir"]), None, config_path, q_multiplier=q_value, emit_csv=False)
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
    selected_q = min(
        q_values,
        key=lambda value: math.inf if sweep[str(value)]["mean_score"] is None else sweep[str(value)]["mean_score"],
    )
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        evaluated = list(pool.map(lambda record: score(record, selected_q), records))
    by_relative = {relative: summary for relative, _, summary in evaluated}
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
    software_pass = all(item["acceptance"]["software"] == "pass" for item in holdout)
    promotion_pass = bool(holdout) and all(
        item["acceptance"]["kalman_promotion"] == "pass" for item in holdout
    )
    report = {
        "schema": "ur10e.stars-lite/evaluation-v1",
        "manifest": {"path": str(manifest_path.resolve()), "sha256": _sha256(manifest_path)},
        "config": {"path": str(config_path.resolve()), "sha256": _sha256(config_path)},
        "jobs": jobs,
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
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    inventory = subcommands.add_parser("inventory", help="freeze eligible historical runs")
    inventory.add_argument("--runs-root", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)
    replay = subcommands.add_parser("replay", help="replay one completed run")
    replay.add_argument("--run-dir", type=Path, required=True)
    replay.add_argument("--output-dir", type=Path, required=True)
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--q-multiplier", type=float, default=1.0)
    replay.add_argument("--reference-run-dir", type=Path)
    evaluate = subcommands.add_parser("evaluate", help="tune and evaluate a frozen manifest")
    evaluate.add_argument("--manifest", type=Path, required=True)
    evaluate.add_argument("--output-dir", type=Path, required=True)
    evaluate.add_argument("--config", type=Path, required=True)
    evaluate.add_argument("--jobs", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "inventory":
        result = inventory_runs(args.runs_root, args.output)
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
        result = evaluate_manifest(args.manifest, args.output_dir, args.config, args.jobs)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, FileExistsError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
