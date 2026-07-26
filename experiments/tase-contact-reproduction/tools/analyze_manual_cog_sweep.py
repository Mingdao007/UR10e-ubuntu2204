#!/usr/bin/env python3
"""Offline analysis of a static UR10e/Kunwei manual CoG sweep.

This module deliberately has no Pinocchio import at module import time.  The
CSV loading and Kunwei regression are dependency-light and remain usable in a
pure regression test environment; the calibrated UR regression is loaded only
from :func:`load_calibrated_model` when the CLI is executed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np


POSE_NAMES = ("P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P0R")
TRAINING_POSES = ("P0", "P1", "P2", "P3", "P4", "P5", "P6")
HELD_OUT_POSES = ("P0R", "P7", "P8")
POST_VALIDATION_POSES = POSE_NAMES[:-1]  # P0..P8; P0R is a repeat, not a refit pose.
GRAVITY_M_S2 = 9.80665
DEFAULT_CALIBRATION_YAML = Path(
    "/home/andy/ur10e_ros2_ws/src/ur10e_bringup/config/ur10e_calibration.yaml"
)
CONTROLLER_PAYLOAD_KG = 0.401
CONTROLLER_COG_TOOL0_M = np.array([0.0, 0.0, 0.025], dtype=float)
MIN_RTDE_ROWS = 2000
MIN_KUNWEI_ROWS = 4000
MIN_KUNWEI_RATE_HZ = 950.0
MAX_STATIC_JOINT_SPEED_RAD_S = 0.001
MAX_STATIC_TCP_SPEED_M_S = 0.001
MAX_STATIC_TCP_TRANSLATION_M = 0.0002
GROSS_PAYLOAD_MASS_DELTA_KG = 0.05
GROSS_PAYLOAD_COG_DELTA_M = 0.01
MAX_HELD_OUT_UR_RESIDUAL_NM = 0.05

_Q_RE = re.compile(r"^actual_q_(\d+)$")
_POSE_RE = re.compile(r"^actual_TCP_pose_(\d+)$")
_TARGET_RE = re.compile(r"^target_moment_(\d+)$")


class DataQualityError(ValueError):
    """Raised when the input bundle cannot support a reproducible fit."""


@dataclass(frozen=True)
class CsvRead:
    path: Path
    columns: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]
    parse_errors: int
    nonfinite_rows: int
    dropped_rows: int


@dataclass(frozen=True)
class PoseObservation:
    name: str
    rotation_base_to_sensor: np.ndarray
    force_manual: np.ndarray
    torque_manual: np.ndarray
    joint_mean_rad: np.ndarray
    joint_columns: tuple[str, ...]
    target_moment_nm: np.ndarray
    source_quality: Mapping[str, Any]


def _json_default(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite")
    return number


def read_numeric_csv(path: Path, required_columns: Sequence[str]) -> CsvRead:
    """Read required numeric fields while accounting for malformed rows."""

    if not path.is_file():
        raise DataQualityError(f"missing CSV: {path}")
    rows: list[Mapping[str, str]] = []
    parse_errors = 0
    nonfinite_rows = 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise DataQualityError(f"empty CSV: {path}") from exc
        if len(set(header)) != len(header):
            raise DataQualityError(f"duplicate CSV columns: {path}")
        missing = [column for column in required_columns if column not in header]
        if missing:
            raise DataQualityError(f"{path} missing required columns: {missing}")
        header_tuple = tuple(header)
        for values in reader:
            if len(values) != len(header):
                parse_errors += 1
                continue
            row = dict(zip(header, values))
            try:
                for column in required_columns:
                    _finite_float(row[column])
            except (TypeError, ValueError):
                if row.get(column, "").strip().lower() in {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
                    nonfinite_rows += 1
                else:
                    parse_errors += 1
                continue
            rows.append(row)
    dropped_rows = parse_errors + nonfinite_rows
    return CsvRead(path, header_tuple, tuple(rows), parse_errors, nonfinite_rows, dropped_rows)


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DataQualityError(f"missing JSON: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataQualityError(f"cannot parse JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DataQualityError(f"JSON root must be an object: {path}")
    return value


def _indexed_columns(columns: Iterable[str], regex: re.Pattern[str], label: str) -> tuple[str, ...]:
    found = [(int(match.group(1)), column) for column in columns if (match := regex.match(column))]
    found.sort()
    indices = [index for index, _ in found]
    if indices != list(range(6)):
        raise DataQualityError(f"{label} columns must be indices 0..5, got {indices}")
    return tuple(column for _, column in found)


def rotvec_to_matrix(rotvec: Sequence[float]) -> np.ndarray:
    vector = np.asarray(rotvec, dtype=float)
    theta = float(np.linalg.norm(vector))
    if theta < 1e-12:
        return np.eye(3)
    axis = vector / theta
    skew = np.array(
        [[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]], [-axis[1], axis[0], 0.0]],
        dtype=float,
    )
    return np.eye(3) + math.sin(theta) * skew + (1.0 - math.cos(theta)) * (skew @ skew)


def skew(vector: Sequence[float]) -> np.ndarray:
    x, y, z = np.asarray(vector, dtype=float)
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=float)


def _mean_fields(rows: Sequence[Mapping[str, str]], columns: Sequence[str]) -> np.ndarray:
    return np.asarray([[float(row[column]) for column in columns] for row in rows], dtype=float).mean(axis=0)


def load_pose_observation(run_dir: Path, pose_name: str) -> PoseObservation:
    pose_dir = run_dir / pose_name
    rtde_path = pose_dir / "rtde" / "normal_position_control_rtde.csv"
    kunwei_path = pose_dir / "kunwei" / "data.csv"
    evidence_path = pose_dir / "rtde" / "evidence.json"
    summary_path = pose_dir / "kunwei" / "summary.json"
    evidence = load_json(evidence_path)
    summary = load_json(summary_path)

    rtde_header = read_numeric_csv(rtde_path, ()).columns
    q_columns = _indexed_columns(rtde_header, _Q_RE, "actual joint")
    pose_columns = _indexed_columns(rtde_header, _POSE_RE, "actual TCP pose")
    target_columns = _indexed_columns(rtde_header, _TARGET_RE, "target moment")
    optional_speed_columns = tuple(f"actual_TCP_speed_{index}" for index in range(6))
    optional_qd_columns = tuple(f"actual_qd_{index}" for index in range(6))
    optional_present = tuple(
        column
        for column in (optional_speed_columns + optional_qd_columns)
        if column in rtde_header
    )
    rtde_required = q_columns + pose_columns + target_columns + optional_present
    rtde = read_numeric_csv(rtde_path, rtde_required)
    kunwei_force = tuple(f"{axis}_kg_manual" for axis in ("Fx", "Fy", "Fz"))
    kunwei_torque = tuple(f"{axis}_kg_m_manual" for axis in ("Mx", "My", "Mz"))
    kunwei = read_numeric_csv(kunwei_path, kunwei_force + kunwei_torque)
    if not rtde.rows or not kunwei.rows:
        raise DataQualityError(f"{pose_name}: no usable rows")

    source_quality: dict[str, Any] = {
        "actual_pose_name": pose_name,
        "joint_columns": list(q_columns),
        "rtde": {
            "path": str(rtde_path.resolve()),
            "row_count": len(rtde.rows),
            "parse_errors": rtde.parse_errors,
            "nonfinite_rows": rtde.nonfinite_rows,
            "dropped_rows": rtde.dropped_rows,
        },
        "kunwei": {
            "path": str(kunwei_path.resolve()),
            "row_count": len(kunwei.rows),
            "parse_errors": kunwei.parse_errors,
            "nonfinite_rows": kunwei.nonfinite_rows,
            "dropped_rows": kunwei.dropped_rows,
        },
        "summary_samples": summary.get("samples"),
        "kunwei_rate_hz": summary.get("rate_hz_by_first_last"),
        "kunwei_summary_parse_errors": summary.get("parse_errors"),
        "kunwei_summary_dropped_sync_bytes": summary.get("dropped_sync_bytes"),
        "kunwei_stop_reason": summary.get("stop_reason"),
        "evidence_sample_count": evidence.get("sample_count"),
        "evidence": {
            "ok": evidence.get("ok"),
            "motion_performed": evidence.get("motion_performed"),
            "maximum_joint_speed_rad_s": evidence.get("maximum_joint_speed_rad_s"),
            "maximum_tcp_speed_m_s": evidence.get("maximum_tcp_speed_m_s"),
            "maximum_tcp_translation_m": evidence.get("maximum_tcp_translation_m"),
        },
    }
    if summary.get("samples") is not None and int(summary["samples"]) != len(kunwei.rows):
        raise DataQualityError(f"{pose_name}: summary samples do not match Kunwei rows")
    if evidence.get("sample_count") is not None and int(evidence["sample_count"]) != len(rtde.rows):
        raise DataQualityError(f"{pose_name}: evidence sample_count does not match RTDE rows")
    if any(
        item["parse_errors"] or item["nonfinite_rows"]
        for item in source_quality.values()
        if isinstance(item, dict) and "parse_errors" in item
    ):
        raise DataQualityError(f"{pose_name}: parse/nonfinite/drop findings prevent fitting")

    q = _mean_fields(rtde.rows, q_columns)
    tcp_pose = _mean_fields(rtde.rows, pose_columns)
    target = _mean_fields(rtde.rows, target_columns)
    force = _mean_fields(kunwei.rows, kunwei_force)
    torque = _mean_fields(kunwei.rows, kunwei_torque)
    if all(column in rtde_header for column in optional_speed_columns):
        speed = _mean_fields(rtde.rows, optional_speed_columns)
        source_quality["observed_mean_tcp_speed"] = speed.tolist()
    if all(column in rtde_header for column in optional_qd_columns):
        qd = _mean_fields(rtde.rows, optional_qd_columns)
        source_quality["observed_mean_joint_speed"] = qd.tolist()
    return PoseObservation(
        pose_name,
        rotvec_to_matrix(tcp_pose[3:]),
        force,
        torque,
        q,
        q_columns,
        target,
        source_quality,
    )


def split_pose_names(actual_pose_names: Sequence[str]) -> dict[str, list[str]]:
    actual = list(actual_pose_names)
    missing = [name for name in POSE_NAMES if name not in actual]
    unexpected = [name for name in actual if name not in POSE_NAMES]
    if missing:
        raise DataQualityError(f"missing required pose directories: {missing}")
    if unexpected:
        raise DataQualityError(f"unexpected pose directories: {unexpected}")
    return {
        "training": list(TRAINING_POSES),
        "held_out": list(HELD_OUT_POSES),
        "all_unique_pose_refit": list(POST_VALIDATION_POSES),
        "excluded_repeat": ["P0R"],
    }


def _fit_linear(design: np.ndarray, values: np.ndarray, parameter_count: int) -> tuple[np.ndarray, dict[str, Any]]:
    if design.shape[0] < parameter_count:
        raise ValueError(f"insufficient observations: {design.shape[0]} < {parameter_count}")
    rank = int(np.linalg.matrix_rank(design))
    if rank < parameter_count:
        raise ValueError(f"rank deficient design: rank={rank}, expected={parameter_count}")
    coefficients, _, _, singular_values = np.linalg.lstsq(design, values, rcond=None)
    condition = float(np.linalg.cond(design))
    return coefficients, {
        "rank": rank,
        "condition_number": condition,
        "singular_values": singular_values.tolist(),
        "observations": int(design.shape[0]),
        "parameters": parameter_count,
    }


def _stack_wrench_design(observations: Sequence[PoseObservation]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    force_design: list[np.ndarray] = []
    torque_design: list[np.ndarray] = []
    force_values: list[np.ndarray] = []
    torque_values: list[np.ndarray] = []
    for observation in observations:
        gravity_sensor = observation.rotation_base_to_sensor.T @ np.array([0.0, 0.0, -1.0])
        force_design.append(np.hstack((np.eye(3), gravity_sensor.reshape(3, 1))))
        # h cross g = -[g]x h.  The first three columns are b_t.
        torque_design.append(np.hstack((np.eye(3), -skew(gravity_sensor))))
        force_values.append(observation.force_manual)
        torque_values.append(observation.torque_manual)
    return (
        np.vstack(force_design),
        np.vstack(torque_design),
        np.concatenate(force_values),
        np.concatenate(torque_values),
    )


def summarize_residuals(residuals: np.ndarray, scale: float, unit: str) -> dict[str, Any]:
    residuals = np.asarray(residuals, dtype=float)
    scaled = residuals * scale
    norms = np.linalg.norm(scaled, axis=1)
    return {
        "unit": unit,
        "rms_by_axis": np.sqrt(np.mean(scaled * scaled, axis=0)).tolist(),
        "rms_all_components": float(np.sqrt(np.mean(scaled * scaled))),
        "rms_vector_norm": float(np.sqrt(np.mean(norms * norms))),
        "mean_vector": np.mean(scaled, axis=0).tolist(),
        "max_vector_norm": float(np.max(norms)),
        "sample_count": int(len(scaled)),
    }


def fit_kunwei_model(observations: Sequence[PoseObservation], gravity_m_s2: float = GRAVITY_M_S2) -> dict[str, Any]:
    """Fit the raw manual-unit Kunwei force/torque model from pose means."""

    if not observations:
        raise ValueError("no observations")
    force_design, torque_design, force_values, torque_values = _stack_wrench_design(observations)
    force_coeff, force_diag = _fit_linear(force_design, force_values, 4)
    torque_coeff, torque_diag = _fit_linear(torque_design, torque_values, 6)
    force_residuals = (force_design @ force_coeff - force_values).reshape(-1, 3)
    torque_residuals = (torque_design @ torque_coeff - torque_values).reshape(-1, 3)
    mass_manual = float(force_coeff[3])
    first_moment_manual = np.asarray(torque_coeff[3:6], dtype=float)
    if mass_manual <= 0.0:
        raise ValueError("fitted Kunwei mass is non-positive; CoG is invalid")
    cog = first_moment_manual / mass_manual
    return {
        "parameters": {
            "force_bias_manual": force_coeff[:3].tolist(),
            "torque_bias_manual": torque_coeff[:3].tolist(),
            "mass_kg_manual": mass_manual,
            "mass_kg": mass_manual,
            "first_moment_kg_m_manual": first_moment_manual.tolist(),
            "cog_sensor_m": cog.tolist(),
            "cog_sensor_mm": (1000.0 * cog).tolist(),
        },
        "fit_diagnostics": {"force": force_diag, "torque": torque_diag},
        "residuals": {
            "force": summarize_residuals(force_residuals, gravity_m_s2, "N"),
            "torque": summarize_residuals(torque_residuals, gravity_m_s2, "Nm"),
        },
        "pose_residuals": _pose_residuals(
            observations,
            force_coeff,
            torque_coeff,
            gravity_m_s2,
        ),
    }


def _pose_residuals(
    observations: Sequence[PoseObservation],
    force_coeff: np.ndarray,
    torque_coeff: np.ndarray,
    gravity_m_s2: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for observation in observations:
        gravity_sensor = observation.rotation_base_to_sensor.T @ np.array([0.0, 0.0, -1.0])
        force_predicted = np.hstack((np.eye(3), gravity_sensor.reshape(3, 1))) @ force_coeff
        torque_predicted = np.hstack((np.eye(3), -skew(gravity_sensor))) @ torque_coeff
        result[observation.name] = {
            "force_residual_N": ((force_predicted - observation.force_manual) * gravity_m_s2).tolist(),
            "force_residual_norm_N": float(np.linalg.norm((force_predicted - observation.force_manual) * gravity_m_s2)),
            "torque_residual_Nm": ((torque_predicted - observation.torque_manual) * gravity_m_s2).tolist(),
            "torque_residual_norm_Nm": float(np.linalg.norm((torque_predicted - observation.torque_manual) * gravity_m_s2)),
        }
    return result


def _pinocchio_payload_terms(model_bundle: Any, q: np.ndarray, gravity_m_s2: float) -> tuple[np.ndarray, np.ndarray]:
    """Return bare gravity and a calibrated tool0 payload gravity regressor."""

    import pinocchio as pin  # noqa: PLC0415 - intentionally lazy

    model = model_bundle.model
    data = model_bundle.data
    pin.computeGeneralizedGravity(model, data, q)
    bare_gravity = np.asarray(data.g, dtype=float).copy()
    pin.forwardKinematics(model, data, q)
    pin.computeJointJacobians(model, data, q)
    pin.updateFramePlacements(model, data)
    jacobian_world = np.asarray(
        pin.getFrameJacobian(model, data, model_bundle.tool0_frame_id, pin.ReferenceFrame.LOCAL_WORLD_ALIGNED),
        dtype=float,
    )
    base_rotation_world = np.asarray(data.oMf[model_bundle.base_frame_id].rotation, dtype=float)
    jacobian_base = np.vstack(
        (base_rotation_world.T @ jacobian_world[:3, :], base_rotation_world.T @ jacobian_world[3:, :])
    )
    tool0_base = data.oMf[model_bundle.base_frame_id].inverse() * data.oMf[model_bundle.tool0_frame_id]
    rotation_base_tool0 = np.asarray(tool0_base.rotation, dtype=float).copy()
    gravity_support_base = np.array([0.0, 0.0, gravity_m_s2], dtype=float)
    regressor = np.empty((model.nv, 4), dtype=float)
    regressor[:, 0] = jacobian_base[:3, :].T @ gravity_support_base
    # For h=m*r_tool0, Jv,cog = Jv,tool0 - skew(R*h/m)*Jw.
    # Multiplying by m*g gives one linear column for each first moment.
    for index in range(3):
        offset_basis_base = rotation_base_tool0[:, index]
        regressor[:, index + 1] = (
            -skew(offset_basis_base) @ jacobian_base[3:, :]
        ).T @ gravity_support_base
    return bare_gravity, regressor


def fit_ur_payload_model(
    observations: Sequence[PoseObservation],
    model_bundle: Any | None = None,
    gravity_terms: Callable[[np.ndarray], tuple[np.ndarray, np.ndarray]] | None = None,
    gravity_m_s2: float = GRAVITY_M_S2,
) -> dict[str, Any]:
    """Fit payload mass and tool0 first moments after subtracting bare gravity.

    ``gravity_terms`` is a pure-test injection point.  Production callers use
    the calibrated Pinocchio implementation above.
    """

    if not observations:
        raise ValueError("no observations")
    if gravity_terms is None:
        if model_bundle is None:
            raise ValueError("model_bundle or gravity_terms is required")
        gravity_terms = lambda q: _pinocchio_payload_terms(model_bundle, q, gravity_m_s2)
    designs = []
    values = []
    for observation in observations:
        bare, regressor = gravity_terms(observation.joint_mean_rad)
        bare = np.asarray(bare, dtype=float)
        regressor = np.asarray(regressor, dtype=float)
        if bare.shape != (6,) or regressor.shape != (6, 4):
            raise ValueError(f"invalid gravity term shapes: {bare.shape}, {regressor.shape}")
        designs.append(regressor)
        values.append(observation.target_moment_nm - bare)
    design = np.vstack(designs)
    value = np.concatenate(values)
    coefficients, diagnostics = _fit_linear(design, value, 4)
    residuals = (design @ coefficients - value).reshape(-1, 6)
    mass = float(coefficients[0])
    if mass <= 0.0:
        raise ValueError("fitted UR payload mass is non-positive; CoG is invalid")
    first_moment = np.asarray(coefficients[1:4], dtype=float)
    cog = first_moment / mass
    setting_first_moment = CONTROLLER_PAYLOAD_KG * CONTROLLER_COG_TOOL0_M
    return {
        "parameters": {
            "mass_kg": mass,
            "first_moment_kg_m": first_moment.tolist(),
            "cog_tool0_m": cog.tolist(),
            "cog_tool0_mm": (1000.0 * cog).tolist(),
        },
        "gravity_m_s2": gravity_m_s2,
        "fit_diagnostics": diagnostics,
        "residuals": summarize_residuals(residuals, 1.0, "Nm"),
        "controller_setting": {
            "mass_kg": CONTROLLER_PAYLOAD_KG,
            "cog_tool0_mm": (1000.0 * CONTROLLER_COG_TOOL0_M).tolist(),
            "first_moment_kg_m": setting_first_moment.tolist(),
            "delta_mass_kg": mass - CONTROLLER_PAYLOAD_KG,
            "delta_first_moment_kg_m": (first_moment - setting_first_moment).tolist(),
            "delta_cog_tool0_mm": (1000.0 * (cog - CONTROLLER_COG_TOOL0_M)).tolist(),
        },
    }


def ur_payload_pose_residuals(
    observations: Sequence[PoseObservation],
    model_bundle: Any,
    coefficients: Sequence[float],
) -> dict[str, Any]:
    """Evaluate UR payload residuals with coefficients already frozen upstream."""

    coefficient_vector = np.asarray(coefficients, dtype=float)
    if coefficient_vector.shape != (4,):
        raise ValueError(f"expected four UR payload coefficients, got {coefficient_vector.shape}")
    result: dict[str, Any] = {}
    for observation in observations:
        bare, regressor = _pinocchio_payload_terms(model_bundle, observation.joint_mean_rad, GRAVITY_M_S2)
        measured_payload = observation.target_moment_nm - bare
        residual = regressor @ coefficient_vector - measured_payload
        result[observation.name] = {
            "residual_Nm": residual.tolist(),
            "residual_norm_Nm": float(np.linalg.norm(residual)),
            "residual_rms_per_joint_Nm": float(
                np.sqrt(np.mean(residual * residual))
            ),
            "bare_gravity_Nm": bare.tolist(),
            "measured_payload_Nm": measured_payload.tolist(),
        }
    return result


def load_calibrated_model(calibration_yaml: Path = DEFAULT_CALIBRATION_YAML) -> Any:
    """Build the existing calibrated Pinocchio model only when explicitly used."""

    tools_dir = Path(__file__).resolve().parent
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    from step5c_calibrated_kinematics_audit import build_calibrated_model  # noqa: PLC0415

    return build_calibrated_model(calibration_yaml=calibration_yaml)


def _quality_findings(observations: Sequence[PoseObservation]) -> list[str]:
    findings: list[str] = []
    for observation in observations:
        quality = observation.source_quality
        evidence = quality["evidence"]
        if evidence.get("ok") is not True:
            findings.append(f"{observation.name}: evidence.ok is not true")
        if evidence.get("motion_performed") is not False:
            findings.append(f"{observation.name}: motion_performed is not false")
        for key in (
            "maximum_joint_speed_rad_s",
            "maximum_tcp_speed_m_s",
            "maximum_tcp_translation_m",
        ):
            value = evidence.get(key)
            if value is None or not math.isfinite(float(value)):
                findings.append(f"{observation.name}: missing/nonfinite static evidence {key}")
        if quality["rtde"]["row_count"] < MIN_RTDE_ROWS:
            findings.append(
                f"{observation.name}: RTDE rows {quality['rtde']['row_count']} "
                f"< {MIN_RTDE_ROWS}"
            )
        if quality["kunwei"]["row_count"] < MIN_KUNWEI_ROWS:
            findings.append(
                f"{observation.name}: Kunwei rows {quality['kunwei']['row_count']} "
                f"< {MIN_KUNWEI_ROWS}"
            )
        if int(quality.get("kunwei_summary_parse_errors", -1)) != 0:
            findings.append(f"{observation.name}: Kunwei summary parse_errors != 0")
        if int(quality.get("kunwei_summary_dropped_sync_bytes", -1)) != 0:
            findings.append(
                f"{observation.name}: Kunwei summary dropped_sync_bytes != 0"
            )
        rate_hz = quality.get("kunwei_rate_hz")
        if (
            rate_hz is None
            or not math.isfinite(float(rate_hz))
            or float(rate_hz) < MIN_KUNWEI_RATE_HZ
        ):
            findings.append(
                f"{observation.name}: Kunwei rate is below {MIN_KUNWEI_RATE_HZ} Hz"
            )
        threshold_checks = (
            (
                "maximum_joint_speed_rad_s",
                MAX_STATIC_JOINT_SPEED_RAD_S,
            ),
            ("maximum_tcp_speed_m_s", MAX_STATIC_TCP_SPEED_M_S),
            ("maximum_tcp_translation_m", MAX_STATIC_TCP_TRANSLATION_M),
        )
        for key, maximum in threshold_checks:
            value = evidence.get(key)
            if value is not None and math.isfinite(float(value)):
                if float(value) > maximum:
                    findings.append(
                        f"{observation.name}: {key}={float(value)} > {maximum}"
                    )
    return findings


def _bounded_conclusion(ur_training: Mapping[str, Any]) -> dict[str, Any]:
    controller = ur_training["controller_setting"]
    delta_mass_kg = abs(float(controller["delta_mass_kg"]))
    delta_cog_m = np.asarray(
        controller["delta_cog_tool0_mm"],
        dtype=float,
    ) / 1000.0
    delta_cog_norm_m = float(np.linalg.norm(delta_cog_m))
    condition = float(ur_training["fit_diagnostics"]["condition_number"])
    held_out = ur_training["held_out_residuals"]
    maximum_held_out_residual_nm = max(
        float(item["residual_norm_Nm"]) for item in held_out.values()
    )
    well_conditioned = math.isfinite(condition) and condition < 100.0
    held_out_consistent = (
        maximum_held_out_residual_nm <= MAX_HELD_OUT_UR_RESIDUAL_NM
    )
    gross_delta = (
        delta_mass_kg > GROSS_PAYLOAD_MASS_DELTA_KG
        or delta_cog_norm_m > GROSS_PAYLOAD_COG_DELTA_M
    )
    gross_mismatch_supported = (
        gross_delta and well_conditioned and held_out_consistent
    )
    return {
        "gross_payload_cog_mismatch_supported": gross_mismatch_supported,
        "status": (
            "supported"
            if gross_mismatch_supported
            else "not_supported"
        ),
        "observed": {
            "absolute_mass_delta_kg": delta_mass_kg,
            "cog_delta_norm_m": delta_cog_norm_m,
            "maximum_held_out_residual_norm_Nm": (
                maximum_held_out_residual_nm
            ),
            "design_condition_number": condition,
        },
        "decision_thresholds": {
            "gross_mass_delta_kg": GROSS_PAYLOAD_MASS_DELTA_KG,
            "gross_cog_delta_m": GROSS_PAYLOAD_COG_DELTA_M,
            "maximum_held_out_residual_norm_Nm": (
                MAX_HELD_OUT_UR_RESIDUAL_NM
            ),
            "maximum_design_condition_number": 100.0,
        },
        "held_out_consistent": held_out_consistent,
        "well_conditioned": well_conditioned,
        "direct_torque_entry_root_cause_identified": False,
        "payload_write_authorized": False,
        "motion_authorized": False,
        "scope_note": (
            "This static sweep tests gross payload/CoG mismatch only. "
            "It does not identify the Direct Torque entry root cause."
        ),
    }


def _source_manifest(run_dir: Path, pose_names: Sequence[str], calibration_yaml: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    for pose_name in pose_names:
        pose_dir = run_dir / pose_name
        paths = [
            pose_dir / "rtde" / "normal_position_control_rtde.csv",
            pose_dir / "rtde" / "evidence.json",
            pose_dir / "kunwei" / "data.csv",
            pose_dir / "kunwei" / "summary.json",
            pose_dir / "kunwei" / "metadata.json",
            pose_dir / "kunwei" / "raw_frames.bin",
        ]
        manifest[pose_name] = {
            "paths": {str(path.relative_to(run_dir)): str(path.resolve()) for path in paths if path.exists()},
            "sha256": {str(path.relative_to(run_dir)): sha256_file(path) for path in paths if path.exists()},
        }
    manifest["calibration_yaml"] = {
        "path": str(calibration_yaml.resolve()),
        "sha256": sha256_file(calibration_yaml) if calibration_yaml.is_file() else None,
    }
    return manifest


def analyze_run(
    run_dir: Path,
    output: Path,
    calibration_yaml: Path = DEFAULT_CALIBRATION_YAML,
    post_validation_refit: bool = False,
    model_loader: Callable[[Path], Any] = load_calibrated_model,
) -> dict[str, Any]:
    """Analyze a complete named sweep and return the deterministic report."""

    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise DataQualityError(f"run directory does not exist: {run_dir}")
    actual_pose_names = sorted(path.name for path in run_dir.iterdir() if path.is_dir())
    split = split_pose_names(actual_pose_names)
    observations = {name: load_pose_observation(run_dir, name) for name in POSE_NAMES}
    quality_findings = _quality_findings(tuple(observations.values()))
    if quality_findings:
        raise DataQualityError("static/no-motion validation failed: " + "; ".join(quality_findings))

    training_observations = [observations[name] for name in TRAINING_POSES]
    held_out_observations = [observations[name] for name in HELD_OUT_POSES]
    kunwei_training = fit_kunwei_model(training_observations)
    kunwei_training["held_out_residuals"] = {
        name: _pose_residuals([observations[name]],
                              np.asarray([*kunwei_training["parameters"]["force_bias_manual"], kunwei_training["parameters"]["mass_kg_manual"]]),
                              np.asarray([*kunwei_training["parameters"]["torque_bias_manual"], *kunwei_training["parameters"]["first_moment_kg_m_manual"]]),
                              GRAVITY_M_S2)[name]
        for name in ("P0R", "P7", "P8")
    }
    model_bundle = model_loader(calibration_yaml)
    ur_training = fit_ur_payload_model(training_observations, model_bundle=model_bundle)
    # Reconstruct the frozen coefficient vector from the reported parameters;
    # this makes the held-out evaluation visibly downstream of training only.
    ur_parameters = ur_training["parameters"]
    ur_coefficients = np.r_[
        ur_parameters["mass_kg"],
        ur_parameters["first_moment_kg_m"],
    ]
    ur_training["held_out_residuals"] = ur_payload_pose_residuals(
        held_out_observations,
        model_bundle,
        ur_coefficients,
    )
    controller_coefficients = np.r_[
        CONTROLLER_PAYLOAD_KG,
        CONTROLLER_PAYLOAD_KG * CONTROLLER_COG_TOOL0_M,
    ]
    ur_training["controller_setting_held_out_residuals"] = (
        ur_payload_pose_residuals(
            held_out_observations,
            model_bundle,
            controller_coefficients,
        )
    )

    report: dict[str, Any] = {
        "analyzer": "analyze_manual_cog_sweep/v1",
        "run_dir": str(run_dir),
        "output": str(output.resolve()),
        "source_paths_and_hashes": _source_manifest(run_dir, POSE_NAMES, calibration_yaml),
        "data_quality": {
            "actual_pose_names": actual_pose_names,
            "static_no_motion_findings": "validated",
            "thresholds": {
                "minimum_rtde_rows": MIN_RTDE_ROWS,
                "minimum_kunwei_rows": MIN_KUNWEI_ROWS,
                "minimum_kunwei_rate_hz": MIN_KUNWEI_RATE_HZ,
                "maximum_static_joint_speed_rad_s": (
                    MAX_STATIC_JOINT_SPEED_RAD_S
                ),
                "maximum_static_tcp_speed_m_s": MAX_STATIC_TCP_SPEED_M_S,
                "maximum_static_tcp_translation_m": (
                    MAX_STATIC_TCP_TRANSLATION_M
                ),
            },
            "per_pose": {name: observations[name].source_quality for name in POSE_NAMES},
        },
        "split": split,
        "actual_joint_means": {
            name: {
                "columns": list(observations[name].joint_columns),
                "mean_rad": observations[name].joint_mean_rad.tolist(),
            }
            for name in POSE_NAMES
        },
        "kunwei_raw_manual_unit_fit": kunwei_training,
        "ur_payload_fit": {
            "calibration_yaml": str(calibration_yaml.resolve()),
            "calibration_hash": getattr(model_bundle, "calibration_hash", None),
            "frozen_training": ur_training,
        },
        "conclusion": _bounded_conclusion(ur_training),
    }
    if post_validation_refit:
        # This branch is intentionally after held-out residuals have been made.
        post_observations = [observations[name] for name in POST_VALIDATION_POSES]
        post_kunwei = fit_kunwei_model(post_observations)
        post_ur = fit_ur_payload_model(post_observations, model_bundle=model_bundle)
        report["post_validation_all_unique_pose_refit"] = {
            "label": "post-validation; P0R excluded as a repeat",
            "poses": list(POST_VALIDATION_POSES),
            "kunwei": post_kunwei,
            "ur_payload": post_ur,
        }
    return report


def _write_json(output: Path, report: Mapping[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2, default=_json_default) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path, help="manual_cog_sweep directory containing P0..P8 and P0R")
    parser.add_argument("--output", required=True, type=Path, help="explicit JSON output path")
    parser.add_argument("--calibration-yaml", type=Path, default=DEFAULT_CALIBRATION_YAML)
    parser.add_argument("--post-validation-refit", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        report = analyze_run(
            args.run_dir,
            args.output,
            calibration_yaml=args.calibration_yaml,
            post_validation_refit=args.post_validation_refit,
        )
        _write_json(args.output, report)
    except (DataQualityError, ValueError, FileNotFoundError, RuntimeError, ImportError) as exc:
        raise SystemExit(f"manual CoG analysis failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
