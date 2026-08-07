"""SHADOW-ONLY early-abort decision helpers for r008 PATH samples.

Active motion abort is blocked (``STOP_FOR_ANDY``). This module only
resolves arming mode and the O(1) trigger predicate; it never stops motion.
"""

from __future__ import annotations

import os
from typing import Mapping

# Locked 2026-08-07 parameters (handoff §0) — do not drift from calibration.
EARLY_ABORT_MODE_ENV = "R008_EARLY_ABORT_MODE"
GUARD_FRAC = 0.10
KAPPA_START = 3.0
KAPPA_END = 1.3
MIDPOINT = 0.5
STEEPNESS = 10.0


def resolve_early_abort_mode(environ: Mapping[str, str] | None = None) -> str:
    """Return ``off`` | ``shadow`` | ``active``.

    Default (unset / empty / unknown) is ``shadow``. ``active`` is accepted as
    a mode token but live wiring must still behave as shadow-only until Andy
    signs a non-hard-stop motion channel.
    """

    env = os.environ if environ is None else environ
    raw = str(env.get(EARLY_ABORT_MODE_ENV, "") or "").strip().lower()
    if raw in {"", "shadow", "on", "1", "true", "yes"}:
        return "shadow"
    if raw in {"off", "0", "false", "no", "disabled"}:
        return "off"
    if raw == "active":
        return "active"
    return "shadow"


def progress_fraction(
    path_time_s: float, *, formal_start_s: float, formal_end_s: float
) -> float:
    span = float(formal_end_s) - float(formal_start_s)
    if span <= 0.0:
        return 0.0
    return (float(path_time_s) - float(formal_start_s)) / span


def should_trigger_early_abort(
    *,
    progress: float,
    partial_mae_n: float,
    best_so_far_mae_n: float,
    kappa: float,
    guard_frac: float = GUARD_FRAC,
) -> bool:
    """True when past guard and partial MAE breaches kappa * best."""

    if progress < float(guard_frac):
        return False
    if best_so_far_mae_n <= 0.0:
        return False
    return float(partial_mae_n) >= float(kappa) * float(best_so_far_mae_n)


__all__ = [
    "EARLY_ABORT_MODE_ENV",
    "GUARD_FRAC",
    "KAPPA_START",
    "KAPPA_END",
    "MIDPOINT",
    "STEEPNESS",
    "resolve_early_abort_mode",
    "progress_fraction",
    "should_trigger_early_abort",
]
