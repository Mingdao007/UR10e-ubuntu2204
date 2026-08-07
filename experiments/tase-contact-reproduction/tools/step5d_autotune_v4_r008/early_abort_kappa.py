"""Single source of truth for the early-abort kappa(progress) formula."""

from __future__ import annotations

import math


def sigmoid_kappa(
    progress_fraction: float,
    *,
    guard_frac: float,
    kappa_start: float,
    kappa_end: float,
    midpoint: float = 0.5,
    steepness: float = 10.0,
) -> float:
    """The early-abort threshold multiplier at a point in the formal window.

    Pure function, no I/O, no ledger/trace access -- this is the single
    source of truth for the kappa(progress) formula. ``policy_decay_sigmoid``
    below (used for offline backtesting) and the live PATH60 trigger hook
    must both import *this* function rather than each re-deriving the
    formula, so the deployed live threshold can never silently drift from
    what was backtested.

    ``progress_fraction`` is elapsed_s / (FORMAL_END_S - FORMAL_START_S),
    i.e. 0..1 across the formal window. Callers should not evaluate the
    trigger before ``guard_frac`` at all -- this function does not
    special-case the guard itself, it just clamps the ramp there.

    Locked 2026-08-07 live parameters: guard_frac=0.10, kappa_start=3.0,
    kappa_end=1.3, midpoint=0.5, steepness=10.0.
    """

    span = max(1e-9, 1.0 - guard_frac)
    progress_in_span = min(1.0, max(0.0, (progress_fraction - guard_frac) / span))
    weight = 1.0 / (1.0 + math.exp(steepness * (progress_in_span - midpoint)))
    return kappa_end + (kappa_start - kappa_end) * weight


__all__ = ["sigmoid_kappa"]
