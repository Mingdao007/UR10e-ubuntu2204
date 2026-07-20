from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_batch_plan import CandidateBatchPlan  # noqa: E402
from step5d_autotune_contract import (  # noqa: E402
    CaptureManifest,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    SafeClosureEvidence,
    TypedSafeClosureEvidence,
    TrialDisposition,
)
from step5d_autotune_live_driver import (  # noqa: E402
    CampaignHomeReference,
    ImmutableBundleStoreReceipt,
    MailboxError,
)
import step5d_autotune_runtime_lifecycle as runtime_lifecycle  # noqa: E402
import build_step5d_autotune_tp_v3 as tp_v3  # noqa: E402
from step5d_autotune_runtime_lifecycle import (  # noqa: E402
    PostAckControllerReadback,
    PostAckClosureCollector,
    PreAckTypedClosureCollector,
    _persist_post_ack_readback,
    next_runtime_batch_candidate,
    prepare_batch_attempt_context,
    recover_runtime_batch_trial_briefs,
)
from ur10e_experiment_runtime import (  # noqa: E402
    ExactAckReceipt,
    SafeClosureReceipt,
    return_reference,
)
from ur10e_experiment_runtime.batch import ReturnReferenceKind  # noqa: E402
from ur10e_experiment_runtime.physical_prior import (  # noqa: E402
    STEP5D_V3_PHYSICAL_PRIOR,
)
from ur10e_experiment_runtime.stage_adapters import (  # noqa: E402
    control_candidate_uid,
)
from step5d_autotune_state_machine import HostCommand, HostPacket  # noqa: E402


def test_campaign_home_equivalence_accepts_rtde_jitter_and_rejects_changed_home(
    tmp_path: Path,
) -> None:
    reference = CampaignHomeReference(
        path=(tmp_path / "campaign-home-reference.json").resolve(),
        sha256="a" * 64,
        backend_id="backend",
        campaign_epoch=1,
        campaign_fingerprint="a" * 64,
        source_fingerprint="b" * 64,
        config_fingerprint="c" * 64,
        home_pose=(0.45, 0.10, 0.055, 3.128, 0.0, 0.042),
        home_q=(0.62, -1.65, -2.55, -0.49, 1.55, -0.95),
        ready_consumed_command_seq=0,
        controller_timestamp_s=1.0,
        connection_epoch=1,
    )

    reference.verify_ready_home_equivalent(
        pose=(0.450020, 0.10, 0.055, 3.128, 0.000080, 0.042),
        joints=(0.6201, -1.65, -2.55, -0.49, 1.55, -0.95),
    )
    with pytest.raises(MailboxError, match="no longer matches durable campaign home"):
        reference.verify_ready_home_equivalent(
            pose=(0.454, 0.10, 0.055, 3.128, 0.0, 0.042),
            joints=reference.home_q,
        )


def _fixture(tmp_path: Path, row: int = 1):
    candidates = tuple(
        ForceCandidate.from_log2(
            p=(index - 1) * 0.25,
            i=0.0,
            damping=0.0,
        )
        for index in range(1, 11)
    )
    plan = CandidateBatchPlan(
        campaign_id="campaign",
        revision=1,
        closed=False,
        code_fix_replay_candidate_uid=None,
        batch_size=10,
        batches=(candidates,),
        payload={},
    )
    profile = ExecutionProfile("nf010-slew010-a010", 0.01)

    def overlay(candidate: ForceCandidate):
        control = {
            "force_p_gain": candidate.force_p_gain,
            "force_i_gain": candidate.force_i_gain,
            "force_damping": candidate.force_damping,
            "orientation_ko": 0.4,
        }
        return {
            **control,
            "control_candidate_uid": control_candidate_uid(control),
            "execution_profile_id": profile.profile_id,
            "step5d_preload_filtered_min_n": 5.0,
            "step5d_preload_filtered_max_n": 22.0,
            "step5d_preload_raw_min_n": 3.0,
            "step5d_preload_raw_max_n": 25.0,
            "step5d_preload_force_norm_max_n": 100.0,
            "step5d_preload_hold_s": 0.0,
            "step5d_preload_timeout_s": 60.0,
        }

    context = prepare_batch_attempt_context(
        plan=plan,
        selected_candidate=candidates[row - 1],
        profile=profile,
        overlay_resolver=overlay,
        campaign_uid="campaign",
        experiment_fingerprint="a" * 64,
        launch_fingerprint="b" * 64,
        controller_readback_fingerprint="c" * 64,
        authorization_ref_sha256="d" * 64,
        stopping_bound_fingerprint=None,
        plant_epoch=1,
        campaign_root=tmp_path,
        campaign_home_pose=(0.4, 0.1, 0.2, 3.14, 0.0, 0.0),
    )
    return context


def _valid_post_ack_readback(context, trial_uid: str) -> PostAckControllerReadback:
    return PostAckControllerReadback(
        batch_uid=context.identity.batch_uid,
        row_index=context.row_index,
        trial_uid=trial_uid,
        return_reference_uid=context.reference.reference_uid,
        return_reference=context.reference.kind,
        ack_command_seq=2,
        consumed_command_seq=2,
        tp_state=(
            "READY_HOME_CLOSED"
            if context.reference.kind is ReturnReferenceKind.CAMPAIGN_HOME
            else "READY_NEAR"
        ),
        batch_row_echo=context.row_index,
        return_kind_echo=context.reference.kind.value,
        return_guard_mask=0x7F,
        position_error_m=0.001,
        orientation_error_rad=0.01,
        tcp_linear_speed_m_s=0.0001,
        tcp_angular_speed_rad_s=0.001,
        qd_max_rad_s=0.001,
        return_phase_echo=40.3,
        return_segment_id=3,
        return_current_angular_speed_rad_s=0.0,
        return_current_angular_acceleration_rad_s2=0.0,
        return_max_angular_speed_rad_s=0.05,
        return_max_angular_acceleration_rad_s2=0.1,
        return_max_sample_gap_s=0.002,
        dwell_s=0.5,
        safety_mode="NORMAL",
        safety_guards={
            name: True
            for name in (
                "force",
                "torque",
                "joints",
                "sensor_freshness",
                "heartbeat",
                "contact_loss",
                "route_workspace",
            )
        },
        transcript_sha256="9" * 64,
    )


def test_exact_batch_context_selects_typed_return_reference(tmp_path: Path) -> None:
    context = _fixture(tmp_path)
    assert context.reference.kind is ReturnReferenceKind.NEAR_READY
    near = (
        *STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m,
        *STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad,
    )
    final_reference = return_reference(
        context.identity,
        10,
        near_ready_pose=near,
        campaign_home_pose=(0.4, 0.1, 0.2, 3.14, 0.0, 0.0),
    )
    assert final_reference.kind is ReturnReferenceKind.CAMPAIGN_HOME


def test_runtime_batch_resume_reselects_attempted_incomplete_row(tmp_path: Path) -> None:
    context = _fixture(tmp_path)
    context.journal.start_attempt(1, "1" * 64)
    candidates = tuple(
        ForceCandidate.from_log2(p=(index - 1) * 0.25, i=0.0, damping=0.0)
        for index in range(1, 11)
    )
    plan = CandidateBatchPlan(
        campaign_id="campaign",
        revision=1,
        closed=False,
        code_fix_replay_candidate_uid=None,
        batch_size=10,
        batches=(candidates,),
        payload={},
    )
    selected = next_runtime_batch_candidate(plan=plan, campaign_root=tmp_path)
    assert selected == candidates[0]


def test_runtime_batch_resume_refuses_durable_post_execution_row(tmp_path: Path) -> None:
    context = _fixture(tmp_path)
    trial_uid = "1" * 64
    context.journal.start_attempt(1, trial_uid)
    context.journal.record_bundle(1, trial_uid, "2" * 64)
    with pytest.raises(ValueError, match="phase-specific reconciliation"):
        _fixture(tmp_path)


def test_post_ack_readback_requires_all_return_guards() -> None:
    payload = {
        "batch_uid": "a" * 64,
        "row_index": 1,
        "trial_uid": "b" * 64,
        "return_reference_uid": "c" * 64,
        "return_reference": ReturnReferenceKind.NEAR_READY,
        "ack_command_seq": 2,
        "consumed_command_seq": 2,
        "tp_state": "READY_NEAR",
        "batch_row_echo": 1,
        "return_kind_echo": "near_ready",
        "return_guard_mask": 0x7F,
        "position_error_m": 0.001,
        "orientation_error_rad": 0.01,
        "tcp_linear_speed_m_s": 0.0001,
        "tcp_angular_speed_rad_s": 0.001,
        "qd_max_rad_s": 0.001,
        "return_phase_echo": 40.3,
        "return_segment_id": 3,
        "return_current_angular_speed_rad_s": 0.0,
        "return_current_angular_acceleration_rad_s2": 0.0,
        "return_max_angular_speed_rad_s": 0.05,
        "return_max_angular_acceleration_rad_s2": 0.1,
        "return_max_sample_gap_s": 0.002,
        "dwell_s": 0.5,
        "safety_mode": "NORMAL",
        "safety_guards": {
            "force": True,
            "torque": True,
            "joints": True,
            "sensor_freshness": True,
            "heartbeat": True,
            "contact_loss": True,
            "route_workspace": True,
        },
        "transcript_sha256": "d" * 64,
    }
    assert PostAckControllerReadback(**payload).controller_readback_sha256
    payload["return_guard_mask"] = 0x3F
    with pytest.raises(ValueError, match="guard mask"):
        PostAckControllerReadback(**payload)
    payload["return_guard_mask"] = 0x7F
    payload["return_max_angular_speed_rad_s"] = 0.061
    with pytest.raises(ValueError, match="frozen limit"):
        PostAckControllerReadback(**payload)


@pytest.mark.parametrize("tamper_before_first_validation", (False, True))
def test_trial_brief_is_durable_before_optimizer_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper_before_first_validation: bool,
) -> None:
    context = _fixture(tmp_path)
    trial_uid = "1" * 64
    source = "2" * 64
    config = "3" * 64
    trial = SimpleNamespace(
        trial_uid=trial_uid,
        source_fingerprint=source,
        config_fingerprint=config,
    )
    context.start_attempt(trial, context.expected_row.trial_overlay)
    bundle_path = (tmp_path / "bundle.json").resolve()
    bundle_path.write_bytes(b"{}\n")
    receipt = ImmutableBundleStoreReceipt(
        trial_uid=trial_uid,
        bundle_path=bundle_path,
        bundle_sha256="4" * 64,
        history_identity="5" * 64,
    )
    context.record_bundle(receipt)
    closure = SafeClosureEvidence(
        tp_position_error_m=0.001,
        tp_orientation_error_rad=0.01,
        tp_joint_error_max_rad=0.001,
        host_position_error_m=0.001,
        host_orientation_error_rad=0.01,
        host_joint_error_max_rad=0.001,
        host_tcp_linear_speed_m_s=0.0001,
        host_tcp_angular_speed_rad_s=0.001,
        host_qd_max_rad_s=0.001,
        host_safety_mode="NORMAL",
        host_dwell_s=0.5,
        trial_token_match=True,
        capture_hashes_complete=True,
        terminal_manifest_complete=True,
        fingerprint_closed=True,
    )
    manifest = CaptureManifest(
        trial_uid=trial_uid,
        backend_id="backend",
        source_fingerprint_pre=source,
        source_fingerprint_post=source,
        config_fingerprint_pre=config,
        config_fingerprint_post=config,
        candidate_token=1,
        terminal_reason=1,
        host_cause=None,
        csv_sha256="6" * 64,
        metadata_sha256="7" * 64,
        terminal_manifest_sha256="8" * 64,
        completion_marker=True,
        cadence_ok=True,
        feedback_fresh=True,
        rnn_oracle_aligned=True,
        safety_normal=True,
        returned_safe=True,
        immutable_bundle_written=True,
        stage25_complete_s=60.0,
        safe_closure_evidence=closure,
    )
    evaluation = Evaluation(
        trial_uid=trial_uid,
        backend_id="backend",
        eligible=True,
        disposition=TrialDisposition.OBJECTIVE,
        objective_mae_n=0.25,
        force_bias_n=0.0,
        force_std_n=0.1,
        coverage_12_plus_minus_1_ratio=1.0,
        complete_bins=550,
        safe_closure=True,
    )
    arm = HostPacket(1, 1, HostCommand.ARM, 1, 111, 1)
    ack = HostPacket(1, 1, HostCommand.ACK_BUNDLE, 1, 111, 2)
    readback = _valid_post_ack_readback(context, trial_uid)
    if tamper_before_first_validation:
        original_load = runtime_lifecycle._load_post_ack_readback
        tampered = False

        def tamper_then_load(*, campaign_root: Path, ack: ExactAckReceipt):
            nonlocal tampered
            if not tampered:
                tampered = True
                path = campaign_root / str(ack.controller_readback_path)
                path.chmod(0o644)
                path.write_text("{}\n", encoding="ascii")
            return original_load(campaign_root=campaign_root, ack=ack)

        monkeypatch.setattr(
            runtime_lifecycle,
            "_load_post_ack_readback",
            tamper_then_load,
        )
        with pytest.raises(ValueError, match="controller readback"):
            context.complete_post_ack(
                trial=trial,
                arm_packet=arm,
                ack_packet=ack,
                store_receipt=receipt,
                manifest=manifest,
                evaluation=evaluation,
                readback=readback,
            )
        row = context.journal.state().rows[0]
        assert row.ack_receipt_sha256 is None
        assert row.closure_receipt_sha256 is None
        assert not (tmp_path / "trial_briefs").exists()
        return

    admission = context.complete_post_ack(
        trial=trial,
        arm_packet=arm,
        ack_packet=ack,
        store_receipt=receipt,
        manifest=manifest,
        evaluation=evaluation,
        readback=readback,
    )
    assert admission.optimizer_eligible is True
    assert context.journal.state().rows[0].optimizer_eligible is True
    assert len(tuple((tmp_path / "trial_briefs").glob("*.json"))) == 1
    readback_paths = tuple((tmp_path / "post_ack_controller_readbacks").glob("*.json"))
    assert len(readback_paths) == 1
    ack_document = context.journal.state().rows[0].ack_receipt_document
    assert ack_document is not None
    assert ack_document["controller_readback_path"] == readback_paths[0].relative_to(
        tmp_path
    ).as_posix()


def test_trial_brief_recovery_closes_safe_closure_crash_cut_once(
    tmp_path: Path,
) -> None:
    context = _fixture(tmp_path)
    trial_uid = "1" * 64
    source = "2" * 64
    config = "3" * 64
    trial = SimpleNamespace(
        trial_uid=trial_uid,
        source_fingerprint=source,
        config_fingerprint=config,
    )
    context.start_attempt(trial, context.expected_row.trial_overlay)
    closure_evidence = SafeClosureEvidence(
        tp_position_error_m=0.001,
        tp_orientation_error_rad=0.01,
        tp_joint_error_max_rad=0.001,
        host_position_error_m=0.001,
        host_orientation_error_rad=0.01,
        host_joint_error_max_rad=0.001,
        host_tcp_linear_speed_m_s=0.0001,
        host_tcp_angular_speed_rad_s=0.001,
        host_qd_max_rad_s=0.001,
        host_safety_mode="NORMAL",
        host_dwell_s=0.5,
        trial_token_match=True,
        capture_hashes_complete=True,
        terminal_manifest_complete=True,
        fingerprint_closed=True,
    )
    manifest = CaptureManifest(
        trial_uid=trial_uid,
        backend_id="backend",
        source_fingerprint_pre=source,
        source_fingerprint_post=source,
        config_fingerprint_pre=config,
        config_fingerprint_post=config,
        candidate_token=1,
        terminal_reason=1,
        host_cause=None,
        csv_sha256="6" * 64,
        metadata_sha256="7" * 64,
        terminal_manifest_sha256="8" * 64,
        completion_marker=True,
        cadence_ok=True,
        feedback_fresh=True,
        rnn_oracle_aligned=True,
        safety_normal=True,
        returned_safe=True,
        immutable_bundle_written=True,
        stage25_complete_s=60.0,
        safe_closure_evidence=closure_evidence,
    )
    evaluation = Evaluation(
        trial_uid=trial_uid,
        backend_id="backend",
        eligible=True,
        disposition=TrialDisposition.OBJECTIVE,
        objective_mae_n=0.25,
        force_bias_n=0.0,
        force_std_n=0.1,
        coverage_12_plus_minus_1_ratio=1.0,
        complete_bins=550,
        safe_closure=True,
    )
    bundle_path = (
        tmp_path / "store" / "trials" / trial_uid / "immutable_trial_bundle.json"
    ).resolve()
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_text(
        json.dumps(
            {
                "trial": {
                    "trial_uid": trial_uid,
                    "source_fingerprint": source,
                    "config_fingerprint": config,
                },
                "capture": asdict(manifest),
                "evaluation": evaluation.history_payload(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="ascii",
    )
    bundle_sha = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    context.journal.record_bundle(1, trial_uid, bundle_sha)
    readback = _valid_post_ack_readback(context, trial_uid)
    readback_path, _ = _persist_post_ack_readback(
        campaign_root=tmp_path,
        readback=readback,
    )
    ack = ExactAckReceipt(
        batch_uid=context.identity.batch_uid,
        row_index=1,
        trial_uid=trial_uid,
        control_candidate_uid=context.expected_row.control_candidate_uid,
        immutable_bundle_sha256=bundle_sha,
        return_reference_uid=context.reference.reference_uid,
        controller_readback_sha256=readback.controller_readback_sha256,
        arm_command_seq=1,
        ack_command_seq=2,
        consumed_command_seq=2,
        controller_readback_path=readback_path,
    )
    context.journal.record_ack_consumed(ack)
    context.journal.record_safe_closure(
        SafeClosureReceipt(
            batch_uid=context.identity.batch_uid,
            row_index=1,
            trial_uid=trial_uid,
            ack_uid=ack.ack_uid,
            return_reference_uid=context.reference.reference_uid,
            controller_readback_sha256=readback.controller_readback_sha256,
            return_reference=ReturnReferenceKind.NEAR_READY,
        )
    )

    first = recover_runtime_batch_trial_briefs(campaign_root=tmp_path)
    second = recover_runtime_batch_trial_briefs(campaign_root=tmp_path)
    assert first[trial_uid] == second[trial_uid]
    assert context.journal.state().unpublished_trial_brief_row_indices == ()
    assert len(tuple((tmp_path / "trial_briefs").glob("*.json"))) == 1

    readback_artifact = next(
        (tmp_path / "post_ack_controller_readbacks").glob("*.json")
    )
    readback_artifact.chmod(0o644)
    readback_artifact.write_text("{}\n", encoding="ascii")
    with pytest.raises(ValueError, match="controller readback"):
        recover_runtime_batch_trial_briefs(campaign_root=tmp_path)


def test_post_ack_collector_requires_continuous_typed_controller_echo(
    tmp_path: Path,
) -> None:
    context = _fixture(tmp_path)
    trial = SimpleNamespace(
        campaign=SimpleNamespace(campaign_epoch=1),
        trial_id=1,
        candidate_token=1,
        trial_uid="1" * 64,
    )
    ack = HostPacket(1, 1, HostCommand.ACK_BUNDLE, 1, 111, 2)
    collector = PostAckClosureCollector(
        context=context,
        trial=trial,
        ack_packet=ack,
        max_sample_gap_s=0.02,
    )
    pose = (
        *context.reference.pose_xyz_m,
        *context.reference.pose_rotvec_rad,
    )
    for index in range(51):
        row = {
            "heartbeat": float(index),
            "normal_force_n": 0.0,
            "force_norm_n": 0.0,
            "torque_norm_nm": 0.0,
            "sensor_age_s": 0.001,
            "rtde_feedback_age_s": 0.001,
            "ur_timestamp": float(index) * 0.01,
            "ur_safety_mode": 1,
            "ur_output_int_register_24": 1,
            "ur_output_int_register_25": 1,
            "ur_output_int_register_26": 76,
            "ur_output_int_register_27": 1,
            "ur_output_int_register_28": 1,
            "ur_output_int_register_29": 111,
            "ur_output_int_register_30": 2,
            "ur_output_int_register_31": 1,
            "ur_output_int_register_32": 1,
            "ur_output_int_register_33": 0x7F,
            "output_double_register_35": 40.3,
            "output_double_register_39": 3,
            "output_double_register_40": 0.0,
            "output_double_register_41": 0.0,
            "output_double_register_42": 0.05,
            "output_double_register_43": 0.1,
            "output_double_register_44": 0.002,
        }
        row.update(
            {
                f"ur_actual_TCP_pose_{axis}": pose[axis]
                for axis in range(6)
            }
        )
        for prefix in ("ur_actual_TCP_speed", "ur_actual_q", "ur_actual_qd"):
            row.update({f"{prefix}_{axis}": 0.0 for axis in range(6)})
        collector.observe(row, monotonic_s=index * 0.01)

    readback = collector.finalize()
    assert readback.consumed_command_seq == ack.command_seq
    assert readback.return_guard_mask == 0x7F


def test_pre_ack_collector_uses_typed_reference_and_qd_not_fake_joint_error(
    tmp_path: Path,
) -> None:
    context = _fixture(tmp_path)
    campaign = SimpleNamespace(
        campaign_epoch=1,
        campaign_fingerprint="c" * 64,
    )
    trial = SimpleNamespace(
        backend_id="backend",
        campaign=campaign,
        source_fingerprint="d" * 64,
        config_fingerprint="e" * 64,
        trial_id=1,
        candidate_token=1,
        trial_uid="1" * 64,
    )
    home_path = (tmp_path / "campaign_home_reference.json").resolve()
    home_path.write_bytes(b"{}\n")
    home = CampaignHomeReference(
        path=home_path,
        sha256=hashlib.sha256(home_path.read_bytes()).hexdigest(),
        backend_id=trial.backend_id,
        campaign_epoch=campaign.campaign_epoch,
        campaign_fingerprint=campaign.campaign_fingerprint,
        source_fingerprint=trial.source_fingerprint,
        config_fingerprint=trial.config_fingerprint,
        home_pose=(0.4, 0.1, 0.2, 3.14, 0.0, 0.0),
        home_q=(0.0,) * 6,
        ready_consumed_command_seq=0,
        controller_timestamp_s=1.0,
        connection_epoch=1,
    )
    arm = HostPacket(1, 1, HostCommand.ARM, 1, 111, 1)
    collector = PreAckTypedClosureCollector(
        context=context,
        trial=trial,
        expected_arm=arm,
        campaign_home_reference=home,
        max_sample_gap_s=0.02,
    )
    pose = (*context.reference.pose_xyz_m, *context.reference.pose_rotvec_rad)
    for index in range(51):
        row = {
            "heartbeat": float(index),
            "normal_force_n": 0.0,
            "force_norm_n": 0.0,
            "torque_norm_nm": 0.0,
            "sensor_age_s": 0.001,
            "rtde_feedback_age_s": 0.001,
            "ur_safety_mode": 1,
            "ur_output_int_register_24": 1,
            "ur_output_int_register_25": 1,
            "ur_output_int_register_26": 70,
            "ur_output_int_register_27": 1,
            "ur_output_int_register_28": 1,
            "ur_output_int_register_29": 111,
            "ur_output_int_register_30": 1,
            "ur_output_int_register_31": 1,
            "ur_output_int_register_32": 1,
            "ur_output_int_register_33": 0x7F,
            "ur_output_double_register_36": 0.001,
            "ur_output_double_register_37": 0.002,
            "ur_output_double_register_38": 0.003,
            "ur_output_double_register_35": 40.3,
            "ur_output_double_register_39": 3,
            "ur_output_double_register_40": 0.0,
            "ur_output_double_register_41": 0.0,
            "ur_output_double_register_42": 0.05,
            "ur_output_double_register_43": 0.1,
            "ur_output_double_register_44": 0.002,
        }
        row.update(
            {f"ur_actual_TCP_pose_{axis}": pose[axis] for axis in range(6)}
        )
        for prefix in ("ur_actual_TCP_speed", "ur_actual_q", "ur_actual_qd"):
            row.update({f"{prefix}_{axis}": 0.0 for axis in range(6)})
        collector.observe(row, monotonic_s=index * 0.01)

    evidence = collector.finalize(
        capture_hashes_complete=True,
        terminal_manifest_complete=True,
        fingerprint_closed=True,
    )
    assert isinstance(evidence, TypedSafeClosureEvidence)
    assert evidence.return_reference_uid == context.reference.reference_uid
    assert evidence.tp_qd_max_rad_s == 0.003
    assert "tp_joint_error_max_rad" not in evidence.payload()
    assert evidence.returned_safe is True


def test_r004_incident_replay_reports_return_phase_mismatch(tmp_path: Path) -> None:
    incident = json.loads(
        (ROOT / "tests/fixtures/v3_r004_return_telemetry_incident.json").read_text(
            encoding="utf-8"
        )
    )
    assert incident["source"]["bridge_csv_sha256"] == (
        "8c68a70f666663871828fdfd4d6170b63a6a1bcbe46952e5cf88939e73eaedd9"
    )
    assert incident["source"]["summary_sha256"] == (
        "9a7fced00022aa6965f1088c45e598c26107b2407ac7fd9fc4088f26a4a037a8"
    )

    context = _fixture(tmp_path)
    campaign = SimpleNamespace(campaign_epoch=1, campaign_fingerprint="c" * 64)
    trial = SimpleNamespace(
        backend_id="backend",
        campaign=campaign,
        source_fingerprint="d" * 64,
        config_fingerprint="e" * 64,
        trial_id=1,
        candidate_token=1,
        trial_uid="1" * 64,
    )
    home_path = (tmp_path / "campaign_home_reference.json").resolve()
    home_path.write_bytes(b"{}\n")
    home = CampaignHomeReference(
        path=home_path,
        sha256=hashlib.sha256(home_path.read_bytes()).hexdigest(),
        backend_id=trial.backend_id,
        campaign_epoch=1,
        campaign_fingerprint=campaign.campaign_fingerprint,
        source_fingerprint=trial.source_fingerprint,
        config_fingerprint=trial.config_fingerprint,
        home_pose=(0.4, 0.1, 0.2, 3.14, 0.0, 0.0),
        home_q=(0.0,) * 6,
        ready_consumed_command_seq=0,
        controller_timestamp_s=1.0,
        connection_epoch=1,
    )
    collector = PreAckTypedClosureCollector(
        context=context,
        trial=trial,
        expected_arm=HostPacket(1, 1, HostCommand.ARM, 1, 533, 1),
        campaign_home_reference=home,
        max_sample_gap_s=0.02,
    )
    fixture_row = incident["final_state70_row"]
    for index in range(51):
        row = dict(fixture_row)
        row["heartbeat"] = str(98943 + index)
        collector.observe(row, monotonic_s=index * 0.01)

    assert collector.ready is False
    assert collector.failure_reason == runtime_lifecycle.RETURN_PHASE_MISMATCH


def test_r005_generated_telemetry_drives_trial1_ack_ready_near_trial2_arm(
    tmp_path: Path,
) -> None:
    rendered = tp_v3.render_script()
    assert "codex_autotune_latch_return_telemetry()" in rendered
    telemetry = tp_v3.simulate_return_telemetry(
        tuple((index * 0.002, 0.0, 0.0, 0.0) for index in range(301))
    )

    context = _fixture(tmp_path)
    campaign = SimpleNamespace(campaign_epoch=1, campaign_fingerprint="c" * 64)
    trial1 = SimpleNamespace(
        backend_id="backend",
        campaign=campaign,
        source_fingerprint="d" * 64,
        config_fingerprint="e" * 64,
        trial_id=1,
        candidate_token=1,
        trial_uid="1" * 64,
    )
    home_path = (tmp_path / "campaign_home_reference.json").resolve()
    home_path.write_bytes(b"{}\n")
    home = CampaignHomeReference(
        path=home_path,
        sha256=hashlib.sha256(home_path.read_bytes()).hexdigest(),
        backend_id=trial1.backend_id,
        campaign_epoch=1,
        campaign_fingerprint=campaign.campaign_fingerprint,
        source_fingerprint=trial1.source_fingerprint,
        config_fingerprint=trial1.config_fingerprint,
        home_pose=(0.4, 0.1, 0.2, 3.14, 0.0, 0.0),
        home_q=(0.0,) * 6,
        ready_consumed_command_seq=0,
        controller_timestamp_s=1.0,
        connection_epoch=1,
    )
    arm1 = HostPacket(1, 1, HostCommand.ARM, 1, 533, 1)
    collector = PreAckTypedClosureCollector(
        context=context,
        trial=trial1,
        expected_arm=arm1,
        campaign_home_reference=home,
        max_sample_gap_s=0.02,
    )
    commands = [arm1]
    pose = (*context.reference.pose_xyz_m, *context.reference.pose_rotvec_rad)
    for index in range(51):
        row = {
            "heartbeat": float(index),
            "normal_force_n": 0.0,
            "force_norm_n": 0.0,
            "torque_norm_nm": 0.0,
            "sensor_age_s": 0.001,
            "rtde_feedback_age_s": 0.001,
            "ur_safety_mode": 1,
            "ur_output_int_register_24": 1,
            "ur_output_int_register_25": 1,
            "ur_output_int_register_26": 70,
            "ur_output_int_register_27": 1,
            "ur_output_int_register_28": 1,
            "ur_output_int_register_29": 533,
            "ur_output_int_register_30": 1,
            "ur_output_int_register_31": 1,
            "ur_output_int_register_32": 1,
            "ur_output_int_register_33": 0x7F,
            "ur_output_double_register_36": 0.001,
            "ur_output_double_register_37": 0.002,
            "ur_output_double_register_38": 0.003,
            **telemetry,
        }
        row.update({f"ur_actual_TCP_pose_{axis}": pose[axis] for axis in range(6)})
        for prefix in ("ur_actual_TCP_speed", "ur_actual_q", "ur_actual_qd"):
            row.update({f"{prefix}_{axis}": 0.0 for axis in range(6)})
        if collector.observe(row, monotonic_s=index * 0.01):
            commands.append(HostPacket(1, 1, HostCommand.ACK_BUNDLE, 1, 533, 2))
            break

    assert collector.ready is True
    assert collector.dwell_s == pytest.approx(0.5)
    assert [packet.command for packet in commands].count(HostCommand.ACK_BUNDLE) == 1
    ready_near = {"state": "READY_NEAR", "consumed_command_seq": 2}
    assert ready_near == {"state": "READY_NEAR", "consumed_command_seq": commands[-1].command_seq}
    arm2 = HostPacket(1, 2, HostCommand.ARM, 2, 533, 3)
    commands.append(arm2)
    assert [packet.command for packet in commands] == [
        HostCommand.ARM,
        HostCommand.ACK_BUNDLE,
        HostCommand.ARM,
    ]
    assert [packet.command_seq for packet in commands] == [1, 2, 3]
