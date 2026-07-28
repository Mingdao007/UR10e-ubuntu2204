"""Focused proof for the canonical semantic/storage episode composition."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ur10e_vic.tacdiffusion.eligibility import EligibilityValidator
from ur10e_vic.tacdiffusion.episode_composition import (
    ActiveTrainingWindow,
    CANONICAL_EXPERT_ACTION_SEMANTICS,
    CANONICAL_EXPERT_POLICY_ID,
    CausalKunweiAlignmentAdapter,
    DiagnosticShadowActionProvider,
    DeterministicExpertActionProvider,
    EpisodeSemanticContext,
    InternalWrenchReconstructionProvider,
    SHADOW_TRANSITION_SCHEMA,
    first_live_shadow_from_receipt,
    resolve_active_training_window,
)
from ur10e_vic.tacdiffusion.episode_recorder import (
    DURABILITY_MODE,
    MAX_UNSEALED_TAIL,
    SPOOL_CAPACITY,
    EpisodeFrameV2,
    EpisodeRecorder,
    RecorderHealth,
    read_episode_artifact,
    validate_sealed_episode_manifest,
)
from ur10e_vic.tacdiffusion.expert import ExpertInput
from ur10e_vic.tacdiffusion.expert_episode_artifact import (
    DurableExpertEpisodeWriter,
    ExpertEpisodeBindings,
    ExpertEpisodeFrame,
    read_expert_episode,
)
from ur10e_vic.tacdiffusion.mainline_dataset import (
    validate_mainline_dataset,
    write_mainline_dataset,
)


SHA = "a" * 64


def _runtime_hashes() -> dict[str, str]:
    return {
        "receiver_source_sha256": "1" * 64,
        "bundle_reference_sha256": "2" * 64,
        "kunwei_calibration_sha256": "3" * 64,
        "runtime_source_sha256": "4" * 64,
    }


def _bindings() -> ExpertEpisodeBindings:
    return ExpertEpisodeBindings(
        episode_id="episode-composition",
        dataset_split="train",
        capture_kind="deterministic_expert_offline",
        training_eligible=True,
        surface_manifest_sha256="5" * 64,
        sensor_calibration_sha256="6" * 64,
        wrench_bias_sha256="7" * 64,
        normalization_sha256="8" * 64,
        action_profile_sha256="9" * 64,
        filter_profile_sha256="a" * 64,
        controller_identity_sha256="b" * 64,
        receiver_identity_sha256="c" * 64,
    )


def _training_context() -> EpisodeSemanticContext:
    return EpisodeSemanticContext.from_training_bindings(
        bindings=_bindings(),
        hash_identities=_runtime_hashes(),
    )


def _health(row_count: int) -> RecorderHealth:
    return RecorderHealth(
        capacity=SPOOL_CAPACITY,
        queue_depth=0,
        enqueued_rows=row_count,
        durable_rows=row_count,
        rejected_rows=0,
        dropped_rows=0,
        overflowed=False,
        stalled=False,
        writer_error=None,
        fault=None,
        durability_mode=DURABILITY_MODE,
        unsealed_tail=0,
        max_unsealed_tail=MAX_UNSEALED_TAIL,
        sealed=True,
        manifest_written=True,
        tamper_free=True,
    )


def _frame(
    index: int,
    *,
    candidate: bool,
    valid: bool = True,
    diagnostic: bool = False,
    reference_valid: bool = True,
    internal_valid: bool = True,
    history_valid: bool = True,
) -> EpisodeFrameV2:
    control = 1.0 + index * 0.002
    action = tuple(float(index + axis) for axis in range(12))
    applied = action if not diagnostic else tuple(float(axis + 1) for axis in range(12))
    return EpisodeFrameV2(
        episode_id="episode-composition",
        sample_index=index,
        control_sequence=index + 1,
        control_time_s=control,
        controller_time_s=control,
        control_clock="controller_timestamp_mapped_to_host_monotonic",
        observation_84d=(float(index),) * 84,
        expert_action_12d=(0.0,) * 12 if diagnostic else action,
        applied_action_12d=applied,
        echoed_action_12d=applied,
        action_generation=1,
        action_age_ticks=0,
        action_echo_coherent=valid,
        external_device_time_s=0.9 + index * 0.001,
        external_host_visible_time_s=control - 0.001,
        external_batch_id=index + 1,
        external_sample_index=index + 1,
        external_hold=False,
        external_held_ticks=0,
        device_age_samples=0,
        host_age_s=0.001,
        internal_wrench_valid=internal_valid and valid,
        external_lineage_valid=valid,
        source_row_torn=not valid,
        source_row_invalid=not valid,
        echoed_action_valid=valid,
        recorder_valid=valid,
        expert_label_available=not diagnostic,
        expert_action_source=(
            "diagnostic_zero6_fixed_stiffness_shadow"
            if diagnostic
            else "deterministic_expert"
        ),
        action_label_semantics=(
            "diagnostic_command_not_expert_shadow_only_v1"
            if diagnostic
            else CANONICAL_EXPERT_ACTION_SEMANTICS
        ),
        diagnostic_command_12d=applied if diagnostic else None,
        controller_echo_12d=applied,
        observation_history_valid=history_valid,
        reference_derivatives_valid=reference_valid,
        desired_pose_6d=(0.0,) * 6,
        desired_twist_6d=(0.0,) * 6,
        desired_acceleration_6d=(0.0,) * 6,
        reference_sample_id=f"reference:{index}",
        candidate_window=candidate,
        capture_phase="active_torque" if candidate else "warmup",
    )


def test_legacy_v1_writer_and_reader_remain_compatible(tmp_path: Path) -> None:
    artifact = tmp_path / "legacy.jsonl"
    manifest = tmp_path / "legacy.manifest.json"
    writer = DurableExpertEpisodeWriter(artifact, bindings=_bindings())
    writer.append(
        ExpertEpisodeFrame(
            episode_id="episode-composition",
            sample_index=0,
            control_sequence=0,
            control_timestamp_s=1.0,
            external_source_sequence=0,
            external_source_timestamp_s=0.999,
            observation_84d=(0.0,) * 84,
            expert_action_12d=(0.0,) * 12,
            applied_action_12d=(0.0,) * 12,
            filtered_f_ff=(0.0,) * 6,
            filter_velocity=(0.0,) * 6,
            receiver_ack_sequence=0,
        )
    )
    writer.finalize(manifest)
    recovered_bindings, recovered = read_expert_episode(artifact)
    header, rows = read_episode_artifact(artifact)
    assert recovered_bindings.capture_kind == "deterministic_expert_offline"
    assert len(recovered) == 1
    assert header["schema"] == "ur10e_tacdiffusion_expert_episode/v1"
    assert len(rows) == 1


def test_diagnostic_command_is_shadow_only_and_ineligible() -> None:
    provider = DiagnosticShadowActionProvider()
    label = provider.produce((1.0,) * 12, (2.0,) * 12)
    assert label.available is False
    assert label.shadow_only is True
    assert label.expert_action_12d == (0.0,) * 12
    assert label.source == "diagnostic_zero6_fixed_stiffness_shadow"

    context = EpisodeSemanticContext.diagnostic(
        episode_id="episode-composition",
        capture_kind="direct_torque_diagnostic_shadow",
        hash_identities=_runtime_hashes(),
    )
    frame = _frame(0, candidate=True, diagnostic=True, internal_valid=False)
    decision = EligibilityValidator().evaluate(
        (frame,),
        recorder_health=_health(1),
        first_live_shadow=False,
        active_window=ActiveTrainingWindow(0, 0),
        semantic_context=context,
    )
    assert decision.training_eligible is False
    assert decision.predicates["expert_label_available"] is False
    assert decision.predicates["expert_policy_authoritative"] is False
    assert "semantic_bindings_complete" in decision.reasons


def test_deterministic_expert_is_the_offline_action_provider() -> None:
    label = DeterministicExpertActionProvider().produce(
        ExpertInput(
            normal_load_n=0.0,
            target_load_n=2.0,
            pose_error=(0.0,) * 6,
            twist=(0.0,) * 6,
            path_progress=0.0,
            tangential_speed_m_s=0.0,
            desired_twist=(0.01,) * 6,
            desired_acceleration=(0.0,) * 6,
        )
    )
    assert label.available is True
    assert label.shadow_only is False
    assert label.policy_id == CANONICAL_EXPERT_POLICY_ID
    assert label.semantics == CANONICAL_EXPERT_ACTION_SEMANTICS
    assert label.source == "deterministic_expert"


def test_active_window_excludes_invalid_warmup_but_retains_it() -> None:
    rows = (
        _frame(0, candidate=False, valid=False),
        _frame(1, candidate=True),
        _frame(2, candidate=True),
    )
    window = resolve_active_training_window(rows)
    assert window.start_sample_index == 1
    assert window.end_sample_index == 2
    assert window.warmup_sample_count == 1
    decision = EligibilityValidator().evaluate(
        rows,
        recorder_health=_health(3),
        first_live_shadow=False,
        active_window=window,
        semantic_context=_training_context(),
    )
    assert decision.training_eligible is True
    assert decision.predicates["retained_rows_valid"] is False
    assert decision.predicates["candidate_window_rows_valid"] is True
    assert decision.data_quality is True


def test_causal_adapter_reports_repeated_batch_hold_and_real_ages() -> None:
    adapter = CausalKunweiAlignmentAdapter(
        expected_frame_id="tool0_tcp",
        calibration_sha256=SHA,
    )
    adapter.align(
        control_timestamp_s=100.000,
        device_time_s=10.000,
        host_visible_time_s=100.000,
        batch_id=7,
        sample_index=0,
        source_sequence=0,
        wrench_tcp_si=(0.0,) * 6,
    )
    adapter.align(
        control_timestamp_s=100.002,
        device_time_s=10.001,
        host_visible_time_s=100.002,
        batch_id=8,
        sample_index=1,
        source_sequence=1,
        wrench_tcp_si=(1.0,) * 6,
    )
    held = adapter.align(
        control_timestamp_s=100.004,
        device_time_s=10.001,
        host_visible_time_s=100.002,
        batch_id=8,
        sample_index=1,
        source_sequence=1,
        wrench_tcp_si=(1.0,) * 6,
    )
    assert held is not None
    assert held.external_hold is True
    assert held.external_held_ticks == 1
    assert held.device_age_samples == 2
    assert held.host_age_s == pytest.approx(0.002)
    assert held.external_sample_index == 1
    assert adapter.last_error is None


def test_causal_adapter_keeps_the_accepted_join_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ur10e_vic.tacdiffusion.episode_composition as composition

    original_join = composition.causal_sync_wrench_1khz_to_control_500hz
    input_sizes: list[tuple[int, int]] = []

    def bounded_join(samples: object, ticks: object, **kwargs: object) -> object:
        input_sizes.append((len(samples), len(ticks)))  # type: ignore[arg-type]
        return original_join(samples, ticks, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        composition,
        "causal_sync_wrench_1khz_to_control_500hz",
        bounded_join,
    )
    adapter = CausalKunweiAlignmentAdapter(
        expected_frame_id="tool0_tcp",
        calibration_sha256=SHA,
    )
    for index in range(100):
        control = 100.0 + index * 0.002
        assert adapter.align(
            control_timestamp_s=control,
            device_time_s=10.0 + index * 0.001,
            host_visible_time_s=control,
            batch_id=index,
            sample_index=index,
            source_sequence=index,
            wrench_tcp_si=(float(index),) * 6,
        ) is not None
    assert adapter.sample_count == 100
    assert max(sample_count for sample_count, _ in input_sizes) <= 2
    assert max(tick_count for _, tick_count in input_sizes) <= 2


def test_causal_adapter_rejects_changed_payload_for_repeated_lineage() -> None:
    adapter = CausalKunweiAlignmentAdapter(
        expected_frame_id="tool0_tcp",
        calibration_sha256=SHA,
    )
    assert adapter.align(
        control_timestamp_s=100.0,
        device_time_s=10.0,
        host_visible_time_s=100.0,
        batch_id=7,
        sample_index=0,
        source_sequence=0,
        wrench_tcp_si=(0.0,) * 6,
    ) is not None
    assert adapter.align(
        control_timestamp_s=100.002,
        device_time_s=10.0,
        host_visible_time_s=100.0,
        batch_id=7,
        sample_index=0,
        source_sequence=0,
        wrench_tcp_si=(1.0,) * 6,
    ) is None
    assert adapter.last_error is not None
    assert "changed its lineage" in adapter.last_error
    assert adapter.fault == adapter.last_error
    assert adapter.align(
        control_timestamp_s=100.004,
        device_time_s=10.001,
        host_visible_time_s=100.004,
        batch_id=8,
        sample_index=1,
        source_sequence=1,
        wrench_tcp_si=(1.0,) * 6,
    ) is not None
    assert adapter.last_error is None
    assert adapter.fault is not None


def test_shadow_transition_is_receipt_driven_and_hash_checked(
    tmp_path: Path,
) -> None:
    assert first_live_shadow_from_receipt(None) is True
    receipt = {
        "schema": SHADOW_TRANSITION_SCHEMA,
        "first_live_shadow_complete": True,
        "training_transition_authorized": True,
    }
    canonical = json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    receipt["receipt_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    path = tmp_path / "shadow-transition.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert first_live_shadow_from_receipt(path) is False
    receipt["training_transition_authorized"] = False
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="authorization"):
        first_live_shadow_from_receipt(path)


def test_missing_binding_internal_wrench_and_reference_derivative_fail_closed() -> None:
    context = EpisodeSemanticContext.diagnostic(
        episode_id="episode-composition",
        capture_kind="diagnostic",
        hash_identities=_runtime_hashes(),
    )
    assert context.bindings_complete is False
    assert InternalWrenchReconstructionProvider().reconstruct({}).valid is False
    with pytest.raises(ValueError, match="missing runtime hash bindings"):
        EpisodeSemanticContext.from_training_bindings(
            bindings=_bindings(),
            hash_identities={"receiver_source_sha256": "1" * 64},
        )
    frame = _frame(
        0,
        candidate=True,
        reference_valid=False,
        internal_valid=False,
    )
    decision = EligibilityValidator().evaluate(
        (frame,),
        recorder_health=_health(1),
        first_live_shadow=False,
        active_window=ActiveTrainingWindow(0, 0),
        semantic_context=context,
    )
    assert decision.training_eligible is False
    assert decision.predicates["semantic_bindings_complete"] is False
    assert decision.predicates["internal_wrench_valid"] is False
    assert decision.predicates["reference_derivatives_valid"] is False


def test_complete_seal_and_downstream_mainline_reader_compatibility(tmp_path: Path) -> None:
    context = _training_context()
    recorder = EpisodeRecorder(
        tmp_path / "episode",
        episode_id="episode-composition",
        metadata={"semantic_context": context.as_metadata()},
    )
    recorder.start()
    frame = _frame(0, candidate=True)
    assert recorder.enqueue(frame) is True
    recorder.close(seal=True)
    validate_sealed_episode_manifest(recorder.artifact_path, recorder.manifest_path)
    health = recorder.health()
    assert health.sealed is True
    assert health.durable_rows == 1
    header, rows = read_episode_artifact(recorder.artifact_path)
    assert header["metadata"]["semantic_context"]["training_candidate"] is True
    assert len(rows) == 1

    dataset_path = tmp_path / "mainline.npz"
    manifest = write_mainline_dataset(
        dataset_path,
        observations=[rows[0]["observation_84d"]],
        actions=[rows[0]["expert_action_12d"]],
        episode_ids=["episode-composition"],
        splits=["train"],
        timestamps_s=[0.002],
        source_raw_artifact_hashes={"episode-composition": "d" * 64},
        surface_calibration_sha256="e" * 64,
        action_profile_sha256="f" * 64,
        filter_profile_sha256="1" * 64,
        normalization_sha256="2" * 64,
    )
    assert validate_mainline_dataset(dataset_path, manifest)["row_count"] == 1
