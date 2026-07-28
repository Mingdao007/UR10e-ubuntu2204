#!/usr/bin/env python3
"""Pure Step5d parameter-search domain with no runtime or I/O dependencies."""

from __future__ import annotations

import math
from typing import Any

from step5d_autotune_contract import ForceCandidate, SearchTier


MIN_SEARCH_FORCE_DAMPING = 5.0


def search_candidate_allowed(candidate: Any) -> bool:
    """Return whether a candidate may be proposed or dispatched in a new search."""

    return (
        isinstance(candidate, ForceCandidate)
        and math.isfinite(candidate.force_damping)
        and candidate.force_damping >= MIN_SEARCH_FORCE_DAMPING
    )


def require_search_candidate(candidate: Any, *, role: str) -> ForceCandidate:
    if not search_candidate_allowed(candidate):
        observed = getattr(candidate, "force_damping", None)
        raise ValueError(
            f"{role} force_damping {observed!r} is below the "
            f"{MIN_SEARCH_FORCE_DAMPING:g} search floor"
        )
    return candidate


def production_candidate_catalog() -> tuple[ForceCandidate, ...]:
    """Return the finite T2 lattice inside the constrained BO feasible domain."""

    candidates: set[ForceCandidate] = set()
    quarters = range(-6, 7)
    for p_quarter in quarters:
        for damping_quarter in quarters:
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
                if candidate.within_tier(SearchTier.T2)
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
    "production_candidate_catalog",
    "require_search_candidate",
    "search_candidate_allowed",
]
