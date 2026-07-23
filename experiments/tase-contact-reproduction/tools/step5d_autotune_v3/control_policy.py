"""Pure rolling control policy; optimizer execution belongs to another process."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from step5d_autotune_contract import ForceCandidate
from ur10e_experiment_runtime.candidate_identity import (
    ControlCandidateUid,
    OccurrenceUid,
    ParameterUid,
    TransportCandidateUid,
)

from .optimizer_types import Observation


PROTOCOL = "v3_full_home_rolling_arm_v1"
BATCH_SIZE = 5
BASELINE = ForceCandidate(
    force_p_gain=0.001,
    force_i_gain=1e-5,
    force_damping=7.0,
)


def _legacy_uid(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


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
        for name in (
            "logical_batch_sequence",
            "row_index",
            "plan_revision",
            "replicate_ordinal",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.row_index > BATCH_SIZE:
            raise ValueError("rolling row_index must be in [1,5]")
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


def batch(
    sequence: int,
    revision: int,
    candidates: Sequence[ForceCandidate],
    roles: Sequence[str],
) -> tuple[PlannedOccurrence, ...]:
    if len(candidates) != BATCH_SIZE or len(roles) != BATCH_SIZE:
        raise ValueError("rolling logical batches contain exactly five occurrences")
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
        raise RuntimeError("rolling occurrence identity collision")
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
        roles = (
            "baseline",
            "baseline",
            "baseline",
            "p_minus_quarter",
            "p_plus_quarter",
        )
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
    return batch(sequence, sequence, candidates, roles)


def recovery_batch(sequence: int) -> tuple[PlannedOccurrence, ...]:
    candidates = (
        BASELINE,
        ForceCandidate.from_log2(p=-0.25, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=0.25, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=0.0, damping=-0.25, i=0.0),
        ForceCandidate.from_log2(p=0.0, damping=0.25, i=0.0),
    )
    return batch(
        sequence,
        sequence,
        candidates,
        (
            "baseline_anchor",
            "p_minus_quarter",
            "p_plus_quarter",
            "damping_minus_quarter",
            "damping_plus_quarter",
        ),
    )


def bo_gate(observations: Sequence[Observation]) -> bool:
    eligible = [item for item in observations if item.eligible]
    return (
        len(eligible) >= 6
        and sum(item.candidate == BASELINE for item in eligible) >= 3
    )


__all__ = [
    "BASELINE",
    "BATCH_SIZE",
    "PROTOCOL",
    "PlannedOccurrence",
    "batch",
    "bo_gate",
    "initialization_batch",
    "recovery_batch",
]
