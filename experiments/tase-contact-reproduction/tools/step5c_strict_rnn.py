#!/usr/bin/env python3
"""Strict Step5d TASE RNN gate.

This module intentionally refuses to produce live commands until the paper
truth contract is verified against the PDF. It prevents a DLS or placeholder
controller from being exposed as a finite-time RNN.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
PAPER_TRUTH_PATH = EXPERIMENT_ROOT / "config" / "step5c_tase_paper_truth.json"
STRICT_DRYRUN_STAGE_ID = "step5c_strict_rnn_dryrun_v1"
STATUS_PAPER_TRUTH_PENDING = 91.0


class PaperTruthPendingError(RuntimeError):
    """Raised when strict RNN equations or parameters are not PDF-verified."""


@dataclass(frozen=True)
class StrictRnnConfig:
    paper_truth_path: Path = PAPER_TRUTH_PATH
    qdot_limit_rad_s: float = 0.30
    epsilon: float = 0.022
    sigr_exponent_r: float = 0.2


@dataclass(frozen=True)
class StrictRnnCommandResult:
    qdot: tuple[float, float, float, float, float, float]
    solver_status: float
    residual_norm: float
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class StrictRnnStepDiagnostics:
    theta_dot_state: tuple[float, float, float, float, float, float]
    lambda_state: tuple[float, float, float, float, float, float]
    proj_input_form: str
    lambda_update_form: str
    proj_input: tuple[float, float, float, float, float, float]
    projected: tuple[float, float, float, float, float, float]
    sigr_arg: tuple[float, float, float, float, float, float]
    sigr_val: tuple[float, float, float, float, float, float]
    theta_dot_update_limited_mask: tuple[bool, bool, bool, bool, bool, bool]
    omega_minus: tuple[float, float, float, float, float, float]
    omega_plus: tuple[float, float, float, float, float, float]
    active_bounds_mask: tuple[bool, bool, bool, bool, bool, bool]
    constraint_residual_norm: float
    xdot_c: tuple[float, float, float, float, float, float]
    epsilon: float
    sigr_exponent_r: float


def load_paper_truth(path: Path = PAPER_TRUTH_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PaperTruthPendingError(f"paper truth file missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PaperTruthPendingError(f"paper truth file is not valid JSON: {path}: {exc}") from exc


def pending_paper_truth_fields(payload: dict[str, Any]) -> list[str]:
    pending = list(payload.get("pending_pdf_verify", []))
    for section_name, section in payload.get("sections", {}).items():
        if isinstance(section, dict):
            for field in section.get("pending_pdf_verify", []):
                pending.append(f"{section_name}.{field}")
    return sorted(str(field) for field in pending)


def assert_paper_truth_verified(payload: dict[str, Any]) -> None:
    pending = pending_paper_truth_fields(payload)
    if pending:
        raise PaperTruthPendingError(
            "strict Step5c RNN is blocked by unverified paper-truth fields: "
            + ", ".join(pending)
        )
    if not payload.get("strict_rnn_enabled", False):
        raise PaperTruthPendingError("strict Step5c RNN is disabled in paper truth config")


def _finite_vector(values: Any, length: int, name: str) -> tuple[float, ...]:
    try:
        vector = tuple(float(value) for value in values)
    except TypeError as exc:
        raise ValueError(f"{name} must be a finite length-{length} vector") from exc
    if len(vector) != length or any(not math.isfinite(value) for value in vector):
        raise ValueError(f"{name} must be a finite length-{length} vector")
    return vector


def _finite_matrix(values: Any, shape: tuple[int, int], name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.shape != shape or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be a finite {shape[0]}x{shape[1]} matrix")
    return array


def _finite_array(values: Any, length: int, name: str) -> np.ndarray:
    return np.asarray(_finite_vector(values, length, name), dtype=float)


def _tuple6(values: np.ndarray) -> tuple[float, float, float, float, float, float]:
    return tuple(float(value) for value in values.reshape(6))  # type: ignore[return-value]


def sigr(value: Any, r: float) -> Any:
    """Finite-time Eq.(23) nonlinearity: sig^r(x)=abs(x)^r*sign(x)."""
    if not math.isfinite(float(r)) or not 0.0 < float(r) <= 1.0:
        raise ValueError("sigr exponent r must be in (0, 1]")
    array = np.asarray(value, dtype=float)
    if not np.all(np.isfinite(array)):
        raise ValueError("sigr input must be finite")
    result = np.power(np.abs(array), float(r)) * np.sign(array)
    return float(result) if result.shape == () else result


def project_omega(values: Any, omega_minus: Any, omega_plus: Any) -> np.ndarray:
    vector = _finite_array(values, 6, "projection_input")
    lower = _finite_array(omega_minus, 6, "omega_minus")
    upper = _finite_array(omega_plus, 6, "omega_plus")
    if np.any(lower > upper):
        raise ValueError("omega_minus must be <= omega_plus element-wise")
    return np.clip(vector, lower, upper)


class StrictTaseRnnSolver:
    """Paper-gated strict RNN solver interface.

    The offline Eq.(23) solver body is available only after the paper-truth
    gate is explicitly satisfied. This module still has no live bridge entry.
    """

    def __init__(self, config: StrictRnnConfig = StrictRnnConfig()) -> None:
        self.config = config
        self.paper_truth = load_paper_truth(config.paper_truth_path)
        assert_paper_truth_verified(self.paper_truth)
        self.theta_dot_state = np.zeros(6, dtype=float)
        self.lambda_state = np.zeros(6, dtype=float)

    def freeze(self) -> None:
        """Hold RNN state unchanged when the command path is invalid."""

    def reset_state(self) -> None:
        """Clear stateful solver memory at explicit contact lifecycle boundaries."""
        self.theta_dot_state = np.zeros(6, dtype=float)
        self.lambda_state = np.zeros(6, dtype=float)

    def warm_start(
        self,
        *,
        J: Any,
        xdot_c: Any,
        omega_minus: Any,
        omega_plus: Any,
        damping: float = 1e-4,
    ) -> None:
        """Initialize (theta_dot, lambda) from a damped DLS boundary command.

        From zero state the early Cartesian velocity follows J @ J.T @ xdot_c
        instead of xdot_c while lambda converges, so an angular-dominant entry
        command can invert a press request into unload on the approach normal
        (v28 speedj_rnn_live soft_low_contact_hold failure). Initializing near
        the boundary command removes that transient; the running dynamics are
        unchanged. If clipping is active, the next Eq.(23) residual is still
        nonzero and lambda continues adapting from the clipped command.
        """
        jacobian = _finite_matrix(J, (6, 6), "J")
        xdot = _finite_array(xdot_c, 6, "xdot_c")
        lower = _finite_array(omega_minus, 6, "omega_minus")
        upper = _finite_array(omega_plus, 6, "omega_plus")
        if np.any(lower > upper):
            raise ValueError("omega_minus must be <= omega_plus element-wise")
        if not math.isfinite(float(damping)) or damping <= 0.0:
            raise ValueError("warm start damping must be positive")
        lhs = jacobian @ jacobian.T + (float(damping) ** 2) * np.eye(6)
        self.lambda_state = np.linalg.solve(lhs, xdot)
        self.theta_dot_state = np.clip(jacobian.T @ self.lambda_state, lower, upper)

    def step(
        self,
        *,
        J: Any,
        xdot_c: Any,
        omega_minus: Any,
        omega_plus: Any,
        dt: float,
        epsilon: float | None = None,
        r: float | None = None,
        cmd_valid: bool = True,
    ) -> StrictRnnStepDiagnostics:
        jacobian = _finite_matrix(J, (6, 6), "J")
        xdot = _finite_array(xdot_c, 6, "xdot_c")
        lower = _finite_array(omega_minus, 6, "omega_minus")
        upper = _finite_array(omega_plus, 6, "omega_plus")
        if np.any(lower > upper):
            raise ValueError("omega_minus must be <= omega_plus element-wise")
        step_s = float(dt)
        if not math.isfinite(step_s) or step_s <= 0.0:
            raise ValueError("dt must be finite and positive")
        eps = self.config.epsilon if epsilon is None else float(epsilon)
        if not math.isfinite(eps) or eps <= 0.0:
            raise ValueError("epsilon must be finite and positive")
        exponent = self.config.sigr_exponent_r if r is None else float(r)
        lambda_update_form = "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)"

        if cmd_valid:
            proj_input = jacobian.T @ self.lambda_state
            projected = np.clip(proj_input, lower, upper)
            sigr_arg = self.theta_dot_state - projected
            sigr_val = np.asarray(sigr(sigr_arg, exponent), dtype=float)
            theta_delta = -(step_s / eps) * sigr_val
            crosses_projection = np.abs(theta_delta) > np.abs(sigr_arg)
            self.theta_dot_state = np.where(
                crosses_projection,
                projected,
                self.theta_dot_state + theta_delta,
            )
            constraint_residual = jacobian @ self.theta_dot_state - xdot
            self.lambda_state = self.lambda_state - (step_s / eps) * constraint_residual
        else:
            proj_input = jacobian.T @ self.lambda_state
            projected = np.clip(proj_input, lower, upper)
            sigr_arg = self.theta_dot_state - projected
            sigr_val = np.zeros(6, dtype=float)
            crosses_projection = np.zeros(6, dtype=bool)
            constraint_residual = jacobian @ self.theta_dot_state - xdot

        active_bounds = np.isclose(projected, lower) | np.isclose(projected, upper)
        return StrictRnnStepDiagnostics(
            theta_dot_state=_tuple6(self.theta_dot_state),
            lambda_state=_tuple6(self.lambda_state),
            proj_input_form="J.T @ lambda_state",
            lambda_update_form=lambda_update_form,
            proj_input=_tuple6(proj_input),
            projected=_tuple6(projected),
            sigr_arg=_tuple6(sigr_arg),
            sigr_val=_tuple6(sigr_val),
            theta_dot_update_limited_mask=tuple(bool(value) for value in crosses_projection.reshape(6)),  # type: ignore[return-value]
            omega_minus=_tuple6(lower),
            omega_plus=_tuple6(upper),
            active_bounds_mask=tuple(bool(value) for value in active_bounds.reshape(6)),  # type: ignore[return-value]
            constraint_residual_norm=float(np.linalg.norm(constraint_residual)),
            xdot_c=_tuple6(xdot),
            epsilon=eps,
            sigr_exponent_r=exponent,
        )

    def solve(self, *, actual_q: Any, actual_qd: Any, target_state: dict[str, Any]) -> StrictRnnCommandResult:
        _finite_vector(actual_q, 6, "actual_q")
        _finite_vector(actual_qd, 6, "actual_qd")
        if not isinstance(target_state, dict):
            raise ValueError("target_state must be a dict")
        diag = self.step(
            J=target_state["J"],
            xdot_c=target_state["xdot_c"],
            omega_minus=target_state["omega_minus"],
            omega_plus=target_state["omega_plus"],
            dt=target_state["dt"],
            epsilon=target_state.get("epsilon"),
            r=target_state.get("r"),
            cmd_valid=bool(target_state.get("cmd_valid", True)),
        )
        return StrictRnnCommandResult(
            qdot=diag.theta_dot_state,
            solver_status=40.0,
            residual_norm=diag.constraint_residual_norm,
            diagnostics=diag.__dict__,
        )


def main() -> int:
    payload = load_paper_truth()
    assert_paper_truth_verified(payload)
    raise SystemExit("strict Step5d RNN offline solver is available; live entrypoint is blocked")


if __name__ == "__main__":
    raise SystemExit(main())
