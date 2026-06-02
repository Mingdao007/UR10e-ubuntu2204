from __future__ import annotations

import numpy as np


def sigr(x: np.ndarray | float, r: float) -> np.ndarray | float:
    """Finite-time power sign function |x|^r sign(x)."""
    return np.sign(x) * np.power(np.abs(x), r)


def simulate_scalar_convergence(
    *,
    r: float,
    initial_error: float,
    gain: float,
    dt: float,
    duration_s: float,
    convergence_tol: float,
) -> dict:
    """Integrate e_dot = -gain * |e|^r sign(e) with explicit Euler."""
    steps = int(round(duration_s / dt)) + 1
    t = np.linspace(0.0, duration_s, steps)
    e = np.empty(steps, dtype=float)
    e[0] = float(initial_error)

    convergence_time = None
    for i in range(1, steps):
        e[i] = e[i - 1] - dt * gain * float(sigr(e[i - 1], r))
        if abs(e[i]) < convergence_tol:
            e[i:] = e[i]
            convergence_time = float(t[i])
            break

    if convergence_time is None:
        convergence_time = float("nan")

    return {
        "t": t,
        "error": e,
        "convergence_time_s": convergence_time,
        "tail_abs_error": float(np.mean(np.abs(e[-max(1, steps // 20) :]))),
        "max_abs_error": float(np.max(np.abs(e))),
    }

