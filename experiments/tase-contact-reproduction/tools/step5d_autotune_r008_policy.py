"""Frozen five-row rolling release policy for Step5d V3 r008."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from statistics import median
from typing import Any, Mapping, Sequence

from ur10e_experiment_runtime.candidate_identity import (
    ControlCandidateUid,
    OccurrenceUid,
    ParameterUid,
    TransportCandidateUid,
)

from step5d_autotune_contract import ForceCandidate
from step5d_autotune_optimizer import Observation, cuda_botorch_joint_candidates


PROTOCOL = "v3_full_home_rolling_arm_v1"
BATCH_SIZE = 5
SUCCESS_MAE_N = 0.10
BASELINE = ForceCandidate(
    force_p_gain=0.001,
    force_i_gain=1e-5,
    force_damping=7.0,
)


def _legacy_uid(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _sha256(value: Any, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True)
class PlannedOccurrence:
    logical_batch_sequence: int
    row_index: int
    candidate: ForceCandidate
    plan_revision: int
    selection_role: str
    replicate_ordinal: int
    bound_control_candidate_uid: ControlCandidateUid | None = None

    def __post_init__(self) -> None:
        for name in ("logical_batch_sequence", "row_index", "plan_revision", "replicate_ordinal"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.row_index > BATCH_SIZE:
            raise ValueError("r008 row_index must be in [1,5]")
        if not isinstance(self.selection_role, str) or not self.selection_role:
            raise ValueError("selection_role must be non-empty")
        if (
            self.bound_control_candidate_uid is not None
            and (
                type(self.bound_control_candidate_uid) is not ControlCandidateUid
                or self.bound_control_candidate_uid.is_legacy
            )
        ):
            raise TypeError("bound_control_candidate_uid uses the wrong UID namespace")

    @property
    def occurrence_uid(self) -> OccurrenceUid:
        if self.bound_control_candidate_uid is not None:
            return OccurrenceUid.from_control(
                self.bound_control_candidate_uid,
                protocol=PROTOCOL,
                logical_batch_sequence=self.logical_batch_sequence,
                row_index=self.row_index,
                plan_revision=self.plan_revision,
                selection_role=self.selection_role,
                replicate_ordinal=self.replicate_ordinal,
            )
        return OccurrenceUid.from_legacy(
            _legacy_uid(
                {
                    "schema": "step5d.r008/occurrence-v1",
                    "protocol": PROTOCOL,
                    "logical_batch_sequence": self.logical_batch_sequence,
                    "row_index": self.row_index,
                    "control_candidate_uid": self.candidate.candidate_uid,
                    "plan_revision": self.plan_revision,
                    "selection_role": self.selection_role,
                    "replicate_ordinal": self.replicate_ordinal,
                }
            )
        )

    @property
    def transport_candidate_uid(self) -> TransportCandidateUid:
        if self.bound_control_candidate_uid is not None:
            return TransportCandidateUid.from_occurrence(
                self.occurrence_uid,
                parameter_uid=ParameterUid.from_candidate_digest(
                    self.candidate.candidate_uid
                ),
                protocol=PROTOCOL,
            )
        return TransportCandidateUid.from_legacy(
            _legacy_uid(
                {
                    "schema": "step5d.r008/transport-candidate-v1",
                    "occurrence_uid": self.occurrence_uid,
                    "candidate": self.candidate.payload(),
                }
            )
        )

    @property
    def control_candidate_uid(self) -> ControlCandidateUid:
        return (
            ControlCandidateUid.from_legacy(self.candidate.candidate_uid)
            if self.bound_control_candidate_uid is None
            else self.bound_control_candidate_uid
        )

    def bind_control_candidate_uid(self, value: str) -> "PlannedOccurrence":
        return PlannedOccurrence(
            logical_batch_sequence=self.logical_batch_sequence,
            row_index=self.row_index,
            candidate=self.candidate,
            plan_revision=self.plan_revision,
            selection_role=self.selection_role,
            replicate_ordinal=self.replicate_ordinal,
            bound_control_candidate_uid=ControlCandidateUid.parse(value),
        )


def _batch(
    sequence: int,
    revision: int,
    candidates: Sequence[ForceCandidate],
    roles: Sequence[str],
) -> tuple[PlannedOccurrence, ...]:
    if len(candidates) != BATCH_SIZE or len(roles) != BATCH_SIZE:
        raise ValueError("r008 logical batches contain exactly five occurrences")
    ordinal: dict[str, int] = {}
    rows = []
    for index, (candidate, role) in enumerate(zip(candidates, roles), start=1):
        ordinal[candidate.candidate_uid] = ordinal.get(candidate.candidate_uid, 0) + 1
        rows.append(
            PlannedOccurrence(
                sequence,
                index,
                candidate,
                revision,
                role,
                ordinal[candidate.candidate_uid],
            )
        )
    if len({row.occurrence_uid for row in rows}) != BATCH_SIZE:
        raise RuntimeError("r008 occurrence identity collision")
    return tuple(rows)


def initialization_batch(sequence: int) -> tuple[PlannedOccurrence, ...]:
    if sequence == 1:
        candidates = (
            BASELINE,
            BASELINE,
            BASELINE,
            ForceCandidate.from_log2(p=-0.25, damping=0.0, i=0.0),
            ForceCandidate.from_log2(p=0.25, damping=0.0, i=0.0),
        )
        roles = ("baseline", "baseline", "baseline", "p_minus_quarter", "p_plus_quarter")
    elif sequence == 2:
        candidates = (
            BASELINE,
            BASELINE,
            BASELINE,
            ForceCandidate.from_log2(p=0.0, damping=-0.25, i=0.0),
            ForceCandidate.from_log2(p=0.0, damping=0.25, i=0.0),
        )
        roles = (
            "baseline",
            "baseline",
            "baseline",
            "damping_minus_quarter",
            "damping_plus_quarter",
        )
    else:
        raise ValueError("initialization batches are sequences 1 and 2")
    return _batch(sequence, sequence, candidates, roles)


def recovery_batch(sequence: int) -> tuple[PlannedOccurrence, ...]:
    candidates = (
        BASELINE,
        ForceCandidate.from_log2(p=-0.25, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=0.25, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=0.0, damping=-0.25, i=0.0),
        ForceCandidate.from_log2(p=0.0, damping=0.25, i=0.0),
    )
    return _batch(
        sequence,
        sequence,
        candidates,
        ("baseline_anchor", "p_minus_quarter", "p_plus_quarter", "damping_minus_quarter", "damping_plus_quarter"),
    )


def bo_gate(observations: Sequence[Observation]) -> bool:
    eligible = [item for item in observations if item.eligible]
    return len(eligible) >= 6 and sum(item.candidate == BASELINE for item in eligible) >= 3


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
