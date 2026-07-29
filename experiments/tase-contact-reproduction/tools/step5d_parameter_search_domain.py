#!/usr/bin/env python3
"""Pure Step5d parameter-search domain with no runtime or I/O dependencies."""

from __future__ import annotations

import math
from typing import Any

from step5d_autotune_contract import (
    LOG2_LATTICE_OCTAVE,
    MAX_PRODUCTION_ORIENTATION_KO,
    MIN_PRODUCTION_FORCE_DAMPING,
    MIN_PRODUCTION_ORIENTATION_KO,
    SEED_ORIENTATION_KO,
    ForceCandidate,
    SearchTier,
)


MIN_SEARCH_FORCE_DAMPING = MIN_PRODUCTION_FORCE_DAMPING
MIN_DAMPING_QUARTER = math.ceil(
    math.log2(MIN_SEARCH_FORCE_DAMPING / ForceCandidate().force_damping)
    / LOG2_LATTICE_OCTAVE
)
MAX_DAMPING_QUARTER = int(
    SearchTier.T2.p_d_radius_octaves / LOG2_LATTICE_OCTAVE
)
MIN_ORIENTATION_QUARTER = math.ceil(
    math.log2(MIN_PRODUCTION_ORIENTATION_KO / SEED_ORIENTATION_KO)
    / LOG2_LATTICE_OCTAVE
)
MAX_ORIENTATION_QUARTER = math.floor(
    math.log2(MAX_PRODUCTION_ORIENTATION_KO / SEED_ORIENTATION_KO)
    / LOG2_LATTICE_OCTAVE
)
ORIENTATION_KO_LATTICE = tuple(
    SEED_ORIENTATION_KO * (2.0 ** (quarter * LOG2_LATTICE_OCTAVE))
    for quarter in range(MIN_ORIENTATION_QUARTER, MAX_ORIENTATION_QUARTER + 1)
)


def search_candidate_allowed(candidate: Any) -> bool:
    """Return whether a candidate may be proposed or dispatched in a new search."""

    return (
        isinstance(candidate, ForceCandidate)
        and math.isfinite(candidate.force_damping)
        and candidate.force_damping >= MIN_SEARCH_FORCE_DAMPING
        and MIN_PRODUCTION_ORIENTATION_KO
        <= candidate.orientation_ko
        <= MAX_PRODUCTION_ORIENTATION_KO
    )


def require_search_candidate(candidate: Any, *, role: str) -> ForceCandidate:
    if not search_candidate_allowed(candidate):
        damping = getattr(candidate, "force_damping", None)
        orientation = getattr(candidate, "orientation_ko", None)
        if isinstance(damping, (int, float)) and damping < MIN_SEARCH_FORCE_DAMPING:
            raise ValueError(
                f"{role} force_damping {damping!r} is below the "
                f"{MIN_SEARCH_FORCE_DAMPING:g} search floor"
            )
        raise ValueError(
            f"{role} orientation_ko {orientation!r} is outside the search "
            f"envelope "
            f"{MIN_PRODUCTION_ORIENTATION_KO:g}<=Ko<="
            f"{MAX_PRODUCTION_ORIENTATION_KO:g}"
        )
    return candidate


def orientation_variants(candidate: ForceCandidate) -> tuple[ForceCandidate, ...]:
    """Vary only orientation K at one already-grounded force-loop anchor."""

    require_search_candidate(candidate, role="orientation anchor")
    return tuple(
        ForceCandidate(
            force_p_gain=candidate.force_p_gain,
            force_i_gain=candidate.force_i_gain,
            force_damping=candidate.force_damping,
            orientation_ko=orientation_ko,
            normal_filter_tau_s=candidate.normal_filter_tau_s,
            target_force_n=candidate.target_force_n,
        )
        for orientation_ko in ORIENTATION_KO_LATTICE
    )


def augment_catalog_with_orientation_anchors(
    base_catalog: tuple[ForceCandidate, ...],
    anchors: tuple[ForceCandidate, ...],
) -> tuple[ForceCandidate, ...]:
    """Compose the force catalog with a bounded K axis around observed anchors.

    This deliberately avoids a 36k x 13 Cartesian expansion.  The producer
    gains K competence around force settings that have real observations,
    while the runtime and GP still see K as a first-class coordinate.
    """

    candidates = set(base_catalog)
    for anchor in anchors:
        candidates.update(orientation_variants(anchor))
    return tuple(sorted(candidates, key=lambda item: item.candidate_uid))


def production_candidate_catalog() -> tuple[ForceCandidate, ...]:
    """Return the finite producer lattice inside the constrained BO domain."""

    candidates: set[ForceCandidate] = set()
    quarters = range(-6, 7)
    for p_quarter in quarters:
        for damping_quarter in range(
            MIN_DAMPING_QUARTER,
            MAX_DAMPING_QUARTER + 1,
        ):
            p = p_quarter * 0.25
            damping = damping_quarter * 0.25
            for tau_quarter in range(-4, 5):
                filter_tau = tau_quarter * 0.25
                candidates.add(
                    ForceCandidate.from_log2(
                        p=p,
                        damping=damping,
                        i=0.0,
                        i_off=True,
                        filter_tau=filter_tau,
                    )
                )
                for i_quarter in range(-4, 5):
                    candidates.add(
                        ForceCandidate.from_log2(
                            p=p,
                            damping=damping,
                            i=i_quarter * 0.25,
                            filter_tau=filter_tau,
                        )
                    )
    bounded = tuple(
        sorted(
            (
                candidate
                for candidate in candidates
                if candidate.within_production_search_envelope()
                and search_candidate_allowed(candidate)
            ),
            key=lambda candidate: candidate.candidate_uid,
        )
    )
    if len(bounded) < 10 or ForceCandidate() not in bounded:
        raise RuntimeError("production candidate catalog is incomplete")
    return bounded


__all__ = [
    "MIN_SEARCH_FORCE_DAMPING",
    "ORIENTATION_KO_LATTICE",
    "augment_catalog_with_orientation_anchors",
    "orientation_variants",
    "production_candidate_catalog",
    "require_search_candidate",
    "search_candidate_allowed",
]
