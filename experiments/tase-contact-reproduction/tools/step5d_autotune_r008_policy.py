"""Frozen five-row rolling release policy for Step5d V3 r008."""

from __future__ import annotations

import math
from statistics import median
from typing import Any, Mapping, Sequence

from step5d_autotune_contract import ForceCandidate
from step5d_autotune_optimizer import cuda_botorch_joint_candidates
from step5d_autotune_v3.control_policy import (
    BASELINE,
    BATCH_SIZE,
    PROTOCOL,
    PlannedOccurrence,
    batch as _batch,
    bo_gate,
    initialization_batch,
    recovery_batch,
)
from step5d_autotune_v3.optimizer_types import Observation
from ur10e_experiment_runtime.candidate_identity import (
    ControlCandidateUid,
    OccurrenceUid,
    TransportCandidateUid,
)


SUCCESS_MAE_N = 0.10


def _sha256(value: Any, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def supercycle_batches(
    observations: Sequence[Observation],
    catalog: Sequence[ForceCandidate],
    *,
    first_sequence: int,
    seed: int = 8008,
) -> tuple[tuple[PlannedOccurrence, ...], tuple[PlannedOccurrence, ...], dict[str, Any]]:
    """Historical r008 adapter; active rolling-v1 must use the two-phase API."""
    if not bo_gate(observations):
        raise ValueError("r008 BO gate is not satisfied")
    batch_a, evidence_a = cuda_botorch_joint_candidates(
        observations, catalog, q=4, seed=seed, anchor=BASELINE
    )
    remaining = tuple(
        item for item in catalog if item.candidate_uid not in {row.candidate_uid for row in batch_a}
    )
    batch_b, evidence_b = cuda_botorch_joint_candidates(
        observations, remaining, q=5, seed=seed + 1, anchor=BASELINE
    )
    a = _batch(
        first_sequence,
        first_sequence,
        (BASELINE, *batch_a),
        ("supercycle_anchor", "qlognei_a", "qlognei_a", "qlognei_a", "qlognei_a"),
    )
    b = _batch(
        first_sequence + 1,
        first_sequence + 1,
        batch_b,
        ("qlognei_b",) * BATCH_SIZE,
    )
    return a, b, {"batch_a": evidence_a, "batch_b": evidence_b}


def supercycle_batch_a(
    observations: Sequence[Observation],
    catalog: Sequence[ForceCandidate],
    *,
    sequence: int,
    seed: int = 9009,
) -> tuple[tuple[PlannedOccurrence, ...], dict[str, Any]]:
    if not bo_gate(observations):
        raise ValueError("rolling-v1 BO gate is not satisfied")
    selected, evidence = cuda_botorch_joint_candidates(
        observations,
        catalog,
        q=4,
        seed=seed,
        anchor=BASELINE,
    )
    rows = _batch(
        sequence,
        sequence,
        (BASELINE, *selected),
        ("supercycle_anchor", "qlognei_a", "qlognei_a", "qlognei_a", "qlognei_a"),
    )
    return rows, {"phase": "batch_a_proposed", "optimizer": evidence, "seed": seed}


def supercycle_batch_b_after_gp_update(
    observations: Sequence[Observation],
    catalog: Sequence[ForceCandidate],
    *,
    sequence: int,
    batch_a_closure: Mapping[str, Any],
    seed: int = 9010,
) -> tuple[tuple[PlannedOccurrence, ...], dict[str, Any]]:
    required = {
        "batch_result_sha256",
        "sealed_bundle_set_sha256",
        "cold_read_verified",
        "gp_update_sha256",
    }
    if not isinstance(batch_a_closure, Mapping) or set(batch_a_closure) != required:
        raise ValueError("Batch B requires exact Batch A closure and GP-update evidence")
    for name in required - {"cold_read_verified"}:
        _sha256(batch_a_closure[name], name=name)
    if batch_a_closure["cold_read_verified"] is not True:
        raise ValueError("Batch B requires cold-read verification of sealed Batch A")
    if not bo_gate(observations):
        raise ValueError("updated rolling-v1 BO gate is not satisfied")
    selected, evidence = cuda_botorch_joint_candidates(
        observations,
        catalog,
        q=5,
        seed=seed,
        anchor=BASELINE,
    )
    rows = _batch(sequence, sequence, selected, ("qlognei_b",) * BATCH_SIZE)
    return rows, {
        "phase": "batch_b_post_gp_update",
        "batch_a_closure": dict(batch_a_closure),
        "optimizer": evidence,
        "seed": seed,
    }


def confirmation_batch(
    incumbent: ForceCandidate,
    challengers: Sequence[ForceCandidate],
    *,
    sequence: int,
) -> tuple[PlannedOccurrence, ...]:
    distinct = tuple(item for item in challengers if item != incumbent)
    if len(distinct) < 2 or distinct[0] == distinct[1]:
        raise ValueError("confirmation requires two distinct challengers")
    return _batch(
        sequence,
        sequence,
        (incumbent, incumbent, incumbent, distinct[0], distinct[1]),
        ("incumbent_confirmation",) * 3 + ("confirmation_challenger",) * 2,
    )


def confirmation_success(objectives_n: Sequence[float]) -> bool:
    if len(objectives_n) != 3 or any(not math.isfinite(value) for value in objectives_n):
        raise ValueError("confirmation requires three finite incumbent objectives")
    return sum(value < SUCCESS_MAE_N for value in objectives_n) >= 2


def anchor_drifted(anchor_objectives_n: Sequence[float], initial_baseline_n: Sequence[float]) -> bool:
    if not anchor_objectives_n or not initial_baseline_n:
        raise ValueError("drift gate requires anchor and initialization observations")
    initial = median(initial_baseline_n)
    return initial <= 0.0 or abs(median(anchor_objectives_n) - initial) / initial > 0.20


def plateau_reached(
    incumbent_improvements_n: Sequence[float],
    predicted_remaining_improvements_n: Sequence[float],
) -> bool:
    return (
        len(incumbent_improvements_n) >= 2
        and len(predicted_remaining_improvements_n) >= 2
        and all(value < 0.02 for value in incumbent_improvements_n[-2:])
        and all(value < 0.01 for value in predicted_remaining_improvements_n[-2:])
    )
