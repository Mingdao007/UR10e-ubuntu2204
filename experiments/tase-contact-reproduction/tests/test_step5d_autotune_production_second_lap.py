from __future__ import annotations

import hashlib
import shutil
import sys
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from step5d_v3_fake_bridge_harness import (  # noqa: E402
    exact_trial_overlay,
    prepared_trial,
    write_evaluated_bundle,
)
from step5d_autotune_batch_plan import load_plan  # noqa: E402
from step5d_autotune_contract import (  # noqa: E402
    CampaignSpec,
    ExecutionProfile,
    TrialTransitionKind,
)
from step5d_autotune_coordinator import (  # noqa: E402
    CampaignCoordinator,
    MailboxObservation,
)
from step5d_autotune_journal import (  # noqa: E402
    ReconcileAction,
    SupervisorJournal,
    TpSnapshot,
)
from step5d_autotune_live_driver import (  # noqa: E402
    AtomicCommandMailbox,
    ImmutableBundleStoreReceipt,
)
from step5d_autotune_runtime_lifecycle import (  # noqa: E402
    PostAckControllerReadback,
    TerminalReadyControllerReadback,
    prepare_batch_attempt_context,
)
from step5d_autotune_state_machine import HostCommand  # noqa: E402
from step5d_autotune_store import (  # noqa: E402
    CampaignStore,
    cold_read_resume_history_subprocess,
)
from step5d_autotune_supervisor import (  # noqa: E402
    CampaignPhase,
    CampaignSupervisor,
    CompletionProtocol,
    execution_profile_integer_id,
)


PLAN = ROOT / "tests/fixtures/step5d_r005_exact_candidate_plan.json"
PLAN_SHA256 = "bed54b7482fa596fcc6bf34fa4c4aabbeecfe9c86903b123aa68f4edeaec5935"


def _batch_context(plan, candidate, profile, campaign_root: Path, row: int):
    context = prepare_batch_attempt_context(
        plan=plan,
        selected_candidate=candidate,
        profile=profile,
        overlay_resolver=lambda selected: exact_trial_overlay(selected, profile),
        campaign_uid="step5d-native-1",
        experiment_fingerprint="a" * 64,
        launch_fingerprint="b" * 64,
        controller_readback_fingerprint="c" * 64,
        authorization_ref_sha256="d" * 64,
        stopping_bound_fingerprint=None,
        plant_epoch=1,
        campaign_root=campaign_root,
        campaign_home_pose=(0.45, 0.10, 0.20, 3.14, 0.0, 0.0),
    )
    assert context.row_index == row
    return context


def test_legacy_r005_synthetic_snapshot_chain_reaches_arm2(tmp_path: Path) -> None:
    assert hashlib.sha256(PLAN.read_bytes()).hexdigest() == PLAN_SHA256
    plan = load_plan(PLAN, campaign_id="step5d-native-1")
    first, second = plan.batches[0][:2]
    assert first != type(first)()

    campaign = CampaignSpec(
        campaign_id="step5d-native-1",
        campaign_epoch=1,
        campaign_fingerprint="a" * 64,
        f0_shadow_reaction_normal_base=(0.0, 0.0, 1.0),
    )
    profile = ExecutionProfile("nf100-slew050-a050", 0.1, 0.5, 0.5)
    campaign_root = tmp_path / "campaign"
    store = CampaignStore(campaign_root / "store")
    store.initialize(
        {
            "campaign": asdict(campaign),
            "execution_profile": profile.payload(),
            "selection_policy": "codex_batches",
        }
    )
    supervisor = CampaignSupervisor(
        campaign=campaign,
        backend_id="step5d_v35_native_backend_v1",
        source_fingerprint="e" * 64,
        config_fingerprint="f" * 64,
        execution_profile=profile,
        selection_policy="codex_batches",
    )
    coordinator = CampaignCoordinator(
        supervisor=supervisor,
        journal=SupervisorJournal(campaign_root / "journal"),
    )
    coordinator.persist_home()
    mailbox_path = tmp_path / "bridge/runtime/command.json"
    mailbox_path.parent.mkdir(parents=True)
    mailbox = AtomicCommandMailbox(mailbox_path)

    context1 = _batch_context(plan, first, profile, campaign_root, 1)
    arm1 = coordinator.issue_arm(
        store,
        require_cuda_botorch=False,
        forced_candidate=first,
        allow_fresh_exact_batch_bootstrap=True,
        attempt_started=lambda trial: context1.start_attempt(
            trial,
            context1.expected_row.trial_overlay,
        ),
    )
    trial1 = supervisor.active_trial
    assert trial1 is not None
    assert trial1.transition.kind is TrialTransitionKind.BATCH_BOOTSTRAP
    assert trial1.transition.source is None
    prepared1 = prepared_trial(
        trial1,
        batch_row_index=1,
        trial_overlay=context1.expected_row.trial_overlay,
    )
    coordinator.dispatch(arm1, prepared_trial=prepared1, sink=mailbox)
    wait_ack1 = TpSnapshot(
        1,
        trial1.trial_id,
        "WAIT_ACK",
        trial1.candidate_token,
        1,
        execution_profile_integer_id(profile),
        arm1.command_seq,
    )
    assert coordinator.reconcile(wait_ack1).decision.action is ReconcileAction.RESUME_CLOSURE

    bundle1, manifest1, evaluation1 = write_evaluated_bundle(
        store,
        trial1,
        campaign_root,
        candidate_index=1,
        reference=context1.reference,
    )
    cold_rows = CampaignStore(store.root).read_resume_history()
    assert len(cold_rows) == 1
    assert cold_rows[0]["trial"]["transition"] == {
        "kind": "batch_bootstrap",
        "source": None,
        "retry_kind": None,
    }
    decision = coordinator.close_trial(
        manifest=manifest1,
        evaluation=evaluation1,
        safe_closure=manifest1.safe_closure_evidence,
        bundle_path=bundle1,
    )
    assert decision.ack_permitted is True
    receipt1 = ImmutableBundleStoreReceipt(
        trial_uid=trial1.trial_uid,
        bundle_path=bundle1,
        bundle_sha256=hashlib.sha256(bundle1.read_bytes()).hexdigest(),
        history_identity=cold_rows[0]["history_identity"],
    )
    context1.record_bundle(receipt1)
    ack1 = coordinator.issue_ack(bundle1, verified_resume_history=cold_rows)
    coordinator.dispatch(ack1, prepared_trial=prepared1, sink=mailbox)
    assert ack1.command is HostCommand.ACK_BUNDLE

    ready_near = TpSnapshot(
        1,
        trial1.trial_id,
        "READY_NEAR",
        trial1.candidate_token,
        1,
        execution_profile_integer_id(profile),
        ack1.command_seq,
    )
    readback1 = PostAckControllerReadback(
        batch_uid=context1.identity.batch_uid,
        row_index=1,
        trial_uid=trial1.trial_uid,
        return_reference_uid=context1.reference.reference_uid,
        return_reference=context1.reference.kind,
        ack_command_seq=ack1.command_seq,
        consumed_command_seq=ack1.command_seq,
        tp_state="READY_NEAR",
        batch_row_echo=1,
        return_kind_echo=context1.reference.kind.value,
        return_guard_mask=0x7F,
        position_error_m=0.001,
        orientation_error_rad=0.01,
        tcp_linear_speed_m_s=0.0005,
        tcp_angular_speed_rad_s=0.005,
        qd_max_rad_s=0.005,
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
    admission1 = context1.complete_post_ack(
        trial=trial1,
        arm_packet=arm1,
        ack_packet=ack1,
        store_receipt=receipt1,
        manifest=manifest1,
        evaluation=evaluation1,
        readback=readback1,
    )
    persisted = coordinator.reconcile(
        ready_near,
        trial_brief_admission=admission1,
    )
    assert persisted.decision.action is ReconcileAction.PERSIST_POST_ACK
    assert supervisor.phase is CampaignPhase.HOME

    context2 = _batch_context(plan, second, profile, campaign_root, 2)
    arm2 = coordinator.issue_arm(
        store,
        require_cuda_botorch=False,
        forced_candidate=second,
        attempt_started=lambda trial: context2.start_attempt(
            trial,
            context2.expected_row.trial_overlay,
        ),
    )
    assert arm2.command is HostCommand.ARM
    assert arm2.trial_id == 2
    assert arm2.command_seq == ack1.command_seq + 1
    assert [arm1.command, ack1.command, arm2.command] == [
        HostCommand.ARM,
        HostCommand.ACK_BUNDLE,
        HostCommand.ARM,
    ]


def test_r006_direct_pending_advance_uses_no_ack_sequence(tmp_path: Path) -> None:
    plan = load_plan(PLAN, campaign_id="step5d-native-1")
    first, second = plan.batches[0][:2]
    campaign = CampaignSpec(
        campaign_id="step5d-native-1",
        campaign_epoch=1,
        campaign_fingerprint="a" * 64,
        f0_shadow_reaction_normal_base=(0.0, 0.0, 1.0),
    )
    profile = ExecutionProfile("nf100-slew050-a050", 0.1, 0.5, 0.5)
    campaign_root = tmp_path / "campaign"
    store = CampaignStore(campaign_root / "store")
    store.initialize(
        {
            "campaign": asdict(campaign),
            "execution_profile": profile.payload(),
            "selection_policy": "codex_batches",
        }
    )
    supervisor = CampaignSupervisor(
        campaign=campaign,
        backend_id="step5d_v35_native_backend_v1",
        source_fingerprint="e" * 64,
        config_fingerprint="f" * 64,
        execution_profile=profile,
        selection_policy="codex_batches",
        completion_protocol=CompletionProtocol.DIRECT_ARM_V1,
    )
    coordinator = CampaignCoordinator(
        supervisor=supervisor,
        journal=SupervisorJournal(campaign_root / "journal"),
    )
    coordinator.persist_home()
    mailbox_path = tmp_path / "bridge/runtime/command.json"
    mailbox_path.parent.mkdir(parents=True)
    mailbox = AtomicCommandMailbox(mailbox_path)
    context1 = _batch_context(plan, first, profile, campaign_root, 1)
    arm1 = coordinator.issue_arm(
        store,
        require_cuda_botorch=False,
        forced_candidate=first,
        allow_fresh_exact_batch_bootstrap=True,
        attempt_started=lambda trial: context1.start_attempt(
            trial, context1.expected_row.trial_overlay
        ),
    )
    trial1 = supervisor.active_trial
    assert trial1 is not None
    prepared1 = prepared_trial(
        trial1,
        batch_row_index=1,
        trial_overlay=context1.expected_row.trial_overlay,
    )
    coordinator.dispatch(arm1, prepared_trial=prepared1, sink=mailbox)
    terminal = TpSnapshot(
        1,
        trial1.trial_id,
        "READY_NEAR",
        trial1.candidate_token,
        1,
        execution_profile_integer_id(profile),
        arm1.command_seq,
    )
    # Crash before the bundle write resumes the sealed-capture/bundle seam.
    assert (
        coordinator.reconcile(terminal).decision.action
        is ReconcileAction.RESUME_CLOSURE
    )
    bundle1, manifest1, evaluation1 = write_evaluated_bundle(
        store,
        trial1,
        campaign_root,
        candidate_index=1,
        reference=context1.reference,
    )
    cold_rows = cold_read_resume_history_subprocess(store.root.resolve())

    def restore_crash_cut(label: str, snapshot: TpSnapshot):
        copied_journal_root = campaign_root / f"journal-{label}"
        shutil.copytree(campaign_root / "journal", copied_journal_root)
        copied_journal = SupervisorJournal(copied_journal_root)
        restored_supervisor = CampaignSupervisor(
            campaign=campaign,
            backend_id="step5d_v35_native_backend_v1",
            source_fingerprint="e" * 64,
            config_fingerprint="f" * 64,
            execution_profile=profile,
            selection_policy="codex_batches",
            completion_protocol=CompletionProtocol.DIRECT_ARM_V1,
        )
        return CampaignCoordinator.restore(
            supervisor=restored_supervisor,
            journal=copied_journal,
            latest=copied_journal.load_latest(),
            resume_history=cold_rows,
            promotion_history=store.read_promotion_history(),
            tp_snapshot=snapshot,
            mailbox_observation=MailboxObservation.missing(),
        )

    # Crash after immutable bundle write/cold-read but before PendingAdvance.
    before_pending = restore_crash_cut("before-pending", terminal)
    assert before_pending.decision.action is ReconcileAction.PERSIST_DIRECT_READY
    assert before_pending.packet is None
    assert before_pending.coordinator.latest is not None
    assert before_pending.coordinator.latest.state.pending_advance is not None
    assert before_pending.coordinator.latest.state.pending_ack is None
    assert before_pending.coordinator.latest.state.high_water.command_seq == 1

    decision = coordinator.close_trial(
        manifest=manifest1,
        evaluation=evaluation1,
        safe_closure=manifest1.safe_closure_evidence,
        bundle_path=bundle1,
    )
    assert decision.completion_protocol is CompletionProtocol.DIRECT_ARM_V1
    assert decision.advance_permitted is True
    assert decision.ack_permitted is False
    assert supervisor.phase is CampaignPhase.WAIT_DIRECT_COMMIT
    receipt1 = ImmutableBundleStoreReceipt(
        trial_uid=trial1.trial_uid,
        bundle_path=bundle1,
        bundle_sha256=hashlib.sha256(bundle1.read_bytes()).hexdigest(),
        history_identity=cold_rows[0]["history_identity"],
    )
    context1.record_bundle(receipt1)
    coordinator.persist_direct_advance(
        bundle1,
        verified_resume_history=cold_rows,
    )
    # Crash after PendingAdvance fsync but before direct completion publication.
    after_pending = restore_crash_cut("after-pending", terminal)
    assert after_pending.decision.action is ReconcileAction.PERSIST_DIRECT_READY
    assert after_pending.packet is None
    assert after_pending.coordinator.latest is not None
    assert after_pending.coordinator.latest.state.pending_advance is not None
    assert after_pending.coordinator.latest.state.pending_ack is None
    readback = TerminalReadyControllerReadback(
        batch_uid=context1.identity.batch_uid,
        row_index=1,
        trial_uid=trial1.trial_uid,
        return_reference_uid=context1.reference.reference_uid,
        return_reference=context1.reference.kind,
        arm_command_seq=arm1.command_seq,
        consumed_command_seq=arm1.command_seq,
        tp_state="READY_NEAR",
        batch_row_echo=1,
        return_kind_echo=context1.reference.kind.value,
        return_guard_mask=0x7F,
        position_error_m=0.001,
        orientation_error_rad=0.01,
        tcp_linear_speed_m_s=0.0005,
        tcp_angular_speed_rad_s=0.005,
        qd_max_rad_s=0.005,
        return_phase_echo=40.3,
        return_segment_id=3,
        return_current_angular_speed_rad_s=0.0,
        return_current_angular_acceleration_rad_s2=0.0,
        return_max_angular_speed_rad_s=0.05,
        return_max_angular_acceleration_rad_s2=0.1,
        return_max_sample_gap_s=0.002,
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
    admission = context1.complete_terminal_ready(
        trial=trial1,
        arm_packet=arm1,
        store_receipt=receipt1,
        manifest=manifest1,
        evaluation=evaluation1,
        readback=readback,
    )
    coordinator.complete_direct_ready(
        bundle1,
        verified_resume_history=cold_rows,
        tp_snapshot=terminal,
        trial_brief_admission=admission,
    )
    assert supervisor.phase is CampaignPhase.HOME
    assert len(tuple((campaign_root / "trial_briefs").glob("*.json"))) == 1
    after_direct_commit = restore_crash_cut("after-direct-commit", terminal)
    assert after_direct_commit.decision.action is ReconcileAction.RESUME_HOME
    assert after_direct_commit.packet is None
    assert len(tuple((campaign_root / "trial_briefs").glob("*.json"))) == 1

    context2 = _batch_context(plan, second, profile, campaign_root, 2)
    arm2 = coordinator.issue_arm(
        store,
        require_cuda_botorch=False,
        forced_candidate=second,
        attempt_started=lambda trial: context2.start_attempt(
            trial, context2.expected_row.trial_overlay
        ),
    )
    assert arm2.command is HostCommand.ARM
    assert arm2.command_seq == arm1.command_seq + 1
    assert coordinator.latest is not None
    assert coordinator.latest.state.pending_ack is None
    before_arm2_send = restore_crash_cut("before-arm2-send", terminal)
    assert before_arm2_send.decision.action is ReconcileAction.SEND_PERSISTED_ARM
    assert before_arm2_send.packet == arm2

    trial2 = supervisor.active_trial
    assert trial2 is not None
    prepared2 = prepared_trial(
        trial2,
        batch_row_index=2,
        trial_overlay=context2.expected_row.trial_overlay,
    )
    coordinator.dispatch(arm2, prepared_trial=prepared2, sink=mailbox)
    run2 = TpSnapshot(
        1,
        trial2.trial_id,
        "RUN",
        trial2.candidate_token,
        0,
        execution_profile_integer_id(profile),
        arm2.command_seq,
    )
    after_arm2_consumed = restore_crash_cut("after-arm2-consumed", run2)
    assert after_arm2_consumed.decision.action is ReconcileAction.MONITOR_ACTIVE
    assert after_arm2_consumed.packet is None
    assert len(tuple((campaign_root / "trial_briefs").glob("*.json"))) == 1
