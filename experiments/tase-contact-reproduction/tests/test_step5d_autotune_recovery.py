#!/usr/bin/env python3
"""Crash-window integration tests for the Step5d autotune coordinator."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import (  # noqa: E402
    CampaignSpec,
    CaptureArtifactPaths,
    CaptureManifest,
    Evaluation,
    ExecutionProfile,
    TrialDisposition,
    canonical_json_bytes,
)
from step5d_autotune_backend import PreparedFingerprint, PreparedTrial  # noqa: E402
from step5d_autotune_coordinator import (  # noqa: E402
    CampaignCoordinator,
    CoordinatorError,
    MailboxObservation,
    RecoveryError,
)
from step5d_autotune_governor import (  # noqa: E402
    AbEvidence,
    SaturationSample,
)
from step5d_autotune_journal import (  # noqa: E402
    JournalReference,
    ReconcileAction,
    SupervisorJournal,
    TpSnapshot,
)
from step5d_autotune_optimizer import choose_candidate  # noqa: E402
from step5d_autotune_live_driver import (  # noqa: E402
    AtomicCommandMailbox,
    BridgeTrialCsvRotator,
    CampaignHomeReference,
    HostClosureCollector,
    TrialArtifactProducer,
    finalize_bundle_and_dispatch_ack_for_test_fixture,
    finalize_produced_bundle_and_dispatch_ack,
)
from step5d_autotune_state_machine import (  # noqa: E402
    HostCommand,
    SafeClosureEvidence,
    TpLoopState,
)
from step5d_autotune_v3.shared_contracts import (  # noqa: E402
    TrialResult as SharedTrialResult,
    TrialSpec as SharedTrialSpec,
)
from step5d_autotune_store import CampaignStore  # noqa: E402
from step5d_autotune_supervisor import (  # noqa: E402
    CampaignPhase,
    CampaignSupervisor,
    execution_profile_integer_id,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def campaign(*, epoch: int = 1, fingerprint: str = SHA_A) -> CampaignSpec:
    return CampaignSpec(
        campaign_id="recovery-fixture",
        campaign_epoch=epoch,
        campaign_fingerprint=fingerprint,
        f0_shadow_reaction_normal_base=(0.0, 0.0, 1.0),
    )


def profile(rate: float = 0.010) -> ExecutionProfile:
    return ExecutionProfile(f"nf{int(rate * 1000):03d}-slew010-a010", rate)


def supervisor(
    *,
    campaign_spec: CampaignSpec | None = None,
    source: str = SHA_B,
    config: str = SHA_C,
    execution_profile: ExecutionProfile | None = None,
    plant_epoch: int = 1,
    selection_policy: str = "adaptive",
) -> CampaignSupervisor:
    return CampaignSupervisor(
        campaign=campaign_spec or campaign(),
        backend_id="step5d_v35_native",
        source_fingerprint=source,
        config_fingerprint=config,
        execution_profile=execution_profile or profile(),
        plant_epoch=plant_epoch,
        selection_policy=selection_policy,
        optimizer_selector=choose_candidate,
    )


def closure() -> SafeClosureEvidence:
    return SafeClosureEvidence(
        tp_position_error_m=0.001,
        tp_orientation_error_rad=0.01,
        tp_joint_error_max_rad=0.005,
        host_position_error_m=0.001,
        host_orientation_error_rad=0.01,
        host_joint_error_max_rad=0.005,
        host_tcp_linear_speed_m_s=0.0005,
        host_tcp_angular_speed_rad_s=0.005,
        host_qd_max_rad_s=0.005,
        host_safety_mode="NORMAL",
        host_dwell_s=0.5,
        trial_token_match=True,
        capture_hashes_complete=True,
        terminal_manifest_complete=True,
        fingerprint_closed=True,
    )


def _sha(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def write_bundle(
    store: CampaignStore,
    trial,
    root: Path,
    *,
    reason: int = 1,
    disposition: TrialDisposition = TrialDisposition.OBJECTIVE,
    objective: float | None = 0.4,
    governor_burden: float = 1.0,
    host_cause: str | None = None,
):
    safe = closure()
    artifact_dir = root / "capture" / trial.trial_uid
    artifact_dir.mkdir(parents=True)
    csv_bytes = (
        "stage,path_time_s,force_b_z_n,trial_uid\n"
        f"25,5.05,12.0,{trial.trial_uid}\n"
    ).encode()
    metadata_bytes = (
        json.dumps(
            {
                "schema_version": "metadata/v1",
                "trial_uid": trial.trial_uid,
                "backend_id": trial.backend_id,
                "candidate_token": trial.candidate_token,
            },
            allow_nan=False,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    terminal_bytes = (
        json.dumps(
            {
                "schema_version": "terminal/v1",
                "trial_uid": trial.trial_uid,
                "backend_id": trial.backend_id,
                "candidate_token": trial.candidate_token,
                "terminal_reason": reason,
                "safe_closure_evidence": safe.payload(),
            },
            allow_nan=False,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    paths = CaptureArtifactPaths(
        artifact_dir / "capture.csv",
        artifact_dir / "metadata.json",
        artifact_dir / "terminal_manifest.json",
    )
    for path, encoded in zip(
        paths.by_role().values(),
        (csv_bytes, metadata_bytes, terminal_bytes),
    ):
        path.write_bytes(encoded)
    manifest = CaptureManifest(
        trial_uid=trial.trial_uid,
        backend_id=trial.backend_id,
        source_fingerprint_pre=trial.source_fingerprint,
        source_fingerprint_post=trial.source_fingerprint,
        config_fingerprint_pre=trial.config_fingerprint,
        config_fingerprint_post=trial.config_fingerprint,
        candidate_token=trial.candidate_token,
        terminal_reason=reason,
        host_cause=host_cause,
        csv_sha256=_sha(csv_bytes),
        metadata_sha256=_sha(metadata_bytes),
        terminal_manifest_sha256=_sha(terminal_bytes),
        completion_marker=reason == 1,
        cadence_ok=True,
        feedback_fresh=True,
        rnn_oracle_aligned=True,
        safety_normal=True,
        returned_safe=True,
        immutable_bundle_written=True,
        stage25_complete_s=60.0 if reason == 1 else 0.0,
        safe_closure_evidence=safe,
    )
    eligible = disposition is TrialDisposition.OBJECTIVE
    evaluation = Evaluation(
        trial_uid=trial.trial_uid,
        backend_id=trial.backend_id,
        eligible=eligible,
        disposition=disposition,
        objective_mae_n=objective if eligible else None,
        force_bias_n=0.0 if eligible else None,
        force_std_n=0.1 if eligible else None,
        coverage_12_plus_minus_1_ratio=1.0 if eligible else None,
        complete_bins=550 if eligible else 0,
        safe_closure=True,
        metrics={
            "governor": {
                "burden_by_layer": {
                    "normal_filter_rate": governor_burden,
                    "host_qdot_slew": governor_burden,
                    "tp_speedj_acceleration": governor_burden,
                },
                "burden_evidence_complete_by_layer": {
                    "normal_filter_rate": True,
                    "host_qdot_slew": True,
                    "tp_speedj_acceleration": True,
                },
                "tracking": {
                    "evidence_complete": True,
                    "lag_s": 0.01,
                    "correlation": 0.98,
                    "nrmse": 0.1,
                },
                "trigger": {
                    "source": "immutable_trial_csv",
                    "sample_count": 110,
                    "normal_filter_persistent": True,
                    "qdot_persistent": False,
                    "host_slew_persistent": False,
                    "tp_accel_persistent": False,
                    "tracking_degraded": False,
                },
                "orientation": {
                    "evidence_complete": True,
                    "qualified": True,
                    "p95_error_rad": 0.01,
                    "max_error_rad": 0.02,
                    "saturation_duty": 0.01,
                },
            }
        },
    )
    bundle = store.write_trial_bundle(
        trial, manifest, evaluation, artifact_paths=paths
    )
    return bundle, manifest, evaluation


def tp_ready(*, consumed: int) -> TpSnapshot:
    return TpSnapshot(0, 0, "READY_HOME", 0, 0, 0, consumed)


def tp_for(trial, state: str, *, consumed: int, reason: int = 1) -> TpSnapshot:
    return TpSnapshot(
        trial.campaign.campaign_epoch,
        trial.trial_id,
        state,
        trial.candidate_token,
        reason,
        execution_profile_integer_id(trial.execution_profile),
        consumed,
    )


def prepared(trial):
    candidate = trial.candidate
    execution = trial.execution_profile
    environment = {
        "STEP5D_AUTOTUNE_TRIAL_UID": trial.trial_uid,
        "STEP5D_AUTOTUNE_CAMPAIGN_EPOCH": str(trial.campaign.campaign_epoch),
        "STEP5D_AUTOTUNE_TRIAL_ID": str(trial.trial_id),
        "STEP5D_AUTOTUNE_CANDIDATE_TOKEN": str(trial.candidate_token),
        "STEP5D_AUTOTUNE_EXECUTION_PROFILE_ID": str(
            execution_profile_integer_id(execution)
        ),
        "STEP5D_AUTOTUNE_COMMAND_SEQ": str(trial.command_seq),
        "STEP5D_AUTOTUNE_FORCE_P": str(candidate.force_p_gain),
        "STEP5D_AUTOTUNE_FORCE_I": str(candidate.force_i_gain),
        "STEP5D_AUTOTUNE_FORCE_DAMPING": str(candidate.force_damping),
        "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S": str(
            execution.normal_max_rate_rad_s
        ),
        "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2": str(
            execution.host_qdot_slew_rad_s2
        ),
        "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2": str(
            execution.tp_speedj_accel_rad_s2
        ),
    }
    frozen = PreparedFingerprint(
        source_fingerprint=trial.source_fingerprint,
        config_fingerprint=trial.config_fingerprint,
        composite_fingerprint=trial.campaign.campaign_fingerprint,
    )
    arguments = (
        "--step5d-autotune-force-p",
        environment["STEP5D_AUTOTUNE_FORCE_P"],
        "--step5d-autotune-force-i",
        environment["STEP5D_AUTOTUNE_FORCE_I"],
        "--step5d-autotune-force-damping",
        environment["STEP5D_AUTOTUNE_FORCE_DAMPING"],
        "--step5d-autotune-normal-rate-rad-s",
        environment["STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S"],
        "--step5d-autotune-host-slew-rad-s2",
        environment["STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2"],
        "--step5d-autotune-speedj-acceleration-rad-s2",
        environment["STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2"],
        "--step5d-autotune-campaign-epoch",
        environment["STEP5D_AUTOTUNE_CAMPAIGN_EPOCH"],
        "--step5d-autotune-trial-id",
        environment["STEP5D_AUTOTUNE_TRIAL_ID"],
        "--step5d-autotune-command",
        "1",
        "--step5d-autotune-candidate-token",
        environment["STEP5D_AUTOTUNE_CANDIDATE_TOKEN"],
        "--step5d-autotune-execution-profile-id",
        environment["STEP5D_AUTOTUNE_EXECUTION_PROFILE_ID"],
        "--step5d-autotune-command-sequence",
        environment["STEP5D_AUTOTUNE_COMMAND_SEQ"],
    )
    return PreparedTrial(
        trial=trial,
        frozen=frozen,
        environment=environment,
        runner_arguments=arguments,
    )


class FakeContinuousSink:
    def __init__(self) -> None:
        self.session_id = "one-continuous-tp-session"
        self.packets = []
        self.latest = None

    def send_command(self, packet, *, prepared_trial) -> None:
        self.packets.append((self.session_id, packet, prepared_trial.trial.trial_uid))
        self.latest = SimpleNamespace(
            packet=packet,
            binding=SimpleNamespace(trial_uid=prepared_trial.trial.trial_uid),
            sha256=hashlib.sha256(repr(packet).encode("utf-8")).hexdigest(),
        )

    def read_latest(self):
        return self.latest


class RecoveryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CampaignStore(self.root / "store")
        self.manager = supervisor()
        self.journal = SupervisorJournal(self.root / "journal")
        self.coordinator = CampaignCoordinator(
            supervisor=self.manager,
            journal=self.journal,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def arm(self):
        packet = self.coordinator.issue_arm(
            self.store, require_cuda_botorch=False
        )
        trial = self.manager.active_trial
        assert trial is not None
        return trial, packet

    def close_and_ack(
        self,
        trial,
        *,
        reason: int = 1,
        disposition: TrialDisposition = TrialDisposition.OBJECTIVE,
        objective: float | None = 0.4,
        governor_burden: float = 1.0,
        host_cause: str | None = None,
    ):
        bundle, manifest, evaluation = write_bundle(
            self.store,
            trial,
            self.root,
            reason=reason,
            disposition=disposition,
            objective=objective,
            governor_burden=governor_burden,
            host_cause=host_cause,
        )
        decision = self.coordinator.close_trial(
            manifest=manifest,
            evaluation=evaluation,
            safe_closure=manifest.safe_closure_evidence,
            bundle_path=bundle,
        )
        self.assertTrue(decision.ack_permitted)
        return bundle, self.coordinator.issue_ack(
            bundle,
            verified_resume_history=self.store.read_resume_history(),
        )

    def restore(
        self,
        snapshot: TpSnapshot,
        *,
        mailbox_observation: MailboxObservation | None = None,
    ):
        return CampaignCoordinator.restore(
            supervisor=supervisor(),
            journal=self.journal,
            latest=self.journal.load_latest(),
            resume_history=self.store.read_resume_history(),
            promotion_history=self.store.read_promotion_history(),
            tp_snapshot=snapshot,
            mailbox_observation=(
                MailboxObservation.missing()
                if mailbox_observation is None
                else mailbox_observation
            ),
        )


class CommandIssuanceTest(RecoveryFixture):
    def test_arm_admits_shared_spec_and_close_admits_one_bound_result(self) -> None:
        trial, _arm = self.arm()
        admission_paths = list(
            (self.root / "store").rglob("shared_trial_admission.json")
        )
        self.assertEqual(len(admission_paths), 1)
        admission = json.loads(admission_paths[0].read_text(encoding="ascii"))
        shared_spec = SharedTrialSpec.from_payload(admission["trial_spec"])
        self.assertEqual(shared_spec.trial_id, trial.trial_uid)
        self.assertEqual(shared_spec.release_id, trial.source_fingerprint)
        self.assertEqual(shared_spec.safety_id, trial.config_fingerprint)

        self.close_and_ack(trial)
        result_path = admission_paths[0].with_name("trial_result.json")
        shared_result = SharedTrialResult.from_payload(
            json.loads(result_path.read_text(encoding="ascii"))
        )
        self.assertEqual(shared_result.trial_id, trial.trial_uid)
        self.assertEqual(shared_result.trial_spec_digest, shared_spec.digest)
        self.assertEqual(len(shared_result.artifacts), 1)

    def test_codex_batch_reconcile_requires_durable_trial_brief(self) -> None:
        manager = supervisor(selection_policy="codex_batches")
        coordinator = CampaignCoordinator(
            supervisor=manager,
            journal=SupervisorJournal(self.root / "codex-journal"),
        )
        arm = coordinator.issue_arm(self.store, require_cuda_botorch=False)
        trial = manager.active_trial
        assert trial is not None
        bundle, manifest, evaluation = write_bundle(
            self.store,
            trial,
            self.root,
            objective=0.4,
        )
        coordinator.close_trial(
            manifest=manifest,
            evaluation=evaluation,
            safe_closure=manifest.safe_closure_evidence,
            bundle_path=bundle,
        )
        ack = coordinator.issue_ack(
            bundle,
            verified_resume_history=self.store.read_resume_history(),
        )
        publication_uid = "9" * 64
        brief_path = (self.root / "trial-brief.json").resolve()
        brief_document = {
            "optimizer_eligible": True,
            "publication_uid": publication_uid,
            "publication_unique": True,
            "trial_uid": trial.trial_uid,
        }
        encoded = canonical_json_bytes(brief_document) + b"\n"
        brief_path.write_bytes(encoded)
        admission = SimpleNamespace(
            trial_uid=trial.trial_uid,
            ack_command_seq=ack.command_seq,
            publication_uid=publication_uid,
            document_sha256=hashlib.sha256(
                canonical_json_bytes(brief_document)
            ).hexdigest(),
            path=brief_path,
            file_sha256=hashlib.sha256(encoded).hexdigest(),
            optimizer_eligible=True,
        )

        result = coordinator.reconcile(
            tp_ready(consumed=ack.command_seq),
            trial_brief_admission=admission,
        )

        self.assertIs(result.decision.action, ReconcileAction.PERSIST_POST_ACK)
        self.assertEqual(len(manager.observations), 1)
        fate = coordinator.latest.state.terminal_fates[-1]
        self.assertEqual(fate.evidence.reference_id, publication_uid)
        self.assertGreater(ack.command_seq, arm.command_seq)

    def test_arm_and_fresh_ack_are_fsynced_then_sent_to_same_continuous_sink(self) -> None:
        trial, arm = self.arm()
        arm_entry = self.journal.load_latest()
        self.assertEqual(arm_entry.state.phase, "trial_active")
        self.assertEqual(arm_entry.state.active_trial.trial_uid, trial.trial_uid)
        self.assertEqual(arm.command, HostCommand.ARM)
        sink = FakeContinuousSink()
        bound = prepared(trial)
        arm_receipt = self.coordinator.dispatch(arm, prepared_trial=bound, sink=sink)
        self.assertEqual(arm_receipt.command, "arm")
        self.assertEqual(
            self.journal.load_latest().state.dispatch_receipt, arm_receipt
        )

        _, ack = self.close_and_ack(trial)
        ack_entry = self.journal.load_latest()
        self.assertEqual(ack_entry.state.phase, "wait_ack")
        self.assertEqual(ack_entry.state.pending_ack.ack_command_seq, ack.command_seq)
        self.assertGreater(ack.command_seq, arm.command_seq)
        self.coordinator.dispatch(ack, prepared_trial=bound, sink=sink)
        self.assertEqual([row[0] for row in sink.packets], [sink.session_id] * 2)
        self.assertEqual([row[1].command for row in sink.packets], [HostCommand.ARM, HostCommand.ACK_BUNDLE])

    def test_prepared_trial_candidate_profile_or_fingerprint_drift_blocks_sink(self) -> None:
        trial, arm = self.arm()
        bound = prepared(trial)
        bound.environment["STEP5D_AUTOTUNE_FORCE_P"] = "0.5"
        sink = FakeContinuousSink()
        with self.assertRaisesRegex(CoordinatorError, "FORCE_P"):
            self.coordinator.dispatch(arm, prepared_trial=bound, sink=sink)
        self.assertEqual(sink.packets, [])

    def test_safety_evaluation_failure_never_publishes_bundle_or_ack(self) -> None:
        trial, arm = self.arm()
        bound = prepared(trial)
        mailbox = AtomicCommandMailbox(self.root / "command.json")
        self.coordinator.dispatch(arm, prepared_trial=bound, sink=mailbox)
        collector = HostClosureCollector.for_test_fixture(
            expected_arm=arm,
            campaign_home_pose=[0.4, -0.2, 0.3, 0.0, 0.0, 0.0],
            campaign_home_q=[0.0] * 6,
            max_sample_gap_s=0.02,
        )
        sample = {
            "timestamp": 1.0,
            "output_int_register_24": arm.campaign_epoch,
            "output_int_register_25": arm.trial_id,
            "output_int_register_26": int(TpLoopState.WAIT_ACK),
            "output_int_register_27": arm.candidate_token,
            "output_int_register_28": 1,
            "output_int_register_29": arm.execution_profile_id,
            "output_int_register_30": arm.command_seq,
            "output_double_register_36": 0.001,
            "output_double_register_37": 0.01,
            "output_double_register_38": 0.005,
            "actual_TCP_pose": [0.4, -0.2, 0.3, 0.0, 0.0, 0.0],
            "actual_q": [0.0] * 6,
            "actual_TCP_speed": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "safety_mode": "NORMAL",
        }
        for index in range(51):
            collector.observe(sample, monotonic_s=index * 0.01)

        def manifest_factory(safe: SafeClosureEvidence) -> CaptureManifest:
            return CaptureManifest(
                trial_uid=trial.trial_uid,
                backend_id=trial.backend_id,
                source_fingerprint_pre=trial.source_fingerprint,
                source_fingerprint_post=trial.source_fingerprint,
                config_fingerprint_pre=trial.config_fingerprint,
                config_fingerprint_post=trial.config_fingerprint,
                candidate_token=trial.candidate_token,
                terminal_reason=1,
                host_cause=None,
                csv_sha256="d" * 64,
                metadata_sha256="e" * 64,
                terminal_manifest_sha256="f" * 64,
                completion_marker=True,
                cadence_ok=True,
                feedback_fresh=True,
                rnn_oracle_aligned=True,
                safety_normal=True,
                returned_safe=True,
                immutable_bundle_written=True,
                stage25_complete_s=55.0,
                safe_closure_evidence=safe,
            )

        class FailingSafetyBackend:
            @staticmethod
            def evaluate_trial(*_args):
                raise RuntimeError("structural evaluator rejected capture")

        artifact_paths = CaptureArtifactPaths(
            self.root / "capture.csv",
            self.root / "metadata.json",
            self.root / "terminal.json",
        )
        with self.assertRaisesRegex(RuntimeError, "structural evaluator"):
            finalize_bundle_and_dispatch_ack_for_test_fixture(
                collector=collector,
                trial=trial,
                manifest_factory=manifest_factory,
                backend=FailingSafetyBackend(),
                store=self.store,
                coordinator=self.coordinator,
                artifact_paths=artifact_paths,
                csv_path=artifact_paths.csv_path,
                prepared_trial=bound,
                command_sink=mailbox,
                capture_hashes_complete=True,
                terminal_manifest_complete=True,
                fingerprint_closed=True,
            )

        latest = self.journal.load_latest().state
        self.assertEqual(latest.phase, "trial_active")
        self.assertIsNone(latest.pending_ack)
        self.assertFalse((self.root / "store" / "outcomes").exists())
        dispatched = mailbox.read_latest()
        assert dispatched is not None
        self.assertIs(dispatched.packet.command, HostCommand.ARM)

    def test_real_per_trial_artifacts_store_receipt_and_ack_full_chain(self) -> None:
        trial, arm = self.arm()
        bound = prepared(trial)
        mailbox_path = self.root / "command.json"
        mailbox = AtomicCommandMailbox(mailbox_path)
        mailbox.send_command(arm, prepared_trial=bound)
        active = mailbox.read_latest()
        assert active is not None
        capture_root = (self.root / "captures").absolute()
        def rtde(state: TpLoopState, *, reason: int = 0):
            return {
                "timestamp": 1.0,
                "output_int_register_24": arm.campaign_epoch,
                "output_int_register_25": arm.trial_id,
                "output_int_register_26": int(state),
                "output_int_register_27": arm.candidate_token,
                "output_int_register_28": reason,
                "output_int_register_29": arm.execution_profile_id,
                "output_int_register_30": arm.command_seq,
                "output_double_register_36": 0.001,
                "output_double_register_37": 0.01,
                "output_double_register_38": 0.005,
                "actual_TCP_pose": [0.4, -0.2, 0.3, 0.0, 0.0, 0.0],
                "actual_q": [0.0] * 6,
                "actual_TCP_speed": [0.0] * 6,
                "actual_qd": [0.0] * 6,
                "safety_mode": "NORMAL",
            }

        def capture_row(index: int, *, reason: int = 0) -> dict[str, object]:
            row: dict[str, object] = {
                "t_monotonic_s": index * 0.02,
                "rtde_feedback_age_s": 0.002,
                "rtde_sent_echo_heartbeat_gap": 0.0,
                "rtde_connected": 1,
                "stop_request": 0,
                "guard_reason": "",
                "step4e_cmd_valid": 1,
                "step4e_controller_state": 524,
                "_step5d_stage25_echo_consumed": 1,
                "_step5d_rnn_accepted": 1,
                "_step5d_safe_hold_active": 0,
                "_step5d_rnn_vs_oracle_qdot_norm": 5e-7,
                "_step5d_raw_rnn_residual_norm": 1e-4,
                "_step5d_post_slew_residual_norm": 1e-4,
                "_step5d_constraint_residual_norm": 1e-4,
                "_step5d_contact_safety_reason": "ok",
                "ur_output_double_register_30": reason,
                "ur_output_double_register_35": 25.0,
            }
            output = rtde(
                TpLoopState.WAIT_ACK if reason else TpLoopState.RUN,
                reason=reason,
            )
            for register in range(24, 31):
                row[f"ur_output_int_register_{register}"] = output[
                    f"output_int_register_{register}"
                ]
            return row

        first_row = capture_row(0)
        rotator = BridgeTrialCsvRotator(capture_root, tuple(first_row))
        for index in range(3000):
            rotator.observe(
                capture_row(index),
                active=active,
                rtde_output=rtde(TpLoopState.RUN),
            )
        wait_ack = rtde(TpLoopState.WAIT_ACK, reason=1)
        rotator.observe(
            capture_row(3000, reason=1),
            active=active,
            rtde_output=wait_ack,
        )
        rotator.close()
        ready = rtde(TpLoopState.READY_HOME)
        for register in (24, 25, 27, 28, 29, 30):
            ready[f"output_int_register_{register}"] = 0
        home_reference = CampaignHomeReference.capture_or_verify(
            (self.root / "campaign_home_reference.json").absolute(),
            binding=active.binding,
            ready_output=ready,
            connection_epoch=0,
        )
        collector = HostClosureCollector(
            expected_arm=arm,
            home_reference=home_reference,
            max_sample_gap_s=0.02,
        )
        for index in range(51):
            collector.observe(wait_ack, monotonic_s=index * 0.01)
        expected_evaluation = Evaluation(
            trial_uid=trial.trial_uid,
            backend_id=trial.backend_id,
            eligible=True,
            disposition=TrialDisposition.OBJECTIVE,
            objective_mae_n=0.2,
            force_bias_n=0.0,
            force_std_n=0.1,
            coverage_12_plus_minus_1_ratio=1.0,
            complete_bins=550,
            safe_closure=True,
        )

        class Backend:
            def evaluate_trial(self, trial_arg, manifest, csv_path):
                self.asserted = (
                    trial_arg == trial
                    and manifest.trial_uid == trial.trial_uid
                    and csv_path == capture_root / trial.trial_uid / "capture.csv"
                )
                return expected_evaluation

        backend = Backend()
        result = finalize_produced_bundle_and_dispatch_ack(
            collector=collector,
            producer=TrialArtifactProducer(capture_root, trial),
            backend=backend,
            store=self.store,
            coordinator=self.coordinator,
            prepared_trial=bound,
            command_sink=mailbox,
        )
        self.assertTrue(backend.asserted)
        self.assertTrue(result.immutable_bundle_path.is_file())
        self.assertEqual(
            result.store_receipt.bundle_path,
            result.immutable_bundle_path,
        )
        self.assertIsNotNone(result.ack_packet)
        dispatched = mailbox.read_latest()
        assert dispatched is not None
        self.assertIs(dispatched.packet.command, HostCommand.ACK_BUNDLE)
        self.assertEqual(
            result.manifest.evidence["immutable_bundle_status"],
            "intent_pending_store_receipt",
        )


class RestartRecoveryTest(RecoveryFixture):
    def test_restart_after_persisted_arm_reissues_exact_arm(self) -> None:
        trial, arm = self.arm()
        restored = self.restore(tp_ready(consumed=0))
        self.assertEqual(restored.decision.action, ReconcileAction.SEND_PERSISTED_ARM)
        self.assertEqual(restored.packet, arm)
        self.assertEqual(restored.coordinator.supervisor.active_trial, trial)

    def test_arm_observed_before_persist_fails_closed(self) -> None:
        self.coordinator.persist_home()
        with self.assertRaisesRegex(RecoveryError, "before_durable_active_persist"):
            self.restore(
                TpSnapshot(1, 1, "ARMED", 1, 0, 111, 1)
            )

    def test_bundle_before_ack_is_recovered_into_a_durable_fresh_ack(self) -> None:
        trial, arm = self.arm()
        write_bundle(self.store, trial, self.root)
        restored = self.restore(tp_for(trial, "WAIT_ACK", consumed=arm.command_seq))
        self.assertEqual(restored.decision.action, ReconcileAction.SEND_PERSISTED_ACK)
        self.assertIsNotNone(restored.packet)
        self.assertGreater(restored.packet.command_seq, arm.command_seq)
        latest = self.journal.load_latest()
        self.assertEqual(latest.state.phase, "wait_ack")
        self.assertEqual(
            latest.state.pending_ack.ack_command_seq,
            restored.packet.command_seq,
        )

    def test_bundle_before_ack_rejects_terminal_reason_mismatch(self) -> None:
        trial, arm = self.arm()
        write_bundle(self.store, trial, self.root, reason=1)
        with self.assertRaisesRegex(RecoveryError, "terminal reason differs"):
            self.restore(
                tp_for(
                    trial,
                    "WAIT_ACK",
                    consumed=arm.command_seq,
                    reason=13,
                )
            )

    def test_consumed_arm_without_bundle_resumes_closure(self) -> None:
        trial, arm = self.arm()
        restored = self.restore(
            tp_for(trial, "WAIT_ACK", consumed=arm.command_seq)
        )
        self.assertEqual(restored.decision.action, ReconcileAction.RESUME_CLOSURE)
        self.assertIsNone(restored.packet)
        self.assertEqual(restored.coordinator.supervisor.active_trial, trial)

    def test_cancel_is_durable_and_never_regresses_high_water_or_tokens(self) -> None:
        trial, _ = self.arm()
        before = self.journal.load_latest().state
        cancelled = self.coordinator.cancel_unconsumed_arm(
            tp_ready(consumed=0),
            mailbox_observation=MailboxObservation.missing(),
        )
        self.assertEqual(cancelled, trial)
        state = self.journal.load_latest().state
        self.assertEqual(state.high_water, before.high_water)
        self.assertEqual(dict(state.candidate_tokens), dict(before.candidate_tokens))
        self.assertEqual(state.terminal_fates[-1].kind, "cancelled_unconsumed")
        next_coordinator = self.coordinator.resume_after_code_change(
            new_journal=SupervisorJournal(self.root / "cancelled-next-epoch"),
            campaign=campaign(epoch=2, fingerprint="9" * 64),
            source_fingerprint="8" * 64,
            config_fingerprint="7" * 64,
        )
        next_state = next_coordinator.journal.load_latest().state
        self.assertEqual(next_state.high_water, before.high_water)
        self.assertEqual(dict(next_state.candidate_tokens), dict(before.candidate_tokens))
        self.assertEqual(next_state.terminal_fates, ())

    def test_consumed_infra_abort_tombstone_crosses_epoch_and_forbids_tuple(self) -> None:
        trial, arm = self.arm()
        sink = FakeContinuousSink()
        self.coordinator.dispatch(arm, prepared_trial=prepared(trial), sink=sink)
        run = self.root / "stopped-bridge"
        partial = run / "autotune_trials" / trial.trial_uid / "capture.csv.part"
        partial.parent.mkdir(parents=True)
        partial.write_text("physically-attempted\n", encoding="utf-8")
        summary = run / "summary.json"
        summary.write_text(
            json.dumps({"stop_reason": "signal_sigint"}) + "\n",
            encoding="utf-8",
        )
        marker = run / ".capture_complete.json"
        marker.write_text(
            json.dumps(
                {
                    "capture_closed": True,
                    "immutable": True,
                    "source_files": [
                        {
                            "path": "summary.json",
                            "sha256": hashlib.sha256(summary.read_bytes()).hexdigest(),
                        },
                        {
                            "path": f"autotune_trials/{trial.trial_uid}/capture.csv.part",
                            "sha256": hashlib.sha256(partial.read_bytes()).hexdigest(),
                        },
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        marker_sha = hashlib.sha256(marker.read_bytes()).hexdigest()
        evidence = JournalReference(
            reference_id=marker_sha,
            path=str(marker),
            sha256=marker_sha,
        )

        terminalized = self.coordinator.terminalize_consumed_infra_abort(
            tp_ready(consumed=arm.command_seq),
            evidence=evidence,
        )

        self.assertEqual(terminalized, trial)
        state = self.journal.load_latest().state
        self.assertEqual(state.phase, "home")
        self.assertEqual(state.terminal_fates[-1].kind, "infra_aborted_consumed")
        next_coordinator = self.coordinator.resume_after_code_change(
            new_journal=SupervisorJournal(self.root / "infra-next-epoch"),
            campaign=campaign(epoch=2, fingerprint="9" * 64),
            source_fingerprint="8" * 64,
            config_fingerprint="7" * 64,
        )
        next_state = next_coordinator.journal.load_latest().state
        self.assertEqual(
            [fate.kind for fate in next_state.terminal_fates],
            ["infra_aborted_consumed"],
        )
        restarted = CampaignCoordinator.restore(
            supervisor=supervisor(
                campaign_spec=campaign(epoch=2, fingerprint="9" * 64),
                source="8" * 64,
                config="7" * 64,
            ),
            journal=next_coordinator.journal,
            latest=next_coordinator.journal.load_latest(),
            resume_history=[],
            promotion_history=[],
            tp_snapshot=tp_ready(consumed=arm.command_seq),
        )
        self.assertEqual(restarted.decision.action, ReconcileAction.RESUME_HOME)
        with self.assertRaisesRegex(ValueError, "already physically attempted"):
            restarted.coordinator.issue_arm(
                self.store,
                require_cuda_botorch=False,
                forced_candidate=trial.candidate,
            )

    def test_exact_mailbox_crash_gap_is_adopted_and_blocks_cancel(self) -> None:
        trial, arm = self.arm()
        sink = FakeContinuousSink()
        sink.send_command(arm, prepared_trial=prepared(trial))
        observation = MailboxObservation.from_command(sink.read_latest())
        with self.assertRaisesRegex(CoordinatorError, "not proven consumed"):
            self.coordinator.cancel_unconsumed_arm(
                tp_ready(consumed=0), mailbox_observation=observation
            )
        restored = self.restore(
            tp_ready(consumed=0), mailbox_observation=observation
        )
        self.assertEqual(restored.packet, arm)
        receipt = self.journal.load_latest().state.dispatch_receipt
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.command_seq, arm.command_seq)

    def test_persisted_ack_before_send_is_reissued_without_new_sequence(self) -> None:
        trial, _ = self.arm()
        _, ack = self.close_and_ack(trial)
        restored = self.restore(tp_for(trial, "WAIT_ACK", consumed=trial.command_seq))
        self.assertEqual(restored.decision.action, ReconcileAction.SEND_PERSISTED_ACK)
        self.assertEqual(restored.packet, ack)
        self.assertEqual(self.journal.load_latest().revision, 2)

    def test_ack_consumed_before_postpersist_closes_only_after_exact_echo(self) -> None:
        trial, _ = self.arm()
        _, ack = self.close_and_ack(trial)
        restored = self.restore(tp_ready(consumed=ack.command_seq))
        self.assertEqual(restored.decision.action, ReconcileAction.PERSIST_POST_ACK)
        self.assertIsNone(restored.packet)
        self.assertEqual(restored.coordinator.supervisor.phase, CampaignPhase.HOME)
        latest = self.journal.load_latest().state
        self.assertEqual(latest.phase, "home")
        self.assertEqual(latest.terminal_fates[-1].kind, "ack_consumed")

    def test_infrastructure_ack_recovers_with_durable_pause_origin(self) -> None:
        trial, _ = self.arm()
        _, ack = self.close_and_ack(
            trial,
            reason=12,
            disposition=TrialDisposition.WAIT_INFRA_READY,
            objective=None,
        )
        restored = self.restore(
            tp_for(trial, "WAIT_INFRA_READY", consumed=ack.command_seq, reason=12)
        )
        self.assertEqual(restored.decision.action, ReconcileAction.PERSIST_POST_ACK)
        self.assertEqual(
            restored.coordinator.supervisor.phase,
            CampaignPhase.WAIT_INFRA_READY,
        )
        latest = self.journal.load_latest()
        self.assertEqual(latest.state.pending_retry.origin_trial.candidate_token, 1)
        self.assertEqual(latest.state.pending_retry.kind, "infrastructure")

    def test_wait_infra_release_arm_crash_reissues_untried_candidate(self) -> None:
        trial, _ = self.arm()
        _, ack = self.close_and_ack(
            trial,
            reason=12,
            disposition=TrialDisposition.WAIT_INFRA_READY,
            objective=None,
        )
        waiting_tp = tp_for(
            trial,
            "WAIT_INFRA_READY",
            consumed=ack.command_seq,
            reason=12,
        )
        restored = self.restore(waiting_tp)
        restored.coordinator.mark_infrastructure_ready(waiting_tp)
        arm_retry = restored.coordinator.issue_arm(
            self.store,
            require_cuda_botorch=False,
        )
        retry_trial = restored.coordinator.supervisor.active_trial
        assert retry_trial is not None
        self.assertNotEqual(retry_trial.candidate, trial.candidate)
        self.assertNotEqual(retry_trial.candidate_token, trial.candidate_token)
        self.assertGreater(retry_trial.trial_id, trial.trial_id)

        crashed = self.restore(waiting_tp)
        self.assertEqual(
            crashed.decision.action,
            ReconcileAction.SEND_PERSISTED_ARM,
        )
        self.assertEqual(crashed.packet, arm_retry)
        release = self.journal.load_latest().state.active_trial.retry_release
        assert release is not None
        self.assertEqual(release.origin_trial_uid, trial.trial_uid)
        self.assertEqual(release.consumed_command_seq, ack.command_seq)

    def test_reason13_ack_fault_persists_and_restores_paused_code_state(self) -> None:
        trial, _ = self.arm()
        _, ack = self.close_and_ack(
            trial,
            reason=13,
            disposition=TrialDisposition.CODE_CONTRACT_BUG,
            objective=None,
        )
        fault = tp_for(
            trial,
            "FAULT",
            consumed=ack.command_seq,
            reason=13,
        )
        restored = self.restore(fault)
        self.assertEqual(restored.decision.action, ReconcileAction.PERSIST_POST_ACK)
        self.assertEqual(
            restored.coordinator.supervisor.phase,
            CampaignPhase.PAUSED_CODE_BUG,
        )
        restarted = self.restore(fault)
        self.assertEqual(restarted.decision.action, ReconcileAction.HOLD_TERMINAL)
        self.assertEqual(
            restarted.coordinator.supervisor.phase,
            CampaignPhase.PAUSED_CODE_BUG,
        )

    def test_safety_reason_ack_fault_persists_stopped_safety(self) -> None:
        trial, _ = self.arm()
        _, ack = self.close_and_ack(
            trial,
            reason=5,
            disposition=TrialDisposition.SAFETY_STOP,
            objective=None,
        )
        restored = self.restore(
            tp_for(trial, "FAULT", consumed=ack.command_seq, reason=5)
        )
        self.assertEqual(restored.decision.action, ReconcileAction.PERSIST_POST_ACK)
        self.assertEqual(
            restored.coordinator.supervisor.phase,
            CampaignPhase.STOPPED_SAFETY,
        )

    def test_reason4_infra_ack_ready_home_keeps_host_wait_infra(self) -> None:
        trial, _ = self.arm()
        _, ack = self.close_and_ack(
            trial,
            reason=4,
            disposition=TrialDisposition.WAIT_INFRA_READY,
            objective=None,
            host_cause="infra_stop",
        )
        ready = tp_ready(consumed=ack.command_seq)
        restored = self.restore(ready)
        self.assertEqual(restored.decision.action, ReconcileAction.PERSIST_POST_ACK)
        self.assertEqual(
            restored.coordinator.supervisor.phase,
            CampaignPhase.WAIT_INFRA_READY,
        )
        restarted = self.restore(ready)
        self.assertEqual(restarted.decision.action, ReconcileAction.HOLD_WAIT_INFRA)

    def test_promotion_rows_must_equal_the_eligible_resume_subset(self) -> None:
        trial, _ = self.arm()
        _, ack = self.close_and_ack(trial)
        with self.assertRaisesRegex(RecoveryError, "promotion history"):
            CampaignCoordinator.restore(
                supervisor=supervisor(),
                journal=self.journal,
                latest=self.journal.load_latest(),
                resume_history=self.store.read_resume_history(),
                promotion_history=[],
                tp_snapshot=tp_ready(consumed=ack.command_seq),
            )


class EpochAndGovernorJournalTest(RecoveryFixture):
    def test_code_change_requires_a_new_journal_root(self) -> None:
        self.coordinator.persist_home()
        self.manager.phase = CampaignPhase.PAUSED_CODE_BUG
        with self.assertRaisesRegex(CoordinatorError, "distinct new journal"):
            self.coordinator.resume_after_code_change(
                new_journal=self.journal,
                campaign=campaign(epoch=2, fingerprint="9" * 64),
                source_fingerprint="8" * 64,
                config_fingerprint="7" * 64,
            )
        new = self.coordinator.resume_after_code_change(
            new_journal=SupervisorJournal(self.root / "journal-epoch-2"),
            campaign=campaign(epoch=2, fingerprint="9" * 64),
            source_fingerprint="8" * 64,
            config_fingerprint="7" * 64,
        )
        self.assertEqual(new.journal.load_latest().state.campaign.campaign_epoch, 2)

    def test_code_epoch_restart_reconstructs_prior_durable_trial_spec_reference(self) -> None:
        trial, _ = self.arm()
        _, ack = self.close_and_ack(
            trial,
            reason=13,
            disposition=TrialDisposition.CODE_CONTRACT_BUG,
            objective=None,
        )
        self.coordinator.reconcile(
            tp_for(trial, "FAULT", consumed=ack.command_seq, reason=13)
        )
        next_campaign = campaign(epoch=2, fingerprint="9" * 64)
        next_journal = SupervisorJournal(self.root / "journal-epoch-2")
        next_coordinator = self.coordinator.resume_after_code_change(
            new_journal=next_journal,
            campaign=next_campaign,
            source_fingerprint="8" * 64,
            config_fingerprint="7" * 64,
        )
        restarted = CampaignCoordinator.restore(
            supervisor=supervisor(
                campaign_spec=next_campaign,
                source="8" * 64,
                config="7" * 64,
            ),
            journal=next_journal,
            latest=next_coordinator.journal.load_latest(),
            resume_history=(),
            promotion_history=(),
            prior_resume_history=self.store.read_resume_history(),
            tp_snapshot=tp_ready(consumed=ack.command_seq),
        )
        current_store = CampaignStore(self.root / "store-epoch-2")
        restarted.coordinator.issue_arm(
            current_store, require_cuda_botorch=False
        )
        active = restarted.coordinator.supervisor.active_trial
        assert active is not None
        self.assertEqual(active.transition.source.trial_uid, trial.trial_uid)

    def test_governor_keep_changes_plant_profile_without_regressing_highwater(self) -> None:
        self.coordinator.persist_home()
        trial_a, _ = self.arm()
        _, ack_a = self.close_and_ack(trial_a, objective=0.5)
        self.coordinator.reconcile(tp_ready(consumed=ack_a.command_seq))
        samples = [
            SaturationSample(index * 0.1, normal_filter_limited=True)
            for index in range(110)
        ]
        candidate, _ = self.coordinator.begin_governor_probe(samples)
        self.assertIsNotNone(candidate)
        trial_b, _ = self.arm()
        self.assertEqual(trial_b.candidate, trial_a.candidate)
        self.assertEqual(trial_b.execution_profile, candidate)
        _, ack_b = self.close_and_ack(
            trial_b, objective=0.5, governor_burden=0.6
        )
        self.coordinator.reconcile(tp_ready(consumed=ack_b.command_seq))
        before = self.journal.load_latest().state.high_water
        decision = self.coordinator.complete_governor_probe(
            AbEvidence(
                burden_a=1.0,
                burden_b=0.6,
                mae_a_n=0.5,
                mae_b_n=0.5,
                tracking_not_worse=True,
                orientation_not_worse=True,
                guards_clean=True,
                safe_closure=True,
            )
        )
        self.assertTrue(decision.keep)
        latest = self.journal.load_latest().state
        self.assertEqual(latest.plant_epoch, 2)
        self.assertEqual(latest.execution_profile_id, "nf015-slew010-a010")
        self.assertEqual(latest.high_water, before)
        self.assertEqual(latest.cooldown_remaining, 3)
        restored = CampaignCoordinator.restore(
            supervisor=supervisor(
                execution_profile=profile(0.015),
                plant_epoch=2,
            ),
            journal=self.journal,
            latest=self.journal.load_latest(),
            resume_history=self.store.read_resume_history(),
            promotion_history=self.store.read_promotion_history(),
            tp_snapshot=tp_ready(consumed=ack_b.command_seq),
            profile_catalog=(profile(),),
        )
        self.assertEqual(restored.coordinator.supervisor.plant_epoch, 2)
        self.assertEqual(restored.coordinator.supervisor.cooldown_remaining, 3)

    def test_governor_probe_restore_preserves_exact_a_and_reissues_frozen_b(self) -> None:
        self.coordinator.persist_home()
        trial_a, _ = self.arm()
        _, ack_a = self.close_and_ack(trial_a, objective=0.5)
        self.coordinator.reconcile(tp_ready(consumed=ack_a.command_seq))
        samples = [
            SaturationSample(index * 0.1, normal_filter_limited=True)
            for index in range(110)
        ]
        profile_b, _ = self.coordinator.begin_governor_probe(samples)
        assert profile_b is not None
        persisted = self.journal.load_latest().state.governor_probe
        assert persisted is not None
        self.assertEqual(persisted.stage, "b")
        self.assertEqual(persisted.force_candidate_uid, trial_a.candidate.candidate_uid)
        self.assertEqual(persisted.trial_a_uid, trial_a.trial_uid)
        self.assertIsNone(persisted.trial_b_uid)

        restored = CampaignCoordinator.restore(
            supervisor=supervisor(),
            journal=self.journal,
            latest=self.journal.load_latest(),
            resume_history=self.store.read_resume_history(),
            promotion_history=self.store.read_promotion_history(),
            tp_snapshot=tp_ready(consumed=ack_a.command_seq),
            profile_catalog=(profile_b,),
            mailbox_observation=MailboxObservation.missing(),
        )
        restored_probe = restored.coordinator.supervisor.recovery_snapshot().governor_probe
        assert restored_probe is not None
        self.assertEqual(restored_probe.identity_a.trial_uid, trial_a.trial_uid)
        self.assertEqual(restored_probe.force_candidate, trial_a.candidate)
        self.assertEqual(restored_probe.profile_b, profile_b)

        arm_b = restored.coordinator.issue_arm(
            self.store, require_cuda_botorch=False
        )
        trial_b = restored.coordinator.supervisor.active_trial
        assert trial_b is not None
        self.assertEqual(trial_b.candidate, trial_a.candidate)
        self.assertEqual(trial_b.execution_profile, profile_b)
        restarted_active = CampaignCoordinator.restore(
            supervisor=supervisor(),
            journal=self.journal,
            latest=self.journal.load_latest(),
            resume_history=self.store.read_resume_history(),
            promotion_history=self.store.read_promotion_history(),
            tp_snapshot=tp_ready(consumed=ack_a.command_seq),
            profile_catalog=(profile_b,),
            mailbox_observation=MailboxObservation.missing(),
        )
        self.assertEqual(restarted_active.decision.action, ReconcileAction.SEND_PERSISTED_ARM)
        self.assertEqual(restarted_active.packet, arm_b)
        active_probe = restarted_active.coordinator.supervisor.recovery_snapshot().governor_probe
        assert active_probe is not None
        self.assertEqual(active_probe.identity_a.trial_uid, trial_a.trial_uid)
        self.assertIsNone(active_probe.identity_b)

    def test_single_success_restores_succeeded_without_probe(self) -> None:
        self.coordinator.persist_home()
        trial_a, _ = self.arm()
        _, ack_a = self.close_and_ack(trial_a, objective=0.29)
        self.coordinator.reconcile(tp_ready(consumed=ack_a.command_seq))
        restored = CampaignCoordinator.restore(
            supervisor=supervisor(),
            journal=self.journal,
            latest=self.journal.load_latest(),
            resume_history=self.store.read_resume_history(),
            promotion_history=self.store.read_promotion_history(),
            tp_snapshot=tp_ready(consumed=ack_a.command_seq),
        )
        self.assertEqual(
            restored.coordinator.supervisor.phase, CampaignPhase.SUCCEEDED
        )
        self.assertIsNone(
            restored.coordinator.supervisor.recovery_snapshot().governor_probe
        )

    def test_ambiguous_governor_revert_restores_without_repeat_probe(self) -> None:
        self.coordinator.persist_home()
        trial_a, _ = self.arm()
        _, ack_a = self.close_and_ack(trial_a, objective=0.5)
        self.coordinator.reconcile(tp_ready(consumed=ack_a.command_seq))
        samples = [
            SaturationSample(index * 0.1, normal_filter_limited=True)
            for index in range(110)
        ]
        profile_b, _ = self.coordinator.begin_governor_probe(samples)
        assert profile_b is not None
        trial_b, _ = self.arm()
        _, ack_b = self.close_and_ack(
            trial_b, objective=0.5, governor_burden=0.8
        )
        self.coordinator.reconcile(tp_ready(consumed=ack_b.command_seq))
        decision = self.coordinator.complete_governor_probe(
            AbEvidence(
                burden_a=1.0,
                burden_b=0.8,
                mae_a_n=0.5,
                mae_b_n=0.5,
                tracking_not_worse=True,
                orientation_not_worse=True,
                guards_clean=True,
                safe_closure=True,
            )
        )
        self.assertEqual(decision.action, "revert")
        self.assertIsNone(self.journal.load_latest().state.governor_probe)
        restored = CampaignCoordinator.restore(
            supervisor=supervisor(),
            journal=self.journal,
            latest=self.journal.load_latest(),
            resume_history=self.store.read_resume_history(),
            promotion_history=self.store.read_promotion_history(),
            tp_snapshot=tp_ready(consumed=ack_b.command_seq),
            profile_catalog=(profile_b,),
        )
        probe = restored.coordinator.supervisor.recovery_snapshot().governor_probe
        self.assertIsNone(probe)
        self.assertEqual(
            restored.coordinator.supervisor.execution_profile,
            trial_a.execution_profile,
        )
        self.assertEqual(restored.coordinator.supervisor.cooldown_remaining, 3)


if __name__ == "__main__":
    unittest.main()
