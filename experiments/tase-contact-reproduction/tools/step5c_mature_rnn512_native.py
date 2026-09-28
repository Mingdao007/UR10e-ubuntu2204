"""Offline candidate adapter for the mature r=1, 512-step C++ RNN.

The shared library is passed explicitly. This module does not select a live
solver profile or authorize a physical attempt.
"""

from __future__ import annotations

import ctypes
import math
from pathlib import Path
import time
from typing import Any

import numpy as np

from step5c_strict_rnn import StrictRnnCommandResult


_DOUBLE_PTR = ctypes.POINTER(ctypes.c_double)


def _vec(values: Any, shape: tuple[int, ...], name: str) -> np.ndarray:
    result = np.ascontiguousarray(values, dtype=np.float64)
    if result.shape != shape or not np.isfinite(result).all():
        raise ValueError(f"{name} must be finite with shape {shape}")
    return result


def _ptr(values: np.ndarray) -> _DOUBLE_PTR:
    return values.ctypes.data_as(_DOUBLE_PTR)


class NativeMatureRnn512:
    """Same state/command seam as StrictTaseRnnSolver, for I-1 replay only."""

    def __init__(self, library: Path) -> None:
        library = Path(library).resolve(strict=True)
        self.library_path = library
        self._lib = ctypes.CDLL(str(library))
        self._lib.tase_rnn512_abi_version.argtypes = []
        self._lib.tase_rnn512_abi_version.restype = ctypes.c_int
        if self._lib.tase_rnn512_abi_version() != 1:
            raise RuntimeError("mature RNN512 native ABI version differs")
        self._lib.tase_rnn512_create.argtypes = []
        self._lib.tase_rnn512_create.restype = ctypes.c_void_p
        self._lib.tase_rnn512_destroy.argtypes = [ctypes.c_void_p]
        self._lib.tase_rnn512_destroy.restype = None
        self._lib.tase_rnn512_restore.argtypes = [ctypes.c_void_p, _DOUBLE_PTR, _DOUBLE_PTR]
        self._lib.tase_rnn512_restore.restype = ctypes.c_int
        self._lib.tase_rnn512_snapshot.argtypes = [ctypes.c_void_p, _DOUBLE_PTR, _DOUBLE_PTR]
        self._lib.tase_rnn512_snapshot.restype = ctypes.c_int
        self._lib.tase_rnn512_solve.argtypes = [
            ctypes.c_void_p, _DOUBLE_PTR, _DOUBLE_PTR, _DOUBLE_PTR,
            _DOUBLE_PTR, ctypes.c_double, ctypes.c_int, _DOUBLE_PTR,
            _DOUBLE_PTR, _DOUBLE_PTR, _DOUBLE_PTR,
        ]
        self._lib.tase_rnn512_solve.restype = ctypes.c_int
        self._handle = self._lib.tase_rnn512_create()
        if not self._handle:
            raise RuntimeError("mature RNN512 native creation failed")

    def close(self) -> None:
        if self._handle:
            self._lib.tase_rnn512_destroy(self._handle)
            self._handle = None

    def __del__(self) -> None:
        if getattr(self, "_handle", None):
            self.close()

    def reset_state(self) -> None:
        self.restore({"theta_dot_state": [0.0] * 6, "lambda_state": [0.0] * 6})

    def freeze(self) -> None:
        pass

    def snapshot(self) -> dict[str, list[float]]:
        theta = np.empty(6, dtype=np.float64)
        lambd = np.empty(6, dtype=np.float64)
        if self._lib.tase_rnn512_snapshot(self._handle, _ptr(theta), _ptr(lambd)) != 0:
            raise RuntimeError("mature RNN512 native snapshot failed")
        return {"theta_dot_state": theta.tolist(), "lambda_state": lambd.tolist()}

    def restore(self, state: dict[str, Any]) -> None:
        theta = _vec(state["theta_dot_state"], (6,), "theta_dot_state")
        lambd = _vec(state["lambda_state"], (6,), "lambda_state")
        if self._lib.tase_rnn512_restore(self._handle, _ptr(theta), _ptr(lambd)) != 0:
            raise RuntimeError("mature RNN512 native restore failed")

    def warm_start(self, *, J: Any, xdot_c: Any, omega_minus: Any,
                   omega_plus: Any, damping: float = 1e-4) -> None:
        jacobian = _vec(J, (6, 6), "J")
        xdot = _vec(xdot_c, (6,), "xdot_c")
        lower = _vec(omega_minus, (6,), "omega_minus")
        upper = _vec(omega_plus, (6,), "omega_plus")
        if np.any(lower > upper) or not math.isfinite(damping) or damping <= 0:
            raise ValueError("mature RNN512 warm-start bounds or damping are invalid")
        lambd = np.linalg.solve(jacobian @ jacobian.T + damping ** 2 * np.eye(6), xdot)
        theta = np.clip(jacobian.T @ lambd, lower, upper)
        self.restore({"theta_dot_state": theta, "lambda_state": lambd})

    def solve(self, *, actual_q: Any, actual_qd: Any,
              target_state: dict[str, Any]) -> StrictRnnCommandResult:
        _vec(actual_q, (6,), "actual_q")
        _vec(actual_qd, (6,), "actual_qd")
        if float(target_state.get("epsilon", .022)) != .022 or float(target_state.get("r", 1.0)) != 1.0:
            raise ValueError("mature RNN512 native equation identity differs")
        jacobian = _vec(target_state["J"], (6, 6), "J")
        xdot = _vec(target_state["xdot_c"], (6,), "xdot_c")
        lower = _vec(target_state["omega_minus"], (6,), "omega_minus")
        upper = _vec(target_state["omega_plus"], (6,), "omega_plus")
        dt = float(target_state["dt"])
        valid = target_state.get("cmd_valid", True)
        if type(valid) is not bool:
            raise ValueError("cmd_valid must be bool")
        qdot = np.empty(6, dtype=np.float64)
        lambd = np.empty(6, dtype=np.float64)
        residual = np.empty(6, dtype=np.float64)
        projected = np.empty(6, dtype=np.float64)
        started = time.perf_counter_ns()
        code = self._lib.tase_rnn512_solve(
            self._handle, _ptr(jacobian), _ptr(xdot), _ptr(lower), _ptr(upper),
            dt, int(valid), _ptr(qdot), _ptr(lambd), _ptr(residual), _ptr(projected),
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1e6
        if code != 0:
            raise RuntimeError(f"mature RNN512 native solve rejected: {code}")
        return StrictRnnCommandResult(
            qdot=tuple(float(value) for value in qdot),
            solver_status=40.0,
            residual_norm=float(np.linalg.norm(residual)),
            diagnostics={
                "backend": "cpp-double",
                "inner_iterations": 512,
                "executed_inner_iterations": 512,
                "active_bounds_mask": tuple(bool(value) for value in (
                    np.isclose(projected, lower) | np.isclose(projected, upper)
                )),
                "solve_wall_ms": elapsed_ms,
            },
        )
