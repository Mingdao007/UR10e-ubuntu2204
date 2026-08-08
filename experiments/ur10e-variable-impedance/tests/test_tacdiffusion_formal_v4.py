from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import numpy as np

from ur10e_vic.tacdiffusion.action import (
    MODEL_MODE_FIXED_K_V1,
    MODEL_MODE_VARIABLE_K_V1,
    TypedModelOutputV1,
    expand_model_output_to_controller_action,
)
from ur10e_vic.tacdiffusion.formal_benchmark import (
    benchmark_formal_50_step_sampler,
    validate_formal_50_step_sampler_candidate,
)
from ur10e_vic.tacdiffusion.contracts import (
    ContactGuardProfileV1,
    DynamicsConformanceBinding,
    DynamicsReceipt,
    DynamicsSample,
    FORMAL_MODEL_RATE_CANDIDATES_HZ,
    FORMAL_OBSERVATION_DIMENSION,
    FORMAL_SAMPLER_STEPS,
    FORMAL_EXPERT_ACTION_COMPONENT_ABS_MAX,
    FORMAL_EXPERT_ACTION_FORCE_NORM_MAX_N,
    FORMAL_EXPERT_ACTION_LIMITS_SCHEMA_V1,
    FORMAL_EXPERT_ACTION_SLEW_PER_S,
    FORMAL_EXPERT_ACTION_TORQUE_NORM_MAX_NM,
    ForceAuthorityReceiptV1,
    FormalEpisodeManifestV1,
    KunweiOnlyForceAuthorityV1,
    ProductionDynamicsConformanceReceiptV1,
    validate_formal_force_source_payload,
    validate_formal_rtde_recipe,
)
from ur10e_vic.tacdiffusion.eligibility import (
    FORMAL_ELIGIBILITY_SCHEMA,
    FormalEligibilityValidator,
    read_formal_eligibility_receipt,
)
from ur10e_vic.tacdiffusion.episode_composition import ActionLabel, ActionLabelContext
from ur10e_vic.tacdiffusion.episode_recorder import (
    EPISODE_FRAME_SCHEMA_V3,
    EPISODE_FRAME_SCHEMA_V4,
    ExpertActionReceiptV1,
    EpisodeFrameV2,
    EpisodeFrameV4,
    FormalEpisodeRecorder,
    RecorderError,
    ReferenceReceiptV1,
    TubeDecisionReceiptV1,
    compute_v3_row_sha256,
    validate_formal_episode_artifact,
)
from ur10e_vic.tacdiffusion.formal_episode import FormalFrameComposerV1
from ur10e_vic.tacdiffusion.expert import FixedKExpertV1, VariableKExpertV1
from ur10e_vic.tacdiffusion.formal_source import load_formal_v4_source_contract
from ur10e_vic.tacdiffusion.governance import (
    build_review_governance_source_contract,
    load_review_governance_source,
)
from ur10e_vic.tacdiffusion.formal_timing import FormalModelTimingEvidence
from ur10e_vic.tacdiffusion.formal_trajectory import build_formal_trajectory_timeline
from ur10e_vic.tacdiffusion.formal_dynamics import (
    FormalDynamicsConformanceV1,
    ProductionDynamicsRuntimeV1,
    build_formal_dynamics_probe_source,
    load_formal_dynamics_conformance_receipt,
    validate_formal_dynamics_probe_source,
)
from ur10e_vic.tacdiffusion.formal_model import (
    _alpha_bar_schedule,
    _denormalize_sample,
    _normalize_training_targets,
    build_variable_k_training_labels,
    FormalDatasetManifestV1,
    FormalModelConfigV1,
    FormalDDPMPredictorV1,
    FormalDDPMTrainerV1,
    FormalCheckpointManifestV1,
    train_formal_ddpm_v1,
)
from ur10e_vic.tacdiffusion.formal_campaign import (
    FORMAL_FIXED_CAMPAIGN_ID,
    FormalQualificationReceiptV1,
    build_formal_campaign_source_contract,
    load_formal_campaign_source_contract,
)
from ur10e_vic.tacdiffusion.formal_equipment import (
    FORMAL_EQUIPMENT_READBACK_SCHEMA_V1,
    load_formal_equipment_contract,
)


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64


def _production_receipt() -> tuple[DynamicsSample, DynamicsReceipt]:
    sample = DynamicsSample(
        sequence=0,
        timestamp_s=0.0,
        previous_q=(0.0,) * 6,
        previous_qd=(0.0,) * 6,
        previous_commanded_no_gravity_torque_nm=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0),
        calibrated_jacobian=tuple(
            tuple(1.0 if row == column else 0.0 for column in range(6))
            for row in range(6)
        ),
        coriolis_torque_nm=(0.0,) * 6,
        joint_damping_torque_nm=(0.0,) * 6,
        source_hashes={"dynamics": _SHA_A},
        model_identity="production_model_v1",
        tcp_identity="tool0_tcp",
        calibration_identity="calibration_v1",
        actual_current_torque_nm_shadow=(2.0,) * 6,
    )
    binding = DynamicsConformanceBinding(
        binding_id="production_binding_v1",
        controller_receipt_identity="controller_receipt_v1",
        controller_receipt_sha256=_SHA_B,
        sample_fingerprint_sha256=sample.fingerprint_sha256,
        model_identity=sample.model_identity,
        tcp_identity=sample.tcp_identity,
        calibration_identity=sample.calibration_identity,
    )
    receipt = DynamicsReceipt.from_sample(
        sample,
        source_kind="production",
        conformance_binding=binding,
    )
    return sample, receipt


def _formal_row() -> tuple[EpisodeFrameV4, FormalEpisodeManifestV1]:
    sample, dynamics = _production_receipt()
    authority = KunweiOnlyForceAuthorityV1()
    guard = ContactGuardProfileV1.no_contact(authority=authority)
    manifest = FormalEpisodeManifestV1(
        manifest_id="formal_manifest_v1",
        force_authority=authority,
        contact_guard_profile=guard,
        rtde_output_fields=("timestamp", "actual_current_as_torque"),
        source_hashes={"manifest": _SHA_C},
        expert_action_limits={
            "schema_version": FORMAL_EXPERT_ACTION_LIMITS_SCHEMA_V1,
            "frame_id": "tool0_tcp",
            "component_abs_max": list(FORMAL_EXPERT_ACTION_COMPONENT_ABS_MAX),
            "force_norm_max_n": FORMAL_EXPERT_ACTION_FORCE_NORM_MAX_N,
            "torque_norm_max_nm": FORMAL_EXPERT_ACTION_TORQUE_NORM_MAX_NM,
            "slew_per_s": list(FORMAL_EXPERT_ACTION_SLEW_PER_S),
        },
    )
    semantic_fingerprint = _SHA_D
    context = ActionLabelContext(
        sequence=0,
        timestamp_s=0.0,
        frame_id="tool0_tcp",
        applied_action_12d=(0.0,) * 12,
        echoed_action_12d=(0.0,) * 12,
        semantic_context_fingerprint_sha256=semantic_fingerprint,
    )
    label = ActionLabel(
        expert_action_12d=(0.0,) * 12,
        available=True,
        source="deterministic_expert",
        semantics="deterministic_expert_guarded_action_12d_v1",
        policy_id="deterministic_expert_v1",
        sequence=0,
        timestamp_s=0.0,
        frame_id="tool0_tcp",
        context_fingerprint_sha256=context.as_json()["context_fingerprint_sha256"],
    )
    row = EpisodeFrameV4(
        episode_id="formal_episode_v1",
        sample_index=0,
        control_sequence=0,
        control_time_s=0.0,
        observation_84d=(0.0,) * 84,
        expert_action_12d=(0.0,) * 12,
        applied_action_12d=(0.0,) * 12,
        echoed_action_12d=(0.0,) * 12,
        action_generation=0,
        action_age_ticks=0,
        action_echo_coherent=True,
        external_device_time_s=0.0,
        external_host_visible_time_s=0.0,
        external_batch_id=0,
        external_sample_index=0,
        external_hold=False,
        external_held_ticks=0,
        device_age_samples=0,
        host_age_s=0.0,
        internal_wrench_valid=True,
        dynamics_sample=sample,
        dynamics_receipt=dynamics,
        action_label_context=context,
        action_label=label,
        semantic_context_fingerprint_sha256=semantic_fingerprint,
        reference_receipt=ReferenceReceiptV1(
            "formal_reference/v1", {"valid": True, "shadow_only": False}
        ),
        force_authority_receipt=ForceAuthorityReceiptV1(
            sequence=0,
            sample_index=0,
            device_time_s=0.0,
            host_visible_time_s=0.0,
            frame_id="tool0_tcp",
            authority=authority,
            source_sample_sha256=_SHA_C,
        ),
        production_dynamics_receipt=ProductionDynamicsConformanceReceiptV1(dynamics),
        expert_action_receipt=ExpertActionReceiptV1(
            "formal_expert_action/v1",
            {"available": True, "shadow_only": False, "expert_action_12d": [0.0] * 12},
        ),
        tube_decision_receipt=TubeDecisionReceiptV1(
            "formal_tube_decision/v1", {"accepted": True, "shadow_only": False}
        ),
        formal_manifest=manifest,
        expert_label_available=True,
        expert_action_source="deterministic_expert",
        action_label_semantics="deterministic_expert_guarded_action_12d_v1",
        candidate_window=True,
        capture_phase="formal_track_state_torque",
        receiver_state=2,
    )
    return row, manifest


def _healthy_recorder() -> dict[str, object]:
    return {
        "sealed": True,
        "manifest_written": True,
        "tamper_free": True,
        "fault": None,
        "writer_error": None,
        "overflowed": False,
        "stalled": False,
        "queue_depth": 0,
        "unsealed_tail": 0,
        "enqueued_rows": 1,
        "durable_rows": 1,
    }


def test_kunwei_authority_guard_profiles_and_formal_recipe_reject_alternates() -> None:
    authority = KunweiOnlyForceAuthorityV1()
    assert authority.source_identity == "kunwei_kwr75_tcp_raw_stream_v1"
    assert authority.sensor_model == "KWR75"
    assert authority.transport == "tcp_raw"
    assert authority.software_baseline_semantics == "software_baseline_only"
    assert ContactGuardProfileV1.no_contact().force_limit_n == 6.0
    assert ContactGuardProfileV1.no_contact().torque_limit_nm == 0.5
    assert ContactGuardProfileV1.expert_contact().force_limit_n == 50.0
    assert ContactGuardProfileV1.expert_contact().torque_limit_nm == 4.0
    recipe = validate_formal_rtde_recipe(
        {
            "output_fields": ["timestamp", "actual_current_as_torque"],
            "force_authority": authority.as_json(),
            "model_rate_candidates_hz": [100, 50],
            "observation_dimension": 84,
        }
    )
    assert recipe["model_rate_candidates_hz"] == [100, 50]
    for forbidden in ("actual_TCP_force", "get_tcp_force", "OnRobot"):
        with pytest.raises(ValueError):
            validate_formal_rtde_recipe(
                {
                    "output_fields": ["timestamp", forbidden],
                    "force_authority": authority.as_json(),
                }
            )
    with pytest.raises(ValueError):
        validate_formal_force_source_payload(
            {"force_authority": {"source_identity": "alternate_sensor"}}
        )
    with pytest.raises(ValueError):
        validate_formal_force_source_payload(
            {"force_authority_receipt": {"source_identity": "alternate_sensor"}}
        )
    with pytest.raises(ValueError):
        validate_formal_rtde_recipe(
            {
                "output_fields": ["timestamp"],
                "force_authority": authority.as_json(),
                "source_identity": "alternate_sensor",
            }
        )
    manifest_payload = FormalEpisodeManifestV1(
        manifest_id="forbidden_manifest",
        force_authority=authority,
        contact_guard_profile=ContactGuardProfileV1.no_contact(authority=authority),
        rtde_output_fields=("timestamp", "actual_current_as_torque"),
        source_hashes={"manifest": _SHA_A},
    ).as_json()
    manifest_payload["rtde_output_fields"] = ["timestamp", "actual_TCP_force"]
    with pytest.raises(ValueError):
        FormalEpisodeManifestV1.from_json(manifest_payload)


def test_fixed_and_variable_k_primitives_are_frozen_and_slew_bounded() -> None:
    fixed = FixedKExpertV1()
    assert fixed.stiffness_6d == (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
    variable = VariableKExpertV1()
    assert variable.raw_stiffness((0.0, 0.0, 0.0), (8.0, 0.0, 0.0)) == 600.0
    assert variable.raw_stiffness((0.010, 0.0, 0.0), (8.0, 0.0, 0.0)) == 800.0
    assert variable.raw_stiffness((0.0, 0.0, 0.0), (12.0, 0.0, 0.0)) == 400.0
    assert variable.raw_stiffness((0.005, 0.0, 0.0), (10.0, 0.0, 0.0)) == 500.0
    bounded = variable.stiffness(
        (0.010, 0.0, 0.0),
        (8.0, 0.0, 0.0),
        previous_stiffness=(600.0, 600.0, 600.0),
        dt_s=0.002,
    )
    assert bounded[:3] == (600.8, 600.8, 600.8)
    assert bounded[3:] == (30.0, 30.0, 30.0)
    label = variable.training_label(
        (0.005, 0.0, 0.0),
        (10.0, 0.0, 0.0),
        feedforward_wrench_6d=(1.0,) * 6,
    )
    assert len(label) == 7
    assert label[:6] == (1.0,) * 6
    assert label[6] == 500.0
    built_labels = build_variable_k_training_labels(
        np.ones((1, 6)),
        np.asarray([[0.005, 0.0, 0.0]]),
        np.asarray([[10.0, 0.0, 0.0]]),
    )
    assert built_labels.shape == (1, 7)
    assert built_labels[0, 6] == 500.0
    assert variable.stiffness_from_model_output(800.0, previous_stiffness=600.0, dt_s=0.002)[:3] == (
        600.8,
        600.8,
        600.8,
    )


def test_action_expansion_is_typed_6d_or_7d_to_canonical_12d() -> None:
    fixed_output = TypedModelOutputV1(MODEL_MODE_FIXED_K_V1, (1.0,) * 6)
    fixed = expand_model_output_to_controller_action(
        fixed_output, mode=MODEL_MODE_FIXED_K_V1
    )
    assert len(fixed.vector12) == 12
    assert fixed.stiffness == (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
    variable_output = TypedModelOutputV1(MODEL_MODE_VARIABLE_K_V1, (1.0,) * 6 + (777.0,))
    variable = expand_model_output_to_controller_action(
        variable_output,
        mode=MODEL_MODE_VARIABLE_K_V1,
    )
    assert len(variable.vector12) == 12
    assert variable.stiffness[:3] == (777.0, 777.0, 777.0)
    assert variable.variable_k_output_n_m == 777.0
    assert expand_model_output_to_controller_action(
        (1.0,) * 6 + (900.0,), mode=MODEL_MODE_VARIABLE_K_V1
    ).stiffness[:3] == (800.0, 800.0, 800.0)
    command_invariant = expand_model_output_to_controller_action(
        variable_output,
        mode=MODEL_MODE_VARIABLE_K_V1,
        previous_stiffness=(600.0, 600.0, 600.0),
        dt_s=0.002,
    )
    assert command_invariant.stiffness[:3] == (600.8, 600.8, 600.8)
    assert variable.active is False and variable.shadow_only is True
    with pytest.raises(ValueError):
        TypedModelOutputV1(MODEL_MODE_VARIABLE_K_V1, (0.0,) * 6)


def test_previous_tick_dynamics_is_primary_and_current_torque_is_shadow_only() -> None:
    sample, with_shadow = _production_receipt()
    without_shadow = DynamicsReceipt.from_sample(
        DynamicsSample(
            **{
                **sample.__dict__,
                "actual_current_torque_nm_shadow": None,
            }
        ),
        source_kind="production",
        conformance_binding=with_shadow.conformance_binding,
    )
    assert with_shadow.internal_wrench_tcp_si == pytest.approx(
        without_shadow.internal_wrench_tcp_si
    )
    assert with_shadow.actual_current_shadow_wrench_tcp_si is not None
    assert with_shadow.actual_current_shadow_delta_norm is not None
    assert with_shadow.authoritative_torque_source == "previous_commanded_no_gravity_torque"
    assert ProductionDynamicsConformanceReceiptV1(with_shadow).previous_tick_only is True


def test_formal_v4_row_receipts_seal_and_legacy_rows_never_qualify(tmp_path: Path) -> None:
    row, manifest = _formal_row()
    payload = row.as_json()
    assert payload["schema"] == EPISODE_FRAME_SCHEMA_V4
    assert row.formal_eligible is True
    assert row.row_seal_valid is True
    assert payload["row_sha256"] == compute_v3_row_sha256(payload)
    tampered = dict(payload)
    tampered["observation_84d"] = [1.0] + list(tampered["observation_84d"])[1:]
    assert tampered["row_sha256"] != compute_v3_row_sha256(tampered)
    decision = FormalEligibilityValidator().evaluate(
        [row],
        recorder_health=_healthy_recorder(),
        formal_manifest=manifest,
        first_live_shadow=False,
    )
    assert decision.formal_eligible is True
    receipt_path = tmp_path / "formal_eligibility.json"
    FormalEligibilityValidator().write_receipt(
        receipt_path, decision, recorder_health=_healthy_recorder()
    )
    receipt = read_formal_eligibility_receipt(receipt_path)
    assert receipt["schema"] == FORMAL_ELIGIBILITY_SCHEMA
    legacy = FormalEligibilityValidator().evaluate(
        [{"schema": EPISODE_FRAME_SCHEMA_V3}],
        recorder_health=_healthy_recorder(),
        first_live_shadow=False,
    )
    assert legacy.formal_eligible is False
    assert legacy.predicates["all_rows_are_v4"] is False


def test_formal_v4_production_receipt_is_bound_to_base_dynamics_receipt() -> None:
    row, _ = _formal_row()
    assert isinstance(row.production_dynamics_receipt, ProductionDynamicsConformanceReceiptV1)
    base = row.production_dynamics_receipt.dynamics_receipt
    mismatched = replace(base, internal_wrench_tcp_si=(1.0,) * 6)
    with pytest.raises(ValueError, match="bound to the base dynamics"):
        replace(
            row,
            production_dynamics_receipt=ProductionDynamicsConformanceReceiptV1(mismatched),
        )


def test_formal_recorder_fails_closed_without_identity(tmp_path: Path) -> None:
    recorder = FormalEpisodeRecorder(tmp_path, episode_id="no_identity")
    with pytest.raises(RecorderError, match="complete_semantic_identity"):
        recorder.start()
    assert not (tmp_path / "episode_v4.jsonl").exists()


def test_formal_recorder_writes_only_sealed_v4_rows(tmp_path: Path) -> None:
    row, _ = _formal_row()
    recorder = FormalEpisodeRecorder(
        tmp_path,
        episode_id="formal_episode_v1",
        metadata={
            "semantic_context": {
                "hash_identities": {"source": _SHA_A},
                "semantic_context_fingerprint_sha256": _SHA_D,
            }
        },
    )
    recorder.start()
    assert recorder.enqueue(row) is True
    manifest = recorder.close(seal=True)
    assert manifest is not None
    header, rows = validate_formal_episode_artifact(
        tmp_path / "episode_v4.jsonl", tmp_path / "episode_v4.manifest.json"
    )
    assert header["schema"] == "ur10e_tacdiffusion_episode_artifact/v4"
    assert len(rows) == 1
    assert rows[0]["schema"] == EPISODE_FRAME_SCHEMA_V4


def test_formal_sampler_benchmark_is_exactly_100_50_and_50_steps() -> None:
    trained = train_formal_ddpm_v1(
        np.zeros((2, FORMAL_OBSERVATION_DIMENSION)),
        np.column_stack((np.zeros((2, 6)), np.full(2, 600.0))),
        mode=MODEL_MODE_VARIABLE_K_V1,
        epochs=1,
    )

    candidate = benchmark_formal_50_step_sampler(
        trained.predictor,
        (0.0,) * FORMAL_OBSERVATION_DIMENSION,
        duration_per_rate_s=0.001,
        checkpoint_sha256=trained.checkpoint_manifest.checkpoint_sha256,
        dataset_sha256=trained.dataset_manifest.dataset_sha256,
    )
    assert candidate["rates_hz"] == list(FORMAL_MODEL_RATE_CANDIDATES_HZ)
    assert candidate["sampler_steps"] == 50
    assert candidate["formal_model_mode"] == MODEL_MODE_VARIABLE_K_V1
    assert candidate["formal_output_dimension"] == 7
    assert all(
        tick["sampler_steps_executed"] == FORMAL_SAMPLER_STEPS
        for result in candidate["rate_results"]
        for tick in result["raw_ticks"]
    )
    validated = validate_formal_50_step_sampler_candidate(
        candidate,
        expected_checkpoint_sha256=trained.checkpoint_manifest.checkpoint_sha256,
        expected_dataset_sha256=trained.dataset_manifest.dataset_sha256,
    )
    assert validated["selection_eligible"] is True
    assert validated["selected_rate_hz"] == 100
    assert all(item["accepted"] for item in validated["recomputed_rate_results"])
    invalid = dict(candidate)
    invalid["rates_hz"] = [500, 200, 100, 50]
    with pytest.raises(ValueError):
        validate_formal_50_step_sampler_candidate(
            invalid,
            expected_checkpoint_sha256=trained.checkpoint_manifest.checkpoint_sha256,
            expected_dataset_sha256=trained.dataset_manifest.dataset_sha256,
        )


def test_formal_timing_evidence_rejects_legacy_rates() -> None:
    assert FormalModelTimingEvidence(100, 1.0, 0.008, 0).accepted is True
    with pytest.raises(ValueError):
        FormalModelTimingEvidence(200, 1.0, 0.001, 0)


def test_formal_dynamics_probe_is_no_motion_no_force_and_receipt_is_hash_bound() -> None:
    source = build_formal_dynamics_probe_source()
    validate_formal_dynamics_probe_source(source)
    for forbidden in (
        "direct_torque(",
        "get_tcp_force(",
        "zero_ftsensor(",
        "speedl(",
        "movej(",
    ):
        assert forbidden not in source
    identity = np.eye(6)
    receipt = FormalDynamicsConformanceV1(
        controller_jacobian_6x6=identity,
        host_jacobian_6x6=identity + 1.0e-5,
        controller_coriolis_nm=np.zeros(6),
        host_coriolis_nm=np.full(6, 1.0e-4),
        q_rad=np.zeros(6),
        qd_rad_s=np.zeros(6),
        controller_source_sha256=_SHA_A,
        calibrated_model_sha256=_SHA_B,
        calibration_identity="calib_test",
    )
    assert receipt.accepted is True
    assert load_formal_dynamics_conformance_receipt(receipt.as_json()).accepted is True
    tampered = receipt.as_json()
    tampered["host_coriolis_nm"][0] = 0.2
    with pytest.raises(ValueError, match="hash mismatch"):
        load_formal_dynamics_conformance_receipt(tampered)

    runtime = ProductionDynamicsRuntimeV1(
        receipt,
        jacobian_provider=lambda _q: np.eye(6),
        coriolis_provider=lambda _q, _qd: np.zeros(6),
        source_hashes={
            "controller_conformance": receipt.receipt_sha256,
            "calibrated_model": receipt.calibrated_model_sha256,
        },
    )
    row = {
        **{f"actual_q_{axis}": 0.0 for axis in range(6)},
        **{f"actual_qd_{axis}": 0.0 for axis in range(6)},
        **{f"commanded_joint_torque_nm_{axis}": float(axis + 1) for axis in range(6)},
        **{f"actual_current_as_torque_{axis}": 0.0 for axis in range(6)},
    }
    sample, dynamics = runtime.produce(row, sequence=0, timestamp_s=0.0)
    assert dynamics.valid is True
    assert dynamics.source_kind == "production"
    assert dynamics.conformance_binding is not None
    assert dynamics.conformance_binding.accepted_for(sample)

    _, manifest = _formal_row()
    runtime = ProductionDynamicsRuntimeV1(
        receipt,
        jacobian_provider=lambda _q: np.eye(6),
        coriolis_provider=lambda _q, _qd: np.zeros(6),
        source_hashes={
            "controller_conformance": receipt.receipt_sha256,
            "calibrated_model": receipt.calibrated_model_sha256,
        },
    )
    composer = FormalFrameComposerV1(
        manifest=manifest,
        semantic_context_fingerprint_sha256=_SHA_D,
        dynamics_runtime=runtime,
    )
    formal_fixture, _ = _formal_row()
    base = EpisodeFrameV2(
        **{
            name: getattr(formal_fixture, name)
            for name in EpisodeFrameV2.__dataclass_fields__
        }
    )
    assert composer.compose(
        base,
        previous_runtime_row=row,
        tube_payload={"decision_id": "tube_ok_0"},
    ) is None
    second = replace(
        base,
        sample_index=1,
        control_sequence=1,
        control_time_s=0.002,
        external_device_time_s=0.001,
        external_host_visible_time_s=0.001,
        external_sample_index=1,
    )
    formal = composer.compose(
        second,
        previous_runtime_row=row,
        tube_payload={"decision_id": "tube_ok_1"},
    )
    assert isinstance(formal, EpisodeFrameV4)
    assert formal.formal_eligible is True
    assert formal.observation_84d[6:12] == pytest.approx(
        formal.dynamics_receipt.internal_wrench_tcp_si
    )


def test_review_governance_source_is_zero_plus_zero_and_one_explicit_luna_max() -> None:
    path = Path(__file__).parents[1] / "config" / "tacdiffusion_formal_v4_review_governance.json"
    contract = load_review_governance_source(path)
    assert contract.as_json() == build_review_governance_source_contract().as_json()
    contract.validate_requested_review(automatic_reviewers=0, explicit_luna_max_reviewers=1)
    with pytest.raises(ValueError):
        contract.validate_requested_review(automatic_reviewers=1)
    with pytest.raises(ValueError):
        contract.validate_requested_review(explicit_luna_max_reviewers=2)
    assert contract.historical_artifacts_are_not_rewritten is True


def test_formal_source_contract_loader_binds_canonical_allowlist_and_profiles() -> None:
    path = Path(__file__).parents[1] / "config" / "tacdiffusion_formal_v4_contract.json"
    contract = load_formal_v4_source_contract(path)
    assert contract.lineage == "tacdiffusion_formal_v4"
    assert contract.no_contact_guard.force_limit_n == 6.0
    assert contract.expert_contact_guard.force_limit_n == 50.0
    assert contract.expert_contact_guard.torque_limit_nm == 4.0
    assert contract.payload["expert_action_limits"] == {
        "schema_version": "ur10e_tacdiffusion_expert_action_limits/v1",
        "frame_id": "tool0_tcp",
        "component_abs_max": [50.0, 50.0, 50.0, 4.0, 4.0, 4.0],
        "force_norm_max_n": 50.0,
        "torque_norm_max_nm": 4.0,
        "slew_per_s": [100.0, 100.0, 100.0, 10.0, 10.0, 10.0],
    }
    assert "actual_current_as_torque" in contract.rtde_output_allowlist
    assert "actual_TCP_force" not in contract.rtde_output_allowlist
    assert contract.review_governance is not None
    assert contract.review_governance.lineage == "tacdiffusion_formal_v4"
    assert contract.review_governance.automatic_reviewers == 0
    assert contract.review_governance.optional_luna_max_reviewer_max == 1
    assert contract.model_active is False and contract.shadow_only is True


@pytest.mark.parametrize(
    ("mode", "output_dimension"),
    ((MODEL_MODE_FIXED_K_V1, 6), (MODEL_MODE_VARIABLE_K_V1, 7)),
)
def test_formal_v4_ddpm_train_predict_is_84d_typed_and_executes_50_steps(
    mode: str,
    output_dimension: int,
) -> None:
    observations = np.arange(168, dtype=np.float64).reshape(2, 84) / 100.0
    targets = np.zeros((2, output_dimension), dtype=np.float64)
    if mode == MODEL_MODE_VARIABLE_K_V1:
        targets[:, 6] = 600.0
    result = train_formal_ddpm_v1(
        observations,
        targets,
        mode=mode,
        epochs=1,
        dataset_id=f"formal_test_{mode}",
    )
    assert result.predictor.config.observation_dimension == 84
    assert result.predictor.config.output_dimension == output_dimension
    assert result.predictor.config.diffusion_steps == 50
    assert result.predictor.config.active_enabled is False
    assert result.predictor.config.shadow_only is True
    sample = result.predictor.sample(observations[0], seed=7)
    default_seed_sample = result.predictor.sample(observations[0])
    assert sample.steps_executed == 50
    assert sample.step_trace == tuple(range(49, -1, -1))
    assert default_seed_sample.steps_executed == 50
    assert default_seed_sample.step_trace == tuple(range(49, -1, -1))
    assert len(sample.values) == output_dimension
    assert np.isfinite(sample.values).all()
    if mode == MODEL_MODE_VARIABLE_K_V1:
        assert 400.0 <= sample.values[6] <= 800.0
    assert FormalDatasetManifestV1.from_json(result.dataset_manifest.as_json()).dataset_sha256 == result.dataset_manifest.dataset_sha256
    assert FormalCheckpointManifestV1.from_json(result.checkpoint_manifest.as_json()).checkpoint_sha256 == result.checkpoint_manifest.checkpoint_sha256


def test_formal_ddpm_uses_alpha_product_and_normalized_variable_k_coordinate() -> None:
    config = FormalModelConfigV1(mode=MODEL_MODE_VARIABLE_K_V1)
    alpha_bar = _alpha_bar_schedule(config)
    betas = np.linspace(config.beta_start, config.beta_end, config.diffusion_steps)
    assert alpha_bar == pytest.approx(np.cumprod(1.0 - betas))
    assert np.all(np.diff(alpha_bar) < 0.0)

    physical = np.zeros((3, 7), dtype=np.float64)
    physical[:, 6] = (400.0, 600.0, 800.0)
    normalized = _normalize_training_targets(physical, mode=MODEL_MODE_VARIABLE_K_V1)
    assert normalized[:, 6] == pytest.approx((-1.0, 0.0, 1.0))
    assert _denormalize_sample(normalized[0], mode=MODEL_MODE_VARIABLE_K_V1)[6] == 400.0
    assert _denormalize_sample(normalized[1], mode=MODEL_MODE_VARIABLE_K_V1)[6] == 600.0
    assert _denormalize_sample(normalized[2], mode=MODEL_MODE_VARIABLE_K_V1)[6] == 800.0
    physical[0, 6] = 399.0
    with pytest.raises(ValueError, match="400..800"):
        _normalize_training_targets(physical, mode=MODEL_MODE_VARIABLE_K_V1)


def test_formal_campaign_source_has_two_200_episode_plans_and_variable_dependency() -> None:
    path = Path(__file__).parents[1] / "config" / "tacdiffusion_formal_v4_campaign_contract.json"
    loaded = load_formal_campaign_source_contract(path)
    built = build_formal_campaign_source_contract()
    assert loaded.as_json() == built.as_json()
    assert loaded.fixed_campaign.phase_counts == {"pilot": 50, "formal_training": 150}
    assert loaded.variable_campaign.phase_counts == {"pilot": 50, "formal_training": 150}
    assert len(loaded.fixed_campaign.build_episode_plan()) == 200
    assert len(loaded.variable_campaign.build_episode_plan()) == 200
    assert set(loaded.fixed_campaign.trajectory_families) == {
        "circle",
        "ellipse",
        "figure_eight",
        "lissajous",
        "linear_grid",
        "rounded_arc",
        "seeded_smooth_spline",
    }
    assert loaded.fixed_campaign.target_loads_n == (3.0, 5.0, 8.0)
    with pytest.raises(ValueError, match="requires fixed-K"):
        loaded.validate_variable_dependency(None)
    qualification = FormalQualificationReceiptV1(
        fixed_campaign_id=FORMAL_FIXED_CAMPAIGN_ID,
        fixed_campaign_complete=True,
        k_load_qualified=True,
        source_sha256=_SHA_A,
    )
    loaded.validate_variable_dependency(qualification)
    assert all(shadow.duration_s == pytest.approx(45.0) for campaign in loaded.campaigns for shadow in campaign.live_shadows)
    assert all(not campaign.model_active and campaign.shadow_only for campaign in loaded.campaigns)


def test_formal_seven_family_timelines_are_500hz_bounded_and_return_to_anchor() -> None:
    anchor = (0.4878, 0.1293, 0.016, 3.12, 0.0, 0.068)
    for seed, family in enumerate(
        (
            "circle",
            "ellipse",
            "figure_eight",
            "lissajous",
            "linear_grid",
            "rounded_arc",
            "seeded_smooth_spline",
        )
    ):
        timeline = build_formal_trajectory_timeline(
            family=family,
            seed=seed,
            anchor_pose_base=anchor,
            u_axis_base=(1.0, 0.0, 0.0),
            v_axis_base=(0.0, 1.0, 0.0),
        )
        assert timeline.rate_hz == 500
        assert len(timeline.rows) == 4001
        assert timeline.max_translation_speed_m_s <= 0.01
        assert timeline.max_translation_acceleration_m_s2 <= 0.2
        assert timeline.rows[0].desired_pose_base == pytest.approx(anchor)
        assert timeline.rows[-1].desired_pose_base == pytest.approx(anchor)


def test_formal_equipment_binds_new_eoat_kunwei_geometry_and_fresh_readback() -> None:
    path = Path(__file__).parents[1] / "config" / "tacdiffusion_formal_v4_equipment_contract.json"
    contract = load_formal_equipment_contract(path)
    assert contract.authority.source_identity == "kunwei_kwr75_tcp_raw_stream_v1"
    assert contract.wrench_transform_sensor_to_tcp_6x6[3][1] == pytest.approx(0.0559)
    assert contract.wrench_transform_sensor_to_tcp_6x6[4][0] == pytest.approx(-0.0559)
    receipt = {
        "schema_version": FORMAL_EQUIPMENT_READBACK_SCHEMA_V1,
        "equipment_contract_sha256": contract.fingerprint_sha256,
        "dashboard": {
            "remote_control": True,
            "safety_mode": "NORMAL",
            "robot_mode": "RUNNING",
            "program_running": False,
        },
        "readback": {
            "payload_kg": 0.413,
            "payload_cog_m": [0.0011, 0.0031, 0.0163],
            "tcp_offset_m_rad": [0.0, 0.0, 0.0874, 0.0, 0.0, 0.0],
            "actual_tcp_speed_m_s_rad_s": [0.0] * 6,
        },
        "checks": {
            "single_writer": True,
            "payload_match": True,
            "cog_match": True,
            "tcp_match": True,
            "stationary": True,
            "kunwei_raw_stream": True,
            "no_ur_force_fields_read": True,
        },
    }
    contract.validate_readback(receipt)
    forbidden = json.loads(json.dumps(receipt))
    forbidden["readback"]["actual_TCP_force"] = [0.0] * 6
    with pytest.raises(ValueError, match="forbidden force token"):
        contract.validate_readback(forbidden)
    mismatched = json.loads(json.dumps(receipt))
    mismatched["readback"]["tcp_offset_m_rad"][2] = 0.1221
    with pytest.raises(ValueError, match="CoG/TCP"):
        contract.validate_readback(mismatched)


def test_legacy_36d_model_source_bytes_remain_pinned_separate_from_formal_path() -> None:
    legacy_model = Path(__file__).parents[1] / "ur10e_vic" / "tacdiffusion" / "model.py"
    assert hashlib.sha256(legacy_model.read_bytes()).hexdigest() == (
        "44e3f6dd0f1b24ad4b7926fda9291b8f98368aefd0ad1f5822d0ebe4164f4744"
    )
    formal_model = Path(__file__).parents[1] / "ur10e_vic" / "tacdiffusion" / "formal_model.py"
    assert formal_model != legacy_model
    assert "FORMAL_OBSERVATION_DIMENSION" in formal_model.read_text(encoding="utf-8")
