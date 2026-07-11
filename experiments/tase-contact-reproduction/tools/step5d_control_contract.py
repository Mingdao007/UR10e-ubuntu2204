#!/usr/bin/env python3
"""Pure Step5d observation/policy/candidate/safety/register contracts.

This module has no network, RTDE, Dashboard, or controller side effects.  It is
the v30 seam between the paper outer loop, a joint-space policy, the safety
envelope, and the layout-524 register writer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol, runtime_checkable

import numpy as np


JOINT_LAYOUT_CODE = 524.0
STRICT_RNN_SOLVER_OK_STATUS = 40.0
ZERO6 = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
V30_QDOT_SLEW_RAD_S2 = 0.20
V30_GUARD_DT_MAX_S = 0.010


Vector3 = tuple[float, float, float]
Vector6 = tuple[float, float, float, float, float, float]
Matrix3 = tuple[Vector3, Vector3, Vector3]
Matrix6 = tuple[Vector6, Vector6, Vector6, Vector6, Vector6, Vector6]


@dataclass(frozen=True)
class Step5dObservation:
    sequence: int
    timestamp_s: float
    q: Vector6
    qd: Vector6
    tcp_pose: Vector6
    tcp_twist: Vector6
    wrench: Vector6
    jacobian: Matrix6
    desired_twist: Vector6
    reaction_normal: Vector3
    approach_normal: Vector3
    command_frame: str
    normal_frame: str
    normal_to_command_rotation: Matrix3 | None = None
    path_time_s: float = 0.0
    force_error_n: float = 0.0
    orientation_error_rad: float = 0.0
    omega_minus: Vector6 = ZERO6
    omega_plus: Vector6 = ZERO6
    dt_s: float = 0.002


@dataclass(frozen=True)
class ControlCandidate:
    qdot: Vector6
    predicted_twist: Vector6
    residual_norm: float
    active_bounds_count: int
    frame_id: str
    solver_status: str
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DlsShadowEvidence:
    qdot: Vector6
    predicted_twist: Vector6
    residual_norm: float
    saturation_count: int
    qdot_delta_norm: float
    twist_delta_norm: float
    desired_approach_m_s: float
    strict_rnn_approach_m_s: float
    dls_approach_m_s: float
    normal_sign_difference: bool
    runtime_fallback_allowed: bool = False


@runtime_checkable
class ControlPolicy(Protocol):
    """A pure policy maps one immutable observation to one candidate."""

    def compute(self, observation: Step5dObservation) -> ControlCandidate:
        ...


class StrictRnnControlPolicy:
    """v30 policy adapter; DLS is deliberately absent from this interface."""

    def __init__(self, solver: Any) -> None:
        self.solver = solver

    def compute(self, observation: Step5dObservation) -> ControlCandidate:
        jacobian = _finite_array(observation.jacobian, (6, 6))
        desired = _finite_array(observation.desired_twist, (6,))
        lower = _finite_array(observation.omega_minus, (6,))
        upper = _finite_array(observation.omega_plus, (6,))
        if (
            jacobian is None
            or desired is None
            or lower is None
            or upper is None
            or np.any(lower > upper)
            or not math.isfinite(float(observation.dt_s))
            or observation.dt_s <= 0.0
        ):
            raise ValueError("strict-RNN policy observation is invalid")
        result = self.solver.solve(
            actual_q=observation.q,
            actual_qd=observation.qd,
            target_state={
                "J": jacobian,
                "xdot_c": desired,
                "omega_minus": lower,
                "omega_plus": upper,
                "dt": float(observation.dt_s),
                "epsilon": getattr(self.solver.config, "epsilon", None),
                "r": getattr(self.solver.config, "sigr_exponent_r", None),
                "cmd_valid": True,
            },
        )
        qdot = _finite_array(result.qdot, (6,))
        if qdot is None:
            raise ValueError("strict-RNN policy returned nonfinite qdot")
        predicted = jacobian @ qdot
        diagnostics = result.diagnostics
        active_bounds = diagnostics.get("active_bounds_mask", ())
        return ControlCandidate(
            qdot=tuple(float(value) for value in qdot),  # type: ignore[arg-type]
            predicted_twist=tuple(float(value) for value in predicted),  # type: ignore[arg-type]
            residual_norm=float(result.residual_norm),
            active_bounds_count=sum(bool(value) for value in active_bounds),
            frame_id=observation.command_frame,
            solver_status=str(result.solver_status),
            diagnostics=diagnostics,
        )


def compute_dls_shadow(
    observation: Step5dObservation,
    strict_rnn_candidate: ControlCandidate,
    *,
    damping: float = 1e-4,
    _workspace: _ControlTickWorkspace | None = None,
) -> DlsShadowEvidence:
    """Compute same-input DLS evidence without producing a command.

    The return type has no register conversion and explicitly forbids runtime
    fallback.  This function may support warm-start/shadow diagnostics only.
    """

    if _workspace is not None and _workspace.candidate_qdot is not None:
        jacobian = _workspace.jacobian
        desired = _workspace.desired
        lower = _workspace.lower
        upper = _workspace.upper
        strict_qdot = _workspace.candidate_qdot
        approach = _workspace.approach
        normal_error = None
    else:
        jacobian = _finite_array(observation.jacobian, (6, 6))
        desired = _finite_array(observation.desired_twist, (6,))
        lower = _finite_array(observation.omega_minus, (6,))
        upper = _finite_array(observation.omega_plus, (6,))
        strict_qdot = _finite_array(strict_rnn_candidate.qdot, (6,))
        _reaction, approach, _frame_transform_applied, normal_error = (
            _canonical_normals_in_command_frame(observation)
        )
    if normal_error is not None:
        raise ValueError(f"DLS shadow normal contract invalid: {normal_error}")
    if any(value is None for value in (jacobian, desired, lower, upper, strict_qdot, approach)):
        raise ValueError("DLS shadow inputs must be finite canonical arrays")
    if not math.isfinite(float(damping)) or damping <= 0.0:
        raise ValueError("DLS shadow damping must be finite and positive")
    assert jacobian is not None and desired is not None
    assert lower is not None and upper is not None and strict_qdot is not None and approach is not None
    if np.any(lower > upper):
        raise ValueError("DLS shadow bounds are invalid")
    lhs = jacobian @ jacobian.T + (float(damping) ** 2) * np.eye(6)
    qdot = jacobian.T @ np.linalg.solve(lhs, desired)
    qdot = np.clip(qdot, lower, upper)
    twist = jacobian @ qdot
    strict_twist = jacobian @ strict_qdot
    desired_approach = float(np.dot(desired[:3], approach))
    strict_approach = float(np.dot(strict_twist[:3], approach))
    dls_approach = float(np.dot(twist[:3], approach))
    saturation_count = int(
        np.count_nonzero(np.isclose(qdot, lower) | np.isclose(qdot, upper))
    )
    return DlsShadowEvidence(
        qdot=tuple(float(value) for value in qdot),  # type: ignore[arg-type]
        predicted_twist=tuple(float(value) for value in twist),  # type: ignore[arg-type]
        residual_norm=float(np.linalg.norm(twist - desired)),
        saturation_count=saturation_count,
        qdot_delta_norm=float(np.linalg.norm(qdot - strict_qdot)),
        twist_delta_norm=float(np.linalg.norm(twist - strict_twist)),
        desired_approach_m_s=desired_approach,
        strict_rnn_approach_m_s=strict_approach,
        dls_approach_m_s=dls_approach,
        normal_sign_difference=(strict_approach > 0.0) != (dls_approach > 0.0),
        runtime_fallback_allowed=False,
    )


@dataclass(frozen=True)
class SafetyDecision:
    accepted: bool
    action: str
    reason: str
    qdot: Vector6
    metrics: Mapping[str, float]


@dataclass(frozen=True)
class RegisterCommand:
    """Host-side semantic command before writing RTDE input registers."""

    heartbeat: float
    qdot: Vector6
    cmd_valid: bool
    path_time_s: float
    force_error_n: float
    orientation_error_rad: float
    layout_code: float
    stop_request: bool
    decision_reason: str

    def as_register_values(self) -> dict[int, float]:
        """Return only the v29/v30 register values owned by this contract."""
        values = {26: self.heartbeat, 28: 1.0 if self.stop_request else 0.0}
        values.update({37 + index: value for index, value in enumerate(self.qdot)})
        values.update(
            {
                43: 1.0 if self.cmd_valid else 0.0,
                44: self.path_time_s,
                45: self.force_error_n,
                46: self.orientation_error_rad,
                47: self.layout_code,
            }
        )
        return values


@dataclass(frozen=True)
class Step5dControlStepResult:
    """One auditable production control step shared by bridge and simulators.

    Only ``register_command.qdot`` is eligible to reach a command sink.  The
    DLS result is deliberately evidence-only and has no command conversion.
    """

    raw_candidate: ControlCandidate
    candidate: ControlCandidate
    dls_shadow: DlsShadowEvidence | None
    decision: SafetyDecision
    register_command: RegisterCommand


def _finite_array(values: Any, shape: tuple[int, ...]) -> np.ndarray | None:
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError):
        return None
    if array.shape != shape or not np.all(np.isfinite(array)):
        return None
    return array


def _normalized(values: Any) -> np.ndarray | None:
    vector = _finite_array(values, (3,))
    if vector is None:
        return None
    norm = float(np.linalg.norm(vector))
    if norm < 1e-9:
        return None
    return vector / norm


def _rotation(values: Any) -> np.ndarray | None:
    matrix = _finite_array(values, (3, 3))
    if matrix is None:
        return None
    if not np.allclose(matrix @ matrix.T, np.eye(3), atol=1e-6):
        return None
    if not math.isclose(float(np.linalg.det(matrix)), 1.0, abs_tol=1e-6):
        return None
    return matrix


def _canonical_normals_in_command_frame(
    observation: Step5dObservation,
    *,
    normal_contract_tolerance: float = 1e-6,
) -> tuple[np.ndarray | None, np.ndarray | None, float, str | None]:
    reaction = _normalized(observation.reaction_normal)
    approach = _normalized(observation.approach_normal)
    if reaction is None or approach is None:
        return None, None, 0.0, "nonfinite_or_bad_shape"
    if float(np.linalg.norm(reaction + approach)) > normal_contract_tolerance:
        return None, None, 0.0, "normal_contract_mismatch"
    frame_transform_applied = 0.0
    if observation.normal_frame != observation.command_frame:
        rotation = _rotation(observation.normal_to_command_rotation)
        if rotation is None:
            return None, None, 0.0, "missing_normal_frame_transform"
        reaction = rotation @ reaction
        approach = rotation @ approach
        frame_transform_applied = 1.0
    elif observation.normal_to_command_rotation is not None:
        rotation = _rotation(observation.normal_to_command_rotation)
        if rotation is None:
            return None, None, 0.0, "invalid_normal_frame_transform"
        if not np.allclose(rotation, np.eye(3), atol=1e-6):
            return None, None, 0.0, "ambiguous_same_frame_transform"
    return reaction, approach, frame_transform_applied, None


@dataclass
class _ControlTickWorkspace:
    """Validated arrays shared only within one production control tick.

    Public helpers still validate independently when called on their own.  The
    combined production pipeline builds this workspace once, then reuses the
    exact same finite arrays through slew, SafetyEnvelope, and DLS shadow.  A
    malformed input declines the fast path and is re-evaluated by the original
    fail-closed helper sequence so rejection reasons cannot drift.
    """

    q: np.ndarray
    qd: np.ndarray
    pose: np.ndarray
    tcp_twist: np.ndarray
    wrench: np.ndarray
    jacobian: np.ndarray
    desired: np.ndarray
    lower: np.ndarray
    upper: np.ndarray
    raw_qdot: np.ndarray
    previous_qdot: np.ndarray
    reaction: np.ndarray
    approach: np.ndarray
    frame_transform_applied: float
    candidate_qdot: np.ndarray | None = None
    candidate_twist: np.ndarray | None = None


def _prepare_control_tick_workspace(
    observation: Step5dObservation,
    raw_candidate: ControlCandidate,
    previous_qdot: Vector6,
) -> _ControlTickWorkspace | None:
    arrays = (
        _finite_array(observation.q, (6,)),
        _finite_array(observation.qd, (6,)),
        _finite_array(observation.tcp_pose, (6,)),
        _finite_array(observation.tcp_twist, (6,)),
        _finite_array(observation.wrench, (6,)),
        _finite_array(observation.jacobian, (6, 6)),
        _finite_array(observation.desired_twist, (6,)),
        _finite_array(observation.omega_minus, (6,)),
        _finite_array(observation.omega_plus, (6,)),
        _finite_array(raw_candidate.qdot, (6,)),
        _finite_array(previous_qdot, (6,)),
    )
    if any(value is None for value in arrays):
        return None
    reaction, approach, frame_transform_applied, normal_error = (
        _canonical_normals_in_command_frame(observation)
    )
    if normal_error is not None or reaction is None or approach is None:
        return None
    (
        q,
        qd,
        pose,
        tcp_twist,
        wrench,
        jacobian,
        desired,
        lower,
        upper,
        raw_qdot,
        previous,
    ) = arrays
    assert all(value is not None for value in arrays)
    return _ControlTickWorkspace(
        q=q,  # type: ignore[arg-type]
        qd=qd,  # type: ignore[arg-type]
        pose=pose,  # type: ignore[arg-type]
        tcp_twist=tcp_twist,  # type: ignore[arg-type]
        wrench=wrench,  # type: ignore[arg-type]
        jacobian=jacobian,  # type: ignore[arg-type]
        desired=desired,  # type: ignore[arg-type]
        lower=lower,  # type: ignore[arg-type]
        upper=upper,  # type: ignore[arg-type]
        raw_qdot=raw_qdot,  # type: ignore[arg-type]
        previous_qdot=previous,  # type: ignore[arg-type]
        reaction=reaction,
        approach=approach,
        frame_transform_applied=frame_transform_applied,
    )


@dataclass(frozen=True)
class SafetyEnvelope:
    qdot_cap_rad_s: float = 0.05
    max_normal_tracking_error_m_s: float = 5e-4
    max_residual_norm: float = 1e-3
    normal_contract_tolerance: float = 1e-6
    predicted_twist_tolerance: float = 1e-8

    def evaluate(
        self,
        observation: Step5dObservation,
        candidate: ControlCandidate,
        *,
        _workspace: _ControlTickWorkspace | None = None,
    ) -> SafetyDecision:
        """Validate one candidate and return execute/hold/stop fail-closed state."""

        if (
            _workspace is not None
            and _workspace.candidate_qdot is not None
            and _workspace.candidate_twist is not None
        ):
            q = _workspace.q
            qd = _workspace.qd
            pose = _workspace.pose
            tcp_twist = _workspace.tcp_twist
            wrench = _workspace.wrench
            J = _workspace.jacobian
            desired = _workspace.desired
            qdot = _workspace.candidate_qdot
            claimed_twist = _workspace.candidate_twist
        else:
            q = _finite_array(observation.q, (6,))
            qd = _finite_array(observation.qd, (6,))
            pose = _finite_array(observation.tcp_pose, (6,))
            tcp_twist = _finite_array(observation.tcp_twist, (6,))
            wrench = _finite_array(observation.wrench, (6,))
            J = _finite_array(observation.jacobian, (6, 6))
            desired = _finite_array(observation.desired_twist, (6,))
            qdot = _finite_array(candidate.qdot, (6,))
            claimed_twist = _finite_array(candidate.predicted_twist, (6,))
        finite_scalars = (
            observation.timestamp_s,
            observation.path_time_s,
            observation.force_error_n,
            observation.orientation_error_rad,
            candidate.residual_norm,
            float(candidate.active_bounds_count),
        )
        if (
            any(value is None for value in (q, qd, pose, tcp_twist, wrench, J, desired, qdot, claimed_twist))
            or any(not math.isfinite(float(value)) for value in finite_scalars)
            or int(observation.sequence) < 0
        ):
            return self._decision(False, "stop", "nonfinite_or_bad_shape")

        try:
            solver_status = float(candidate.solver_status)
        except (TypeError, ValueError):
            solver_status = math.nan
        if (
            not math.isfinite(solver_status)
            or solver_status != STRICT_RNN_SOLVER_OK_STATUS
        ):
            return self._decision(False, "stop", "strict_rnn_solver_status_invalid")

        assert J is not None and desired is not None and qdot is not None and claimed_twist is not None
        if not observation.command_frame or candidate.frame_id != observation.command_frame:
            return self._decision(False, "stop", "candidate_frame_mismatch")
        if _workspace is not None:
            reaction = _workspace.reaction
            approach = _workspace.approach
            frame_transform_applied = _workspace.frame_transform_applied
            normal_error = None
            if (
                float(np.linalg.norm(reaction + approach))
                > self.normal_contract_tolerance
            ):
                normal_error = "normal_contract_mismatch"
        else:
            reaction, approach, frame_transform_applied, normal_error = (
                _canonical_normals_in_command_frame(
                    observation,
                    normal_contract_tolerance=self.normal_contract_tolerance,
                )
            )
        if normal_error is not None:
            return self._decision(
                False,
                "stop" if normal_error == "nonfinite_or_bad_shape" else "safe_hold",
                normal_error,
            )
        assert reaction is not None and approach is not None

        computed_twist = J @ qdot
        if not np.allclose(computed_twist, claimed_twist, atol=self.predicted_twist_tolerance, rtol=0.0):
            return self._decision(False, "stop", "predicted_twist_contract_mismatch")
        qdot_max = float(np.max(np.abs(qdot)))
        if qdot_max > float(self.qdot_cap_rad_s) + 1e-12:
            return self._decision(False, "stop", "qdot_bound_exceeded", qdot_max_abs_rad_s=qdot_max)

        desired_approach = float(np.dot(desired[:3], approach))
        predicted_approach = float(np.dot(computed_twist[:3], approach))
        normal_tracking_error = abs(predicted_approach - desired_approach)
        metrics = {
            "desired_approach_m_s": desired_approach,
            "predicted_approach_m_s": predicted_approach,
            "normal_tracking_error_m_s": normal_tracking_error,
            "qdot_max_abs_rad_s": qdot_max,
            "residual_norm": float(candidate.residual_norm),
            "active_bounds_count": float(candidate.active_bounds_count),
            "frame_transform_applied": frame_transform_applied,
        }
        if desired_approach <= 0.0:
            return self._decision(False, "safe_hold", "outer_approach_not_pressing", **metrics)
        if predicted_approach <= 0.0:
            return self._decision(False, "safe_hold", "approach_normal_unload_mismatch", **metrics)
        if normal_tracking_error > float(self.max_normal_tracking_error_m_s):
            return self._decision(False, "safe_hold", "approach_normal_tracking_error", **metrics)
        if float(candidate.residual_norm) > float(self.max_residual_norm):
            return self._decision(False, "safe_hold", "constraint_residual_norm_exceeded", **metrics)
        if int(candidate.active_bounds_count) > 0:
            return self._decision(False, "safe_hold", "active_bounds_present", **metrics)
        return self._decision(True, "execute", "ok", qdot=qdot, **metrics)

    @staticmethod
    def _decision(
        accepted: bool,
        action: str,
        reason: str,
        *,
        qdot: np.ndarray | None = None,
        **metrics: float,
    ) -> SafetyDecision:
        values = ZERO6 if qdot is None else tuple(float(value) for value in qdot)
        return SafetyDecision(
            accepted=accepted,
            action=action,
            reason=reason,
            qdot=values,  # type: ignore[arg-type]
            metrics=metrics,
        )


def apply_direction_preserving_slew(
    observation: Step5dObservation,
    candidate: ControlCandidate,
    *,
    previous_qdot: Vector6 = ZERO6,
    dt_s: float = 0.002,
    max_slew_rad_s2: float = 0.2,
    dt_max_s: float = 0.02,
    copy_diagnostics: bool = True,
    _workspace: _ControlTickWorkspace | None = None,
) -> ControlCandidate:
    """Scale the whole qdot delta, then recompute Jqdot in the command frame.

    Component-wise clipping can reverse a Cartesian normal component through a
    coupled Jacobian.  One scalar scale preserves the delta direction; the
    returned candidate still has to pass :class:`SafetyEnvelope` again.
    """

    if _workspace is not None:
        qdot = _workspace.raw_qdot
        previous = _workspace.previous_qdot
        jacobian = _workspace.jacobian
        desired = _workspace.desired
    else:
        qdot = _finite_array(candidate.qdot, (6,))
        previous = _finite_array(previous_qdot, (6,))
        jacobian = _finite_array(observation.jacobian, (6, 6))
        desired = _finite_array(observation.desired_twist, (6,))
    if qdot is None or previous is None or jacobian is None or desired is None:
        raise ValueError("slew inputs must be finite six-dimensional values")
    if (
        not math.isfinite(float(dt_s))
        or dt_s <= 0.0
        or not math.isfinite(float(max_slew_rad_s2))
        or max_slew_rad_s2 <= 0.0
        or not math.isfinite(float(dt_max_s))
        or dt_max_s <= 0.0
    ):
        raise ValueError("slew timing and rate must be finite and positive")
    delta = qdot - previous
    delta_limit = float(max_slew_rad_s2) * min(float(dt_s), float(dt_max_s))
    max_delta = float(np.max(np.abs(delta)))
    scale = 1.0 if max_delta <= delta_limit or max_delta <= 0.0 else delta_limit / max_delta
    limited = previous + scale * delta
    predicted = jacobian @ limited
    if _workspace is not None:
        _workspace.candidate_qdot = limited
        _workspace.candidate_twist = predicted
    diagnostics = dict(candidate.diagnostics) if copy_diagnostics else {}
    diagnostics.update(
        {
            "direction_preserving_slew": True,
            "slew_active": scale < 1.0,
            "slew_scale": scale,
            "slew_limit_rad_s": delta_limit,
        }
    )
    return replace(
        candidate,
        qdot=tuple(float(value) for value in limited),  # type: ignore[arg-type]
        predicted_twist=tuple(float(value) for value in predicted),  # type: ignore[arg-type]
        residual_norm=float(np.linalg.norm(predicted - desired)),
        diagnostics=diagnostics,
    )


def decision_to_register_command(
    observation: Step5dObservation,
    decision: SafetyDecision,
) -> RegisterCommand:
    """Convert a decision to layout 524 without performing an RTDE write.

    A bounded semantic rejection keeps cmd_valid asserted with an exact zero
    qdot, so the TP holds rather than interpreting a transient invalid packet as
    a profile change.  Structural/nonfinite failures assert stop_request.
    """

    stop = decision.action == "stop"

    def finite_or_zero(value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
        return parsed if math.isfinite(parsed) else 0.0

    heartbeat = finite_or_zero(observation.sequence)
    if heartbeat < 0.0:
        heartbeat = 0.0
    return RegisterCommand(
        heartbeat=heartbeat,
        qdot=decision.qdot if decision.accepted else ZERO6,
        cmd_valid=decision.accepted or decision.action == "safe_hold",
        path_time_s=finite_or_zero(observation.path_time_s),
        force_error_n=finite_or_zero(observation.force_error_n),
        orientation_error_rad=finite_or_zero(observation.orientation_error_rad),
        layout_code=JOINT_LAYOUT_CODE,
        stop_request=stop,
        decision_reason=decision.reason,
    )


def fail_closed_control_step(
    observation: Step5dObservation,
    *,
    reason: str,
    deferred_diagnostics: "DeferredV30Diagnostics",
    solver_status: str = "invalid",
) -> Step5dControlStepResult:
    """Create and record an exact-zero structural failure result."""

    failure = ControlCandidate(
        qdot=ZERO6,
        predicted_twist=ZERO6,
        residual_norm=math.inf,
        active_bounds_count=0,
        frame_id=observation.command_frame or "invalid",
        solver_status=solver_status,
        diagnostics={"contract_failure": reason},
    )
    decision = SafetyDecision(
        accepted=False,
        action="stop",
        reason=reason,
        qdot=ZERO6,
        metrics={},
    )
    command = decision_to_register_command(observation, decision)
    if command.qdot != ZERO6:
        raise RuntimeError("fail-closed control step did not produce exact zero")
    if not deferred_diagnostics.record(
        observation,
        failure,
        decision,
        command,
        None,
    ):
        raise RuntimeError("v30 deferred diagnostics capacity exhausted")
    return Step5dControlStepResult(
        raw_candidate=failure,
        candidate=failure,
        dls_shadow=None,
        decision=decision,
        register_command=command,
    )


def step5d_v30_contract_pipeline(
    observation: Step5dObservation,
    raw_candidate: ControlCandidate,
    *,
    previous_qdot: Vector6 | None,
    safety_envelope: SafetyEnvelope,
    deferred_diagnostics: "DeferredV30Diagnostics",
    max_slew_rad_s2: float = V30_QDOT_SLEW_RAD_S2,
    dt_max_s: float = V30_GUARD_DT_MAX_S,
) -> Step5dControlStepResult:
    """Exact candidate -> slew -> DLS-shadow -> safety -> register seam.

    This is intentionally engine- and transport-independent.  The live bridge,
    wall-clock harness, MuJoCo adapter, and Gazebo adapter must call this same
    function rather than reproducing its logic.
    """

    try:
        prior = previous_qdot or ZERO6
        workspace = _prepare_control_tick_workspace(
            observation,
            raw_candidate,
            prior,
        )
        candidate = apply_direction_preserving_slew(
            observation,
            raw_candidate,
            previous_qdot=prior,
            dt_s=float(observation.dt_s),
            max_slew_rad_s2=float(max_slew_rad_s2),
            dt_max_s=float(dt_max_s),
            copy_diagnostics=False,
            _workspace=workspace,
        )
        decision = safety_envelope.evaluate(
            observation,
            candidate,
            _workspace=workspace,
        )
        try:
            dls_shadow = compute_dls_shadow(
                observation,
                candidate,
                _workspace=workspace,
            )
        except (ValueError, np.linalg.LinAlgError, FloatingPointError, OverflowError):
            if decision.accepted:
                raise
            # A candidate already rejected by the authoritative SafetyEnvelope
            # must remain rejected with its specific reason.  Missing DLS
            # evidence cannot become a fallback or mask a normal/frame guard.
            dls_shadow = None
    except (
        ValueError,
        RuntimeError,
        np.linalg.LinAlgError,
        FloatingPointError,
        OverflowError,
        AttributeError,
        TypeError,
    ) as exc:
        return fail_closed_control_step(
            observation,
            reason=f"v30_contract_structural_failure:{type(exc).__name__}",
            deferred_diagnostics=deferred_diagnostics,
            solver_status=str(getattr(raw_candidate, "solver_status", "invalid")),
        )
    command = decision_to_register_command(observation, decision)
    if not decision.accepted and command.qdot != ZERO6:
        raise RuntimeError("rejected v30 candidate did not produce exact zero")
    if not deferred_diagnostics.record(
        observation,
        candidate,
        decision,
        command,
        dls_shadow,
    ):
        raise RuntimeError("v30 deferred diagnostics capacity exhausted")
    return Step5dControlStepResult(
        raw_candidate=raw_candidate,
        candidate=candidate,
        dls_shadow=dls_shadow,
        decision=decision,
        register_command=command,
    )


def step5d_v30_control_step(
    observation: Step5dObservation,
    policy: ControlPolicy,
    *,
    previous_qdot: Vector6 | None,
    safety_envelope: SafetyEnvelope,
    deferred_diagnostics: "DeferredV30Diagnostics",
    max_slew_rad_s2: float = V30_QDOT_SLEW_RAD_S2,
    dt_max_s: float = V30_GUARD_DT_MAX_S,
) -> Step5dControlStepResult:
    """Run policy and production contract as one fail-closed operation.

    A policy exception, missing return value, wrong type, or malformed output
    cannot escape before an exact-zero stop command and evidence row exist.
    """

    try:
        raw_candidate = policy.compute(observation)
        if not isinstance(raw_candidate, ControlCandidate):
            raise TypeError("ControlPolicy must return ControlCandidate")
    except (
        ValueError,
        RuntimeError,
        np.linalg.LinAlgError,
        FloatingPointError,
        OverflowError,
        AttributeError,
        TypeError,
    ) as exc:
        return fail_closed_control_step(
            observation,
            reason=f"strict_rnn_policy_failure:{type(exc).__name__}",
            deferred_diagnostics=deferred_diagnostics,
        )
    return step5d_v30_contract_pipeline(
        observation,
        raw_candidate,
        previous_qdot=previous_qdot,
        safety_envelope=safety_envelope,
        deferred_diagnostics=deferred_diagnostics,
        max_slew_rad_s2=max_slew_rad_s2,
        dt_max_s=dt_max_s,
    )


V30_DEFERRED_NUMERIC_FIELDS = (
    "sequence",
    "timestamp_s",
    "accepted",
    "stop_request",
    "cmd_valid",
    "path_time_s",
    "force_error_n",
    "orientation_error_rad",
    "residual_norm",
    "active_bounds_count",
    "desired_approach_m_s",
    "predicted_approach_m_s",
    *(f"qdot_{index}" for index in range(6)),
    *(f"predicted_twist_{index}" for index in range(6)),
    *(f"dls_shadow_qdot_{index}" for index in range(6)),
    *(f"dls_shadow_twist_{index}" for index in range(6)),
    "dls_shadow_residual_norm",
    "dls_shadow_saturation_count",
    "dls_shadow_qdot_delta_norm",
    "dls_shadow_twist_delta_norm",
    "dls_shadow_normal_sign_difference",
    *(f"register_{index}" for index in (26, 28, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47)),
)


class DeferredV30Diagnostics:
    """Preallocated, lossless per-tick evidence buffer for a bounded run.

    Recording performs no file I/O and grows no Python container.  A future
    live runner must provision the full canary/run capacity before entry and
    flush ``numeric[:count]`` plus the fixed reason/action arrays only after
    leaving the 500 Hz loop.  Capacity exhaustion is a fail-closed condition.
    """

    def __init__(self, capacity: int = 33_000) -> None:
        if int(capacity) < 1:
            raise ValueError("deferred diagnostic capacity must be positive")
        self.capacity = int(capacity)
        self.numeric = np.empty(
            (self.capacity, len(V30_DEFERRED_NUMERIC_FIELDS)),
            dtype=np.float64,
        )
        self.reasons: list[str | None] = [None] * self.capacity
        self.actions: list[str | None] = [None] * self.capacity
        self.count = 0
        self.overflowed = False
        self._field_index = {
            name: index for index, name in enumerate(V30_DEFERRED_NUMERIC_FIELDS)
        }
        self._qdot_slice = slice(
            self._field_index["qdot_0"], self._field_index["qdot_5"] + 1
        )
        self._predicted_slice = slice(
            self._field_index["predicted_twist_0"],
            self._field_index["predicted_twist_5"] + 1,
        )
        self._dls_qdot_slice = slice(
            self._field_index["dls_shadow_qdot_0"],
            self._field_index["dls_shadow_qdot_5"] + 1,
        )
        self._dls_twist_slice = slice(
            self._field_index["dls_shadow_twist_0"],
            self._field_index["dls_shadow_twist_5"] + 1,
        )
        self._register_qdot_slice = slice(
            self._field_index["register_37"], self._field_index["register_42"] + 1
        )

    def record(
        self,
        observation: Step5dObservation,
        candidate: ControlCandidate,
        decision: SafetyDecision,
        command: RegisterCommand,
        dls_shadow: DlsShadowEvidence | None = None,
    ) -> bool:
        if self.count >= self.capacity:
            self.overflowed = True
            return False
        row = self.numeric[self.count]
        field = self._field_index
        row[field["sequence"]] = float(observation.sequence)
        row[field["timestamp_s"]] = float(observation.timestamp_s)
        row[field["accepted"]] = 1.0 if decision.accepted else 0.0
        row[field["stop_request"]] = 1.0 if command.stop_request else 0.0
        row[field["cmd_valid"]] = 1.0 if command.cmd_valid else 0.0
        row[field["path_time_s"]] = command.path_time_s
        row[field["force_error_n"]] = command.force_error_n
        row[field["orientation_error_rad"]] = command.orientation_error_rad
        row[field["residual_norm"]] = float(candidate.residual_norm)
        row[field["active_bounds_count"]] = float(candidate.active_bounds_count)
        row[field["desired_approach_m_s"]] = float(
            decision.metrics.get("desired_approach_m_s", math.nan)
        )
        row[field["predicted_approach_m_s"]] = float(
            decision.metrics.get("predicted_approach_m_s", math.nan)
        )
        row[self._qdot_slice] = candidate.qdot
        row[self._predicted_slice] = candidate.predicted_twist
        if dls_shadow is None:
            row[self._dls_qdot_slice] = math.nan
            row[self._dls_twist_slice] = math.nan
            row[field["dls_shadow_residual_norm"]] = math.nan
            row[field["dls_shadow_saturation_count"]] = math.nan
            row[field["dls_shadow_qdot_delta_norm"]] = math.nan
            row[field["dls_shadow_twist_delta_norm"]] = math.nan
            row[field["dls_shadow_normal_sign_difference"]] = math.nan
        else:
            if dls_shadow.runtime_fallback_allowed:
                raise ValueError("DLS shadow must never be a runtime fallback")
            row[self._dls_qdot_slice] = dls_shadow.qdot
            row[self._dls_twist_slice] = dls_shadow.predicted_twist
            row[field["dls_shadow_residual_norm"]] = dls_shadow.residual_norm
            row[field["dls_shadow_saturation_count"]] = float(
                dls_shadow.saturation_count
            )
            row[field["dls_shadow_qdot_delta_norm"]] = dls_shadow.qdot_delta_norm
            row[field["dls_shadow_twist_delta_norm"]] = dls_shadow.twist_delta_norm
            row[field["dls_shadow_normal_sign_difference"]] = (
                1.0 if dls_shadow.normal_sign_difference else 0.0
            )
        row[field["register_26"]] = command.heartbeat
        row[field["register_28"]] = 1.0 if command.stop_request else 0.0
        row[self._register_qdot_slice] = command.qdot
        row[field["register_43"]] = 1.0 if command.cmd_valid else 0.0
        row[field["register_44"]] = command.path_time_s
        row[field["register_45"]] = command.force_error_n
        row[field["register_46"]] = command.orientation_error_rad
        row[field["register_47"]] = command.layout_code
        self.reasons[self.count] = decision.reason
        self.actions[self.count] = decision.action
        self.count += 1
        return True
