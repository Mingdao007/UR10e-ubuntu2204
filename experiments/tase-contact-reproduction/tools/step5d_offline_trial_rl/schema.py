from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator


ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["force_p_gain", "force_i_gain", "force_damping", "orientation_ko"],
    "properties": {
        "force_p_gain": {"type": "number", "exclusiveMinimum": 0},
        "force_i_gain": {"type": "number", "exclusiveMinimum": 0},
        "force_damping": {"type": "number", "exclusiveMinimum": 0},
        "orientation_ko": {"type": ["number", "null"], "exclusiveMinimum": 0},
    },
}

DATASET_RECORD_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "step5d.offline-trial-rl/dataset-record-v1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema", "record_uid", "source_kind", "source_artifact_sha256",
        "campaign_uid", "trial_uid", "legacy_trial_number", "source_git_sha",
        "plant_epoch", "controller_identity", "tp_identity", "trajectory_identity",
        "objective_identity", "parameter_semantics_fingerprint", "action",
        "action_complete", "sequence_artifact", "outcome_class", "metric_role",
        "objective", "reward", "reward_eligible", "constraint_eligible",
        "constraint_unsafe", "structural_failures", "features", "missing_reasons",
        "split_group",
    ],
    "properties": {
        "schema": {"const": "step5d.offline-trial-rl/dataset-record-v1"},
        "record_uid": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "source_kind": {"enum": ["v2_control_plane", "v3_diagnostic_bundle"]},
        "source_artifact_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "campaign_uid": {"type": "string", "minLength": 1},
        "trial_uid": {"type": "string", "minLength": 1},
        "legacy_trial_number": {"type": ["integer", "null"], "minimum": 0},
        "source_git_sha": {"type": ["string", "null"]},
        "plant_epoch": {"type": "string", "minLength": 1},
        "controller_identity": {"type": "object"},
        "tp_identity": {"type": "object"},
        "trajectory_identity": {"type": "object"},
        "objective_identity": {"type": "object"},
        "parameter_semantics_fingerprint": {"type": "string", "minLength": 1},
        "action": ACTION_SCHEMA,
        "action_complete": {"type": "boolean"},
        "sequence_artifact": {
            "oneOf": [
                {"type": "null"},
                {
                    "type": "object", "additionalProperties": False,
                    "required": ["path_role", "sha256", "size_bytes"],
                    "properties": {
                        "path_role": {"const": "immutable_source"},
                        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "size_bytes": {"type": "integer", "minimum": 0},
                    },
                },
            ]
        },
        "outcome_class": {"enum": [
            "valid_parameter_observation", "parameter_constraint_violation",
            "infrastructure_failure", "model_mismatch", "observer_gap",
            "operator_stop", "unknown"
        ]},
        "metric_role": {"enum": ["trainable_objective", "diagnostic_only", "unavailable"]},
        "objective": {"type": ["number", "null"]},
        "reward": {"type": ["number", "null"]},
        "reward_eligible": {"type": "boolean"},
        "constraint_eligible": {"type": "boolean"},
        "constraint_unsafe": {"type": ["boolean", "null"]},
        "structural_failures": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "features": {"type": "object", "additionalProperties": {"type": ["number", "boolean", "null"]}},
        "missing_reasons": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "split_group": {"type": "string", "minLength": 1},
    },
}

DATASET_MANIFEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "step5d.offline-trial-rl/historical-dataset-manifest-v1",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "sources", "records", "counts", "dataset_sha256"],
    "properties": {
        "schema": {"const": "step5d.offline-trial-rl/historical-dataset-manifest-v1"},
        "sources": {"type": "array", "items": {"type": "object"}},
        "records": {"type": "array", "items": DATASET_RECORD_SCHEMA},
        "counts": {"type": "object"},
        "dataset_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
}

PROPOSAL_ARTIFACT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "step5d.offline-trial-rl/offline-policy-proposal-v1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema", "result_status", "promotion_status", "usable_as",
        "current_v3_optimizer_eligible", "warm_start_eligible",
        "dataset_manifest_sha256", "dataset_manifest_file_sha256",
        "algorithm", "model_fingerprint", "config", "gates", "validation",
        "blockers", "observed_action_bounds", "candidate_ranking", "proposal",
        "fallback", "code_git_sha", "code_source_sha256", "source_digests",
        "artifact_fingerprint",
    ],
    "properties": {
        "schema": {"const": "step5d.offline-trial-rl/offline-policy-proposal-v1"},
        "result_status": {"enum": ["proposal_available", "insufficient_valid_trials"]},
        "promotion_status": {"const": "offline_only"},
        "usable_as": {"const": "next_campaign_warm_start"},
        "current_v3_optimizer_eligible": {"const": False},
        "warm_start_eligible": {"type": "boolean"},
        "dataset_manifest_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "dataset_manifest_file_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "algorithm": {"const": "deterministic_bootstrap_ridge_ensemble"},
        "model_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "config": {"type": "object"}, "gates": {"type": "object"},
        "validation": {"type": "object"},
        "blockers": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "observed_action_bounds": {"type": "object"},
        "candidate_ranking": {"type": "array", "items": {"type": "object"}},
        "proposal": {"type": ["object", "null"]},
        "fallback": {"type": "object"},
        "code_git_sha": {"type": ["string", "null"]},
        "code_source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "source_digests": {"type": "array", "items": {"type": "object"}},
        "artifact_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
}


def validate_record(record: dict[str, Any]) -> None:
    Draft202012Validator(DATASET_RECORD_SCHEMA).validate(record)


def validate_manifest(manifest: dict[str, Any]) -> None:
    Draft202012Validator(DATASET_MANIFEST_SCHEMA).validate(manifest)


def validate_proposal_artifact(artifact: dict[str, Any]) -> None:
    Draft202012Validator(PROPOSAL_ARTIFACT_SCHEMA).validate(artifact)
