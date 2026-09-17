"""Native equality/box QP; a failed solve never produces an admissible command."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import threading
import time

import numpy as np


class QpError(RuntimeError):
    pass


@dataclass(frozen=True)
class QpSolverProfile:
    library: Path
    qdot_limit_rad_s: float = 0.05
    deadline_s: float = 0.001
    preconstruct_outside_tick: bool = True
    _library_sha256: str = field(init=False, repr=False)

    def __post_init__(self):
        if not np.isfinite(self.qdot_limit_rad_s) or not 0 < self.qdot_limit_rad_s <= 0.15:
            raise ValueError("QP joint speed cap must be in (0, 0.15]")
        if not np.isfinite(self.deadline_s) or self.deadline_s <= 0:
            raise ValueError("QP runtime requires a finite positive deadline")
        if self.preconstruct_outside_tick is not True:
            raise ValueError("QP must be constructed outside the control tick")
        object.__setattr__(self, "library", Path(self.library).resolve(strict=True))
        object.__setattr__(self, "_library_sha256", hashlib.sha256(self.library.read_bytes()).hexdigest())

    def as_dict(self):
        return {"schema": "contact-qp-profile-v1", "id": "native-equality-qp",
                "backend": "osqp-codegen-c", "library": str(self.library),
                "library_sha256": self._library_sha256,
                "qdot_limit_rad_s": self.qdot_limit_rad_s, "deadline_s": self.deadline_s,
                "preconstruct_outside_tick": True}

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class QpResult:
    qdot: tuple[float, ...]
    iterations: int
    equality_residual: float
    bound_violation: float
    primal_residual: float
    dual_residual: float
    elapsed_s: float


_LOCKS: dict[str, threading.Lock] = {}


def _array(value, shape, name):
    result = np.asarray(value, dtype=np.float64)
    if result.shape != shape or not np.isfinite(result).all():
        raise QpError(f"{name} must be finite with shape {shape}")
    return np.ascontiguousarray(result)


class NativeContactQp:
    """Each instance owns its warm state; the generated C workspace is serialized.

    A deadline is checked before exposing a command. This is not a preemptive
    scheduler or proof of a 500 Hz physical loop. None disables the offline
    deadline only. Real-time admission belongs to the complete writer.
    """
    def __init__(self, library: Path, *, deadline_s: float | None = 0.001):
        if deadline_s is not None and (not np.isfinite(deadline_s) or deadline_s <= 0):
            raise ValueError("deadline must be positive or None for offline use")
        self.deadline_s = deadline_s
        path = str(Path(library).resolve(strict=True))
        self._lib = ctypes.CDLL(path)
        self._lock = _LOCKS.setdefault(path, threading.Lock())
        self._solve = self._lib.contact_qp_solve
        array = np.ctypeslib.ndpointer(dtype=np.float64, flags="C_CONTIGUOUS")
        self._solve.argtypes = [array] * 9
        self._solve.restype = ctypes.c_int
        self.x = np.zeros(6)
        self.y = np.zeros(12)
        self._out_x = np.zeros(6)
        self._out_y = np.zeros(12)
        self._diagnostics = np.zeros(3)

    def reset(self):
        self.x.fill(0)
        self.y.fill(0)

    def snapshot(self):
        return {"x": self.x.tolist(), "y": self.y.tolist()}

    def restore(self, state):
        x = _array(state["x"], (6,), "warm x")
        y = _array(state["y"], (12,), "warm y")
        self.x[:] = x
        self.y[:] = y

    def solve(self, J, twist, lower, upper) -> QpResult:
        started = time.perf_counter()
        jac = _array(J, (6, 6), "J")
        vel = _array(twist, (6,), "twist")
        lo = _array(lower, (6,), "lower")
        hi = _array(upper, (6,), "upper")
        if np.any(lo > hi):
            raise QpError("inverted joint velocity bounds")
        with self._lock:
            status = self._solve(jac, vel, lo, hi, self.x, self.y,
                                 self._out_x, self._out_y, self._diagnostics)
        elapsed = time.perf_counter() - started
        if status != 1:
            raise QpError(f"OSQP did not solve accurately: status={status}")
        if self.deadline_s is not None and elapsed > self.deadline_s:
            raise QpError(f"QP deadline exceeded: {elapsed:.6f}s")
        if not np.isfinite(self._out_x).all() or not np.isfinite(self._out_y).all():
            raise QpError("nonfinite native QP solution")
        residual = float(np.max(np.abs(jac @ self._out_x - vel)))
        violation = float(max(0, np.max(lo-self._out_x), np.max(self._out_x-hi)))
        if residual > 1e-6 or violation > 1e-7:
            raise QpError(f"QP command validation failed: equality={residual}, bounds={violation}")
        self.x[:] = self._out_x
        self.y[:] = self._out_y
        return QpResult(tuple(map(float, self.x)), int(self._diagnostics[0]),
                        residual, violation, float(self._diagnostics[1]),
                        float(self._diagnostics[2]), elapsed)
