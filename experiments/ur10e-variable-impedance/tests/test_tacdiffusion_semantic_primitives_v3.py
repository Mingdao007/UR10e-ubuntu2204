"""Focused offline proof for typed dynamics/action contracts and episode v3."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from ur10e_vic.tacdiffusion.action import ActionProfile
from ur10e_vic.tacdiffusion.contracts import (
    DynamicsConformanceBinding,
    DynamicsReceipt,
    DynamicsSample,
    OFFLINE_FAKE_RTDE_FIXTURE_ID,
)
from ur10e_vic.tacdiffusion.eligibility import EligibilityValidator
from ur10e_vic.tacdiffusion.episode_composition import (
    ActiveTrainingWindow,
    ActionLabelContext,
    ActionLabelProvider,
    CANONICAL_EXPERT_ACTION_SEMANTICS,
    CANONICAL_EXPERT_POLICY_ID,
    DeterministicExpertActionProvider,
    DiagnosticShadowActionProvider,
    EpisodeSemanticContext,
    FakeRTDEDynamicsProvider,
    InternalWrenchReconstructionProvider,
)
from ur10e_vic.tacdiffusion.episode_recorder import (
    EPISODE_ARTIFACT_SCHEMA_V2,
    EpisodeReceipt,
    EpisodeFrameV3,
    EpisodeRecorder,
    compute_v3_row_sha256,
    read_episode_artifact,
    validate_sealed_episode_manifest,
)
from ur10e_vic.tacdiffusion.expert import ExpertInput


HASHES = {
    "source": "a" * 64,
    "model": "b" * 64,
    "tcp": "c" * 64,
    "calibration": "d" * 64,
}


def _sample(
    sequence: int = 0,
    timestamp_s: float = 0.002,
    *,
    actual_current: tuple[float, ...] | None = None,
    jacobian_frame_id: str = "tool0_tcp",
) -> DynamicsSample:
    target = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    coriolis = (0.2,) * 6
    damping = (0.1,) * 6
    commanded = tuple(torque + c - d for torque, c, d in zip(target, coriolis, damping))
    return DynamicsSample(
        sequence=sequence,
        timestamp_s=timestamp_s,
        previous_q=(0.1,) * 6,
        previous_qd=(0.2,) * 6,
        previous_commanded_no_gravity_torque_nm=commanded,
        calibrated_jacobian=np.eye(6).tolist(),
        coriolis_torque_nm=coriolis,
        joint_damping_torque_nm=damping,
        source_hashes=HASHES,
        model_identity="model-v1",
        tcp_identity="tcp-v1",
        calibration_identity="calibration-v1",
        frame_id="tool0_tcp",
        canonical_tcp_frame_id="tool0_tcp",
        jacobian_frame_id=jacobian_frame_id,
        source_identity="offline-fake-rtde",
        actual_current_torque_nm_shadow=actual_current,
    )


def _context() -> EpisodeSemanticContext:
    bindings = {
        "episode_id": "typed-v3",
        "dataset_split": "train",
        "capture_kind": "offline_typed_fixture",
        "training_eligible": True,
        "surface_manifest_sha256": "1" * 64,
        "sensor_calibration_sha256": "2" * 64,
        "wrench_bias_sha256": "3" * 64,
        "normalization_sha256": "4" * 64,
        "action_profile_sha256": "5" * 64,
        "filter_profile_sha256": "6" * 64,
        "controller_identity_sha256": "7" * 64,
        "receiver_identity_sha256": "8" * 64,
    }
    from ur10e_vic.tacdiffusion.expert_episode_artifact import ExpertEpisodeBindings

    return EpisodeSemanticContext.from_training_bindings(
        bindings=ExpertEpisodeBindings(**bindings),
        hash_identities={
            "receiver_source_sha256": "9" * 64,
            "bundle_reference_sha256": "a" * 64,
            "kunwei_calibration_sha256": "b" * 64,
            "runtime_source_sha256": "c" * 64,
        },
    )


def _frame(context: EpisodeSemanticContext) -> EpisodeFrameV3:
    sample = _sample()
    dynamics = FakeRTDEDynamicsProvider().produce(sample)
    expert_input = ExpertInput(
        normal_load_n=0.0,
        target_load_n=2.0,
        pose_error=(0.0,) * 6,
        twist=(0.0,) * 6,
        path_progress=0.0,
        tangential_speed_m_s=0.01,
    )
    action_context = ActionLabelContext.expert(
        sequence=0,
        timestamp_s=0.002,
        frame_id="tool0_tcp",
        expert_input=expert_input,
        dynamics_receipt=dynamics,
        semantic_context_fingerprint_sha256=context.fingerprint_sha256,
    )
    label = DeterministicExpertActionProvider().produce(action_context)
    return EpisodeFrameV3(
        episode_id="typed-v3",
        sample_index=0,
        control_sequence=0,
        control_time_s=0.002,
        observation_84d=(0.0,) * 84,
        expert_action_12d=label.expert_action_12d,
        applied_action_12d=label.expert_action_12d,
        echoed_action_12d=label.expert_action_12d,
        action_generation=1,
        action_age_ticks=0,
        action_echo_coherent=True,
        external_device_time_s=0.001,
        external_host_visible_time_s=0.001,
        external_batch_id=0,
        external_sample_index=0,
        external_hold=False,
        external_held_ticks=0,
        device_age_samples=0,
        host_age_s=0.001,
        internal_wrench_valid=True,
        external_lineage_valid=True,
        controller_time_s=0.002,
        control_clock="offline_monotonic",
        expert_label_available=True,
        expert_action_source="deterministic_expert",
        action_label_semantics=CANONICAL_EXPERT_ACTION_SEMANTICS,
        controller_echo_12d=label.expert_action_12d,
        observation_history_valid=True,
        reference_derivatives_valid=True,
        desired_pose_6d=(0.0,) * 6,
        desired_twist_6d=(0.0,) * 6,
        desired_acceleration_6d=(0.0,) * 6,
        reference_sample_id="reference:0",
        candidate_window=True,
        capture_phase="active_torque",
        dynamics_sample=sample,
        dynamics_receipt=dynamics,
        action_label_context=action_context,
        action_label=label,
        identity_enabled=True,
        semantic_context_fingerprint_sha256=context.fingerprint_sha256,
    )


def test_known_wrench_authority_and_actual_current_shadow() -> None:
    sample = _sample(actual_current=tuple(value + 1.0 for value in (1, 2, 3, 4, 5, 6)))
    receipt = FakeRTDEDynamicsProvider().produce(sample)
    assert receipt.valid is True
    assert receipt.authoritative_torque_source == "previous_commanded_no_gravity_torque"
    assert receipt.internal_wrench_tcp_si == pytest.approx((1, 2, 3, 4, 5, 6))
    assert receipt.actual_current_shadow_wrench_tcp_si != receipt.internal_wrench_tcp_si

    production = InternalWrenchReconstructionProvider().produce(sample)
    assert production.valid is False
    assert production.reason == "missing_or_invalid_controller_conformance_binding"

    binding = DynamicsConformanceBinding(
        binding_id="controller-binding-v1",
        controller_receipt_identity="controller-receipt-v1",
        controller_receipt_sha256="e" * 64,
        sample_fingerprint_sha256=sample.fingerprint_sha256,
        model_identity=sample.model_identity,
        tcp_identity=sample.tcp_identity,
        calibration_identity=sample.calibration_identity,
    )
    conformed = InternalWrenchReconstructionProvider().produce(
        sample,
        conformance_binding=binding,
    )
    assert conformed.valid is True


def test_dynamics_frames_temporal_lineage_and_tamper_fail_closed() -> None:
    with pytest.raises(ValueError, match="Jacobian frame"):
        _sample(jacobian_frame_id="base")
    first = _sample()
    with pytest.raises(ValueError, match="nonconsecutive"):
        FakeRTDEDynamicsProvider().produce(_sample(sequence=2, timestamp_s=0.006), previous_sample=first)
    with pytest.raises(ValueError, match="non-monotonic"):
        FakeRTDEDynamicsProvider().produce(_sample(sequence=1, timestamp_s=0.002), previous_sample=first)
    with pytest.raises(ValueError, match="stale"):
        DynamicsReceipt.from_sample(first, now_s=1.0)
    receipt = FakeRTDEDynamicsProvider().produce(first)
    with pytest.raises(ValueError, match="model_identity"):
        receipt.validate_against(replace(first, model_identity="tampered-model"))
    with pytest.raises(ValueError, match="source_hashes"):
        receipt.validate_against(replace(first, source_hashes={"source": "f" * 64}))
    with pytest.raises(ValueError, match="binding hash"):
        replace(
            DynamicsConformanceBinding(
                binding_id="controller-binding-v1",
                controller_receipt_identity="controller-receipt-v1",
                controller_receipt_sha256="e" * 64,
                sample_fingerprint_sha256=first.fingerprint_sha256,
                model_identity=first.model_identity,
                tcp_identity=first.tcp_identity,
                calibration_identity=first.calibration_identity,
            ),
            controller_receipt_identity="tampered",
        )


def test_action_provider_contract_state_frame_bounds_and_slew() -> None:
    provider = DeterministicExpertActionProvider()
    assert isinstance(provider, ActionLabelProvider)
    acquire = provider.produce(
        ActionLabelContext.expert(
            sequence=0,
            timestamp_s=0.0,
            frame_id="tool0_tcp",
            expert_input=ExpertInput(0.0, 5.0, (0.0,) * 6, (0.0,) * 6, 0.0, 0.01),
        )
    )
    track = provider.produce(
        ActionLabelContext.expert(
            sequence=1,
            timestamp_s=0.002,
            frame_id="tool0_tcp",
            expert_input=ExpertInput(4.0, 5.0, (0.0,) * 6, (0.0,) * 6, 0.2, 0.01),
            previous_action_12d=acquire.expert_action_12d,
        )
    )
    assert acquire.available and track.available
    assert track.state_id == "TRACK"
    assert all(abs(value) < 1000.0 for value in track.expert_action_12d)
    assert max(abs(left - right) for left, right in zip(acquire.expert_action_12d, track.expert_action_12d)) <= 20.0
    with pytest.raises(ValueError, match="nonconsecutive"):
        provider.produce(
            ActionLabelContext.expert(
                sequence=3,
                timestamp_s=0.006,
                frame_id="tool0_tcp",
                expert_input=ExpertInput(4.0, 5.0, (0.0,) * 6, (0.0,) * 6, 0.2, 0.01),
            )
        )
    with pytest.raises(ValueError, match="frame mismatch"):
        DeterministicExpertActionProvider().produce(
            ActionLabelContext.expert(
                sequence=0,
                timestamp_s=0.0,
                frame_id="base",
                expert_input=ExpertInput(0.0, 5.0, (0.0,) * 6, (0.0,) * 6, 0.0, 0.01),
            )
        )
    diagnostic = DiagnosticShadowActionProvider().produce(
        ActionLabelContext.diagnostic(
            sequence=0,
            timestamp_s=0.0,
            frame_id="tool0_tcp",
            applied_action_12d=(1.0,) * 12,
            echoed_action_12d=(1.0,) * 12,
        )
    )
    assert diagnostic.available is False and diagnostic.shadow_only is True


def test_v3_seal_tail_tamper_and_eligibility_fail_closed(tmp_path: Path) -> None:
    context = _context()
    recorder = EpisodeRecorder(tmp_path, episode_id="typed-v3", semantic_context=context)
    recorder.start()
    assert recorder.enqueue(_frame(context))
    manifest = recorder.close(seal=True)
    assert manifest is not None
    header, rows = read_episode_artifact(recorder.artifact_path)
    assert header["schema"] == "ur10e_tacdiffusion_episode_artifact/v3"
    assert len(rows) == 1
    assert rows[0]["observation_84d"] == [0.0] * 84
    assert rows[0]["dynamics_receipt"]["valid"] is True
    assert rows[0]["row_sha256"] == compute_v3_row_sha256(rows[0])
    assert validate_sealed_episode_manifest(recorder.artifact_path, recorder.manifest_path) == manifest
    decision = EligibilityValidator().evaluate(
        rows,
        recorder_health=recorder.health(),
        first_live_shadow=False,
        active_window=ActiveTrainingWindow(0, 0),
        semantic_context=context,
    )
    assert decision.training_eligible is True

    disabled = dict(rows[0])
    disabled["identity_enabled"] = False
    assert not EligibilityValidator().evaluate(
        (disabled,), recorder_health=recorder.health(), first_live_shadow=False, semantic_context=context
    ).training_eligible
    invalid_dynamics = dict(rows[0])
    invalid_dynamics["dynamics_receipt"] = dict(invalid_dynamics["dynamics_receipt"])
    invalid_dynamics["dynamics_receipt"]["valid"] = False
    assert not EligibilityValidator().evaluate(
        (invalid_dynamics,), recorder_health=recorder.health(), first_live_shadow=False, semantic_context=context
    ).training_eligible

    raw = recorder.artifact_path.read_bytes()
    recorder.artifact_path.write_bytes(raw.rsplit(b"\n", 2)[0] + b"\n")
    with pytest.raises(ValueError, match="tail|hash"):
        read_episode_artifact(recorder.artifact_path)


def test_v3_receipt_serialization_and_exact_row_seal() -> None:
    context = _context()
    frame = replace(
        _frame(context),
        observation_receipt=EpisodeReceipt(
            "observation_fixture/v1",
            {"z": 2, "a": [1.0, 2.0], "nested": {"ok": True}},
        ),
        reference_receipt=EpisodeReceipt(
            "reference_fixture/v1",
            {"reference_id": "reference:0", "sequence": 0},
        ),
    )
    observation = frame.observation_receipt
    assert observation is not None
    assert observation.as_json() == {
        "schema": "observation_fixture/v1",
        "payload": {"a": [1.0, 2.0], "nested": {"ok": True}, "z": 2},
    }
    assert observation.canonical_bytes() == json.dumps(
        observation.as_json(), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    with pytest.raises(ValueError, match="schema"):
        EpisodeReceipt("", {})
    payload = frame.as_json()
    assert payload["observation_receipt"]["schema"] == "observation_fixture/v1"
    assert payload["reference_receipt"]["schema"] == "reference_fixture/v1"
    unsigned = dict(payload)
    supplied = unsigned.pop("row_sha256")
    exact = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    assert supplied == exact == compute_v3_row_sha256(payload)
    assert frame.row_seal_valid is True


def test_v3_row_seal_tamper_rejected_and_old_v3_without_seal_readable(tmp_path: Path) -> None:
    context = _context()
    recorder = EpisodeRecorder(tmp_path / "new", episode_id="typed-v3", semantic_context=context)
    recorder.start()
    assert recorder.enqueue(_frame(context))
    recorder.close(seal=True)
    original_lines = recorder.artifact_path.read_bytes().splitlines(keepends=True)

    valid_row = json.loads(original_lines[1])
    tampered_row = json.loads(original_lines[1])
    tampered_row["observation_84d"][0] = 1.0
    original_lines[1] = (
        json.dumps(tampered_row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        + b"\n"
    )
    recorder.artifact_path.write_bytes(b"".join(original_lines))
    with pytest.raises(ValueError, match="row seal mismatch"):
        read_episode_artifact(recorder.artifact_path)

    # Restore the original row content, then emulate an already-written v3
    # artifact from before the row-seal field existed.
    old_row = valid_row
    old_row.pop("row_sha256", None)
    old_row_line = (
        json.dumps(old_row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        + b"\n"
    )
    old_tail = json.loads(original_lines[-1])
    old_tail["content_sha256"] = hashlib.sha256(old_row_line).hexdigest()
    old_tail_unsigned = dict(old_tail)
    old_tail_unsigned.pop("tail_sha256", None)
    old_tail["tail_sha256"] = hashlib.sha256(
        (
            json.dumps(old_tail_unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    legacy = tmp_path / "legacy-v3.jsonl"
    legacy.write_bytes(original_lines[0] + old_row_line + (
        json.dumps(old_tail, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        + b"\n"
    ))
    header, rows = read_episode_artifact(legacy)
    assert header["schema"] == "ur10e_tacdiffusion_episode_artifact/v3"
    assert len(rows) == 1
    assert "row_sha256" not in rows[0]


def test_v1_and_v2_artifacts_remain_readable(tmp_path: Path) -> None:
    v2 = tmp_path / "legacy-v2.jsonl"
    v2.write_text(
        "\n".join(
            (
                json.dumps({"schema": EPISODE_ARTIFACT_SCHEMA_V2, "format_version": 2}),
                json.dumps({"schema": "ur10e_tacdiffusion_episode_frame/v2", "sample_index": 0}),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    header, rows = read_episode_artifact(v2)
    assert header["schema"] == EPISODE_ARTIFACT_SCHEMA_V2
    assert len(rows) == 1
