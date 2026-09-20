#!/usr/bin/env python3
"""Offline comparison of an equation-aligned TASE RNN hypothesis and QP.

Both joint-level baselines consume the same paper outer-loop task velocity,
Jacobian, and box bounds.  This module has no robot, RTDE, bridge, or package
entry point.  The RNN delegates Eq.(23) to the existing stateful strict solver;
the checked-in truth gate remains disabled for live use, while this separate
truth file makes the explicitly offline research comparison runnable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from contact_qp import NativeContactQp
from step5c_strict_rnn import StrictRnnConfig, StrictRnnStepDiagnostics, StrictTaseRnnSolver
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopOutput,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
OFFLINE_TRUTH_PATH = EXPERIMENT_ROOT / "config" / "step5c_tase_offline_baseline_truth.json"


@dataclass(frozen=True)
class TaseOfflineConfig:
    """Reviewed offline defaults; these do not authorize a live controller."""

    epsilon: float = 0.022
    sigr_exponent_r: float = 0.2
    qdot_limit_rad_s: float = 0.15
    dt_s: float = 0.002
    lambda_update_sign: str = "minus"

    def validate(self) -> None:
        values = (
            self.epsilon,
            self.sigr_exponent_r,
            self.qdot_limit_rad_s,
            self.dt_s,
        )
        if not all(np.isfinite(value) for value in values):
            raise ValueError("TASE offline configuration must be finite")
        if self.epsilon <= 0.0 or not 0.0 < self.sigr_exponent_r <= 1.0:
            raise ValueError("TASE offline epsilon/r are outside the paper domain")
        if self.qdot_limit_rad_s <= 0.0 or self.dt_s <= 0.0:
            raise ValueError("TASE offline limits and dt must be positive")
        if self.lambda_update_sign not in {"plus", "minus"}:
            raise ValueError("TASE offline lambda_update_sign must be plus or minus")


@dataclass(frozen=True)
class TaseJointSample:
    """One shared six-DOF task for both joint-level baselines."""

    jacobian: Any
    xdot_c: Any
    omega_minus: Any
    omega_plus: Any
    dt_s: float
    cmd_valid: bool = True


def _finite_array(value: Any, shape: tuple[int, ...], name: str) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if array.shape != shape or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be a finite array with shape {shape}")
    return np.ascontiguousarray(array)


def _validated_sample(sample: TaseJointSample, config: TaseOfflineConfig) -> tuple[np.ndarray, ...]:
    jacobian = _finite_array(sample.jacobian, (6, 6), "jacobian")
    xdot_c = _finite_array(sample.xdot_c, (6,), "xdot_c")
    lower = _finite_array(sample.omega_minus, (6,), "omega_minus")
    upper = _finite_array(sample.omega_plus, (6,), "omega_plus")
    if np.any(lower > upper):
        raise ValueError("omega_minus must be <= omega_plus")
    dt_s = float(sample.dt_s)
    if not np.isfinite(dt_s) or dt_s <= 0.0:
        raise ValueError("sample dt_s must be positive and finite")
    config.validate()
    return jacobian, xdot_c, lower, upper, np.asarray(dt_s)


class TaseRnnBaseline:
    """Stateful Eq.(23) hypothesis, explicitly offline-only."""

    def __init__(
        self,
        config: TaseOfflineConfig = TaseOfflineConfig(),
        *,
        paper_truth_path: Path = OFFLINE_TRUTH_PATH,
    ) -> None:
        config.validate()
        self.config = config
        self.solver = StrictTaseRnnSolver(
            StrictRnnConfig(
                paper_truth_path=Path(paper_truth_path),
                qdot_limit_rad_s=config.qdot_limit_rad_s,
                epsilon=config.epsilon,
                sigr_exponent_r=config.sigr_exponent_r,
                backend="numpy",
                offline_hypothesis=True,
                lambda_update_sign=config.lambda_update_sign,
            )
        )

    @property
    def theta_dot_state(self) -> np.ndarray:
        return self.solver.theta_dot_state.copy()

    @property
    def lambda_state(self) -> np.ndarray:
        return self.solver.lambda_state.copy()

    def reset(self) -> None:
        self.solver.reset_state()

    def snapshot(self) -> dict[str, list[float]]:
        return self.solver.snapshot()

    def restore(self, state: dict[str, Any]) -> None:
        self.solver.restore(state)

    def step(self, sample: TaseJointSample) -> StrictRnnStepDiagnostics:
        jacobian, xdot_c, lower, upper, dt_s = _validated_sample(sample, self.config)
        return self.solver.step(
            J=jacobian,
            xdot_c=xdot_c,
            omega_minus=lower,
            omega_plus=upper,
            dt=float(dt_s),
            epsilon=self.config.epsilon,
            r=self.config.sigr_exponent_r,
            cmd_valid=bool(sample.cmd_valid),
        )


class TaseQpBaseline:
    """Matched Eq.(20) equality-and-box QP, offline only."""

    def __init__(self, library: Path | str, config: TaseOfflineConfig) -> None:
        config.validate()
        self.config = config
        self.library = Path(library).resolve(strict=True)
        self.solver = NativeContactQp(self.library, deadline_s=None)

    def reset(self) -> None:
        self.solver.reset()

    @property
    def qdot_state(self) -> np.ndarray:
        return self.solver.x.copy()

    def snapshot(self) -> dict[str, list[float]]:
        return self.solver.snapshot()

    def restore(self, state: dict[str, Any]) -> None:
        self.solver.restore(state)

    def step(self, sample: TaseJointSample):
        jacobian, xdot_c, lower, upper, _dt_s = _validated_sample(sample, self.config)
        if not sample.cmd_valid:
            return None
        return self.solver.solve(jacobian, xdot_c, lower, upper)


@dataclass(frozen=True)
class TaseOfflineProviderStep:
    """One shared outer-loop output consumed by both joint-level baselines."""

    outer: Step5dOuterLoopOutput
    sample: TaseJointSample
    rnn: StrictRnnStepDiagnostics
    qp: Any


class TaseOfflineProvider:
    """Full offline provider boundary: outer loop, then matched solvers.

    The provider owns the outer-loop state so a snapshot is sufficient to
    replay the complete boundary.  Inputs remain prescribed offline data; no
    method in this class performs robot I/O or claims contact evidence.
    """

    def __init__(
        self,
        *,
        qp_library: Path | str,
        config: TaseOfflineConfig = TaseOfflineConfig(),
        outer_config: Step5dOuterLoopConfig,
        outer_state: Step5dOuterLoopState = Step5dOuterLoopState(),
    ) -> None:
        config.validate()
        self.config = config
        self.outer_config = outer_config
        self.outer_state = outer_state
        self.rnn = TaseRnnBaseline(config)
        self.qp = TaseQpBaseline(qp_library, config)

    def reset(self) -> None:
        self.outer_state = Step5dOuterLoopState()
        self.rnn.reset()
        self.qp.reset()

    def snapshot(self) -> dict[str, Any]:
        return {
            "outer_loop": {
                "force_integral_n_s": float(self.outer_state.force_integral_n_s),
                "xdot_p_prev_m_s": [float(value) for value in self.outer_state.xdot_p_prev_m_s],
            },
            "rnn": self.rnn.snapshot(),
            "qp": self.qp.snapshot(),
        }

    def restore(self, state: dict[str, Any]) -> None:
        try:
            outer = state["outer_loop"]
            force_integral = float(outer["force_integral_n_s"])
            xdot_p_prev = _finite_array(outer["xdot_p_prev_m_s"], (3,), "outer_loop.xdot_p_prev_m_s")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid TASE provider outer-loop snapshot") from exc
        if not np.isfinite(force_integral):
            raise ValueError("outer_loop.force_integral_n_s must be finite")
        self.outer_state = Step5dOuterLoopState(
            force_integral_n_s=force_integral,
            xdot_p_prev_m_s=tuple(float(value) for value in xdot_p_prev),
        )
        self.rnn.restore(state["rnn"])
        self.qp.restore(state["qp"])

    def step(
        self,
        inputs: Step5dOuterLoopInputs,
        *,
        jacobian: Any,
        omega_minus: Any,
        omega_plus: Any,
        include_diagnostics: bool | str = True,
    ) -> TaseOfflineProviderStep:
        outer = compute_step5d_outer_loop(
            self.outer_config,
            self.outer_state,
            inputs,
            include_diagnostics=include_diagnostics,
        )
        self.outer_state = outer.next_state
        sample = TaseJointSample(
            jacobian=jacobian,
            xdot_c=np.asarray(outer.xdot_c, dtype=float),
            omega_minus=omega_minus,
            omega_plus=omega_plus,
            dt_s=inputs.dt_s,
            cmd_valid=outer.cmd_valid,
        )
        rnn = self.rnn.step(sample)
        qp = self.qp.step(sample)
        return TaseOfflineProviderStep(outer=outer, sample=sample, rnn=rnn, qp=qp)


def compare_joint_baselines(
    samples: Iterable[TaseJointSample],
    *,
    qp_library: Path | str,
    config: TaseOfflineConfig = TaseOfflineConfig(),
) -> dict[str, Any]:
    """Run both baselines over identical inputs and return auditable metrics."""

    config.validate()
    rnn = TaseRnnBaseline(config)
    qp = TaseQpBaseline(qp_library, config)
    rnn_residuals: list[float] = []
    qp_residuals: list[float] = []
    rnn_bound_violations: list[float] = []
    qp_bound_violations: list[float] = []
    rnn_qdots: list[tuple[float, ...]] = []
    qp_qdots: list[tuple[float, ...]] = []
    sample_count = 0
    valid_sample_count = 0
    invalid_sample_count = 0
    for sample in samples:
        jacobian, xdot_c, lower, upper, _dt_s = _validated_sample(sample, config)
        rnn_diag = rnn.step(sample)
        rnn_qdot = np.asarray(rnn_diag.theta_dot_state, dtype=float)
        rnn_qdots.append(tuple(float(value) for value in rnn_qdot))
        qp_result = qp.step(sample)
        if sample.cmd_valid:
            rnn_residuals.append(float(np.linalg.norm(jacobian @ rnn_qdot - xdot_c)))
            rnn_bound_violations.append(
                float(max(0.0, np.max(lower - rnn_qdot), np.max(rnn_qdot - upper)))
            )
            assert qp_result is not None
            qp_qdot = np.asarray(qp_result.qdot, dtype=float)
            qp_qdots.append(tuple(float(value) for value in qp_qdot))
            qp_residuals.append(float(np.linalg.norm(jacobian @ qp_qdot - xdot_c)))
            qp_bound_violations.append(
                float(max(0.0, np.max(lower - qp_qdot), np.max(qp_qdot - upper)))
            )
            valid_sample_count += 1
        else:
            assert qp_result is None
            held_qdot = qp.qdot_state
            qp_qdots.append(tuple(float(value) for value in held_qdot))
            invalid_sample_count += 1
        sample_count += 1
    if sample_count == 0:
        raise ValueError("at least one TASE comparison sample is required")
    if valid_sample_count == 0:
        raise ValueError("at least one valid TASE comparison sample is required")
    return {
        "schema": "tase-offline-baseline-comparison-v1",
        "scope": "offline_only_no_robot_io_or_physical_claim",
        "equations": {
            "rnn": "TASE Eq.23 stateful theta_dot/lambda Euler discretization",
            "qp": "TASE Eq.20 min 0.5*qdot.T*qdot subject to J*qdot=xdot_c and box bounds",
        },
        "shared_inputs": {
            "samples": sample_count,
            "valid_samples": valid_sample_count,
            "invalid_samples": invalid_sample_count,
            "epsilon": config.epsilon,
            "sigr_exponent_r": config.sigr_exponent_r,
            "qdot_limit_rad_s": config.qdot_limit_rad_s,
        },
        "rnn": {
            "max_valid_equality_residual": max(rnn_residuals),
            "final_valid_equality_residual": rnn_residuals[-1],
            "max_bound_violation": max(rnn_bound_violations),
            "qdots": rnn_qdots,
        },
        "qp": {
            "max_valid_equality_residual": max(qp_residuals),
            "final_valid_equality_residual": qp_residuals[-1],
            "max_bound_violation": max(qp_bound_violations),
            "qdots": qp_qdots,
        },
    }
