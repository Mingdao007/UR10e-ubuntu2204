from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO / "src/ur10e_experiment_runtime"))
sys.path.insert(0, str(ROOT / "tools"))

from ur10e_experiment_runtime import (  # noqa: E402
    BatchIdentity,
    BatchRow,
    RegistryError,
    load_experiment_spec,
    plan_experiment,
    validate_experiment_spec,
)
from ur10e_experiment_runtime.stage_adapters import (  # noqa: E402
    CSV_HANDSHAKE_COLUMNS,
    CSV_IDENTITY_COLUMNS,
    HOST_TO_TP_INTEGER_REGISTERS,
    OVERLAY_FIELDS,
    TP_TO_HOST_INTEGER_REGISTERS,
    legacy_trial_uid,
    normalize_trial_overlay,
    verify_exact_ack,
)
from kunwei_rtde_bridge import (  # noqa: E402
    STEP5D_AUTOTUNE_HANDSHAKE_INPUT_FIELDS,
    STEP5D_AUTOTUNE_HANDSHAKE_INPUT_NAMES,
    STEP5D_AUTOTUNE_HANDSHAKE_OUTPUT_FIELDS,
    STEP5D_AUTOTUNE_HANDSHAKE_OUTPUT_NAMES,
)
from step5d_autotune_contract import (  # noqa: E402
    CampaignSpec,
    ExecutionProfile,
    ForceCandidate,
    TrialSpec,
    TrialTransition,
    TrialTransitionKind,
)
from step5d_autotune_live_driver import (  # noqa: E402
    BridgeTrialCsvRotator,
    TrialArtifactProducer,
)
from step5d_autotune_state_machine import (  # noqa: E402
    HOST_TO_TP_INTEGER_REGISTERS as LEGACY_HOST_TO_TP,
    TP_TO_HOST_INTEGER_REGISTERS as LEGACY_TP_TO_HOST,
)
from step5d_autotune_v3.runtime_profile import DEFAULT_OVERLAY  # noqa: E402
from step5d_autotune_v3.runtime_profile import load_launch_profile  # noqa: E402
from run_step5d_autotune_v3_live import initial_control_overlays  # noqa: E402


SPEC = (
    ROOT
    / "config/experiments/step5d_strict_rnn_autotune_v1.json"
)


def _trial() -> TrialSpec:
    return TrialSpec(
        campaign=CampaignSpec("parity", 1, "a" * 64),
        trial_id=1,
        candidate_token=2,
        command_seq=3,
        plant_epoch=1,
        candidate=ForceCandidate(),
        execution_profile=ExecutionProfile(
            "nf050-slew050-a050", 0.05, 0.5, 0.5
        ),
        backend_id="step5d_v35_native_backend_v1",
        source_fingerprint="b" * 64,
        config_fingerprint="c" * 64,
        transition=TrialTransition(TrialTransitionKind.BASELINE),
    )


def test_step5d_spec_and_plan_freeze_current_behavior_without_external_actions() -> None:
    spec = load_experiment_spec(SPEC)
    plan = plan_experiment(spec, "offline")
    assert plan["executable"] is True
    assert plan["external_actions"] == []
    adapter = plan["adapter_plan"]
    assert adapter["stage_id"] == "step5d_strict_rnn_autotune_v1"
    assert adapter["source_stage_id"] == "step5d_strict_rnn_ablation_v35"
    assert adapter["trajectory_parameters"]["parameters"] == {
        "equation_id": "canonical_cycloid_linear_time_v1",
        "amplitude_m": 0.015,
        "omega_rad_s": 0.1,
    }
    assert adapter["external_actions"] == []
    for row in spec.document["legacy_provenance"]:
        path = ROOT / row["artifact_path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    drifted = spec.document
    drifted["bindings"]["source_sha256"] = "0" * 64
    with pytest.raises(RegistryError, match="source digest"):
        validate_experiment_spec(drifted)


def test_register_and_csv_contracts_match_the_current_bridge_and_state_machine() -> None:
    assert {
        name: HOST_TO_TP_INTEGER_REGISTERS[name] for name in LEGACY_HOST_TO_TP
    } == LEGACY_HOST_TO_TP
    assert {
        name: TP_TO_HOST_INTEGER_REGISTERS[name] for name in LEGACY_TP_TO_HOST
    } == LEGACY_TP_TO_HOST
    assert STEP5D_AUTOTUNE_HANDSHAKE_INPUT_FIELDS == [
        f"input_int_register_{index}" for index in range(24, 31)
    ]
    assert STEP5D_AUTOTUNE_HANDSHAKE_INPUT_NAMES == list(HOST_TO_TP_INTEGER_REGISTERS)
    assert STEP5D_AUTOTUNE_HANDSHAKE_OUTPUT_FIELDS == [
        f"output_int_register_{index}" for index in range(24, 34)
    ]
    assert STEP5D_AUTOTUNE_HANDSHAKE_OUTPUT_NAMES == list(TP_TO_HOST_INTEGER_REGISTERS)
    assert CSV_IDENTITY_COLUMNS == BridgeTrialCsvRotator.IDENTITY_COLUMNS
    assert CSV_HANDSHAKE_COLUMNS == tuple(
        f"ur_output_int_register_{index}" for index in range(24, 34)
    )
    assert TrialArtifactProducer.HANDSHAKE_COLUMNS == CSV_HANDSHAKE_COLUMNS[:7]


def test_trial_uid_and_overlay_are_golden_parity_with_current_v3() -> None:
    trial = _trial()
    material = {
        "campaign_id": trial.campaign.campaign_id,
        "campaign_epoch": trial.campaign.campaign_epoch,
        "campaign_fingerprint": trial.campaign.campaign_fingerprint,
        "trial_id": trial.trial_id,
        "candidate_token": trial.candidate_token,
        "command_seq": trial.command_seq,
        "plant_epoch": trial.plant_epoch,
        "candidate": trial.candidate.payload(),
        "execution_profile": trial.execution_profile.payload(),
        "backend_id": trial.backend_id,
        "source_fingerprint": trial.source_fingerprint,
        "config_fingerprint": trial.config_fingerprint,
        "transition": trial.transition.payload(),
        "search_attestation": None,
    }
    assert legacy_trial_uid(material) == trial.trial_uid
    normalized = normalize_trial_overlay(DEFAULT_OVERLAY)
    assert normalized == DEFAULT_OVERLAY
    assert tuple(normalized) == OVERLAY_FIELDS
    altered = dict(DEFAULT_OVERLAY)
    altered["orientation_ko"] = 0.8
    with pytest.raises(ValueError, match="control_candidate_uid"):
        normalize_trial_overlay(altered)


def test_exact_ack_requires_current_identity_bundle_and_safe_closure() -> None:
    arm = {
        "campaign_epoch": 1,
        "trial_id": 2,
        "command": 1,
        "candidate_token": 3,
        "execution_profile_id": 533,
        "command_seq": 4,
    }
    ack = {**arm, "command": 2, "command_seq": 5}
    assert verify_exact_ack(
        arm=arm,
        ack=ack,
        safe_closure=True,
        immutable_bundle_written=True,
    )
    assert not verify_exact_ack(
        arm=arm,
        ack={**ack, "candidate_token": 99},
        safe_closure=True,
        immutable_bundle_written=True,
    )
    assert not verify_exact_ack(
        arm=arm,
        ack=ack,
        safe_closure=False,
        immutable_bundle_written=True,
    )


def test_batch_identity_binds_the_exact_current_ten_control_overlay_rows() -> None:
    spec = load_experiment_spec(SPEC)
    profile = load_launch_profile()
    overlays = initial_control_overlays(profile)
    identity = BatchIdentity(
        campaign_uid="campaign-uid",
        experiment_fingerprint=spec.fingerprint,
        launch_fingerprint=profile.fingerprint,
        adapter_fingerprint="a" * 64,
        physical_prior_fingerprint="b" * 64,
        safety_envelope_fingerprint="c" * 64,
        return_policy_fingerprint="d" * 64,
        controller_readback_fingerprint="e" * 64,
        authorization_ref_sha256="f" * 64,
        plant_epoch=1,
        rows=tuple(
            BatchRow(
                row_index=index,
                control_candidate={
                    name: overlay[name]
                    for name in (
                        "force_p_gain",
                        "force_i_gain",
                        "force_damping",
                        "orientation_ko",
                    )
                },
                trial_overlay=overlay,
            )
            for index, overlay in enumerate(overlays, start=1)
        ),
    )
    assert len(identity.rows) == 10
    assert [row.control_candidate_uid for row in identity.rows] == [
        overlay["control_candidate_uid"] for overlay in overlays
    ]


def test_other_stage_adapter_ids_remain_unregistered_in_this_tranche() -> None:
    spec = load_experiment_spec(SPEC)
    mutated = spec.document
    mutated["components"]["stage_adapter"]["id"] = (
        "step6b_strict_rnn_autotune_adapter_v1"
    )
    with pytest.raises(RegistryError, match="allowlisted"):
        validate_experiment_spec(mutated)
