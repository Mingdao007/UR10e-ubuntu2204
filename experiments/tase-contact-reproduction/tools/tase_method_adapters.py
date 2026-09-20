"""Selectable offline TASE method adapters for the common controller seam.

These adapters reuse the existing Step5d outer loop but own one solver and one
outer-loop state each.  They are transport-free and deliberately carry an
offline-only marker; the yield live-entry registry remains unavailable for
both methods.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from step5c_strict_rnn import StrictRnnStepDiagnostics
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    rotvec_to_matrix,
)
from tase_offline_baselines import (
    TaseJointSample,
    TaseOfflineConfig,
    TaseQpBaseline,
    TaseRnnBaseline,
)


DEFAULT_OUTER_CONFIG = Step5dOuterLoopConfig(
    kp=4.0,
    ko=5.0,
    kf=1.0,
    Md_scalar=12.0,
    Bd_scalar=550.0,
    force_target_n=5.0,
    delay_T_s=0.004,
)


def _finite_vector(value: Any, length: int, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if array.shape != (length,) or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be a finite length-{length} vector")
    return np.ascontiguousarray(array)


def _finite_matrix(value: Any, shape: tuple[int, int], name: str) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if array.shape != shape or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be a finite {shape} matrix")
    return np.ascontiguousarray(array)


def _tuple6(value: Any) -> tuple[float, float, float, float, float, float]:
    return tuple(float(item) for item in _finite_vector(value, 6, "six-vector"))  # type: ignore[return-value]


def _tuple3(value: Any) -> tuple[float, float, float]:
    return tuple(float(item) for item in _finite_vector(value, 3, "three-vector"))  # type: ignore[return-value]


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _reaction_normal_from_force(force_base: np.ndarray) -> tuple[float, float, float]:
    norm = float(np.linalg.norm(force_base))
    if norm < 1e-9:
        return (0.0, 0.0, -1.0)
    return _tuple3(force_base / norm)


@dataclass(frozen=True)
class TaseAdapterResult:
    qdot_rad_s: tuple[float, float, float, float, float, float]
    xdot_c: tuple[float, float, float, float, float, float]
    residual_norm: float
    solver_status: float
    diagnostics: dict[str, Any]


class TaseOfflineMethodAdapter:
    """One independently selectable RNN or QP method implementation."""

    schema = "tase-offline-method-adapter-state-v1"
    offline_only = True
    live_eligible = False

    def __init__(
        self,
        *,
        method_name: str,
        solver_name: str,
        variant: str,
        config: TaseOfflineConfig,
        outer_config: Step5dOuterLoopConfig,
        qp_library: Path | str | None = None,
    ) -> None:
        if solver_name not in {"rnn", "qp"}:
            raise ValueError("TASE offline solver must be rnn or qp")
        self.method_name = str(method_name)
        self.solver_name = solver_name
        self.variant = str(variant)
        self.config = config
        self.outer_config = outer_config
        self.outer_state = Step5dOuterLoopState()
        self.stopped = False
        if solver_name == "rnn":
            self.solver = TaseRnnBaseline(config)
        else:
            if qp_library is None:
                raise ValueError("TASE_QP offline adapter requires qp_library")
            self.solver = TaseQpBaseline(qp_library, config)

    def reset(self) -> None:
        self.outer_state = Step5dOuterLoopState()
        self.solver.reset()
        self.stopped = False

    def stop(self, _reason: str = "offline_stop") -> None:
        self.stopped = True

    def close(self) -> None:
        self.stop("offline_close")

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "method_name": self.method_name,
            "solver_name": self.solver_name,
            "variant": self.variant,
            "offline_only": True,
            "live_eligible": False,
            "stopped": bool(self.stopped),
            "outer_loop": {
                "force_integral_n_s": float(self.outer_state.force_integral_n_s),
                "xdot_p_prev_m_s": [float(value) for value in self.outer_state.xdot_p_prev_m_s],
            },
            "solver": self.solver.snapshot(),
        }

    def _restore_unchecked(self, state: Mapping[str, Any]) -> None:
        if (
            state.get("schema") != self.schema
            or state.get("method_name") != self.method_name
            or state.get("solver_name") != self.solver_name
            or state.get("variant") != self.variant
            or state.get("offline_only") is not True
            or state.get("live_eligible") is not False
            or not isinstance(state.get("stopped"), bool)
        ):
            raise ValueError("TASE offline adapter snapshot identity differs")
        outer = _mapping(state.get("outer_loop"), "outer_loop snapshot")
        force_integral = float(outer.get("force_integral_n_s"))
        xdot_p_prev = _finite_vector(
            outer.get("xdot_p_prev_m_s"), 3, "outer_loop.xdot_p_prev_m_s"
        )
        if not math.isfinite(force_integral):
            raise ValueError("outer_loop.force_integral_n_s must be finite")
        solver_state = _mapping(state.get("solver"), "solver snapshot")
        self.outer_state = Step5dOuterLoopState(
            force_integral_n_s=force_integral,
            xdot_p_prev_m_s=_tuple3(xdot_p_prev),
        )
        self.solver.restore(dict(solver_state))
        self.stopped = bool(state["stopped"])

    def restore(self, state: Mapping[str, Any]) -> None:
        """Restore outer and solver state as one transaction."""
        before = self.snapshot()
        try:
            self._restore_unchecked(state)
        except Exception as exc:
            try:
                self._restore_unchecked(before)
            except Exception as rollback_exc:  # pragma: no cover - invariant breach
                raise RuntimeError("TASE adapter restore rollback failed") from rollback_exc
            raise exc

    def _outer_inputs(
        self,
        measured: Mapping[str, Any],
        target: Mapping[str, Any],
        dt_s: float,
    ) -> tuple[Step5dOuterLoopInputs, np.ndarray, np.ndarray, np.ndarray]:
        pose = _finite_vector(measured.get("tcp_pose_base"), 6, "tcp_pose_base")
        speed = _finite_vector(measured.get("tcp_velocity_base"), 6, "tcp_velocity_base")
        wrench = _finite_vector(measured.get("wrench_tcp"), 6, "wrench_tcp")
        jacobian = _finite_matrix(measured.get("jacobian_base"), (6, 6), "jacobian_base")
        constraints = _mapping(measured.get("constraints"), "constraints")
        lower = _finite_vector(constraints.get("joint_velocity_lower"), 6, "lower")
        upper = _finite_vector(constraints.get("joint_velocity_upper"), 6, "upper")
        if np.any(lower > upper):
            raise ValueError("inverted joint velocity bounds")
        position = _finite_vector(target.get("x_pd_base"), 3, "x_pd_base")
        velocity = _finite_vector(target.get("xdot_pd_base"), 3, "xdot_pd_base")
        force_target = float(target.get("force_target_n"))
        if not math.isfinite(force_target):
            raise ValueError("force_target_n must be finite")
        rotation = rotvec_to_matrix(pose[3:])
        force_base = rotation @ wrench[:3]
        reaction = target.get("control_reaction_normal_base")
        if reaction is None:
            reaction = _reaction_normal_from_force(force_base)
        reaction = _tuple3(_finite_vector(reaction, 3, "control_reaction_normal_base"))
        inputs = Step5dOuterLoopInputs(
            tcp_pose_base=_tuple6(pose),
            tcp_speed_base=_tuple6(speed),
            force_tcp_n=_tuple3(wrench[:3]),
            x_pd_base=_tuple3(position),
            xdot_pd_base=_tuple3(velocity),
            dt_s=float(dt_s),
            cmd_valid=bool(measured.get("cmd_valid", True)),
            integral_enabled=bool(measured.get("integral_enabled", True)),
            integral_reset_reason=str(measured.get("integral_reset_reason", "")),
            control_reaction_normal_base=reaction,
        )
        return inputs, jacobian, lower, upper

    def step(
        self,
        measured: Mapping[str, Any],
        target: Mapping[str, Any],
        dt_s: float,
    ) -> TaseAdapterResult:
        if self.stopped:
            raise RuntimeError("TASE offline adapter is stopped")
        if not math.isfinite(float(dt_s)) or float(dt_s) <= 0.0:
            raise ValueError("dt_s must be finite and positive")
        before = self.snapshot()
        try:
            inputs, jacobian, lower, upper = self._outer_inputs(measured, target, dt_s)
            outer_config = replace(
                self.outer_config,
                force_target_n=float(target["force_target_n"]),
            )
            outer = compute_step5d_outer_loop(outer_config, self.outer_state, inputs)
            self.outer_state = outer.next_state
            sample = TaseJointSample(
                jacobian=jacobian,
                xdot_c=np.asarray(outer.xdot_c, dtype=float),
                omega_minus=lower,
                omega_plus=upper,
                dt_s=float(dt_s),
                cmd_valid=outer.cmd_valid,
            )
            solver_result = self.solver.step(sample)
            if self.solver_name == "rnn":
                assert isinstance(solver_result, StrictRnnStepDiagnostics)
                qdot = np.asarray(solver_result.theta_dot_state, dtype=float)
                residual = float(solver_result.constraint_residual_norm)
                status = 40.0
                solver_diagnostics = {
                    "lambda_state": solver_result.lambda_state,
                    "active_bounds_mask": solver_result.active_bounds_mask,
                    "proj_input_form": solver_result.proj_input_form,
                    "lambda_update_form": solver_result.lambda_update_form,
                }
            else:
                if solver_result is None:
                    qdot = self.solver.qdot_state
                    residual = float(np.linalg.norm(jacobian @ qdot - np.asarray(outer.xdot_c)))
                    status = 0.0
                    solver_diagnostics = {"invalid_sample": True}
                else:
                    qdot = np.asarray(solver_result.qdot, dtype=float)
                    residual = float(solver_result.equality_residual)
                    status = 1.0
                    solver_diagnostics = {
                        "iterations": solver_result.iterations,
                        "bound_violation": solver_result.bound_violation,
                    }
            return TaseAdapterResult(
                qdot_rad_s=_tuple6(qdot),
                xdot_c=_tuple6(outer.xdot_c),
                residual_norm=residual,
                solver_status=status,
                diagnostics={
                    "offline_only": True,
                    "live_eligible": False,
                    "method_name": self.method_name,
                    "solver_name": self.solver_name,
                    "variant": self.variant,
                    "outer": outer.diagnostics,
                    "solver": solver_diagnostics,
                },
            )
        except Exception:
            self._restore_unchecked(before)
            raise


def _offline_config(value: Mapping[str, Any] | None) -> TaseOfflineConfig:
    options = {} if value is None else dict(value)
    unknown = set(options) - set(TaseOfflineConfig.__dataclass_fields__)
    if unknown:
        raise ValueError("unknown TASE offline configuration: " + ",".join(sorted(unknown)))
    return TaseOfflineConfig(**options)


def _outer_config(value: Step5dOuterLoopConfig | Mapping[str, Any] | None) -> Step5dOuterLoopConfig:
    if value is None:
        return DEFAULT_OUTER_CONFIG
    if isinstance(value, Step5dOuterLoopConfig):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("outer_config must be Step5dOuterLoopConfig or mapping")
    options = dict(value)
    unknown = set(options) - set(Step5dOuterLoopConfig.__dataclass_fields__)
    if unknown:
        raise ValueError("unknown TASE outer configuration: " + ",".join(sorted(unknown)))
    return Step5dOuterLoopConfig(**options)


def create_tase_offline_adapter(
    *,
    method_name: str,
    solver_name: str,
    variant: str,
    config: Mapping[str, Any] | None = None,
    outer_config: Step5dOuterLoopConfig | Mapping[str, Any] | None = None,
    qp_library: Path | str | None = None,
) -> TaseOfflineMethodAdapter:
    """Factory used by the transport-free registry only."""

    options = {} if config is None else dict(config)
    if solver_name == "rnn":
        expected_sign = "plus" if variant == "printed_eq23_plus" else "minus"
        requested_sign = options.get("lambda_update_sign", expected_sign)
        if requested_sign != expected_sign:
            raise ValueError(
                f"{method_name} requires explicit lambda_update_sign={expected_sign!r}; "
                f"received {requested_sign!r}"
            )
        options["lambda_update_sign"] = expected_sign

    return TaseOfflineMethodAdapter(
        method_name=method_name,
        solver_name=solver_name,
        variant=variant,
        config=_offline_config(options),
        outer_config=_outer_config(outer_config),
        qp_library=qp_library,
    )
