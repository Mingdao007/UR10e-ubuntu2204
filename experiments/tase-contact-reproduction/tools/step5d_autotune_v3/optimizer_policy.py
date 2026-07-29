"""Pure candidate-transition and tier policy shared by online and optimizer code.

This module deliberately contains no NumPy, Torch, BoTorch, GPyTorch, GPU,
filesystem, subprocess, controller, ROS, or RTDE dependency.  Online execution
may validate a proposed transition with these functions without importing the
optimizer implementation.
"""

from __future__ import annotations

import math
from typing import Iterable

from step5d_autotune_contract import (
    LOG2_LATTICE_OCTAVE,
    ForceCandidate,
    SearchAttestation,
    SearchTier,
)

from .optimizer_types import Observation


def candidate_vector(
    candidate: ForceCandidate,
) -> tuple[float, float, float, float, float, float, float]:
    return (
        candidate.log2_p,
        candidate.log2_damping,
        candidate.log2_filter_tau,
        0.0 if candidate.force_i_gain == 0.0 else candidate.log2_i,
        1.0 if candidate.force_i_gain == 0.0 else 0.0,
        candidate.log2_orientation_ko,
        candidate.log2_motion_kp,
    )


def _changed_coordinates(
    a: ForceCandidate,
    b: ForceCandidate,
) -> tuple[str, ...]:
    changed: list[str] = []
    if not math.isclose(a.log2_p, b.log2_p, abs_tol=1e-9):
        changed.append("p")
    if not math.isclose(a.log2_damping, b.log2_damping, abs_tol=1e-9):
        changed.append("damping")
    if not math.isclose(a.log2_filter_tau, b.log2_filter_tau, abs_tol=1e-9):
        changed.append("filter_tau")
    if not math.isclose(
        a.log2_orientation_ko, b.log2_orientation_ko, abs_tol=1e-9
    ):
        changed.append("orientation_ko")
    if not math.isclose(a.log2_motion_kp, b.log2_motion_kp, abs_tol=1e-9):
        changed.append("motion_kp")
    if a.i_mode != b.i_mode:
        changed.append("i_mode")
    elif a.i_mode == "positive" and not math.isclose(
        a.log2_i, b.log2_i, abs_tol=1e-9
    ):
        changed.append("i")
    return tuple(changed)


def _coordinate(candidate: ForceCandidate, axis: str) -> float:
    if axis == "p":
        return candidate.log2_p
    if axis == "damping":
        return candidate.log2_damping
    if axis == "filter_tau":
        return candidate.log2_filter_tau
    if axis == "orientation_ko":
        return candidate.log2_orientation_ko
    if axis == "motion_kp":
        return candidate.log2_motion_kp
    if axis == "i" and candidate.i_mode == "positive":
        return candidate.log2_i
    raise ValueError(f"candidate has no continuous {axis} coordinate")


def live_trust_region_step(
    incumbent: ForceCandidate,
    candidate: ForceCandidate,
) -> bool:
    changed = _changed_coordinates(incumbent, candidate)
    if len(changed) != 1:
        return False
    field = changed[0]
    if field == "i_mode":
        return (
            math.isclose(incumbent.log2_p, candidate.log2_p, abs_tol=1e-9)
            and math.isclose(
                incumbent.log2_damping,
                candidate.log2_damping,
                abs_tol=1e-9,
            )
        )
    before = _coordinate(incumbent, field)
    after = _coordinate(candidate, field)
    return math.isclose(
        abs(after - before),
        LOG2_LATTICE_OCTAVE,
        abs_tol=1e-9,
    )


def one_step_neighbors(
    incumbent: ForceCandidate,
    tier: SearchTier,
) -> tuple[ForceCandidate, ...]:
    candidates: set[ForceCandidate] = set()
    p, damping = incumbent.log2_p, incumbent.log2_damping
    filter_tau = incumbent.log2_filter_tau
    orientation = incumbent.log2_orientation_ko
    motion = incumbent.log2_motion_kp
    i = 0.0 if incumbent.force_i_gain == 0.0 else incumbent.log2_i
    for delta in (-LOG2_LATTICE_OCTAVE, LOG2_LATTICE_OCTAVE):
        candidates.add(
            ForceCandidate.from_log2(
                p=p + delta,
                damping=damping,
                orientation=orientation,
                motion=motion,
                i=i,
                i_off=incumbent.i_mode == "off",
                filter_tau=filter_tau,
            )
        )
        candidates.add(
            ForceCandidate.from_log2(
                p=p,
                damping=damping + delta,
                orientation=orientation,
                motion=motion,
                i=i,
                i_off=incumbent.i_mode == "off",
                filter_tau=filter_tau,
            )
        )
        if tier is not SearchTier.T1 and incumbent.i_mode == "positive":
            candidates.add(
                ForceCandidate.from_log2(
                    p=p,
                    damping=damping,
                    orientation=orientation,
                    motion=motion,
                    i=i + delta,
                    filter_tau=filter_tau,
                )
            )
        candidates.add(
            ForceCandidate.from_log2(
                p=p,
                damping=damping,
                orientation=orientation,
                motion=motion,
                i=i,
                i_off=incumbent.i_mode == "off",
                filter_tau=filter_tau + delta,
            )
        )
        if tier is not SearchTier.T1:
            candidates.add(
                ForceCandidate.from_log2(
                    p=p,
                    damping=damping,
                    orientation=orientation + delta,
                    motion=motion,
                    i=i,
                    i_off=incumbent.i_mode == "off",
                    filter_tau=filter_tau,
                )
            )
            candidates.add(
                ForceCandidate.from_log2(
                    p=p,
                    damping=damping,
                    orientation=orientation,
                    motion=motion + delta,
                    i=i,
                    i_off=incumbent.i_mode == "off",
                    filter_tau=filter_tau,
                )
            )
    if tier is not SearchTier.T1:
        candidates.add(
            ForceCandidate.from_log2(
                p=p,
                damping=damping,
                orientation=orientation,
                motion=motion,
                i=0.0 if incumbent.i_mode == "off" else incumbent.log2_i,
                i_off=incumbent.i_mode != "off",
                filter_tau=filter_tau,
            )
        )
    return tuple(
        sorted(
            (
                candidate
                for candidate in candidates
                if candidate.within_tier(tier)
                and live_trust_region_step(incumbent, candidate)
            ),
            key=candidate_vector,
        )
    )


def one_step_toward(
    actual: ForceCandidate,
    target: ForceCandidate,
) -> ForceCandidate:
    """Return one executable lattice transition from ``actual`` toward ``target``."""

    if actual == target:
        return actual
    for axis in ("p", "damping", "i", "filter_tau", "orientation_ko", "motion_kp"):
        if axis == "i" and (
            actual.i_mode != "positive" or target.i_mode != "positive"
        ):
            continue
        before = _coordinate(actual, axis)
        after = _coordinate(target, axis)
        if math.isclose(before, after, abs_tol=1e-9):
            continue
        coordinate = before + math.copysign(
            min(LOG2_LATTICE_OCTAVE, abs(after - before)),
            after - before,
        )
        kwargs = {
            "p": actual.log2_p,
            "damping": actual.log2_damping,
            "orientation": actual.log2_orientation_ko,
            "motion": actual.log2_motion_kp,
            "i": 0.0 if actual.i_mode == "off" else actual.log2_i,
            "i_off": actual.i_mode == "off",
            "filter_tau": actual.log2_filter_tau,
        }
        kwargs[
            {"orientation_ko": "orientation", "motion_kp": "motion"}.get(axis, axis)
        ] = coordinate
        candidate = ForceCandidate.from_log2(**kwargs)
        if not live_trust_region_step(actual, candidate):
            raise RuntimeError(
                "one-step transition construction violated live trust region"
            )
        return candidate
    if actual.i_mode != target.i_mode:
        candidate = ForceCandidate.from_log2(
            p=actual.log2_p,
            damping=actual.log2_damping,
            orientation=actual.log2_orientation_ko,
            motion=actual.log2_motion_kp,
            i=0.0 if actual.i_mode == "off" else actual.log2_i,
            i_off=actual.i_mode != "off",
            filter_tau=actual.log2_filter_tau,
        )
        if not live_trust_region_step(actual, candidate):
            raise RuntimeError(
                "I-mode transition construction violated live trust region"
            )
        return candidate
    raise RuntimeError("distinct candidates have no executable transition")


def eligible_for_context(
    observations: Iterable[Observation],
    *,
    profile_id: str,
    plant_epoch: int,
) -> list[Observation]:
    return [
        observation
        for observation in observations
        if observation.profile_id == profile_id
        and observation.plant_epoch == plant_epoch
        and observation.eligible
    ]


def observations_for_context(
    observations: Iterable[Observation],
    *,
    profile_id: str,
    plant_epoch: int,
) -> list[Observation]:
    return [
        observation
        for observation in observations
        if observation.profile_id == profile_id
        and observation.plant_epoch == plant_epoch
    ]


def incumbent(
    observations: Iterable[Observation],
    *,
    fallback: ForceCandidate | None = None,
) -> ForceCandidate:
    eligible = [observation for observation in observations if observation.eligible]
    if not eligible:
        return fallback or ForceCandidate()
    return min(eligible, key=lambda observation: observation.objective).candidate


def _recent_structural_failure(outcomes: list[Observation]) -> bool:
    return any(outcome.evaluation.structural_failures for outcome in outcomes[-6:])


def transition_axis_direction(
    before: ForceCandidate,
    after: ForceCandidate,
) -> tuple[str, int] | None:
    changed = _changed_coordinates(before, after)
    if len(changed) != 1 or changed[0] not in {
        "p",
        "damping",
        "i",
        "filter_tau",
    }:
        return None
    if not live_trust_region_step(before, after):
        return None
    axis = changed[0]
    delta = _coordinate(after, axis) - _coordinate(before, axis)
    return axis, int(math.copysign(1, delta))


def _is_t2_boundary(candidate: ForceCandidate, axis: str, direction: int) -> bool:
    if axis in {"p", "damping"}:
        radius = SearchTier.T2.p_d_radius_octaves
    elif axis == "i" and candidate.i_mode == "positive":
        radius = SearchTier.T2.positive_i_radius_octaves
    elif axis == "filter_tau":
        radius = 1.0
    else:
        return False
    return math.isclose(
        _coordinate(candidate, axis),
        direction * radius,
        abs_tol=1e-9,
    )


def _outward_improvement_chains(
    outcomes: list[Observation],
    boundary_incumbent: ForceCandidate,
) -> tuple[tuple[str, int], ...]:
    eligible = [outcome for outcome in outcomes if outcome.eligible]
    chains: set[tuple[str, int]] = set()
    for first_index, first in enumerate(eligible):
        for second_index in range(first_index + 1, len(eligible)):
            second = eligible[second_index]
            first_step = transition_axis_direction(first.candidate, second.candidate)
            if first_step is None or second.objective >= first.objective:
                continue
            for third in eligible[second_index + 1 :]:
                second_step = transition_axis_direction(
                    second.candidate,
                    third.candidate,
                )
                if (
                    second_step == first_step
                    and third.objective < second.objective
                    and third.candidate == boundary_incumbent
                    and _is_t2_boundary(
                        boundary_incumbent,
                        first_step[0],
                        first_step[1],
                    )
                ):
                    chains.add(first_step)
    return tuple(sorted(chains))


def _t3_unlocked_frontiers(
    outcomes: list[Observation],
) -> tuple[tuple[str, int, ForceCandidate], ...]:
    eligible = [outcome for outcome in outcomes if outcome.eligible]
    frontiers: set[tuple[str, int, ForceCandidate]] = set()
    for boundary in {outcome.candidate for outcome in eligible}:
        if not boundary.within_tier(SearchTier.T2):
            continue
        for axis, direction in _outward_improvement_chains(outcomes, boundary):
            frontiers.add((axis, direction, boundary))
    return tuple(
        sorted(
            frontiers,
            key=lambda row: (row[0], row[1], candidate_vector(row[2])),
        )
    )


def _same_ray(
    candidate: ForceCandidate,
    *,
    boundary: ForceCandidate,
    axis: str,
    direction: int,
) -> bool:
    if candidate.i_mode != boundary.i_mode:
        return False
    for other_axis in ("p", "damping", "i", "filter_tau"):
        if other_axis == axis:
            continue
        if other_axis == "i" and candidate.i_mode != "positive":
            continue
        if not math.isclose(
            _coordinate(candidate, other_axis),
            _coordinate(boundary, other_axis),
            abs_tol=1e-9,
        ):
            return False
    distance = direction * (
        _coordinate(candidate, axis) - _coordinate(boundary, axis)
    )
    return distance >= -1e-9


def search_attestation_matches(
    outcomes: list[Observation],
    *,
    pending_candidate: ForceCandidate,
    attestation: SearchAttestation,
) -> bool:
    if not outcomes or not isinstance(attestation, SearchAttestation):
        return False
    contexts = {(outcome.profile_id, outcome.plant_epoch) for outcome in outcomes}
    if len(contexts) != 1:
        return False
    profile_id, plant_epoch = next(iter(contexts))
    eligible = [outcome for outcome in outcomes if outcome.eligible]
    if not eligible:
        return False
    best = incumbent(eligible)
    latest = outcomes[-1]
    if (
        not latest.eligible
        or latest.candidate != best
        or latest.latest_trace_sha256 is None
        or attestation.profile_id != profile_id
        or attestation.plant_epoch != plant_epoch
        or attestation.source_trial_uid != latest.evaluation.trial_uid
        or attestation.latest_trace_sha256 != latest.latest_trace_sha256
        or attestation.from_candidate != best
        or attestation.to_candidate != pending_candidate
        or attestation.next_candidate_uid != pending_candidate.candidate_uid
        or not attestation.exact_replay_passed
        or pending_candidate.within_tier(SearchTier.T2)
        or not pending_candidate.within_tier(SearchTier.T3)
    ):
        return False
    step = transition_axis_direction(best, pending_candidate)
    if step != (attestation.outward_axis, attestation.outward_direction):
        return False
    for axis, direction, boundary in _t3_unlocked_frontiers(outcomes):
        if step != (axis, direction):
            continue
        if not _same_ray(best, boundary=boundary, axis=axis, direction=direction):
            continue
        if not _same_ray(
            pending_candidate,
            boundary=boundary,
            axis=axis,
            direction=direction,
        ):
            continue
        if direction * (
            _coordinate(pending_candidate, axis) - _coordinate(best, axis)
        ) > 0.0:
            return True
    return False


def unlocked_tier(
    observations: list[Observation],
    *,
    pending_candidate: ForceCandidate | None = None,
    search_attestation: SearchAttestation | None = None,
) -> SearchTier:
    outcomes = list(observations)
    if not outcomes:
        return SearchTier.T1
    if len({(item.profile_id, item.plant_epoch) for item in outcomes}) != 1:
        return SearchTier.T1
    eligible = [observation for observation in outcomes if observation.eligible]
    if len(eligible) < 6 or _recent_structural_failure(outcomes):
        return SearchTier.T1
    if pending_candidate is None or search_attestation is None:
        return SearchTier.T2
    if any(not outcome.eligible for outcome in outcomes[-6:]):
        return SearchTier.T2
    if search_attestation_matches(
        outcomes,
        pending_candidate=pending_candidate,
        attestation=search_attestation,
    ):
        return SearchTier.T3
    return SearchTier.T2


def success_confirmed(
    observations: Iterable[Observation],
    *,
    profile_id: str,
    plant_epoch: int,
    threshold_n: float = 0.30,
) -> bool:
    return any(
        observation.eligible
        and observation.profile_id == profile_id
        and observation.plant_epoch == plant_epoch
        and observation.objective <= threshold_n
        for observation in observations
    )


__all__ = [
    "candidate_vector",
    "eligible_for_context",
    "incumbent",
    "live_trust_region_step",
    "observations_for_context",
    "one_step_neighbors",
    "one_step_toward",
    "search_attestation_matches",
    "success_confirmed",
    "transition_axis_direction",
    "unlocked_tier",
]
