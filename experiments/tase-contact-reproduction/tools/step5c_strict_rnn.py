#!/usr/bin/env python3
"""Strict Step5d TASE RNN gate.

This module intentionally refuses to produce live commands until the paper
truth contract is verified against the PDF. It prevents a DLS or placeholder
controller from being exposed as a finite-time RNN.
"""

from __future__ import annotations

import json
import math
import time
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
    sigr_exponent_r: float = 1.0
    inner_iterations: int = 1
    backend: str = "numpy"


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
        self._validate_config()
        self.paper_truth = load_paper_truth(config.paper_truth_path)
        assert_paper_truth_verified(self.paper_truth)
        self.theta_dot_state = np.zeros(6, dtype=float)
        self.lambda_state = np.zeros(6, dtype=float)
        self._cp: Any | None = None
        self._cupy_kernel: Any | None = None
        self._cupy_serial_reference_kernel: Any | None = None
        self._cupy_stream: Any | None = None
        self._cupy_stream_priority: int | None = None
        self._cupy_stream_priority_capability = "not_applicable"
        self._cupy_component_events: tuple[Any, Any, Any, Any] | None = None
        self._last_cupy_component_timing: dict[str, float] | None = None
        self._cupy_theta_dot_state: Any | None = None
        self._cupy_lambda_state: Any | None = None
        self._cupy_input_buffer: Any | None = None
        self._cupy_work_buffer: Any | None = None
        self._cupy_host_input_owner: Any | None = None
        self._cupy_host_work_owner: Any | None = None
        self._cupy_host_input: np.ndarray | None = None
        self._cupy_host_work: np.ndarray | None = None
        self._cupy_parallel_equivalence: dict[str, Any] | None = None
        if self.config.backend == "cupy":
            self._init_cupy_backend()

    def _validate_config(self) -> None:
        if int(self.config.inner_iterations) < 1:
            raise ValueError("inner_iterations must be a positive integer")
        if self.config.backend not in {"numpy", "cupy"}:
            raise ValueError("backend must be 'numpy' or 'cupy'")

    def freeze(self) -> None:
        """Hold RNN state unchanged when the command path is invalid."""

    def reset_state(self) -> None:
        """Clear stateful solver memory at explicit contact lifecycle boundaries."""
        self.theta_dot_state = np.zeros(6, dtype=float)
        self.lambda_state = np.zeros(6, dtype=float)
        self._sync_cupy_state_from_numpy()

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
        self._sync_cupy_state_from_numpy()

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
        started = time.perf_counter()
        if self.config.backend == "cupy":
            diag = self._solve_cupy(target_state, capture_components=False)
            executed_inner_iterations = int(self.config.inner_iterations)
        else:
            diag = None
            executed_inner_iterations = int(self.config.inner_iterations)
            for _ in range(executed_inner_iterations):
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
            assert diag is not None
        return self._command_result(diag, started, executed_inner_iterations)

    def solve_component_timed(
        self,
        *,
        actual_q: Any,
        actual_qd: Any,
        target_state: dict[str, Any],
    ) -> tuple[StrictRnnCommandResult, dict[str, float]]:
        """Run one unchanged CuPy solve with preallocated component events.

        This diagnostic path is intentionally separate from acceptance timing;
        CUDA event synchronization can add overhead.  It identifies whether a
        wall-time outlier is dominated by CPU packing/enqueue, device kernel,
        D2H, or host completion wait and never discards the sample.
        """

        if self.config.backend != "cupy":
            raise RuntimeError("component timing requires the CuPy backend")
        _finite_vector(actual_q, 6, "actual_q")
        _finite_vector(actual_qd, 6, "actual_qd")
        if not isinstance(target_state, dict):
            raise ValueError("target_state must be a dict")
        started = time.perf_counter()
        diag = self._solve_cupy(target_state, capture_components=True)
        result = self._command_result(
            diag,
            started,
            int(self.config.inner_iterations),
        )
        components = dict(self._last_cupy_component_timing or {})
        components["solve_api_wall_ms"] = (time.perf_counter() - started) * 1000.0
        return result, components

    def _command_result(
        self,
        diag: StrictRnnStepDiagnostics,
        started: float,
        executed_inner_iterations: int,
    ) -> StrictRnnCommandResult:
        diagnostics = dict(diag.__dict__)
        diagnostics.update(
            {
                "inner_iterations": executed_inner_iterations,
                "executed_inner_iterations": executed_inner_iterations,
                "backend": self.config.backend,
                "solve_wall_ms": (time.perf_counter() - started) * 1000.0,
            }
        )
        return StrictRnnCommandResult(
            qdot=diag.theta_dot_state,
            solver_status=40.0,
            residual_norm=diag.constraint_residual_norm,
            diagnostics=diagnostics,
        )

    def _init_cupy_backend(self) -> None:
        try:
            import cupy as cp  # type: ignore[import-not-found]
        except Exception as exc:
            raise RuntimeError("Strict RNN backend 'cupy' requires importable CuPy before live bridge start") from exc
        self._cp = cp
        # CuPy 13.6 on the Ubuntu runtime exposes a dedicated nonblocking
        # Stream but neither deviceGetStreamPriorityRange nor Stream(priority).
        # Priority is therefore an observed capability, never a startup gate.
        try:
            priority_range = getattr(cp.cuda.runtime, "deviceGetStreamPriorityRange", None)
            if priority_range is None:
                raise TypeError("priority API unavailable")
            _least_priority, greatest_priority = priority_range()
            self._cupy_stream_priority = int(greatest_priority)
            self._cupy_stream = cp.cuda.Stream(
                non_blocking=True,
                priority=self._cupy_stream_priority,
            )
            self._cupy_stream_priority_capability = "highest_priority_enabled"
        except (AttributeError, TypeError):
            self._cupy_stream_priority = None
            self._cupy_stream = cp.cuda.Stream(non_blocking=True)
            self._cupy_stream_priority_capability = "unsupported_by_cupy_13_6"
        self._cupy_component_events = (
            cp.cuda.Event(),
            cp.cuda.Event(),
            cp.cuda.Event(),
            cp.cuda.Event(),
        )
        # Fixed-size host/device staging removes all per-tick CuPy allocations
        # and collapses result readback to one contiguous transfer.  The host
        # arrays are page-locked to avoid a possible CUDA-driver staging copy.
        # The v30 A/B evidence did not eliminate the periodic outlier, so this
        # is a bounded marshaling optimization, not a claimed root-cause fix.
        self._cupy_input_buffer = cp.empty(54, dtype=cp.float32)
        self._cupy_work_buffer = cp.zeros(48, dtype=cp.float32)
        try:
            self._cupy_host_input_owner = cp.cuda.alloc_pinned_memory(54 * np.dtype(np.float32).itemsize)
            self._cupy_host_work_owner = cp.cuda.alloc_pinned_memory(48 * np.dtype(np.float32).itemsize)
        except Exception as exc:
            raise RuntimeError("Strict RNN CuPy backend requires pinned host staging buffers") from exc
        self._cupy_host_input = np.frombuffer(
            self._cupy_host_input_owner,
            dtype=np.float32,
            count=54,
        )
        self._cupy_host_work = np.frombuffer(
            self._cupy_host_work_owner,
            dtype=np.float32,
            count=48,
        )
        self._cupy_theta_dot_state = self._cupy_work_buffer[0:6]
        self._cupy_lambda_state = self._cupy_work_buffer[6:12]
        self._precompile_cupy_backend()
        self._cupy_parallel_equivalence = self.validate_cupy_parallel_equivalence(samples=100)

    @property
    def cupy_host_staging_pinned(self) -> bool:
        """Whether both fixed host/device transfer buffers are page-locked."""

        return bool(
            self.config.backend == "cupy"
            and self._cupy_host_input_owner is not None
            and self._cupy_host_work_owner is not None
            and self._cupy_host_input is not None
            and self._cupy_host_work is not None
        )

    @property
    def cupy_dedicated_stream(self) -> bool:
        """Whether solver transfers/kernel/readback use one private stream."""

        return bool(
            self.config.backend == "cupy"
            and self._cupy_stream is not None
        )

    @property
    def cupy_stream_priority(self) -> int | None:
        return self._cupy_stream_priority

    @property
    def cupy_stream_priority_capability(self) -> str:
        return self._cupy_stream_priority_capability

    @property
    def cupy_parallel_equivalence(self) -> dict[str, Any] | None:
        """Startup proof that block-6 and retained serial equations agree."""

        return (
            dict(self._cupy_parallel_equivalence)
            if self._cupy_parallel_equivalence is not None
            else None
        )

    def _precompile_cupy_backend(self) -> None:
        if self._cp is None:
            raise RuntimeError("CuPy backend is not initialized")
        self._solve_cupy(
            {
                "J": np.eye(6, dtype=float),
                "xdot_c": np.zeros(6, dtype=float),
                "omega_minus": np.full(6, -0.15, dtype=float),
                "omega_plus": np.full(6, 0.15, dtype=float),
                "dt": 0.002,
                "epsilon": self.config.epsilon,
                "r": self.config.sigr_exponent_r,
                "cmd_valid": True,
            }
        )

    def _sync_cupy_state_from_numpy(self) -> None:
        if self._cp is None:
            return
        if (
            self._cupy_work_buffer is None
            or self._cupy_host_work is None
            or self._cupy_stream is None
        ):
            raise RuntimeError("CuPy state buffers are not initialized")
        np.copyto(self._cupy_host_work[0:6], self.theta_dot_state, casting="unsafe")
        np.copyto(self._cupy_host_work[6:12], self.lambda_state, casting="unsafe")
        self._cupy_work_buffer[0:12].set(
            self._cupy_host_work[0:12],
            stream=self._cupy_stream,
        )
        # State reset/warm-start is outside the 500 Hz loop.  Complete both
        # host-to-device copies here so their deferred cost cannot leak into
        # the first post-boundary solve tick.
        self._cupy_stream.synchronize()

    def _cupy_solve_kernel(self) -> Any:
        if self._cupy_kernel is not None:
            return self._cupy_kernel
        if self._cp is None:
            raise RuntimeError("CuPy backend is not initialized")
        self._cupy_kernel = self._cp.RawKernel(
            r"""
extern "C" __global__
void strict_rnn_solve(
    const float* J,
    const float* xdot,
    const float* lower,
    const float* upper,
    float* theta,
    float* lambda_state,
    float* proj_input_out,
    float* projected_out,
    float* sigr_arg_out,
    float* sigr_val_out,
    float* limited_out,
    float* residual_out,
    const float dt,
    const float epsilon,
    const float r,
    const int inner_iterations,
    const int cmd_valid
) {
    const int i = (int)threadIdx.x;
    if (i >= 6) {
        return;
    }
    __shared__ float proj[6];
    __shared__ float projected[6];
    __shared__ float sigr_arg[6];
    __shared__ float sigr_val[6];
    __shared__ float limited[6];
    __shared__ float residual[6];
    for (int iter = 0; iter < inner_iterations; ++iter) {
        float projection_accum = 0.0f;
        for (int row = 0; row < 6; ++row) {
            projection_accum += J[row * 6 + i] * lambda_state[row];
        }
        proj[i] = projection_accum;
        projected[i] = fminf(fmaxf(projection_accum, lower[i]), upper[i]);
        sigr_arg[i] = theta[i] - projected[i];
        __syncthreads();
        if (cmd_valid) {
            float abs_arg = fabsf(sigr_arg[i]);
            float sign_arg = (sigr_arg[i] > 0.0f) - (sigr_arg[i] < 0.0f);
            sigr_val[i] = powf(abs_arg, r) * sign_arg;
            float theta_delta = -(dt / epsilon) * sigr_val[i];
            limited[i] = fabsf(theta_delta) > abs_arg ? 1.0f : 0.0f;
            theta[i] = limited[i] > 0.5f ? projected[i] : theta[i] + theta_delta;
        } else {
            sigr_val[i] = 0.0f;
            limited[i] = 0.0f;
        }
        // Every row residual must observe all six updated theta values.
        __syncthreads();
        float residual_accum = 0.0f;
        for (int col = 0; col < 6; ++col) {
            residual_accum += J[i * 6 + col] * theta[col];
        }
        residual[i] = residual_accum - xdot[i];
        __syncthreads();
        if (cmd_valid) {
            lambda_state[i] -= (dt / epsilon) * residual[i];
        }
        // The next projection must observe all six updated lambda values.
        __syncthreads();
    }
    proj_input_out[i] = proj[i];
    projected_out[i] = projected[i];
    sigr_arg_out[i] = sigr_arg[i];
    sigr_val_out[i] = sigr_val[i];
    limited_out[i] = limited[i];
    residual_out[i] = residual[i];
}
""",
            "strict_rnn_solve",
        )
        return self._cupy_kernel

    def _get_cupy_serial_reference_kernel(self) -> Any:
        """Retained scalar equations used only by the startup equivalence gate."""

        if self._cupy_serial_reference_kernel is not None:
            return self._cupy_serial_reference_kernel
        if self._cp is None:
            raise RuntimeError("CuPy backend is not initialized")
        self._cupy_serial_reference_kernel = self._cp.RawKernel(
            r"""
extern "C" __global__
void strict_rnn_solve_serial_reference(
    const float* J,
    const float* xdot,
    const float* lower,
    const float* upper,
    float* theta,
    float* lambda_state,
    float* proj_input_out,
    float* projected_out,
    float* sigr_arg_out,
    float* sigr_val_out,
    float* limited_out,
    float* residual_out,
    const float dt,
    const float epsilon,
    const float r,
    const int inner_iterations,
    const int cmd_valid
) {
    float proj[6];
    float projected[6];
    float sigr_arg[6];
    float sigr_val[6];
    float limited[6];
    float residual[6];
    for (int iter = 0; iter < inner_iterations; ++iter) {
        for (int i = 0; i < 6; ++i) {
            float accum = 0.0f;
            for (int row = 0; row < 6; ++row) {
                accum += J[row * 6 + i] * lambda_state[row];
            }
            proj[i] = accum;
            projected[i] = fminf(fmaxf(accum, lower[i]), upper[i]);
            sigr_arg[i] = theta[i] - projected[i];
            if (cmd_valid) {
                float abs_arg = fabsf(sigr_arg[i]);
                float sign_arg = (sigr_arg[i] > 0.0f) - (sigr_arg[i] < 0.0f);
                sigr_val[i] = powf(abs_arg, r) * sign_arg;
                float theta_delta = -(dt / epsilon) * sigr_val[i];
                limited[i] = fabsf(theta_delta) > abs_arg ? 1.0f : 0.0f;
                theta[i] = limited[i] > 0.5f ? projected[i] : theta[i] + theta_delta;
            } else {
                sigr_val[i] = 0.0f;
                limited[i] = 0.0f;
            }
        }
        for (int row = 0; row < 6; ++row) {
            float accum = 0.0f;
            for (int col = 0; col < 6; ++col) {
                accum += J[row * 6 + col] * theta[col];
            }
            residual[row] = accum - xdot[row];
        }
        if (cmd_valid) {
            for (int i = 0; i < 6; ++i) {
                lambda_state[i] -= (dt / epsilon) * residual[i];
            }
        }
    }
    for (int i = 0; i < 6; ++i) {
        proj_input_out[i] = proj[i];
        projected_out[i] = projected[i];
        sigr_arg_out[i] = sigr_arg[i];
        sigr_val_out[i] = sigr_val[i];
        limited_out[i] = limited[i];
        residual_out[i] = residual[i];
    }
}
""",
            "strict_rnn_solve_serial_reference",
        )
        return self._cupy_serial_reference_kernel

    def validate_cupy_parallel_equivalence(self, *, samples: int = 100) -> dict[str, Any]:
        """Fail startup unless block-6 matches the original serial equations.

        Both device states evolve independently across deterministic inputs;
        all 48 state/diagnostic float32 values are compared bit-for-bit after
        every solve.  This runs before the control loop and never touches the
        production state buffers.
        """

        if int(samples) < 1:
            raise ValueError("parallel equivalence samples must be positive")
        if self._cp is None or self._cupy_stream is None:
            raise RuntimeError("CuPy backend is not initialized")
        cp = self._cp
        input_device = cp.empty(54, dtype=cp.float32)
        parallel_work = cp.zeros(48, dtype=cp.float32)
        serial_work = cp.zeros(48, dtype=cp.float32)
        host_input = np.empty(54, dtype=np.float32)
        parallel_host = np.empty(48, dtype=np.float32)
        serial_host = np.empty(48, dtype=np.float32)
        rng = np.random.default_rng(56030)

        def args_for(work: Any) -> tuple[Any, ...]:
            return (
                input_device[0:36],
                input_device[36:42],
                input_device[42:48],
                input_device[48:54],
                work[0:6],
                work[6:12],
                work[12:18],
                work[18:24],
                work[24:30],
                work[30:36],
                work[36:42],
                work[42:48],
                np.float32(0.002),
                np.float32(self.config.epsilon),
                np.float32(self.config.sigr_exponent_r),
                np.int32(int(self.config.inner_iterations)),
                np.int32(1),
            )

        max_abs_difference = 0.0
        for index in range(int(samples)):
            jacobian = np.eye(6, dtype=np.float32)
            jacobian += rng.normal(0.0, 0.02, size=(6, 6)).astype(np.float32)
            host_input[0:36] = jacobian.reshape(36)
            host_input[36:42] = rng.uniform(-0.003, 0.003, size=6).astype(np.float32)
            host_input[42:48] = -0.05
            host_input[48:54] = 0.05
            input_device.set(host_input, stream=self._cupy_stream)
            self._cupy_solve_kernel()(
                (1,),
                (6,),
                args_for(parallel_work),
                stream=self._cupy_stream,
            )
            self._get_cupy_serial_reference_kernel()(
                (1,),
                (1,),
                args_for(serial_work),
                stream=self._cupy_stream,
            )
            parallel_work.get(
                out=parallel_host,
                stream=self._cupy_stream,
                blocking=True,
            )
            serial_work.get(
                out=serial_host,
                stream=self._cupy_stream,
                blocking=True,
            )
            difference = float(np.max(np.abs(parallel_host - serial_host)))
            max_abs_difference = max(max_abs_difference, difference)
            if not np.array_equal(parallel_host, serial_host):
                raise RuntimeError(
                    "parallel strict-RNN kernel failed serial-equation equivalence "
                    f"at sample {index}: max_abs_difference={difference}"
                )
        return {
            "samples": int(samples),
            "compared_float32_values_per_sample": 48,
            "bitwise_equal": True,
            "max_abs_difference": max_abs_difference,
            "inner_iterations": int(self.config.inner_iterations),
            "parallel_block_threads": 6,
            "serial_reference_threads": 1,
        }

    def _solve_cupy(
        self,
        target_state: dict[str, Any],
        *,
        capture_components: bool = False,
    ) -> StrictRnnStepDiagnostics:
        if (
            self._cp is None
            or self._cupy_theta_dot_state is None
            or self._cupy_lambda_state is None
            or self._cupy_input_buffer is None
            or self._cupy_work_buffer is None
            or self._cupy_host_input is None
            or self._cupy_host_work is None
            or self._cupy_stream is None
            or (capture_components and self._cupy_component_events is None)
        ):
            raise RuntimeError("CuPy backend is not initialized")
        cp = self._cp
        jacobian = _finite_matrix(target_state["J"], (6, 6), "J")
        xdot = _finite_array(target_state["xdot_c"], 6, "xdot_c")
        lower = _finite_array(target_state["omega_minus"], 6, "omega_minus")
        upper = _finite_array(target_state["omega_plus"], 6, "omega_plus")
        if np.any(lower > upper):
            raise ValueError("omega_minus must be <= omega_plus element-wise")
        step_s = float(target_state["dt"])
        if not math.isfinite(step_s) or step_s <= 0.0:
            raise ValueError("dt must be finite and positive")
        eps = self.config.epsilon if target_state.get("epsilon") is None else float(target_state["epsilon"])
        if not math.isfinite(eps) or eps <= 0.0:
            raise ValueError("epsilon must be finite and positive")
        exponent = self.config.sigr_exponent_r if target_state.get("r") is None else float(target_state["r"])
        if not math.isfinite(float(exponent)) or not 0.0 < float(exponent) <= 1.0:
            raise ValueError("sigr exponent r must be in (0, 1]")

        component_started = time.perf_counter()
        pack_started = time.perf_counter()
        np.copyto(self._cupy_host_input[0:36], jacobian.reshape(36), casting="unsafe")
        np.copyto(self._cupy_host_input[36:42], xdot, casting="unsafe")
        np.copyto(self._cupy_host_input[42:48], lower, casting="unsafe")
        np.copyto(self._cupy_host_input[48:54], upper, casting="unsafe")
        cpu_pack_ms = (time.perf_counter() - pack_started) * 1000.0
        if capture_components:
            assert self._cupy_component_events is not None
            event_start, event_h2d, event_kernel, event_d2h = self._cupy_component_events
            event_start.record(self._cupy_stream)
        h2d_enqueue_started = time.perf_counter()
        self._cupy_input_buffer.set(
            self._cupy_host_input,
            stream=self._cupy_stream,
        )
        h2d_enqueue_ms = (time.perf_counter() - h2d_enqueue_started) * 1000.0
        if capture_components:
            event_h2d.record(self._cupy_stream)
        proj_input = self._cupy_work_buffer[12:18]
        projected = self._cupy_work_buffer[18:24]
        sigr_arg = self._cupy_work_buffer[24:30]
        sigr_val = self._cupy_work_buffer[30:36]
        limited = self._cupy_work_buffer[36:42]
        residual = self._cupy_work_buffer[42:48]
        kernel_enqueue_started = time.perf_counter()
        self._cupy_solve_kernel()(
            (1,),
            (6,),
            (
                self._cupy_input_buffer[0:36],
                self._cupy_input_buffer[36:42],
                self._cupy_input_buffer[42:48],
                self._cupy_input_buffer[48:54],
                self._cupy_theta_dot_state,
                self._cupy_lambda_state,
                proj_input,
                projected,
                sigr_arg,
                sigr_val,
                limited,
                residual,
                np.float32(step_s),
                np.float32(eps),
                np.float32(exponent),
                np.int32(int(self.config.inner_iterations)),
                np.int32(1 if bool(target_state.get("cmd_valid", True)) else 0),
            ),
            stream=self._cupy_stream,
        )
        kernel_enqueue_ms = (time.perf_counter() - kernel_enqueue_started) * 1000.0
        if capture_components:
            event_kernel.record(self._cupy_stream)
            d2h_enqueue_started = time.perf_counter()
            self._cupy_work_buffer.get(
                out=self._cupy_host_work,
                stream=self._cupy_stream,
                blocking=False,
            )
            d2h_enqueue_ms = (time.perf_counter() - d2h_enqueue_started) * 1000.0
            event_d2h.record(self._cupy_stream)
            completion_wait_started = time.perf_counter()
            event_d2h.synchronize()
            completion_wait_ms = (time.perf_counter() - completion_wait_started) * 1000.0
            self._last_cupy_component_timing = {
                "cpu_pack_ms": cpu_pack_ms,
                "h2d_enqueue_ms": h2d_enqueue_ms,
                "kernel_enqueue_ms": kernel_enqueue_ms,
                "d2h_enqueue_ms": d2h_enqueue_ms,
                "host_completion_wait_ms": completion_wait_ms,
                "cuda_h2d_ms": float(cp.cuda.get_elapsed_time(event_start, event_h2d)),
                "cuda_kernel_ms": float(cp.cuda.get_elapsed_time(event_h2d, event_kernel)),
                "cuda_d2h_ms": float(cp.cuda.get_elapsed_time(event_kernel, event_d2h)),
                "cupy_component_wall_ms": (time.perf_counter() - component_started) * 1000.0,
            }
        else:
            self._cupy_work_buffer.get(
                out=self._cupy_host_work,
                stream=self._cupy_stream,
                blocking=True,
            )
        np.copyto(self.theta_dot_state, self._cupy_host_work[0:6], casting="unsafe")
        np.copyto(self.lambda_state, self._cupy_host_work[6:12], casting="unsafe")
        proj_input_np = self._cupy_host_work[12:18]
        projected_np = self._cupy_host_work[18:24]
        sigr_arg_np = self._cupy_host_work[24:30]
        sigr_val_np = self._cupy_host_work[30:36]
        limited_np = self._cupy_host_work[36:42]
        residual_np = self._cupy_host_work[42:48]
        active_bounds = np.isclose(projected_np, lower) | np.isclose(projected_np, upper)
        return StrictRnnStepDiagnostics(
            theta_dot_state=_tuple6(self.theta_dot_state),
            lambda_state=_tuple6(self.lambda_state),
            proj_input_form="J.T @ lambda_state",
            lambda_update_form="lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
            proj_input=_tuple6(proj_input_np),
            projected=_tuple6(projected_np),
            sigr_arg=_tuple6(sigr_arg_np),
            sigr_val=_tuple6(sigr_val_np),
            theta_dot_update_limited_mask=tuple(bool(value > 0.5) for value in limited_np.reshape(6)),  # type: ignore[return-value]
            omega_minus=_tuple6(lower),
            omega_plus=_tuple6(upper),
            active_bounds_mask=tuple(bool(value) for value in active_bounds.reshape(6)),  # type: ignore[return-value]
            constraint_residual_norm=float(np.linalg.norm(residual_np)),
            xdot_c=_tuple6(xdot),
            epsilon=eps,
            sigr_exponent_r=exponent,
        )


def main() -> int:
    payload = load_paper_truth()
    assert_paper_truth_verified(payload)
    raise SystemExit("strict Step5d RNN offline solver is available; live entrypoint is blocked")


if __name__ == "__main__":
    raise SystemExit(main())
