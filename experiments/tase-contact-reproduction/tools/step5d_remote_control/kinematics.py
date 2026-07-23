"""Calibrated kinematics helpers for the minimal Step5d live path."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np


CANONICAL_JOINTS = (
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
)


def _as_float(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{path}: expected finite float")
    value_f = float(value)
    if value_f != value_f or value_f in (float("inf"), float("-inf")):
        raise ValueError(f"{path}: non-finite")
    return value_f


def _as_float_seq(value: object, n: int, path: str) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or len(value) != n:
        raise TypeError(f"{path}: expected {n} values")
    return tuple(_as_float(item, f"{path}[{index}]") for index, item in enumerate(value))


def _as_path(value: object, *, root: Path, path: str) -> Path:
    candidate = Path(str(value))
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    return candidate


def _skew(v: Sequence[float]) -> np.ndarray:
    values = np.asarray(v, dtype=float).reshape(-1)
    if values.shape != (3,) or not np.all(np.isfinite(values)):
        raise ValueError("offset: expected 3 finite values")
    x, y, z = (float(value) for value in values)
    return np.array(
        [
            [0.0, -z, y],
            [z, 0.0, -x],
            [-y, x, 0.0],
        ],
        dtype=float,
    )


@dataclass(frozen=True)
class KinematicSample:
    q: tuple[float, float, float, float, float, float]
    qd: tuple[float, float, float, float, float, float]
    tcp_pose: tuple[float, float, float, float, float, float]
    tcp_twist: tuple[float, float, float, float, float, float]
    wrench_tcp: tuple[float, float, float, float, float, float]
    jacobian: tuple[tuple[float, ...], ...]
    q_min: tuple[float, float, float, float, float, float]
    q_max: tuple[float, float, float, float, float, float]
    timestamp_s: float


@dataclass(frozen=True)
class CalibratedKinematics:
    model_bundle: Any
    base_frame: int
    tool0_frame: int
    flange_frame: int
    q_min: tuple[float, float, float, float, float, float]
    q_max: tuple[float, float, float, float, float, float]
    tcp_offset_tool0_m: tuple[float, float, float]
    calibration_hash: str

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any]) -> "CalibratedKinematics":
        workspace_root = Path(__file__).resolve().parents[4]
        calibration_yaml = _as_path(cfg["kinematics"]["calibration_yaml"], root=workspace_root, path="kinematics.calibration_yaml")
        xacro_path = _as_path(cfg["kinematics"]["xacro_path"], root=workspace_root, path="kinematics.xacro_path")
        model_bundle = build_model_bundle(calibration_yaml=calibration_yaml, xacro_path=xacro_path)
        expected_hash = str(cfg["calibration"]["expected_hash"])
        if model_bundle.calibration_hash != expected_hash:
            raise RuntimeError(
                f"calibration hash mismatch: {model_bundle.calibration_hash} != {expected_hash}"
            )

        nq = int(getattr(model_bundle.model, "nq", 0))
        nv = int(getattr(model_bundle.model, "nv", 0))
        if nq != 6 or nv != 6:
            raise RuntimeError(f"expected nq=6,nv=6, got nq={nq},nv={nv}")

        if len(model_bundle.model.names) < 7:
            raise RuntimeError("invalid model joint names")
        joint_names = [str(model_bundle.model.names[idx]) for idx in range(1, 7)]
        if tuple(joint_names) != CANONICAL_JOINTS:
            raise RuntimeError(f"unexpected model joint order: {tuple(joint_names)}")

        q_min = tuple(float(v) for v in np.asarray(model_bundle.model.lowerPositionLimit, dtype=float).reshape(-1))
        q_max = tuple(float(v) for v in np.asarray(model_bundle.model.upperPositionLimit, dtype=float).reshape(-1))
        if len(q_min) != 6 or len(q_max) != 6:
            raise RuntimeError(f"expected 6 joint limits, got {len(q_min)} / {len(q_max)}")

        return cls(
            model_bundle=model_bundle,
            base_frame=model_bundle.base_frame_id,
            tool0_frame=model_bundle.tool0_frame_id,
            flange_frame=model_bundle.flange_frame_id,
            q_min=q_min,
            q_max=q_max,
            tcp_offset_tool0_m=_as_float_seq(cfg["kinematics"]["tcp_offset_tool0_m"], 3, "kinematics.tcp_offset_tool0_m"),
            calibration_hash=model_bundle.calibration_hash,
        )

    def evaluate(
        self,
        q: Sequence[float],
        qd: Sequence[float],
        timestamp_s: float,
    ) -> KinematicSample:
        q_array = np.asarray(_as_float_seq(q, 6, "q"), dtype=float)
        qd_array = np.asarray(_as_float_seq(qd, 6, "qd"), dtype=float)
        t_s = _as_float(timestamp_s, "timestamp_s")

        from pinocchio import (  # pylint: disable=import-outside-toplevel
            ReferenceFrame,
            computeJointJacobians,
            forwardKinematics,
            getFrameJacobian,
            log3,
            updateFramePlacements,
        )

        model = self.model_bundle.model
        data = self.model_bundle.data
        forwardKinematics(model, data, q_array, qd_array)
        computeJointJacobians(model, data, q_array)
        updateFramePlacements(model, data)

        base_to_tool0 = data.oMf[self.base_frame].inverse() * data.oMf[self.tool0_frame]
        tool_jac_world_aligned = getFrameJacobian(
            model,
            data,
            self.tool0_frame,
            ReferenceFrame.LOCAL_WORLD_ALIGNED,
        )

        tool_jac_world = np.asarray(tool_jac_world_aligned, dtype=float)
        world_to_base_rotation = np.asarray(data.oMf[self.base_frame].rotation, dtype=float).T
        jacobian_tool0_base = np.vstack(
            (
                world_to_base_rotation @ tool_jac_world[:3],
                world_to_base_rotation @ tool_jac_world[3:],
            )
        )
        offset_tool0 = np.asarray(self.tcp_offset_tool0_m, dtype=float)
        offset_base = np.asarray(base_to_tool0.rotation, dtype=float) @ offset_tool0

        jac_linear = jacobian_tool0_base[:3] - _skew(offset_base) @ jacobian_tool0_base[3:]
        jac_full = np.vstack((jac_linear, jacobian_tool0_base[3:]))

        tcp_twist_base = jac_full @ qd_array
        tcp_pose_base = base_to_tool0.translation + base_to_tool0.rotation @ offset_tool0

        rotvec = tuple(float(v) for v in log3(np.asarray(base_to_tool0.rotation, dtype=float)))

        return KinematicSample(
            q=tuple(float(v) for v in q_array),
            qd=tuple(float(v) for v in qd_array),
            tcp_pose=(float(tcp_pose_base[0]), float(tcp_pose_base[1]), float(tcp_pose_base[2]), rotvec[0], rotvec[1], rotvec[2]),
            tcp_twist=(
                float(tcp_twist_base[0]),
                float(tcp_twist_base[1]),
                float(tcp_twist_base[2]),
                float(tcp_twist_base[3]),
                float(tcp_twist_base[4]),
                float(tcp_twist_base[5]),
            ),
            wrench_tcp=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            jacobian=tuple(tuple(float(value) for value in row) for row in jac_full),
            q_min=self.q_min,
            q_max=self.q_max,
            timestamp_s=t_s,
        )


def build_model_bundle(*, calibration_yaml: Path, xacro_path: Path):
    from step5c_calibrated_kinematics_audit import build_calibrated_model

    return build_calibrated_model(calibration_yaml=calibration_yaml, xacro_path=xacro_path)
