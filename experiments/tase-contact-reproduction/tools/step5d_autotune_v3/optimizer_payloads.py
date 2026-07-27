"""Pure adapters between legacy optimizer values and versioned shared contracts."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from step5d_autotune_contract import Evaluation, ForceCandidate, TrialDisposition
from .control_policy import PlannedOccurrence
from .optimizer_types import Observation
from ur10e_experiment_runtime import ControlCandidateUid

from .shared_contracts import (
    CandidateSuggestion,
    SHARED_SCHEMA_DIGEST,
    UnitParameter,
    canonical_sha256,
)


PARAMETER_UNITS = {
    "target_force_n": "N",
    "force_p_gain": "m/(s*N)",
    "force_i_gain": "m/(s^2*N)",
    "force_damping": "1",
    "normal_filter_tau_s": "s",
}
OPTIMIZER_IDENTITY_FIELDS = {
    "optimizer_digest",
    "schema_digest",
    "build_digest",
}
OCCURRENCE_CONSTRAINT_FIELDS = {
    "logical_batch_sequence",
    "plan_revision",
    "replicate_ordinal",
    "row_index",
    "selection_role",
}


def candidate_payload(candidate: ForceCandidate) -> dict[str, float]:
    return {
        "target_force_n": candidate.target_force_n,
        "force_p_gain": candidate.force_p_gain,
        "force_i_gain": candidate.force_i_gain,
        "force_damping": candidate.force_damping,
        "normal_filter_tau_s": candidate.normal_filter_tau_s,
    }


def observation_payload(observation: Observation) -> dict[str, Any]:
    return {
        "candidate": candidate_payload(observation.candidate),
        "evaluation": observation.evaluation.history_payload(),
        "profile_id": observation.profile_id,
        "plant_epoch": observation.plant_epoch,
        "latest_trace_sha256": observation.latest_trace_sha256,
        "control_candidate_uid": (
            None
            if observation.control_candidate_uid is None
            else str(observation.control_candidate_uid)
        ),
    }


def decode_observation(payload: Any) -> Observation:
    required = {
        "candidate",
        "evaluation",
        "profile_id",
        "plant_epoch",
        "latest_trace_sha256",
        "control_candidate_uid",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ValueError("optimizer observation fields differ")
    evaluation = payload["evaluation"]
    evaluation_fields = {
        "schema_version",
        "trial_uid",
        "backend_id",
        "eligible",
        "disposition",
        "objective_mae_n",
        "force_bias_n",
        "force_std_n",
        "coverage_12_plus_minus_1_ratio",
        "complete_bins",
        "safe_closure",
        "structural_failures",
        "metrics",
    }
    if not isinstance(evaluation, Mapping) or set(evaluation) != evaluation_fields:
        raise ValueError("optimizer evaluation fields differ")
    evaluation_values = dict(evaluation)
    evaluation_values.pop("schema_version")
    evaluation_values["disposition"] = TrialDisposition(
        evaluation_values["disposition"]
    )
    evaluation_values["structural_failures"] = tuple(
        evaluation_values["structural_failures"]
    )
    control_uid = payload["control_candidate_uid"]
    return Observation(
        candidate=ForceCandidate.from_payload(payload["candidate"]),
        evaluation=Evaluation(**evaluation_values),
        profile_id=payload["profile_id"],
        plant_epoch=payload["plant_epoch"],
        latest_trace_sha256=payload["latest_trace_sha256"],
        control_candidate_uid=(
            None if control_uid is None else ControlCandidateUid.parse(control_uid)
        ),
    )


def accepted_history_digest(observations: Sequence[Observation]) -> str:
    return canonical_sha256(
        {"observations": [observation_payload(value) for value in observations]}
    )


def optimizer_identity(
    *,
    optimizer_digest: str,
    build_digest: str,
) -> dict[str, str]:
    return {
        "optimizer_digest": optimizer_digest,
        "schema_digest": SHARED_SCHEMA_DIGEST,
        "build_digest": build_digest,
    }


def validate_optimizer_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != OPTIMIZER_IDENTITY_FIELDS:
        raise ValueError("optimizer identity fields differ")
    result = {name: value[name] for name in sorted(OPTIMIZER_IDENTITY_FIELDS)}
    CandidateSuggestion.create(
        parameters={
            "identity_probe": UnitParameter(
                value=0.0,
                unit="1",
                lower_bound=0.0,
                upper_bound=0.0,
            )
        },
        optimizer_digest=result["optimizer_digest"],
        schema_digest=result["schema_digest"],
        build_digest=result["build_digest"],
        seed=0,
        accepted_history_digest="0" * 64,
        constraints={},
        uncertainty={},
    )
    if result["schema_digest"] != SHARED_SCHEMA_DIGEST:
        raise ValueError("optimizer shared schema digest differs")
    return result


def _candidate_parameters(
    candidate: ForceCandidate,
    catalog: Sequence[ForceCandidate],
) -> dict[str, UnitParameter]:
    rows = tuple(catalog)
    if not rows:
        raise ValueError("optimizer catalog must not be empty")
    payloads = [candidate_payload(value) for value in rows]
    values = candidate_payload(candidate)
    return {
        name: UnitParameter(
            value=value,
            unit=PARAMETER_UNITS[name],
            lower_bound=min(row[name] for row in payloads),
            upper_bound=max(row[name] for row in payloads),
        )
        for name, value in values.items()
    }


def encode_suggestions(
    occurrences: Sequence[PlannedOccurrence],
    *,
    catalog: Sequence[ForceCandidate],
    identity: Mapping[str, str],
    seed: int,
    history_digest: str,
    evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    bound_identity = validate_optimizer_identity(identity)
    uncertainty = {
        "representation": "optimizer_evidence_digest",
        "optimizer_evidence_digest": canonical_sha256(dict(evidence)),
    }
    optimizer_evidence = evidence.get("optimizer")
    if isinstance(optimizer_evidence, Mapping):
        selected = optimizer_evidence.get("selected_acquisition")
        if isinstance(selected, (int, float)) and not isinstance(selected, bool):
            uncertainty["selected_acquisition"] = float(selected)
    suggestions: list[dict[str, Any]] = []
    for occurrence in occurrences:
        candidate = CandidateSuggestion.create(
            parameters=_candidate_parameters(occurrence.candidate, catalog),
            optimizer_digest=bound_identity["optimizer_digest"],
            schema_digest=bound_identity["schema_digest"],
            build_digest=bound_identity["build_digest"],
            seed=seed,
            accepted_history_digest=history_digest,
            constraints={
                "logical_batch_sequence": occurrence.logical_batch_sequence,
                "plan_revision": occurrence.plan_revision,
                "replicate_ordinal": occurrence.replicate_ordinal,
                "row_index": occurrence.row_index,
                "selection_role": occurrence.selection_role,
            },
            uncertainty=uncertainty,
        )
        suggestions.append(candidate.to_payload())
    return suggestions


def decode_suggestion(
    value: Any,
    *,
    catalog: Sequence[ForceCandidate],
    identity: Mapping[str, str],
    seed: int,
    history_digest: str,
) -> PlannedOccurrence:
    candidate = CandidateSuggestion.from_payload(value)
    bound_identity = validate_optimizer_identity(identity)
    if (
        candidate.optimizer_digest != bound_identity["optimizer_digest"]
        or candidate.schema_digest != bound_identity["schema_digest"]
        or candidate.build_digest != bound_identity["build_digest"]
        or candidate.seed != seed
        or candidate.accepted_history_digest != history_digest
        or set(candidate.constraints) != OCCURRENCE_CONSTRAINT_FIELDS
        or set(candidate.parameters) != set(PARAMETER_UNITS)
    ):
        raise ValueError("optimizer suggestion binding differs")
    catalog_rows = tuple(catalog)
    if not catalog_rows:
        raise ValueError("optimizer catalog must not be empty")
    catalog_payloads = [candidate_payload(row) for row in catalog_rows]
    for name, parameter in candidate.parameters.items():
        lower = min(row[name] for row in catalog_payloads)
        upper = max(row[name] for row in catalog_payloads)
        if (
            parameter.unit != PARAMETER_UNITS[name]
            or parameter.lower_bound != lower
            or parameter.upper_bound != upper
            or not lower <= parameter.value <= upper
        ):
            raise ValueError("optimizer suggestion parameter catalog binding differs")
    constraints = candidate.constraints
    return PlannedOccurrence(
        logical_batch_sequence=constraints["logical_batch_sequence"],
        row_index=constraints["row_index"],
        candidate=ForceCandidate.from_payload(
            {
                name: parameter.value
                for name, parameter in candidate.parameters.items()
            }
        ),
        plan_revision=constraints["plan_revision"],
        selection_role=constraints["selection_role"],
        replicate_ordinal=constraints["replicate_ordinal"],
    )


__all__ = [
    "OCCURRENCE_CONSTRAINT_FIELDS",
    "OPTIMIZER_IDENTITY_FIELDS",
    "PARAMETER_UNITS",
    "accepted_history_digest",
    "candidate_payload",
    "decode_observation",
    "decode_suggestion",
    "encode_suggestions",
    "observation_payload",
    "optimizer_identity",
    "validate_optimizer_identity",
]
