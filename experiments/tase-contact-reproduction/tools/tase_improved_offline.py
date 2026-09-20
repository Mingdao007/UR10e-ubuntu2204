#!/usr/bin/env python3
"""Offline-only TASE-improved controller primitives.

This module stays on the existing TASE outer-loop/provider seam.  The
improved variant adds only a local reaction-normal force projection and a
bounded weighted-slack box QP.  Its integral policy is selected in the shared
outer loop by ``CONTACT_GATED_LEAKY_POLICY``.  No bridge, robot I/O, TP
package, per-point curvature, or learned model is used here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import itertools
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from step5d_paper_outer_loop import (
    CONTACT_GATED_LEAKY_POLICY,
    Step5dOuterLoopConfig,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    rotvec_to_matrix,
)
from tase_method_adapters import (
    DEFAULT_OUTER_CONFIG,
    TaseAdapterResult,
    TaseOfflineMethodAdapter,
    _finite_matrix,
    _finite_vector,
    _tuple3,
    _tuple6,
)


IMPROVED_METHOD_NAME = "TASE_IMPROVED"
IMPROVED_VARIANT = "local-normal-gated-leaky-normal-priority-slack-qp"


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _finite_vector3(value: Any, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if array.shape != (3,) or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be a finite length-3 vector")
    return np.ascontiguousarray(array)


@dataclass(frozen=True)
class LocalNormalForceProjection:
    """Force split in the current local reaction-normal frame."""

    normal_base: tuple[float, float, float]
    force_base_n: tuple[float, float, float]
    normal_force_base_n: tuple[float, float, float]
    tangential_force_base_n: tuple[float, float, float]
    normal_force_n: float
    tangential_force_n: float
    force_norm_n: float


def project_local_normal_force(
    force_base: Any,
    reaction_normal_base: Any,
    *,
    min_norm: float = 1e-9,
) -> LocalNormalForceProjection:
    """Project a measured wrench force onto one local normal and its tangent.

    The normal is an instantaneous reaction-normal proxy supplied by the
    existing outer-loop seam (or derived from the measured force by that
    seam).  No surface map, per-point curvature, or learned model enters the
    projection.
    """

    force = _finite_vector3(force_base, "force_base")
    normal = _finite_vector3(reaction_normal_base, "reaction_normal_base")
    minimum = float(min_norm)
    if not math.isfinite(minimum) or minimum <= 0.0:
        raise ValueError("min_norm must be finite and positive")
    normal_norm = float(np.linalg.norm(normal))
    if normal_norm < minimum:
        raise ValueError("reaction_normal_base norm is too small")
    unit_normal = normal / normal_norm
    normal_force_n = float(np.dot(force, unit_normal))
    normal_force = normal_force_n * unit_normal
    tangential_force = force - normal_force
    return LocalNormalForceProjection(
        normal_base=_tuple3(unit_normal),
        force_base_n=_tuple3(force),
        normal_force_base_n=_tuple3(normal_force),
        tangential_force_base_n=_tuple3(tangential_force),
        normal_force_n=normal_force_n,
        tangential_force_n=float(np.linalg.norm(tangential_force)),
        force_norm_n=float(np.linalg.norm(force)),
    )


@dataclass(frozen=True)
class TaseImprovedConfig:
    """Software-only parameters for the improved variant."""

    force_integral_limit_n_s: float = 1.0
    force_contact_gate_n: float = 0.5
    force_integral_leak_tau_s: float = 0.5
    force_normal_velocity_limit_m_s: float = 0.003
    force_integral_authority_error_n: float = 0.5
    normal_weight: float = 100.0
    tangential_weight: float = 1.0
    orientation_weight: float = 0.25
    qp_regularization: float = 1e-8
    local_normal_min_norm: float = 1e-9

    def validate(self) -> None:
        values = (
            self.force_integral_limit_n_s,
            self.force_contact_gate_n,
            self.force_integral_leak_tau_s,
            self.force_normal_velocity_limit_m_s,
            self.force_integral_authority_error_n,
            self.normal_weight,
            self.tangential_weight,
            self.orientation_weight,
            self.qp_regularization,
            self.local_normal_min_norm,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("TASE-improved configuration must be finite")
        if self.force_integral_limit_n_s <= 0.0:
            raise ValueError("force_integral_limit_n_s must be positive")
        if self.force_contact_gate_n < 0.0:
            raise ValueError("force_contact_gate_n must be non-negative")
        if self.force_integral_leak_tau_s <= 0.0:
            raise ValueError("force_integral_leak_tau_s must be positive")
        if self.force_normal_velocity_limit_m_s <= 0.0:
            raise ValueError("force_normal_velocity_limit_m_s must be positive")
        if self.force_integral_authority_error_n <= 0.0:
            raise ValueError("force_integral_authority_error_n must be positive")
        if min(self.normal_weight, self.tangential_weight, self.orientation_weight) <= 0.0:
            raise ValueError("normal, tangential, and orientation weights must be positive")
        if self.normal_weight < self.tangential_weight:
            raise ValueError("normal_weight must be at least tangential_weight")
        if self.qp_regularization <= 0.0:
            raise ValueError("qp_regularization must be positive")
        if self.local_normal_min_norm <= 0.0:
            raise ValueError("local_normal_min_norm must be positive")


@dataclass(frozen=True)
class NormalPriorityQpResult:
    """Result of the deterministic bounded task-slack QP."""

    qdot: tuple[float, float, float, float, float, float]
    task_slack: tuple[float, float, float, float, float, float]
    actual_twist: tuple[float, float, float, float, float, float]
    residual: tuple[float, float, float, float, float, float]
    objective: float
    normal_slack: float
    tangential_slack: tuple[float, float]
    orientation_slack: tuple[float, float, float]
    residual_norm: float
    normal_residual: float
    tangential_residual_norm: float
    orientation_residual_norm: float
    bound_violation: float
    active_bounds: tuple[bool, bool, bool, bool, bool, bool]
    exact_feasible: bool
    active_set_candidates: int


def _tangent_basis(unit_normal: np.ndarray) -> np.ndarray:
    reference = np.array(
        (1.0, 0.0, 0.0) if abs(float(unit_normal[0])) < 0.9 else (0.0, 1.0, 0.0),
        dtype=float,
    )
    first = reference - float(np.dot(reference, unit_normal)) * unit_normal
    first /= float(np.linalg.norm(first))
    second = np.cross(unit_normal, first)
    second /= float(np.linalg.norm(second))
    return np.vstack((first, second))


def _solve_box_quadratic(
    H: np.ndarray,
    gradient_target: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    *,
    tolerance: float = 1e-9,
) -> tuple[np.ndarray, float, int]:
    """Solve a six-variable positive-definite box QP by active-set enumeration."""

    dimension = int(H.shape[0])
    best: tuple[float, np.ndarray] | None = None
    candidate_count = 0
    for active in itertools.product((-1, 0, 1), repeat=dimension):
        q = np.zeros(dimension, dtype=float)
        fixed = [index for index, status in enumerate(active) if status]
        free = [index for index, status in enumerate(active) if not status]
        if fixed:
            for index in fixed:
                q[index] = lower[index] if active[index] < 0 else upper[index]
        if free:
            H_ff = H[np.ix_(free, free)]
            rhs = gradient_target[free]
            if fixed:
                rhs = rhs - H[np.ix_(free, fixed)] @ q[fixed]
            try:
                q[free] = np.linalg.solve(H_ff, rhs)
            except np.linalg.LinAlgError:
                continue
        if not np.all(np.isfinite(q)):
            continue
        if np.any(q < lower - tolerance) or np.any(q > upper + tolerance):
            continue
        q = np.minimum(np.maximum(q, lower), upper)
        gradient = H @ q - gradient_target
        if any(
            (status < 0 and gradient[index] < -tolerance)
            or (status > 0 and gradient[index] > tolerance)
            for index, status in enumerate(active)
            if status
        ):
            continue
        candidate_count += 1
        objective = float(0.5 * q @ H @ q - gradient_target @ q)
        if best is None or objective < best[0]:
            best = (objective, q.copy())

    if best is None:
        # The enumeration is exact for a strictly convex box QP.  This
        # projected fallback is only a numerical guard for round-off in the
        # KKT test, never a live-time or network fallback.
        q = np.clip(np.linalg.solve(H, gradient_target), lower, upper)
        step = 1.0 / max(float(np.linalg.eigvalsh(H)[-1]), 1e-12)
        for _ in range(2000):
            next_q = np.clip(q - step * (H @ q - gradient_target), lower, upper)
            if np.allclose(next_q, q, rtol=0.0, atol=1e-12):
                break
            q = next_q
        best = (float(0.5 * q @ H @ q - gradient_target @ q), q)
    return best[1], best[0], candidate_count


def solve_normal_priority_qp(
    *,
    jacobian: Any,
    xdot_c: Any,
    reaction_normal_base: Any,
    omega_minus: Any,
    omega_plus: Any,
    normal_weight: float = 100.0,
    tangential_weight: float = 1.0,
    orientation_weight: float = 0.25,
    regularization: float = 1e-8,
) -> NormalPriorityQpResult:
    """Solve a bounded task-slack QP with normal-priority feasibility.

    The QP uses ``A qdot = desired_task + slack`` after resolving the local
    normal, two tangent directions, and orientation rows.  The normal slack is
    weighted more heavily than tangential/orientation slack.  Thus an
    infeasible task remains bounded while the policy spends feasibility budget
    on the contact-normal component first.
    """

    J = _finite_matrix(jacobian, (6, 6), "jacobian")
    desired_twist = _finite_vector(xdot_c, 6, "xdot_c")
    lower = _finite_vector(omega_minus, 6, "omega_minus")
    upper = _finite_vector(omega_plus, 6, "omega_plus")
    if np.any(lower > upper):
        raise ValueError("omega_minus must be <= omega_plus")
    reaction = _finite_vector3(reaction_normal_base, "reaction_normal_base")
    reaction_norm = float(np.linalg.norm(reaction))
    if reaction_norm < 1e-9:
        raise ValueError("reaction_normal_base norm is too small")
    reaction /= reaction_norm
    approach = -reaction
    tangent = _tangent_basis(reaction)
    A = np.vstack(
        (
            approach @ J[:3, :],
            tangent @ J[:3, :],
            J[3:, :],
        )
    )
    desired = np.concatenate(
        (
            np.asarray((float(approach @ desired_twist[:3]),), dtype=float),
            tangent @ desired_twist[:3],
            desired_twist[3:],
        )
    )
    weights = np.asarray(
        (float(normal_weight), float(tangential_weight), float(tangential_weight),
         float(orientation_weight), float(orientation_weight), float(orientation_weight)),
        dtype=float,
    )
    regularization_value = float(regularization)
    if not np.all(np.isfinite(weights)) or np.any(weights <= 0.0):
        raise ValueError("QP task weights must be finite and positive")
    if not math.isfinite(regularization_value) or regularization_value <= 0.0:
        raise ValueError("QP regularization must be finite and positive")
    weighted_A = weights[:, None] * A
    weighted_desired = weights * desired
    H = weighted_A.T @ weighted_A + regularization_value * np.eye(6)
    gradient_target = weighted_A.T @ weighted_desired
    qdot, _solver_objective, candidates = _solve_box_quadratic(
        H, gradient_target, lower, upper
    )
    actual_twist = J @ qdot
    residual = actual_twist - desired_twist
    task_slack = A @ qdot - desired
    bound_violation = float(max(0.0, np.max(lower - qdot), np.max(qdot - upper)))
    normal_slack = float(task_slack[0])
    tangential_slack = tuple(float(value) for value in task_slack[1:3])
    orientation_slack = tuple(float(value) for value in task_slack[3:6])
    objective = float(
        0.5 * np.dot(weights * task_slack, weights * task_slack)
        + 0.5 * regularization_value * np.dot(qdot, qdot)
    )
    return NormalPriorityQpResult(
        qdot=_tuple6(qdot),
        task_slack=_tuple6(task_slack),
        actual_twist=_tuple6(actual_twist),
        residual=_tuple6(residual),
        objective=float(objective),
        normal_slack=normal_slack,
        tangential_slack=tangential_slack,  # type: ignore[arg-type]
        orientation_slack=orientation_slack,  # type: ignore[arg-type]
        residual_norm=float(np.linalg.norm(residual)),
        normal_residual=abs(normal_slack),
        tangential_residual_norm=float(np.linalg.norm(task_slack[1:3])),
        orientation_residual_norm=float(np.linalg.norm(task_slack[3:6])),
        bound_violation=bound_violation,
        active_bounds=tuple(
            bool(np.isclose(qdot[index], lower[index], atol=1e-9, rtol=0.0)
                 or np.isclose(qdot[index], upper[index], atol=1e-9, rtol=0.0))
            for index in range(6)
        ),  # type: ignore[arg-type]
        exact_feasible=bool(
            np.max(np.abs(task_slack)) <= 1e-6 and bound_violation <= 1e-7
        ),
        active_set_candidates=candidates,
    )


class TaseImprovedOfflineMethodAdapter:
    """Explicit TASE-improved adapter with no transport or live eligibility."""

    schema = "tase-improved-offline-method-adapter-state-v1"
    offline_only = True
    live_eligible = False

    def __init__(
        self,
        *,
        config: TaseImprovedConfig = TaseImprovedConfig(),
        outer_config: Step5dOuterLoopConfig = DEFAULT_OUTER_CONFIG,
        method_name: str = IMPROVED_METHOD_NAME,
        variant: str = IMPROVED_VARIANT,
    ) -> None:
        config.validate()
        self.config = config
        self.method_name = str(method_name)
        self.variant = str(variant)
        self.outer_config = self._improved_outer_config(outer_config)
        self.outer_state = Step5dOuterLoopState()
        self.sample_count = 0
        self.stopped = False

    def _improved_outer_config(
        self, outer_config: Step5dOuterLoopConfig
    ) -> Step5dOuterLoopConfig:
        return replace(
            outer_config,
            force_integral_policy=CONTACT_GATED_LEAKY_POLICY,
            force_integral_limit_n_s=self.config.force_integral_limit_n_s,
            force_contact_gate_n=self.config.force_contact_gate_n,
            force_integral_leak_tau_s=self.config.force_integral_leak_tau_s,
            force_normal_velocity_limit_m_s=self.config.force_normal_velocity_limit_m_s,
            force_integral_authority_error_n=self.config.force_integral_authority_error_n,
        )

    def reset(self) -> None:
        self.outer_state = Step5dOuterLoopState()
        self.sample_count = 0
        self.stopped = False

    def stop(self, _reason: str = "offline_stop") -> None:
        self.stopped = True

    def close(self) -> None:
        self.stop("offline_close")

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "method_name": self.method_name,
            "variant": self.variant,
            "offline_only": True,
            "live_eligible": False,
            "stopped": bool(self.stopped),
            "sample_count": int(self.sample_count),
            "outer_loop": {
                "force_integral_n_s": float(self.outer_state.force_integral_n_s),
                "xdot_p_prev_m_s": [
                    float(value) for value in self.outer_state.xdot_p_prev_m_s
                ],
            },
        }

    def _restore_unchecked(self, state: Mapping[str, Any]) -> None:
        if (
            state.get("schema") != self.schema
            or state.get("method_name") != self.method_name
            or state.get("variant") != self.variant
            or state.get("offline_only") is not True
            or state.get("live_eligible") is not False
            or not isinstance(state.get("stopped"), bool)
        ):
            raise ValueError("TASE-improved snapshot identity differs")
        sample_count = state.get("sample_count")
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 0:
            raise ValueError("TASE-improved snapshot sample_count is invalid")
        outer = _mapping(state.get("outer_loop"), "outer_loop snapshot")
        force_integral = float(outer.get("force_integral_n_s"))
        previous = _finite_vector(outer.get("xdot_p_prev_m_s"), 3, "xdot_p_prev_m_s")
        if not math.isfinite(force_integral):
            raise ValueError("outer_loop.force_integral_n_s must be finite")
        self.outer_state = Step5dOuterLoopState(
            force_integral_n_s=force_integral,
            xdot_p_prev_m_s=_tuple3(previous),
        )
        self.sample_count = sample_count
        self.stopped = bool(state["stopped"])

    def restore(self, state: Mapping[str, Any]) -> None:
        """Restore the complete improved state as one transaction."""

        before = self.snapshot()
        try:
            self._restore_unchecked(state)
        except Exception as exc:
            try:
                self._restore_unchecked(before)
            except Exception as rollback_exc:  # pragma: no cover - invariant breach
                raise RuntimeError("TASE-improved restore rollback failed") from rollback_exc
            raise exc

    def _outer_inputs(
        self,
        measured: Mapping[str, Any],
        target: Mapping[str, Any],
        dt_s: float,
    ) -> tuple[Any, np.ndarray, np.ndarray, np.ndarray]:
        adjusted_target = dict(target)
        if adjusted_target.get("control_reaction_normal_base") is None:
            local_normal = measured.get("local_normal_base")
            if local_normal is not None:
                adjusted_target["control_reaction_normal_base"] = local_normal
        # Reuse the existing TASE adapter parser and outer-input contract.  It
        # does not perform I/O and keeps the RNN/QP adapter identities intact.
        return TaseOfflineMethodAdapter._outer_inputs(  # type: ignore[misc]
            self, measured, adjusted_target, dt_s
        )

    def step(
        self,
        measured: Mapping[str, Any],
        target: Mapping[str, Any],
        dt_s: float,
    ) -> TaseAdapterResult:
        if self.stopped:
            raise RuntimeError("TASE-improved offline adapter is stopped")
        actual_dt = float(dt_s)
        if not math.isfinite(actual_dt) or actual_dt <= 0.0:
            raise ValueError("dt_s must be finite and positive")
        before = self.snapshot()
        first_sample = self.sample_count == 0
        try:
            inputs, jacobian, lower, upper = self._outer_inputs(measured, target, actual_dt)
            outer_config = replace(
                self.outer_config,
                force_target_n=float(target["force_target_n"]),
            )
            outer = compute_step5d_outer_loop(
                outer_config,
                self.outer_state,
                inputs,
                include_diagnostics=True,
            )
            pose = _finite_vector(measured.get("tcp_pose_base"), 6, "tcp_pose_base")
            wrench = _finite_vector(measured.get("wrench_tcp"), 6, "wrench_tcp")
            force_base = rotvec_to_matrix(pose[3:]) @ wrench[:3]
            projection = project_local_normal_force(
                force_base,
                inputs.control_reaction_normal_base,
                min_norm=self.config.local_normal_min_norm,
            )
            qp_result = solve_normal_priority_qp(
                jacobian=jacobian,
                xdot_c=outer.xdot_c,
                reaction_normal_base=projection.normal_base,
                omega_minus=lower,
                omega_plus=upper,
                normal_weight=self.config.normal_weight,
                tangential_weight=self.config.tangential_weight,
                orientation_weight=self.config.orientation_weight,
                regularization=self.config.qp_regularization,
            )
            qdot = np.asarray(qp_result.qdot, dtype=float)
            solver_status = 1.0 if outer.cmd_valid else 0.0
            outer_diagnostics = dict(outer.diagnostics)
            integral_saturated = bool(
                outer_diagnostics.get("integral_saturated", False)
                or outer_diagnostics.get("integral_velocity_saturated", False)
            )
            qp_status = (
                "normal_priority_slack_qp_solved"
                if outer.cmd_valid
                else "invalid_command_zero_task_qp_diagnostic"
            )
            diagnostics = {
                "scope": "offline_numerical_sanity_only_no_robot_io_or_physical_claim",
                "offline_only": True,
                "live_eligible": False,
                "method_name": self.method_name,
                "solver_name": "normal_priority_box_qp_with_task_slack",
                "variant": self.variant,
                "first_sample": first_sample,
                "sample_index": int(self.sample_count),
                "actual_dt_s": actual_dt,
                "normal_force": {
                    "normal_base": projection.normal_base,
                    "force_base_n": projection.force_base_n,
                    "normal_force_base_n": projection.normal_force_base_n,
                    "tangential_force_base_n": projection.tangential_force_base_n,
                    "normal_force_n": projection.normal_force_n,
                    "tangential_force_n": projection.tangential_force_n,
                    "force_norm_n": projection.force_norm_n,
                },
                "force_normal_component_n": projection.normal_force_n,
                "force_tangential_component_n": projection.tangential_force_n,
                "force_normal_vector_base_n": projection.normal_force_base_n,
                "force_tangential_vector_base_n": projection.tangential_force_base_n,
                "outer": outer_diagnostics,
                "integral_state_n_s": float(
                    outer_diagnostics.get("integral_state_n_s", 0.0)
                ),
                "integral": {
                    "policy": CONTACT_GATED_LEAKY_POLICY,
                    "state_n_s": float(
                        outer_diagnostics.get("integral_state_n_s", 0.0)
                    ),
                    "raw_state_n_s": float(
                        outer_diagnostics.get("integral_raw_state_n_s", 0.0)
                    ),
                    "leaky_state_n_s": float(
                        outer_diagnostics.get("integral_leaky_state_n_s", 0.0)
                    ),
                    "contact_gated": bool(
                        outer_diagnostics.get("integral_contact_gated", False)
                    ),
                    "leak_factor": float(
                        outer_diagnostics.get("integral_leak_factor", 1.0)
                    ),
                    "state_clamped": bool(
                        outer_diagnostics.get("integral_state_clamped", False)
                    ),
                    "authority_clamped": bool(
                        outer_diagnostics.get("integral_authority_clamped", False)
                    ),
                    "anti_windup_frozen": bool(
                        outer_diagnostics.get("integral_conditional_frozen", False)
                    ),
                    "velocity_saturated": bool(
                        outer_diagnostics.get("integral_velocity_saturated", False)
                    ),
                    "saturated": integral_saturated,
                    "reset_reason": str(
                        outer_diagnostics.get("integral_reset_reason", "")
                    ),
                },
                "integral_saturated": integral_saturated,
                "saturation": {
                    "integral": integral_saturated,
                    "qp_active_bounds": qp_result.active_bounds,
                    "qp_bound_violation": qp_result.bound_violation,
                },
                "qp": {
                    "status": qp_status,
                    "task_slack": qp_result.task_slack,
                    "normal_slack": qp_result.normal_slack,
                    "tangential_slack": qp_result.tangential_slack,
                    "orientation_slack": qp_result.orientation_slack,
                    "residual": qp_result.residual,
                    "residual_norm": qp_result.residual_norm,
                    "normal_residual": qp_result.normal_residual,
                    "tangential_residual_norm": qp_result.tangential_residual_norm,
                    "orientation_residual_norm": qp_result.orientation_residual_norm,
                    "bound_violation": qp_result.bound_violation,
                    "active_bounds": qp_result.active_bounds,
                    "exact_feasible": qp_result.exact_feasible,
                    "objective": qp_result.objective,
                    "active_set_candidates": qp_result.active_set_candidates,
                },
                "qp_slack": qp_result.task_slack,
                "qp_residual": qp_result.residual,
                "qp_residual_norm": qp_result.residual_norm,
                "qp_normal_slack": qp_result.normal_slack,
                "qp_tangential_slack": qp_result.tangential_slack,
                "qp_orientation_slack": qp_result.orientation_slack,
                "qp_normal_residual": qp_result.normal_residual,
                "qp_tangential_residual_norm": qp_result.tangential_residual_norm,
                "qp_orientation_residual_norm": qp_result.orientation_residual_norm,
            }
            # Commit both outer state and sample count only after the complete
            # numerical transition and all diagnostics have succeeded.
            self.outer_state = outer.next_state
            self.sample_count += 1
            return TaseAdapterResult(
                qdot_rad_s=_tuple6(qdot),
                xdot_c=_tuple6(outer.xdot_c),
                residual_norm=qp_result.residual_norm,
                solver_status=solver_status,
                diagnostics=diagnostics,
            )
        except Exception:
            self._restore_unchecked(before)
            raise


TaseImprovedOfflineController = TaseImprovedOfflineMethodAdapter


def _coerce_improved_config(value: Mapping[str, Any] | None) -> TaseImprovedConfig:
    options = {} if value is None else dict(value)
    unknown = set(options) - set(TaseImprovedConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(
            "unknown TASE-improved configuration: " + ",".join(sorted(unknown))
        )
    config = TaseImprovedConfig(**options)
    config.validate()
    return config


def _coerce_outer_config(
    value: Step5dOuterLoopConfig | Mapping[str, Any] | None,
) -> Step5dOuterLoopConfig:
    if value is None:
        return DEFAULT_OUTER_CONFIG
    if isinstance(value, Step5dOuterLoopConfig):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("outer_config must be Step5dOuterLoopConfig or mapping")
    options = dict(value)
    unknown = set(options) - set(Step5dOuterLoopConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(
            "unknown TASE outer configuration: " + ",".join(sorted(unknown))
        )
    return Step5dOuterLoopConfig(**options)


def create_tase_improved_offline_adapter(
    *,
    config: Mapping[str, Any] | None = None,
    outer_config: Step5dOuterLoopConfig | Mapping[str, Any] | None = None,
    qp_library: Path | str | None = None,
) -> TaseImprovedOfflineMethodAdapter:
    """Factory used by the transport-free offline registry.

    ``qp_library`` is accepted for interface symmetry with TASE-QP but is not
    required: this variant's bounded slack QP is a deterministic NumPy
    numerical policy and does not load a native library.
    """

    del qp_library
    return TaseImprovedOfflineMethodAdapter(
        config=_coerce_improved_config(config),
        outer_config=_coerce_outer_config(outer_config),
    )


__all__ = [
    "CONTACT_GATED_LEAKY_POLICY",
    "IMPROVED_METHOD_NAME",
    "IMPROVED_VARIANT",
    "LocalNormalForceProjection",
    "NormalPriorityQpResult",
    "TaseImprovedConfig",
    "TaseImprovedOfflineController",
    "TaseImprovedOfflineMethodAdapter",
    "create_tase_improved_offline_adapter",
    "project_local_normal_force",
    "solve_normal_priority_qp",
]
