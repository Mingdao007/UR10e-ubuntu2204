from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from step5d_autotune_v3.shared_contracts import (
    ArtifactReference,
    CandidateSuggestion,
    MetricValue,
    OptimizerDeploymentCertificate,
    SHARED_SCHEMA_DEFINITION,
    SHARED_SCHEMA_DIGEST,
    SafetyObservation,
    SharedContractError,
    TerminalDisposition,
    TrialResult,
    TrialSpec,
    UnitParameter,
    accept_terminal_result,
    canonical_sha256,
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def parameters() -> dict[str, UnitParameter]:
    return {
        "force_p_gain": UnitParameter(
            value=0.02,
            unit="m/(s*N)",
            lower_bound=0.005,
            upper_bound=0.08,
        ),
        "target_force": UnitParameter(
            value=12.0,
            unit="N",
            lower_bound=8.0,
            upper_bound=16.0,
        ),
    }


def suggestion() -> CandidateSuggestion:
    return CandidateSuggestion.create(
        parameters=parameters(),
        optimizer_digest=digest("optimizer"),
        schema_digest=digest("schema"),
        build_digest=digest("build"),
        seed=8008,
        accepted_history_digest=digest("history"),
        constraints={"safe_envelope": digest("safety")},
        uncertainty={"objective_std_n": 0.25, "unit": "N"},
    )


def trial_spec() -> TrialSpec:
    candidate = suggestion()
    return TrialSpec(
        trial_id="trial-0001",
        suggestion_id=candidate.suggestion_id,
        parameters=candidate.parameters,
        release_id=digest("release"),
        safety_id=digest("safety"),
        deadline_unix_ns=2_000_000_000,
        required_observations=(
            "bridge_heartbeat",
            "controller_state",
            "force_sensor",
        ),
    )


def trial_result(*, stop_reason: str = "completed") -> TrialResult:
    spec = trial_spec()
    return TrialResult(
        trial_id=spec.trial_id,
        disposition=TerminalDisposition.SUCCEEDED,
        metrics={"force_mae": MetricValue(value=0.5, unit="N")},
        stop_reason=stop_reason,
        started_at_unix_ns=1_000,
        completed_at_unix_ns=2_000,
        safety_observations=(
            SafetyObservation(
                name="safety_mode_normal",
                passed=True,
                observed_at_unix_ns=1_500,
                evidence_sha256=digest("safety-observation"),
            ),
        ),
        artifacts=(
            ArtifactReference(
                role="trial_bundle",
                uri="artifact://sha256/trial-bundle",
                sha256=digest("trial-bundle"),
                size_bytes=128,
            ),
        ),
        runner_version="online-runner/1",
        trial_spec_digest=spec.digest,
    )


def test_candidate_suggestion_is_deterministic_and_not_authorization() -> None:
    first = suggestion()
    second = suggestion()

    assert first == second
    assert first.suggestion_id == second.suggestion_id
    assert "authorization" not in first.to_payload()
    assert "runtime" not in first.to_payload()
    assert "gpu" not in first.to_payload()

    with pytest.raises(
        SharedContractError,
        match="cannot carry execution authorization",
    ):
        CandidateSuggestion.create(
            parameters=parameters(),
            optimizer_digest=digest("optimizer"),
            schema_digest=digest("schema"),
            build_digest=digest("build"),
            seed=8008,
            accepted_history_digest=digest("history"),
            constraints={"arm": True},
            uncertainty={},
        )


def test_trial_spec_round_trip_binds_units_bounds_and_digest() -> None:
    spec = trial_spec()
    payload = spec.to_payload()

    assert TrialSpec.from_payload(payload) == spec
    payload["parameters"]["target_force"]["bounds"]["upper"] = 11.0
    with pytest.raises(SharedContractError):
        TrialSpec.from_payload(payload)


def test_one_trial_accepts_only_one_terminal_result() -> None:
    spec = trial_spec()
    first = trial_result()
    assert TrialResult.from_payload(first.to_payload()) == first
    assert accept_terminal_result(spec, None, first) is first
    assert accept_terminal_result(spec, first, trial_result()) is first

    with pytest.raises(
        SharedContractError,
        match="different terminal result",
    ):
        accept_terminal_result(
            spec,
            first,
            trial_result(stop_reason="different"),
        )

    different_spec = TrialSpec(
        **{
            **spec.__dict__,
            "trial_id": "trial-0002",
        }
    )
    with pytest.raises(
        SharedContractError,
        match="TrialSpec binding differs",
    ):
        accept_terminal_result(different_spec, None, first)


def test_optimizer_deployment_certificate_is_separate_from_trial_wire() -> None:
    certificate = OptimizerDeploymentCertificate(
        optimizer_digest=digest("optimizer"),
        build_digest=digest("build"),
        runtime_attestation_digest=digest("runtime-attestation"),
        gpu_attestation_digest=digest("gpu-attestation"),
        module_closure_digest=digest("module-closure"),
    )

    assert (
        OptimizerDeploymentCertificate.from_payload(certificate.to_payload())
        == certificate
    )
    assert "runtime_attestation_digest" not in trial_spec().to_payload()
    assert "gpu_attestation_digest" not in suggestion().to_payload()


def test_shared_contract_source_has_no_side_effect_family_imports() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "tools/step5d_autotune_v3/shared_contracts.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "import os",
        "import pathlib",
        "import subprocess",
        "import torch",
        "import rclpy",
        "import socket",
    ):
        assert forbidden not in source


def test_shared_schema_digest_binds_fields_types_and_constraints() -> None:
    assert canonical_sha256(SHARED_SCHEMA_DEFINITION) == SHARED_SCHEMA_DIGEST
    changed = copy.deepcopy(SHARED_SCHEMA_DEFINITION)
    changed["trial_spec"]["fields"]["deadline_unix_ns"] = "non_negative_int"
    assert canonical_sha256(changed) != SHARED_SCHEMA_DIGEST
