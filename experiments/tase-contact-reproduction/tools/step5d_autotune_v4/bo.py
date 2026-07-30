"""Deterministic staged-bootstrap and trust-region primitives for V4 BO."""

from __future__ import annotations

import math
from dataclasses import replace
from enum import Enum
from typing import Sequence

from .contracts import (
    D_ANCHOR,
    I_GRID,
    I_ON_ANCHOR,
    KO_ANCHOR,
    KP_ANCHOR,
    P_ANCHOR,
    TAU_ANCHOR,
    V4Candidate,
    V4ContractError,
    validate_live_transition,
)


class BootstrapStage(str, Enum):
    PD = "pd"
    TAU = "tau"
    I = "i"
    KO_KP = "ko_kp"


P_PROBES = (0.0002973017788, 0.0004204482076)
D_PROBES = (23.5450996, 33.2977992)
TAU_PROBES = (0.2943137453, 0.4162224903)


def anchor() -> V4Candidate:
    return V4Candidate()


def initial_pd_batches() -> tuple[tuple[V4Candidate, ...], tuple[V4Candidate, ...]]:
    base = anchor()
    batch_a = (
        base,
        base,
        base,
        replace(base, force_p_gain=P_PROBES[0]),
        replace(base, force_p_gain=P_PROBES[1]),
    )
    batch_b = (
        base,
        base,
        base,
        replace(base, force_damping=D_PROBES[0]),
        replace(base, force_damping=D_PROBES[1]),
    )
    return batch_a, batch_b


def tau_qualification_batch(incumbent: V4Candidate) -> tuple[V4Candidate, ...]:
    if not math.isclose(incumbent.force_i_gain, 0.0, abs_tol=1e-15):
        raise V4ContractError("tau qualification must run before I is enabled")
    return (
        incumbent,
        incumbent,
        incumbent,
        replace(incumbent, normal_filter_tau_s=TAU_PROBES[0]),
        replace(incumbent, normal_filter_tau_s=TAU_PROBES[1]),
    )


def i_qualification_path(incumbent: V4Candidate) -> tuple[V4Candidate, ...]:
    if not incumbent.i_off:
        raise V4ContractError("I qualification path must start from I-off")
    return tuple(
        replace(incumbent, force_i_gain=I_ON_ANCHOR * (2.0**k)) for k in I_GRID
    )


def ko_kp_grid(incumbent: V4Candidate) -> tuple[V4Candidate, ...]:
    """Open Ko/Kp last; each returned candidate changes one coordinate."""
    ko_values = tuple(
        KO_ANCHOR * (2.0 ** (0.25 * index)) for index in range(1, 5)
    )
    kp_values = tuple(
        KP_ANCHOR * (2.0 ** (0.25 * index)) for index in range(1, 5)
    )
    return tuple(
        [replace(incumbent, orientation_ko=value) for value in ko_values]
        + [replace(incumbent, motion_kp=value) for value in kp_values]
    )


def order_for_live_execution(
    start: V4Candidate, candidates: Sequence[V4Candidate]
) -> tuple[V4Candidate, ...]:
    """Build a deterministic one-coordinate, <=0.25-octave live path."""
    remaining = list(candidates)
    ordered: list[V4Candidate] = []
    current = start
    while remaining:
        selected_index: int | None = None
        for index, candidate in enumerate(remaining):
            try:
                validate_live_transition(current, candidate)
            except V4ContractError:
                continue
            selected_index = index
            break
        if selected_index is None:
            raise V4ContractError(
                "candidate set has no safe one-coordinate 0.25-octave continuation"
            )
        current = remaining.pop(selected_index)
        ordered.append(current)
    return tuple(ordered)


def assert_first_release_anchor(candidate: V4Candidate) -> None:
    """The r001 release anchor cannot silently change D or normalization."""
    if not math.isclose(candidate.force_damping, D_ANCHOR, abs_tol=1e-12):
        raise V4ContractError("first V4 release anchor requires D=28")
    if not math.isclose(candidate.force_p_gain, P_ANCHOR, abs_tol=1e-14):
        raise V4ContractError("first V4 release anchor P differs")
    if not math.isclose(candidate.normal_filter_tau_s, TAU_ANCHOR, abs_tol=1e-12):
        raise V4ContractError("first V4 release anchor tau differs")


__all__ = [
    "BootstrapStage",
    "D_PROBES",
    "P_PROBES",
    "TAU_PROBES",
    "anchor",
    "assert_first_release_anchor",
    "i_qualification_path",
    "initial_pd_batches",
    "ko_kp_grid",
    "order_for_live_execution",
    "tau_qualification_batch",
]
