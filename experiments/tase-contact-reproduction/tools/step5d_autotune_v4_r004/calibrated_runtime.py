"""Hash-bound calibrated Jacobian and strict-RNN command primitive for V4.

This module is side-effect free with respect to the controller.  It consumes a
fresh RTDE kinematic observation and returns a proposed joint velocity plus the
same calibrated TCP Jacobian used to verify that proposal before register
publication.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import numpy as np
import pinocchio as pin

from step5c_calibrated_kinematics_audit import (
    CalibratedModel,
    build_calibrated_model,
    rotvec_to_matrix,
)
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)

from step5d_autotune_v4.contracts import V4Candidate, V4Contract
from step5d_autotune_v4_r004.path_controller import derive_force_terms
from step5d_autotune_v4_r004.path_reference import step5_path_reference

if TYPE_CHECKING:
    from step5d_autotune_v4_r004.motion_profile import V4MotionProfile


PATH_STAGE_ID = "step5d_strict_rnn_autotune_v1"
TARGET_ROTVEC = np.asarray((3.120752062, 0.0, 0.068626833), dtype=float)
TCP_OFFSET_TOOL0 = np.asarray((0.0, 0.0, 0.0874), dtype=float)
SOLVER_GATE_PATH = Path(__file__).resolve().parents[2] / "config/step5d_liveprep_solver_gate.json"


class CalibratedRuntimeError(RuntimeError):
    """A source binding or live kinematic observation is invalid."""


def _validate_motion_profile(value: object) -> V4MotionProfile:
    from step5d_autotune_v4_r004.motion_profile import V4MotionProfile

    if not isinstance(value, V4MotionProfile):
        raise TypeError("motion_profile must be a typed V4MotionProfile")
    return value


def _finite_vector(value: Any, size: int, role: str) -> np.ndarray:
    try:
        result = np.asarray(tuple(float(item) for item in value), dtype=float)
    except (TypeError, ValueError) as exc:
        raise CalibratedRuntimeError(f"{role} must be a finite length-{size} vector") from exc
    if result.shape != (size,) or not np.all(np.isfinite(result)):
        raise CalibratedRuntimeError(f"{role} must be a finite length-{size} vector")
    return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def observed_model_hashes(contract: V4Contract) -> dict[str, str]:
    """Re-read every contract-bound model source before live publication."""

    binding = contract.raw.get("robot_model_binding")
    if not isinstance(binding, Mapping):
        raise CalibratedRuntimeError("robot_model_binding is missing")
    observed: dict[str, str] = {}
    for role in contract.model_hashes:
        path_text = binding.get(f"{role}_path")
        if not isinstance(path_text, str) or not path_text:
            raise CalibratedRuntimeError(f"{role} source path is missing")
        path = Path(path_text)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        path = path.resolve()
        if path.is_symlink() or not path.is_file():
            raise CalibratedRuntimeError(f"{role} source is unavailable")
        observed[role] = _sha256(path)
    if observed != dict(contract.model_hashes):
        raise CalibratedRuntimeError("robot-model/Jacobian source binding differs")
    return observed


def tcp_jacobian_base(
    model_bundle: CalibratedModel,
    actual_q: Sequence[float],
    tcp_offset_tool0: Sequence[float] = TCP_OFFSET_TOOL0,
) -> np.ndarray:
    """Calibrated base-frame TCP Jacobian, including the active TCP lever arm."""

    q = _finite_vector(actual_q, 6, "actual_q")
    offset = _finite_vector(tcp_offset_tool0, 3, "tcp_offset_tool0")
    model = model_bundle.model
    data = model_bundle.data
    pin.forwardKinematics(model, data, q)
    pin.computeJointJacobians(model, data, q)
    pin.updateFramePlacements(model, data)
    tool_jac_world = pin.getFrameJacobian(
        model,
        data,
        model_bundle.tool0_frame_id,
        pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
    )
    base_world_rotation = data.oMf[model_bundle.base_frame_id].rotation.T
    linear = base_world_rotation @ tool_jac_world[:3, :]
    angular = base_world_rotation @ tool_jac_world[3:, :]
    tool0_base = (
        data.oMf[model_bundle.base_frame_id].inverse()
        * data.oMf[model_bundle.tool0_frame_id]
    )
    lever_base = tool0_base.rotation @ offset
    skew = np.asarray(
        (
            (0.0, -lever_base[2], lever_base[1]),
            (lever_base[2], 0.0, -lever_base[0]),
            (-lever_base[1], lever_base[0], 0.0),
        ),
        dtype=float,
    )
    result = np.vstack((linear - skew @ angular, angular))
    if result.shape != (6, 6) or not np.all(np.isfinite(result)):
        raise CalibratedRuntimeError("calibrated TCP Jacobian is invalid")
    return result


@dataclass(frozen=True)
class CalibratedCommand:
    qdot: tuple[float, float, float, float, float, float]
    jacobian_6x6: tuple[tuple[float, ...], ...]
    observed_model_hashes: Mapping[str, str]
    tangential_error_m: tuple[float, float]
    orientation_error_rad: tuple[float, float, float]
    path_time_s: float
    solver_status: float


class V4CalibratedRuntime:
    """Stateful strict-RNN producer; the adapter invariant gate remains final."""

    def __init__(
        self,
        contract: V4Contract,
        candidate: V4Candidate,
        *,
        motion_profile: V4MotionProfile | None = None,
        force_integral_limit_n_s: float = 1.0,
    ) -> None:
        self.contract = contract
        self.candidate = candidate
        if motion_profile is not None:
            motion_profile = _validate_motion_profile(motion_profile)
        self.motion_profile = motion_profile
        limit = float(force_integral_limit_n_s)
        if not math.isfinite(limit) or limit <= 0.0:
            raise CalibratedRuntimeError(
                "force_integral_limit_n_s must be positive and finite"
            )
        self.force_integral_limit_n_s = limit
        self.model_hashes = observed_model_hashes(contract)
        self.model = build_calibrated_model()
        qdot_limit = 0.15 if motion_profile is None else motion_profile.qdot_cap_rad_s
        self.solver = StrictTaseRnnSolver(
            StrictRnnConfig(
                paper_truth_path=SOLVER_GATE_PATH,
                qdot_limit_rad_s=qdot_limit,
                epsilon=0.022,
                sigr_exponent_r=1.0,
                inner_iterations=1,
                backend="numpy",
            )
        )
        self._active_mode: str | None = None
        self._outer_state = Step5dOuterLoopState()

    def path_errors(
        self,
        *,
        actual_tcp_pose: Sequence[float],
        path_time_s: float,
        motion_kp: float,
    ) -> tuple[tuple[float, float], tuple[float, float, float]]:
        pose = _finite_vector(actual_tcp_pose, 6, "actual_TCP_pose")
        if not math.isfinite(path_time_s) or path_time_s < 0.0:
            raise CalibratedRuntimeError("path_time_s must be finite and non-negative")
        if not math.isfinite(motion_kp) or motion_kp <= 0.0:
            raise CalibratedRuntimeError("motion_kp must be positive")
        reference = step5_path_reference(
            PATH_STAGE_ID,
            (float(pose[0]), float(pose[1])),
            float(path_time_s),
        )
        error_xy = reference["path_error_xy"]
        feedforward_xy = reference["desired_velocity_xy"]
        tangential = (
            float(error_xy[0]) + float(feedforward_xy[0]) / motion_kp,
            float(error_xy[1]) + float(feedforward_xy[1]) / motion_kp,
        )
        desired_rotation = rotvec_to_matrix(TARGET_ROTVEC)
        actual_rotation = rotvec_to_matrix(pose[3:])
        orientation = pin.log3(desired_rotation @ actual_rotation.T)
        return (
            tangential,
            tuple(float(value) for value in orientation),  # type: ignore[return-value]
        )

    def command(
        self,
        *,
        actual_q: Sequence[float],
        actual_qd: Sequence[float],
        actual_tcp_pose: Sequence[float],
        desired_twist: Sequence[float],
        actual_dt_s: float,
        mode: str,
        path_time_s: float,
    ) -> CalibratedCommand:
        q = _finite_vector(actual_q, 6, "actual_q")
        qd = _finite_vector(actual_qd, 6, "actual_qd")
        _finite_vector(actual_tcp_pose, 6, "actual_TCP_pose")
        twist = _finite_vector(desired_twist, 6, "desired_twist")
        if not math.isfinite(actual_dt_s) or actual_dt_s <= 0.0 or actual_dt_s >= 0.08:
            raise CalibratedRuntimeError("actual_dt_s is outside (0,80ms)")
        if mode not in {"baseline", "path", "hold", "retract", "stop"}:
            raise CalibratedRuntimeError(f"unknown command mode: {mode}")
        jacobian = tcp_jacobian_base(self.model, q)
        qdot_limit = 0.15 if self.motion_profile is None else self.motion_profile.qdot_cap_rad_s
        lower = np.maximum(self.model.model.lowerPositionLimit - q, -qdot_limit)
        upper = np.minimum(self.model.model.upperPositionLimit - q, qdot_limit)
        if np.any(lower > upper):
            raise CalibratedRuntimeError("joint velocity bounds are inverted")
        if mode in {"hold", "retract", "stop"}:
            self.solver.freeze()
            qdot = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            status = 0.0
            self._active_mode = None
        else:
            if self._active_mode != mode:
                self.solver.reset_state()
                self.solver.warm_start(
                    J=jacobian,
                    xdot_c=twist,
                    omega_minus=lower,
                    omega_plus=upper,
                )
                self._active_mode = mode
            result = self.solver.solve(
                actual_q=q,
                actual_qd=qd,
                target_state={
                    "J": jacobian,
                    "xdot_c": twist,
                    "omega_minus": lower,
                    "omega_plus": upper,
                    "dt": float(actual_dt_s),
                    "epsilon": 0.022,
                    "r": 1.0,
                    "cmd_valid": True,
                },
            )
            qdot = tuple(float(value) for value in result.qdot)
            status = float(result.solver_status)
            if len(qdot) != 6 or not all(math.isfinite(value) for value in qdot):
                raise CalibratedRuntimeError("strict RNN returned invalid qdot")
        return CalibratedCommand(
            qdot=qdot,  # type: ignore[arg-type]
            jacobian_6x6=tuple(
                tuple(float(value) for value in row) for row in jacobian
            ),
            observed_model_hashes=dict(self.model_hashes),
            tangential_error_m=(0.0, 0.0),
            orientation_error_rad=(0.0, 0.0, 0.0),
            path_time_s=float(path_time_s),
            solver_status=status,
        )

    def desired_twist(
        self,
        *,
        actual_tcp_pose: Sequence[float],
        actual_tcp_speed: Sequence[float],
        force_tcp_n: Sequence[float],
        filtered_normal_n: float,
        internal_setpoint_n: float,
        actual_dt_s: float,
        mode: str,
        path_time_s: float,
    ) -> tuple[float, float, float, float, float, float]:
        """Run the mature paper-form P/I/D outer loop at monotonic actual dt."""

        pose = _finite_vector(actual_tcp_pose, 6, "actual_TCP_pose")
        speed = _finite_vector(actual_tcp_speed, 6, "actual_TCP_speed")
        force_tcp = _finite_vector(force_tcp_n, 3, "force_tcp_n")
        if not math.isfinite(filtered_normal_n):
            raise CalibratedRuntimeError("filtered_normal_n must be finite")
        if (
            not math.isfinite(internal_setpoint_n)
            or internal_setpoint_n < 1.0
            or internal_setpoint_n > 5.0
        ):
            raise CalibratedRuntimeError("internal_setpoint_n is outside [1,5]")
        if not math.isfinite(actual_dt_s) or actual_dt_s <= 0.0 or actual_dt_s >= 0.08:
            raise CalibratedRuntimeError("actual_dt_s is outside (0,80ms)")
        if mode not in {"baseline", "path", "hold", "retract", "stop"}:
            raise CalibratedRuntimeError(f"unknown outer-loop mode: {mode}")
        if mode in {"hold", "retract", "stop"}:
            return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        reference = step5_path_reference(
            PATH_STAGE_ID,
            (float(pose[0]), float(pose[1])),
            float(path_time_s if mode == "path" else 0.0),
        )
        if mode == "path":
            desired_xy = reference["desired_xy"]
            desired_vxy = reference["desired_velocity_xy"]
        else:
            desired_xy = (float(pose[0]), float(pose[1]))
            desired_vxy = (0.0, 0.0)

        # The installed TCP has tool +Z approximately base -Z.  Kunwei Fz is
        # transformed into base and its reaction-normal component is replaced
        # by the actual-dt filtered load so tau changes real control behavior.
        rotation_base_from_tcp = rotvec_to_matrix(pose[3:])
        reaction_normal_base = np.asarray((0.0, 0.0, 1.0), dtype=float)
        force_base = rotation_base_from_tcp @ force_tcp
        raw_normal = float(force_base @ reaction_normal_base)
        filtered_force_base = (
            force_base
            + (float(filtered_normal_n) - raw_normal) * reaction_normal_base
        )
        filtered_force_tcp = rotation_base_from_tcp.T @ filtered_force_base
        terms = derive_force_terms(self.candidate)
        output = compute_step5d_outer_loop(
            Step5dOuterLoopConfig(
                kp=self.candidate.motion_kp,
                ko=self.candidate.orientation_ko,
                kf=terms["kf"],
                Md_scalar=terms["Md"],
                Bd_scalar=terms["Bd"],
                force_target_n=float(internal_setpoint_n),
                force_integral_limit_n_s=float(self.force_integral_limit_n_s),
                delay_T_s=float(actual_dt_s),
                force_sign_convention="step5_step6_positive_normal_load",
            ),
            self._outer_state,
            Step5dOuterLoopInputs(
                tcp_pose_base=tuple(float(value) for value in pose),
                tcp_speed_base=tuple(float(value) for value in speed),
                force_tcp_n=tuple(float(value) for value in filtered_force_tcp),
                control_reaction_normal_base=(0.0, 0.0, 1.0),
                x_pd_base=(
                    float(desired_xy[0]),
                    float(desired_xy[1]),
                    float(pose[2]),
                ),
                xdot_pd_base=(
                    float(desired_vxy[0]),
                    float(desired_vxy[1]),
                    0.0,
                ),
                dt_s=float(actual_dt_s),
                cmd_valid=True,
            ),
            include_diagnostics="compact",
        )
        self._outer_state = output.next_state
        twist = tuple(float(value) for value in output.xdot_c)
        if len(twist) != 6 or not all(math.isfinite(value) for value in twist):
            raise CalibratedRuntimeError("paper outer loop returned invalid twist")
        # Bound the task-space proposal before strict RNN; the independent
        # Jacobian gate repeats the authoritative cap after solving qdot.
        linear = np.asarray(twist[:3], dtype=float)
        angular = np.asarray(twist[3:], dtype=float)
        if mode == "baseline":
            # Baseline qualification is a 1-D force primitive.  The mature
            # outer loop also computes a tool-orientation correction, but the
            # V4 baseline contract forbids XY and angular motion.  Remove those
            # optional path capabilities before strict-RNN composition.
            linear[:2] = 0.0
            angular[:] = 0.0
        if self.motion_profile is None:
            total_linear_cap = 0.0005
            normal_cap = 0.00035
            tangential_cap = 0.00035
            angular_cap = 0.05
        else:
            total_linear_cap = self.motion_profile.total_linear_cap_m_s
            normal_cap = self.motion_profile.normal_linear_cap_m_s
            tangential_cap = self.motion_profile.tangential_cap_m_s
            angular_cap = self.motion_profile.angular_cap_rad_s
        linear[2] = float(np.clip(linear[2], -normal_cap, normal_cap))
        tangential_norm = float(np.linalg.norm(linear[:2]))
        if tangential_norm > tangential_cap:
            linear[:2] *= tangential_cap / tangential_norm
        linear_norm = float(np.linalg.norm(linear))
        if linear_norm > total_linear_cap:
            linear *= total_linear_cap / linear_norm
        angular_norm = float(np.linalg.norm(angular))
        if angular_norm > angular_cap:
            angular *= angular_cap / angular_norm
        return tuple(float(value) for value in np.r_[linear, angular])  # type: ignore[return-value]


__all__ = [
    "CalibratedCommand",
    "CalibratedRuntimeError",
    "PATH_STAGE_ID",
    "V4CalibratedRuntime",
    "observed_model_hashes",
    "tcp_jacobian_base",
]
