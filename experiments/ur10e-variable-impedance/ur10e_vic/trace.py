"""Hash-bound UR bridge trace conversion and command-inert shadow ablation."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Sequence

import numpy as np

from .constraints import ImpedanceBounds
from .contracts import ImpedanceObservation, PoseSample
from .dbil.dataset import sha256_file
from .backends import CapabilitySeparatedShadowMux
from .policies import (
    DBILPredictor,
    DBILShadowPolicy,
    DirectionalStiffnessAdaptor,
    DirectionalVICPolicy,
    FixedImpedancePolicy,
    ScriptedPhase,
    ScriptedPhasePolicy,
)


@dataclass(frozen=True)
class URTraceArrays:
    timestamp_s: np.ndarray
    pose_history: np.ndarray
    twist_history: np.ndarray
    wrench_history: np.ndarray
    joint_position_rad: np.ndarray
    joint_velocity_rad_s: np.ndarray
    nominal_zft: np.ndarray
    actual_command: np.ndarray
    actual_command_valid: np.ndarray
    actual_command_kind: str
    calibration_hash: str
    frame_transform_hash: str
    lineage_verified: bool
    task_zft_verified: bool
    task_zft_hash: str
    source_sha256: str

    def __post_init__(self) -> None:
        count = self.timestamp_s.shape[0]
        shapes = {
            "pose_history": (count, 16, 7),
            "twist_history": (count, 16, 6),
            "wrench_history": (count, 16, 6),
            "joint_position_rad": (count, 6),
            "joint_velocity_rad_s": (count, 6),
            "nominal_zft": (count, 7),
            "actual_command": (count, 6),
        }
        for name, shape in shapes.items():
            if getattr(self, name).shape != shape:
                raise ValueError(f"{name} must have shape {shape}")
        if count <= 0 or not all(
            np.isfinite(getattr(self, name)).all() for name in shapes
        ):
            raise ValueError("UR trace arrays must be non-empty and finite")
        if not np.isfinite(self.timestamp_s).all() or np.any(
            np.diff(self.timestamp_s) <= 0.0
        ):
            raise ValueError("UR trace timestamps must be finite and strictly increasing")
        if self.actual_command_valid.shape != (count,):
            raise ValueError("actual_command_valid must have shape [N]")
        if self.actual_command_kind not in {"step5b_twist", "strict_rnn_qdot"}:
            raise ValueError("unsupported actual command kind")
        if not all(
            value.strip()
            for value in (
                self.calibration_hash,
                self.frame_transform_hash,
                self.task_zft_hash,
                self.source_sha256,
            )
        ):
            raise ValueError("trace lineage identifiers must be non-empty")

    def observation(self, index: int) -> ImpedanceObservation:
        poses = tuple(
            PoseSample(sample[:3], sample[3:7]) for sample in self.pose_history[index]
        )
        nominal = self.nominal_zft[index]
        return ImpedanceObservation(
            sequence=index,
            timestamp_s=float(self.timestamp_s[index]),
            pose_history=poses,
            twist_history=self.twist_history[index],
            wrench_history=self.wrench_history[index],
            nominal_zft=PoseSample(nominal[:3], nominal[3:7]),
            joint_position_rad=self.joint_position_rad[index],
            joint_velocity_rad_s=self.joint_velocity_rad_s[index],
            jacobian_base=None,
            frame_id="base",
            sensor_id="kunwei_wrench_rotated_tcp_to_base",
            calibration_hash=self.calibration_hash,
        )

    def __len__(self) -> int:
        return int(self.timestamp_s.shape[0])


def _rotvec_to_quaternion(rotvec: np.ndarray) -> np.ndarray:
    angle = np.linalg.norm(rotvec, axis=1)
    axis = rotvec / np.maximum(angle[:, None], 1e-12)
    quaternion = np.concatenate(
        (np.cos(angle / 2.0)[:, None], axis * np.sin(angle / 2.0)[:, None]),
        axis=1,
    )
    quaternion[angle <= 1e-12] = np.asarray((1.0, 0.0, 0.0, 0.0))
    quaternion[quaternion[:, 0] < 0.0] *= -1.0
    return quaternion


def _rotate_vectors_wxyz(quaternion: np.ndarray, vectors: np.ndarray) -> np.ndarray:
    xyz = quaternion[:, 1:4]
    twice_cross = 2.0 * np.cross(xyz, vectors)
    return vectors + quaternion[:, :1] * twice_cross + np.cross(xyz, twice_cross)


def _slerp_to_grid(
    source_time: np.ndarray, source_quaternion: np.ndarray, grid: np.ndarray
) -> np.ndarray:
    right = np.searchsorted(source_time, grid, side="right")
    right = np.clip(right, 1, source_time.size - 1)
    left = right - 1
    fraction = (grid - source_time[left]) / (source_time[right] - source_time[left])
    first = source_quaternion[left]
    second = np.array(source_quaternion[right], copy=True)
    dot = np.sum(first * second, axis=1)
    second[dot < 0.0] *= -1.0
    dot = np.abs(dot)
    theta = np.arccos(np.clip(dot, -1.0, 1.0))
    sin_theta = np.sin(theta)
    near = np.abs(sin_theta) < 1e-8
    first_weight = np.empty_like(theta)
    second_weight = np.empty_like(theta)
    first_weight[near] = 1.0 - fraction[near]
    second_weight[near] = fraction[near]
    first_weight[~near] = np.sin((1.0 - fraction[~near]) * theta[~near]) / sin_theta[~near]
    second_weight[~near] = np.sin(fraction[~near] * theta[~near]) / sin_theta[~near]
    result = first_weight[:, None] * first + second_weight[:, None] * second
    result /= np.maximum(np.linalg.norm(result, axis=1, keepdims=True), 1e-12)
    result[result[:, 0] < 0.0] *= -1.0
    return result


def _finite_values(row: dict[str, str], names: Sequence[str]) -> list[float] | None:
    try:
        values = [float(row[name]) for name in names]
    except (KeyError, TypeError, ValueError):
        return None
    return values if all(math.isfinite(value) for value in values) else None


def _load_role_artifact(path: Path, expected_role: str) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise ValueError(f"{expected_role} artifact is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{expected_role} artifact must be valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"{expected_role} artifact requires schema_version=1")
    if payload.get("artifact_role") != expected_role:
        raise ValueError(f"artifact role must be {expected_role}")
    return payload, sha256_file(path)


def _load_task_zft_trajectory(
    path: Path, grid: np.ndarray
) -> tuple[np.ndarray, str]:
    payload, artifact_hash = _load_role_artifact(path, "time_indexed_task_zft")
    samples = payload.get("samples")
    if not isinstance(samples, list) or len(samples) < 2:
        raise ValueError("task ZFT artifact needs at least two samples")
    timestamps: list[float] = []
    poses: list[np.ndarray] = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("task ZFT sample must be an object")
        timestamp = float(sample.get("timestamp_s", math.nan))
        values = np.asarray(sample.get("pose"), dtype=float)
        if (
            not math.isfinite(timestamp)
            or values.shape != (7,)
            or not np.isfinite(values).all()
        ):
            raise ValueError("task ZFT samples must contain finite timestamp/pose7")
        quaternion_norm = float(np.linalg.norm(values[3:7]))
        if abs(quaternion_norm - 1.0) > 1e-3:
            raise ValueError("task ZFT quaternion must be unit norm")
        values[3:7] /= quaternion_norm
        if values[3] < 0.0:
            values[3:7] *= -1.0
        timestamps.append(timestamp)
        poses.append(values)
    source_time = np.asarray(timestamps, dtype=float)
    if np.any(np.diff(source_time) <= 0.0):
        raise ValueError("task ZFT timestamps must be strictly increasing")
    if source_time[0] > grid[0] + 1e-12 or source_time[-1] < grid[-1] - 1e-12:
        raise ValueError("task ZFT artifact does not cover the trace time range")
    pose_values = np.asarray(poses, dtype=float)
    position = np.column_stack(
        [np.interp(grid, source_time, pose_values[:, index]) for index in range(3)]
    )
    quaternion = _slerp_to_grid(source_time, pose_values[:, 3:7], grid)
    return np.concatenate((position, quaternion), axis=1), artifact_hash


def convert_ur_bridge_trace(
    csv_path: Path,
    dataset_output: Path,
    manifest_output: Path,
    *,
    resample_hz: int = 200,
    command_kind: str = "step5b_twist",
    calibration_sha256: str | None = None,
    frame_transform_sha256: str | None = None,
    task_zft: Sequence[float] | None = None,
    calibration_artifact: Path | None = None,
    frame_transform_artifact: Path | None = None,
    task_zft_artifact: Path | None = None,
) -> dict[str, Any]:
    if resample_hz != 200:
        raise ValueError("the first UR shadow adapter pins resampling to 200 Hz")
    for role, artifact, legacy in (
        ("calibration", calibration_artifact, calibration_sha256),
        ("frame transform", frame_transform_artifact, frame_transform_sha256),
        ("task ZFT", task_zft_artifact, task_zft),
    ):
        if artifact is not None and legacy is not None:
            raise ValueError(f"{role} artifact cannot be mixed with a legacy value")
    pose_columns = tuple(f"ur_actual_TCP_pose_{index}" for index in range(6))
    twist_columns = tuple(f"ur_actual_TCP_speed_{index}" for index in range(6))
    q_columns = tuple(f"ur_actual_q_{index}" for index in range(6))
    qd_columns = tuple(f"ur_actual_qd_{index}" for index in range(6))
    if command_kind == "step5b_twist":
        command_columns = (
            "step4e_cmd_vx_m_s",
            "step4e_cmd_vy_m_s",
            "step4e_cmd_vz_m_s",
            "step4e_cmd_wx_rad_s",
            "step4e_cmd_wy_rad_s",
            "step4e_cmd_wz_rad_s",
        )
    elif command_kind == "strict_rnn_qdot":
        command_columns = tuple(
            f"_step5c_cmd_qd{index}_rad_s" for index in range(6)
        )
    else:
        raise ValueError("command_kind must be step5b_twist or strict_rnn_qdot")
    command_valid_column = "step4e_cmd_valid"
    wrench_tcp_columns = (
        "fx_n_zeroed",
        "fy_n_zeroed",
        "fz_n_zeroed",
        "mx_nm_zeroed",
        "my_nm_zeroed",
        "mz_nm_zeroed",
    )
    raw: dict[str, list[list[float] | float]] = {
        name: []
        for name in (
            "time",
            "pose",
            "twist",
            "wrench_tcp",
            "q",
            "qd",
            "command",
            "command_valid",
        )
    }
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = set(
            pose_columns
            + twist_columns
            + q_columns
            + qd_columns
            + command_columns
            + wrench_tcp_columns
            + ("t_monotonic_s", command_valid_column)
        )
        missing = sorted(required - set(reader.fieldnames or ()))
        if missing:
            raise ValueError(f"trace is missing required columns: {', '.join(missing)}")
        for row_index, row in enumerate(reader, start=2):
            timestamp = _finite_values(row, ("t_monotonic_s",))
            pose = _finite_values(row, pose_columns)
            twist = _finite_values(row, twist_columns)
            q = _finite_values(row, q_columns)
            qd = _finite_values(row, qd_columns)
            if not all(value is not None for value in (timestamp, pose, twist, q, qd)):
                continue
            wrench_tcp = _finite_values(row, wrench_tcp_columns)
            if wrench_tcp is None:
                continue
            command = _finite_values(row, command_columns)
            command_valid = _finite_values(row, (command_valid_column,))
            if command is None or command_valid is None:
                raise ValueError(f"missing/nonfinite actual command at CSV row {row_index}")
            raw["time"].append(timestamp[0])
            raw["pose"].append(pose)
            raw["twist"].append(twist)
            raw["wrench_tcp"].append(wrench_tcp)
            raw["q"].append(q)
            raw["qd"].append(qd)
            raw["command"].append(command)
            raw["command_valid"].append(command_valid[0])
    time_values = np.asarray(raw["time"], dtype=float)
    if time_values.size < 16 or np.any(np.diff(time_values) <= 0.0):
        raise ValueError("trace needs at least 16 strictly increasing finite rows")
    grid = np.arange(time_values[0], time_values[-1], 1.0 / resample_hz)
    if grid.size < 16:
        raise ValueError("trace is shorter than one 80 ms observation window")

    def interpolate(name: str, width: int) -> np.ndarray:
        values = np.asarray(raw[name], dtype=float)
        return np.column_stack(
            [np.interp(grid, time_values, values[:, index]) for index in range(width)]
        )

    raw_pose = np.asarray(raw["pose"], dtype=float)
    raw_quaternion = _rotvec_to_quaternion(raw_pose[:, 3:6])
    raw_wrench_tcp = np.asarray(raw["wrench_tcp"], dtype=float)
    raw_wrench_base = np.concatenate(
        (
            _rotate_vectors_wxyz(raw_quaternion, raw_wrench_tcp[:, :3]),
            _rotate_vectors_wxyz(raw_quaternion, raw_wrench_tcp[:, 3:6]),
        ),
        axis=1,
    )
    pose_position = interpolate("pose", 6)[:, :3]
    pose = np.concatenate(
        (pose_position, _slerp_to_grid(time_values, raw_quaternion, grid)), axis=1
    )
    twist = interpolate("twist", 6)
    wrench = np.column_stack(
        [np.interp(grid, time_values, raw_wrench_base[:, index]) for index in range(6)]
    )
    q = interpolate("q", 6)
    qd = interpolate("qd", 6)
    zoh_indices = np.clip(
        np.searchsorted(time_values, grid, side="right") - 1,
        0,
        time_values.size - 1,
    )
    command = np.asarray(raw["command"], dtype=float)[zoh_indices]
    command_valid = np.asarray(raw["command_valid"], dtype=float)[zoh_indices] > 0.5
    starts = range(0, grid.size - 16 + 1)
    pose_windows = np.stack([pose[start : start + 16] for start in starts])
    twist_windows = np.stack([twist[start : start + 16] for start in starts])
    wrench_windows = np.stack([wrench[start : start + 16] for start in starts])
    last_indices = np.arange(15, grid.size)
    source_hash = sha256_file(csv_path)
    for name, value in (
        ("calibration_sha256", calibration_sha256),
        ("frame_transform_sha256", frame_transform_sha256),
    ):
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"{name} must be a lowercase SHA-256")
    calibration_binding: dict[str, Any] | None = None
    if calibration_artifact is not None:
        calibration_payload, calibration_lineage = _load_role_artifact(
            calibration_artifact, "sensor_calibration_lineage"
        )
        if not str(calibration_payload.get("sensor_id", "")).strip():
            raise ValueError("sensor calibration lineage requires sensor_id")
        calibration_binding = {
            "artifact": calibration_artifact.name,
            "sha256": calibration_lineage,
            "artifact_role": "sensor_calibration_lineage",
        }
    else:
        calibration_lineage = (
            f"diagnostic_unverified_sha256:{calibration_sha256}"
            if calibration_sha256 is not None
            else "unverified_sensor_calibration"
        )
    frame_binding: dict[str, Any] | None = None
    if frame_transform_artifact is not None:
        frame_payload, frame_lineage = _load_role_artifact(
            frame_transform_artifact, "wrench_frame_transform"
        )
        if not all(
            str(frame_payload.get(name, "")).strip()
            for name in ("source_frame", "target_frame")
        ):
            raise ValueError("wrench frame transform requires source/target frames")
        frame_binding = {
            "artifact": frame_transform_artifact.name,
            "sha256": frame_lineage,
            "artifact_role": "wrench_frame_transform",
        }
    else:
        frame_lineage = (
            f"diagnostic_unverified_sha256:{frame_transform_sha256}"
            if frame_transform_sha256 is not None
            else "unverified_tcp_to_base_transform"
        )
    lineage_verified = calibration_binding is not None and frame_binding is not None

    task_zft_grid: np.ndarray | None = None
    task_zft_binding: dict[str, Any] | None = None
    task_zft_status = "observed-pose placeholder; deterministic/fixed proposal evidence invalid"
    if task_zft_artifact is not None:
        task_zft_grid, task_zft_hash = _load_task_zft_trajectory(
            task_zft_artifact, grid
        )
        task_zft_binding = {
            "artifact": task_zft_artifact.name,
            "sha256": task_zft_hash,
            "artifact_role": "time_indexed_task_zft",
        }
        task_zft_status = "verified time-indexed task ZFT artifact"
    elif task_zft is not None:
        task_zft_values = np.asarray(tuple(float(value) for value in task_zft))
        if task_zft_values.shape != (7,) or not np.isfinite(task_zft_values).all():
            raise ValueError("task_zft must be one finite pose7")
        q_norm = np.linalg.norm(task_zft_values[3:7])
        if abs(q_norm - 1.0) > 1e-3:
            raise ValueError("task_zft quaternion must be unit norm")
        task_zft_grid = np.tile(task_zft_values, (grid.size, 1))
        task_zft_status = "legacy unbound constant task ZFT; diagnostic only"
    task_zft_verified = task_zft_binding is not None
    dataset_output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=dataset_output.parent, suffix=".npz", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        np.savez_compressed(
            temporary,
            timestamp_s=grid[last_indices],
            pose_history=pose_windows,
            twist_history=twist_windows,
            wrench_history=wrench_windows,
            joint_position_rad=q[last_indices],
            joint_velocity_rad_s=qd[last_indices],
            nominal_zft=(
                task_zft_grid[last_indices]
                if task_zft_grid is not None
                else pose[last_indices]
            ),
            actual_command=command[last_indices],
            actual_command_valid=command_valid[last_indices],
            actual_command_kind=np.asarray(command_kind),
            calibration_hash=np.asarray(calibration_lineage),
            frame_transform_hash=np.asarray(frame_lineage),
            lineage_verified=np.asarray(lineage_verified),
            task_zft_verified=np.asarray(task_zft_verified),
            task_zft_hash=np.asarray(
                task_zft_binding["sha256"]
                if task_zft_binding is not None
                else "unverified_task_zft"
            ),
            source_sha256=np.asarray(source_hash),
        )
    os.replace(temporary_path, dataset_output)
    manifest = {
        "schema_version": 2,
        "source_csv_artifact": csv_path.name,
        "source_sha256": source_hash,
        "dataset_artifact": dataset_output.name,
        "path_semantics": "artifact names are relative to the relocated evidence bundle",
        "dataset_sha256": sha256_file(dataset_output),
        "observations": int(last_indices.size),
        "resample_hz": resample_hz,
        "history_window": 16,
        "actual_command_kind": command_kind,
        "actual_command_missing_policy": "fail conversion; never substitute zeros",
        "command_resampling": "zero_order_hold",
        "pose_quaternion_resampling": "shortest_arc_slerp",
        "wrench_frame": "base (both force and moment rotated together from TCP)",
        "calibration_hash": calibration_lineage,
        "frame_transform_hash": frame_lineage,
        "lineage_verified": lineage_verified,
        "task_zft_verified": task_zft_verified,
        "artifact_bindings": {
            "sensor_calibration_lineage": calibration_binding,
            "wrench_frame_transform": frame_binding,
            "time_indexed_task_zft": task_zft_binding,
        },
        "jacobian_status": "not_present; policy-shadow only; backend command blocked",
        "nominal_zft_status": task_zft_status,
        "claim_boundary": "UR trace schema/shadow evidence only",
    }
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def load_ur_trace_dataset(path: Path) -> URTraceArrays:
    with np.load(path, allow_pickle=False) as archive:
        return URTraceArrays(
            timestamp_s=np.asarray(archive["timestamp_s"], dtype=float),
            pose_history=np.asarray(archive["pose_history"], dtype=float),
            twist_history=np.asarray(archive["twist_history"], dtype=float),
            wrench_history=np.asarray(archive["wrench_history"], dtype=float),
            joint_position_rad=np.asarray(archive["joint_position_rad"], dtype=float),
            joint_velocity_rad_s=np.asarray(archive["joint_velocity_rad_s"], dtype=float),
            nominal_zft=np.asarray(archive["nominal_zft"], dtype=float),
            actual_command=np.asarray(archive["actual_command"], dtype=float),
            actual_command_valid=np.asarray(archive["actual_command_valid"], dtype=bool),
            actual_command_kind=str(np.asarray(archive["actual_command_kind"]).item()),
            calibration_hash=str(np.asarray(archive["calibration_hash"]).item()),
            frame_transform_hash=str(np.asarray(archive["frame_transform_hash"]).item()),
            lineage_verified=bool(np.asarray(archive["lineage_verified"]).item()),
            task_zft_verified=bool(np.asarray(archive["task_zft_verified"]).item()),
            task_zft_hash=(
                str(np.asarray(archive["task_zft_hash"]).item())
                if "task_zft_hash" in archive.files
                else "unverified_task_zft"
            ),
            source_sha256=str(np.asarray(archive["source_sha256"]).item()),
        )


def _command_sha256(values: np.ndarray, valid: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(valid, dtype=np.uint8).tobytes())
    digest.update(np.ascontiguousarray(values, dtype=np.float64).tobytes())
    return digest.hexdigest()


def run_shadow_policy_ablation(
    trace: URTraceArrays,
    dbil_predictor: DBILPredictor,
    *,
    max_observations: int = 64,
) -> dict[str, Any]:
    active_indices = np.flatnonzero(trace.actual_command_valid)
    count = min(active_indices.size, max_observations)
    if count <= 0:
        raise ValueError("ablation requires at least one observation")
    bounds = ImpedanceBounds()
    policies = {
        "fixed": FixedImpedancePolicy(bounds),
        "scripted": ScriptedPhasePolicy(
            bounds, (ScriptedPhase(0.0, 1.0), ScriptedPhase(0.1, 0.7))
        ),
        "deterministic": DirectionalVICPolicy(DirectionalStiffnessAdaptor(bounds)),
        "dbil_shadow": DBILShadowPolicy(
            DirectionalStiffnessAdaptor(bounds), dbil_predictor
        ),
    }
    selected_indices = active_indices[:count]
    baseline_before = np.empty((count, 6), dtype=float)
    baseline_valid_before = np.empty(count, dtype=bool)
    mux = CapabilitySeparatedShadowMux()
    for output_index, index in enumerate(selected_indices):
        off_result = mux.select(
            None,
            trace.actual_command[index],
            bool(trace.actual_command_valid[index]),
        )
        baseline_before[output_index] = off_result.command
        baseline_valid_before[output_index] = off_result.command_valid
    summaries: dict[str, Any] = {}
    shadow_outputs: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for name, policy in policies.items():
        stiffness: list[tuple[float, ...]] = []
        valid = 0
        selected_qdot = np.empty((count, 6), dtype=float)
        selected_valid = np.empty(count, dtype=bool)
        bypasses = 0
        for output_index, index in enumerate(selected_indices):
            proposal = policy.propose(trace.observation(index))
            if not proposal.shadow_only:
                raise ValueError(f"{name} violated shadow-only contract")
            stiffness.append(proposal.stiffness)
            valid += int(proposal.valid)
            on_result = mux.select(
                proposal,
                trace.actual_command[index],
                bool(trace.actual_command_valid[index]),
            )
            bypasses += int(on_result.route == "baseline_shadow_bypass")
            selected_qdot[output_index] = on_result.command
            selected_valid[output_index] = on_result.command_valid
        values = np.asarray(stiffness)
        shadow_outputs[name] = (selected_qdot, selected_valid)
        summaries[name] = {
            "valid_proposals": valid,
            "observations": count,
            "stiffness_first": values[0].tolist(),
            "stiffness_last": values[-1].tolist(),
            "stiffness_min": values.min(axis=0).tolist(),
            "shadow_only": True,
            "mux_shadow_bypasses": bypasses,
            "output_command_sha256": _command_sha256(selected_qdot, selected_valid),
            "output_matches_shadow_off": bool(
                np.array_equal(baseline_before, selected_qdot)
                and np.array_equal(baseline_valid_before, selected_valid)
            ),
            "claim_valid_proposals": (
                valid
                if trace.lineage_verified and trace.task_zft_verified
                else 0
            ),
        }
    before_hash = _command_sha256(baseline_before, baseline_valid_before)
    after_hashes = {
        name: _command_sha256(values, valid)
        for name, (values, valid) in shadow_outputs.items()
    }
    all_unchanged = all(
        np.array_equal(baseline_before, values)
        and np.array_equal(baseline_valid_before, valid)
        and before_hash == after_hashes[name]
        for name, (values, valid) in shadow_outputs.items()
    )
    return {
        "schema_version": 2,
        "policies": summaries,
        "baseline_command_sha256_before": before_hash,
        "shadow_on_command_sha256_by_policy": after_hashes,
        "baseline_command_bitwise_unchanged": all_unchanged,
        "comparison_method": (
            "same trace through capability mux twice: shadow off (proposal=None) "
            "versus shadow on (computed proposal); no copied after-array"
        ),
        "trace_source_sha256": trace.source_sha256,
        "actual_command_kind": trace.actual_command_kind,
        "lineage_verified": trace.lineage_verified,
        "task_zft_verified": trace.task_zft_verified,
        "calibration_hash": trace.calibration_hash,
        "frame_transform_hash": trace.frame_transform_hash,
        "task_zft_hash": trace.task_zft_hash,
        "claim_evidence_valid": trace.lineage_verified and trace.task_zft_verified,
        "claim_boundary": "shadow proposals only; no command output mutation",
        "live_motion_authorized": False,
        "dbil_active_enabled": False,
    }
