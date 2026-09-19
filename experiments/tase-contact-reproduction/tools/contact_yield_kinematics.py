"""UR10e calibrated TCP kinematics/Jacobian, with an explicit Cartesian fallback.

When the calibrated Pinocchio model and xacro sources load, this module uses
the same TCP lever-arm Jacobian as the existing Step5c helper.  If those
sources are unavailable, callers must label the plant as a simplified
Cartesian servo and must not claim full-robot qualification.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from contact_semantics import finite_vector3, finite_vector6


TCP_OFFSET_TOOL0 = np.asarray((0.0, 0.0, 0.0874), dtype=float)
ROS_PYTHON_PATHS = (
    "/opt/ros/humble/lib/python3.10/site-packages",
    "/opt/ros/humble/local/lib/python3.10/dist-packages",
)


class KinematicsError(RuntimeError):
    pass


def _ensure_ros_pythonpath() -> None:
    for path in ROS_PYTHON_PATHS:
        if Path(path).is_dir() and path not in sys.path:
            sys.path.insert(0, path)


@dataclass(frozen=True)
class RobotKinematics:
    kind: str
    calibration_hash: str | None
    tcp_offset_tool0: tuple[float, float, float]
    claim_scope: str

    def pose_and_jacobian(self, q: Any) -> dict[str, Any]:
        raise NotImplementedError

    def mass_matrix(self, q: Any) -> np.ndarray | None:
        return None


class CartesianServoKinematics(RobotKinematics):
    """Identity map from a 6-vector Cartesian state.  Not a UR10e model."""

    def __init__(self) -> None:
        super().__init__(
            kind="simplified_cartesian_servo",
            calibration_hash=None,
            tcp_offset_tool0=tuple(float(value) for value in TCP_OFFSET_TOOL0),
            claim_scope=(
                "simplified Cartesian servo; not full robot qualification; "
                "Jacobian is identity on a 6-vector pose-rate state"
            ),
        )

    def pose_and_jacobian(self, q: Any) -> dict[str, Any]:
        pose = finite_vector6(q, "cartesian_pose_rate_state")
        return {
            "position_m": tuple(float(value) for value in pose[:3]),
            "rotation": np.eye(3),
            "jacobian": np.eye(6),
            "kind": self.kind,
            "claim_scope": self.claim_scope,
        }


class Ur10eKinematics(RobotKinematics):
    def __init__(self, model_bundle: Any) -> None:
        super().__init__(
            kind="ur10e_calibrated_pinocchio",
            calibration_hash=str(model_bundle.calibration_hash),
            tcp_offset_tool0=tuple(float(value) for value in TCP_OFFSET_TOOL0),
            claim_scope=(
                "offline calibrated UR10e FK/Jacobian from existing Step5c "
                "helpers; URDF inertia, not identified payload dynamics, and "
                "not hardware qualification"
            ),
        )
        self._bundle = model_bundle

    def pose_and_jacobian(self, q: Any) -> dict[str, Any]:
        import pinocchio as pin

        joints = finite_vector6(q, "actual_q")
        model = self._bundle.model
        data = self._bundle.data
        pin.forwardKinematics(model, data, joints)
        pin.computeJointJacobians(model, data, joints)
        pin.updateFramePlacements(model, data)
        tool_jac_world = pin.getFrameJacobian(
            model,
            data,
            self._bundle.tool0_frame_id,
            pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
        )
        base_world_rotation = data.oMf[self._bundle.base_frame_id].rotation.T
        linear = base_world_rotation @ tool_jac_world[:3, :]
        angular = base_world_rotation @ tool_jac_world[3:, :]
        tool0_base = (
            data.oMf[self._bundle.base_frame_id].inverse()
            * data.oMf[self._bundle.tool0_frame_id]
        )
        lever_base = tool0_base.rotation @ TCP_OFFSET_TOOL0
        skew = np.array(
            (
                (0.0, -lever_base[2], lever_base[1]),
                (lever_base[2], 0.0, -lever_base[0]),
                (-lever_base[1], lever_base[0], 0.0),
            ),
            dtype=float,
        )
        jacobian = np.vstack((linear - skew @ angular, angular))
        rotation = tool0_base.rotation.copy()
        position = tool0_base.translation + lever_base
        if jacobian.shape != (6, 6) or not np.all(np.isfinite(jacobian)):
            raise KinematicsError("calibrated TCP Jacobian is invalid")
        return {
            "position_m": tuple(float(value) for value in position),
            "rotation": rotation,
            "jacobian": jacobian,
            "kind": self.kind,
            "claim_scope": self.claim_scope,
        }

    def mass_matrix(self, q: Any) -> np.ndarray | None:
        import pinocchio as pin

        joints = finite_vector6(q, "actual_q")
        pin.crba(self._bundle.model, self._bundle.data, joints)
        mass = np.array(self._bundle.data.M, dtype=float, copy=True)
        mass = 0.5 * (mass + mass.T)
        if mass.shape != (6, 6) or not np.all(np.isfinite(mass)):
            raise KinematicsError("URDF mass matrix is invalid")
        return mass


def try_load_ur10e_kinematics() -> Ur10eKinematics | None:
    _ensure_ros_pythonpath()
    try:
        from step5c_calibrated_kinematics_audit import build_calibrated_model
    except Exception:
        return None
    try:
        bundle = build_calibrated_model()
    except Exception:
        return None
    return Ur10eKinematics(bundle)


def load_kinematics(*, require_ur10e: bool = False) -> RobotKinematics:
    loaded = try_load_ur10e_kinematics()
    if loaded is not None:
        return loaded
    if require_ur10e:
        raise KinematicsError("UR10e calibrated model helpers are unavailable")
    return CartesianServoKinematics()
