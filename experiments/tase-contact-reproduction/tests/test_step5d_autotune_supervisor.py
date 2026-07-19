from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import (  # noqa: E402
    CampaignSpec,
    CaptureManifest,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    TrialTransitionKind,
    TrialDisposition,
)
from step5d_autotune_governor import (  # noqa: E402
    AbEvidence,
    AbTrialIdentity,
    SaturationSample,
)
from step5d_autotune_journal import TpSnapshot  # noqa: E402
from step5d_autotune_state_machine import SafeClosureEvidence  # noqa: E402
from step5d_autotune_optimizer import Observation  # noqa: E402
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
        campaign_id="supervisor-fixture",
        campaign_epoch=epoch,
        campaign_fingerprint=fingerprint,
        f0_shadow_reaction_normal_base=(0.0, 0.0, 1.0),
    )


def profile() -> ExecutionProfile:
    return ExecutionProfile("nf010-slew010-a010", 0.010)


def supervisor(*, selection_policy: str = "adaptive") -> CampaignSupervisor:
    return CampaignSupervisor(
        campaign=campaign(),
        backend_id="step5d_v35_native",
        source_fingerprint=SHA_B,
        config_fingerprint=SHA_C,
        execution_profile=profile(),
        selection_policy=selection_policy,
    )


def ready_home(*, consumed_command_seq: int = 0) -> TpSnapshot:
    return TpSnapshot(
        campaign_epoch_echo=0,
        trial_id_echo=0,
        state="READY_HOME",
        candidate_token_echo=0,
        terminal_reason=0,
        execution_profile_integer_id_echo=0,
        consumed_command_seq=consumed_command_seq,
    )


def closure(**updates) -> SafeClosureEvidence:
    payload = {
        "tp_position_error_m": 0.001,
        "tp_orientation_error_rad": 0.01,
        "tp_joint_error_max_rad": 0.005,
        "host_position_error_m": 0.001,
        "host_orientation_error_rad": 0.01,
        "host_joint_error_max_rad": 0.005,
        "host_tcp_linear_speed_m_s": 0.0005,
        "host_tcp_angular_speed_rad_s": 0.005,
        "host_qd_max_rad_s": 0.005,
        "host_safety_mode": "NORMAL",
        "host_dwell_s": 0.5,
        "trial_token_match": True,
        "capture_hashes_complete": True,
        "terminal_manifest_complete": True,
        "fingerprint_closed": True,
    }
    payload.update(updates)
    return SafeClosureEvidence(**payload)


def capture(trial, *, reason: int, host_cause: str | None = None) -> CaptureManifest:
    return CaptureManifest(
        trial_uid=trial.trial_uid,
        backend_id=trial.backend_id,
        source_fingerprint_pre=trial.source_fingerprint,
        source_fingerprint_post=trial.source_fingerprint,
        config_fingerprint_pre=trial.config_fingerprint,
        config_fingerprint_post=trial.config_fingerprint,
        candidate_token=trial.candidate_token,
        terminal_reason=reason,
        host_cause=host_cause,
        csv_sha256="d" * 64,
        metadata_sha256="e" * 64,
        terminal_manifest_sha256="f" * 64,
        completion_marker=reason == 1,
        cadence_ok=True,
        feedback_fresh=True,
        rnn_oracle_aligned=True,
        safety_normal=True,
        returned_safe=True,
        immutable_bundle_written=True,
        stage25_complete_s=60.0 if reason == 1 else 0.0,
        safe_closure_evidence=closure(),
    )


def evaluation(
    trial,
    *,
    disposition: TrialDisposition,
    objective: float | None = None,
    safe: bool = True,
    governor_burden: float = 1.0,
    tracking_lag_s: float = 0.01,
    tracking_correlation: float = 0.98,
    tracking_nrmse: float = 0.1,
    normal_filter_persistent: bool = True,
) -> Evaluation:
    eligible = disposition is TrialDisposition.OBJECTIVE
    return Evaluation(
        trial_uid=trial.trial_uid,
        backend_id=trial.backend_id,
        eligible=eligible,
        disposition=disposition,
        objective_mae_n=objective if eligible else None,
        force_bias_n=0.0 if eligible else None,
        force_std_n=0.1 if eligible else None,
        coverage_12_plus_minus_1_ratio=1.0 if eligible else None,
        complete_bins=550 if eligible else 0,
        safe_closure=safe,
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
                    "lag_s": tracking_lag_s,
                    "correlation": tracking_correlation,
                    "nrmse": tracking_nrmse,
                },
                "trigger": {
                    "source": "immutable_trial_csv",
                    "sample_count": 110,
                    "normal_filter_persistent": normal_filter_persistent,
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
                    "saturation_duty": governor_burden,
                },
            }
        },
    )


def profile_diagnostic_evaluation(
    trial,
    *,
    objective: float = 0.5,
    governor_burden: float = 0.2,
    transport_ineligible: bool = False,
) -> Evaluation:
    failures = (
        (
            "cadence_failed",
            "feedback_failed",
            "orientation_profile_unqualified",
        )
        if transport_ineligible
        else ("orientation_profile_unqualified",)
    )
    return Evaluation(
        trial_uid=trial.trial_uid,
        backend_id=trial.backend_id,
        eligible=False,
        disposition=TrialDisposition.FAIL_CLOSED,
        objective_mae_n=None,
        force_bias_n=None,
        force_std_n=None,
        coverage_12_plus_minus_1_ratio=None,
        complete_bins=550,
        safe_closure=True,
        structural_failures=failures,
        metrics={
            "governor": {
                "burden_by_layer": {
                    "normal_filter_rate": governor_burden,
                    "host_qdot_slew": 0.0,
                    "tp_speedj_acceleration": 0.0,
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
                "nontrainable_profile_diagnostic": {
                    "available": True,
                    "trainable_objective": False,
                    "failure_scope": (
                        "governor_profile_nontrainable"
                        if transport_ineligible
                        else "orientation_profile_unqualified"
                    ),
                    "structural_failures": (
                        list(failures) if transport_ineligible else None
                    ),
                    "force_mae_n": objective,
                },
                "orientation": {
                    "evidence_complete": True,
                    "qualified": False,
                    "p95_error_rad": 0.01,
                    "max_error_rad": 0.02,
                    "saturation_duty": governor_burden,
                },
            }
        },
    )


def bundle(root: Path, trial) -> Path:
    path = root / trial.trial_uid / "immutable_trial_bundle.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"trial_uid": trial.trial_uid}) + "\n")
    return path


def seed_t3_outcome(
    manager: CampaignSupervisor,
    *,
    objective: float = 0.29,
    uid_digit: str = "1",
) -> Observation:
    candidate = ForceCandidate.from_log2(p=1.75, damping=0.0, i=0.0)
    identity = SimpleNamespace(
        trial_uid=uid_digit * 64,
        backend_id=manager.backend_id,
    )
    result = evaluation(
        identity,
        disposition=TrialDisposition.OBJECTIVE,
        objective=objective,
    )
    seeded = Observation(
        candidate=candidate,
        evaluation=result,
        profile_id=manager.execution_profile.profile_id,
        plant_epoch=manager.plant_epoch,
        latest_trace_sha256="2" * 64,
    )
    manager.outcome_timeline.append(seeded)
    manager.observations.append(seeded)
    return seeded


class Step5dAutotuneSupervisorTest(unittest.TestCase):
    def test_fresh_campaign_continues_tp_global_command_sequence(self) -> None:
        manager = supervisor()
        manager.seed_command_sequence_from_tp(6)
        trial = manager.next_trial(require_cuda_botorch=False).trial
        self.assertEqual(trial.command_seq, 7)

    def test_cancel_unconsumed_arm_preserves_all_high_water_marks(self) -> None:
        manager = supervisor()
        trial = manager.next_trial(require_cuda_botorch=False).trial
        before = manager.recovery_snapshot()

        cancelled = manager.cancel_unconsumed_arm(
            ready_home(),
            persisted_trial_uid=trial.trial_uid,
            has_dispatch_receipt=False,
        )

        after = manager.recovery_snapshot()
        self.assertEqual(cancelled, trial)
        self.assertEqual(manager.phase, CampaignPhase.HOME)
        self.assertIsNone(after.active)
        self.assertEqual(after.trial_counter, before.trial_counter)
        self.assertEqual(after.command_seq, before.command_seq)
        self.assertEqual(after.candidate_tokens, before.candidate_tokens)
        self.assertEqual(
            after.candidate_tokens[trial.candidate.candidate_uid],
            trial.candidate_token,
        )
        self.assertEqual(manager._next_candidate_token, trial.candidate_token + 1)

        distinct = ForceCandidate.from_log2(p=0.25, damping=0.0, i=0.0)
        self.assertEqual(manager._token_for(distinct), trial.candidate_token + 1)
        next_trial = manager.next_trial(require_cuda_botorch=False).trial
        self.assertEqual(next_trial.trial_id, trial.trial_id + 1)
        self.assertEqual(next_trial.command_seq, trial.command_seq + 1)
        self.assertEqual(next_trial.candidate_token, trial.candidate_token)

    def test_cancel_unconsumed_arm_preconditions_fail_closed(self) -> None:
        manager = supervisor()
        trial = manager.next_trial(require_cuda_botorch=False).trial
        snapshot = ready_home()

        with self.assertRaisesRegex(ValueError, "persisted_trial_uid"):
            manager.cancel_unconsumed_arm(
                snapshot,
                persisted_trial_uid="not-a-trial-uid",
                has_dispatch_receipt=False,
            )
        with self.assertRaisesRegex(ValueError, "has_dispatch_receipt"):
            manager.cancel_unconsumed_arm(
                snapshot,
                persisted_trial_uid=trial.trial_uid,
                has_dispatch_receipt=0,
            )

        wrong_trial_uid = (
            ("0" if trial.trial_uid[0] != "0" else "1") + trial.trial_uid[1:]
        )
        with self.assertRaisesRegex(RuntimeError, "journal persistence"):
            manager.cancel_unconsumed_arm(
                snapshot,
                persisted_trial_uid=wrong_trial_uid,
                has_dispatch_receipt=False,
            )
        with self.assertRaisesRegex(RuntimeError, "dispatched ARM"):
            manager.cancel_unconsumed_arm(
                snapshot,
                persisted_trial_uid=trial.trial_uid,
                has_dispatch_receipt=True,
            )

        invalid_snapshots = (
            replace(snapshot, state="ARMED"),
            replace(snapshot, consumed_command_seq=trial.command_seq),
            replace(snapshot, campaign_epoch_echo=1),
            replace(snapshot, trial_id_echo=1),
            replace(snapshot, candidate_token_echo=1),
            replace(snapshot, execution_profile_integer_id_echo=111),
            replace(snapshot, terminal_reason=1),
        )
        for invalid_snapshot in invalid_snapshots:
            with self.subTest(snapshot=invalid_snapshot):
                with self.assertRaisesRegex(RuntimeError, "unconsumed at Home"):
                    manager.cancel_unconsumed_arm(
                        invalid_snapshot,
                        persisted_trial_uid=trial.trial_uid,
                        has_dispatch_receipt=False,
                    )
                self.assertEqual(manager.active_trial, trial)
                self.assertEqual(manager.phase, CampaignPhase.TRIAL_ACTIVE)

    def test_forced_candidate_uses_executed_neighbor_without_repeating_it(self) -> None:
        manager = supervisor()
        identity = SimpleNamespace(trial_uid="9" * 64, backend_id=manager.backend_id)
        seed = ForceCandidate()
        manager.outcome_timeline.append(
            Observation(
                candidate=seed,
                evaluation=profile_diagnostic_evaluation(identity),
                profile_id=manager.execution_profile.profile_id,
                plant_epoch=manager.plant_epoch,
                latest_trace_sha256="8" * 64,
            )
        )
        requested = ForceCandidate.from_log2(p=0.25, damping=0.0, i=0.0)
        trial = manager.next_trial(
            require_cuda_botorch=False,
            forced_candidate=requested,
        ).trial
        self.assertEqual(trial.candidate, requested)
        self.assertEqual(trial.transition.kind, TrialTransitionKind.FORCE_SEARCH)
        self.assertEqual(trial.transition.source.trial_uid, identity.trial_uid)

    def test_codex_batch_can_probe_positive_i_early_without_widening_p_d(self) -> None:
        identity = SimpleNamespace(trial_uid="9" * 64, backend_id="step5d_v35_native")
        seed = ForceCandidate()

        def seeded_manager(selection_policy: str) -> CampaignSupervisor:
            manager = supervisor(selection_policy=selection_policy)
            manager.outcome_timeline.append(
                Observation(
                    candidate=seed,
                    evaluation=profile_diagnostic_evaluation(identity),
                    profile_id=manager.execution_profile.profile_id,
                    plant_epoch=manager.plant_epoch,
                    latest_trace_sha256="8" * 64,
                )
            )
            return manager

        positive_i_probe = ForceCandidate.from_log2(p=0.0, damping=0.0, i=0.25)
        with self.assertRaisesRegex(ValueError, "selection-policy envelope"):
            seeded_manager("adaptive").next_trial(
                require_cuda_botorch=False,
                forced_candidate=positive_i_probe,
            )

        trial = seeded_manager("codex_batches").next_trial(
            require_cuda_botorch=False,
            forced_candidate=positive_i_probe,
        ).trial
        self.assertEqual(trial.candidate, positive_i_probe)

        coarse_i_probe = ForceCandidate.from_i_multiplier(
            p=0.0,
            damping=0.0,
            i_multiplier=100.0,
        )
        with self.assertRaisesRegex(ValueError, "selection-policy envelope"):
            seeded_manager("adaptive").next_trial(
                require_cuda_botorch=False,
                forced_candidate=coarse_i_probe,
            )
        coarse_trial = seeded_manager("codex_batches").next_trial(
            require_cuda_botorch=False,
            forced_candidate=coarse_i_probe,
        ).trial
        self.assertEqual(coarse_trial.candidate, coarse_i_probe)
        self.assertEqual(
            coarse_trial.transition.kind,
            TrialTransitionKind.I_SCALE_PROBE,
        )

        migrated = seeded_manager("codex_batches")
        migrated.resume_after_code_change(
            campaign=campaign(epoch=2, fingerprint="9" * 64),
            source_fingerprint="8" * 64,
            config_fingerprint="7" * 64,
        )
        migrated_trial = migrated.next_trial(
            require_cuda_botorch=False,
            forced_candidate=coarse_i_probe,
        ).trial
        self.assertEqual(migrated_trial.candidate, coarse_i_probe)
        self.assertEqual(
            migrated_trial.transition.kind,
            TrialTransitionKind.CODE_EPOCH_SEARCH,
        )
        self.assertEqual(
            migrated_trial.transition.source.trial_uid,
            identity.trial_uid,
        )

        replay_manager = seeded_manager("codex_batches")
        replay_manager.resume_after_code_change(
            campaign=campaign(epoch=2, fingerprint="9" * 64),
            source_fingerprint="8" * 64,
            config_fingerprint="7" * 64,
        )
        replay = replay_manager.next_trial(
            require_cuda_botorch=False,
            forced_candidate=seed,
            allow_archived_code_fix_replay=True,
        ).trial
        self.assertEqual(replay.candidate, seed)
        self.assertEqual(replay.transition.kind, TrialTransitionKind.RETRY)
        self.assertEqual(replay.transition.retry_kind, "code_fix")

        widened_p_probe = ForceCandidate.from_log2(p=1.25, damping=0.0, i=0.25)
        with self.assertRaisesRegex(ValueError, "selection-policy envelope"):
            seeded_manager("codex_batches").next_trial(
                require_cuda_botorch=False,
                forced_candidate=widened_p_probe,
            )

    def close_and_ack(self, manager, trial, manifest, result, root, **kwargs):
        decision = manager.close_trial(
            manifest=manifest,
            evaluation=result,
            safe_closure=kwargs.pop("safe_closure", closure()),
            bundle_path=kwargs.pop("bundle_path", bundle(root, trial)),
            **kwargs,
        )
        self.assertTrue(decision.ack_permitted)
        packet = manager.ack_bundle()
        self.assertGreater(packet.command_seq, trial.command_seq)
        return decision

    def test_optimizer_admission_requires_exact_ack_and_survives_recovery(self) -> None:
        manager = supervisor()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial = manager.next_trial(require_cuda_botorch=False).trial
            decision = manager.close_trial(
                manifest=capture(trial, reason=1),
                evaluation=evaluation(
                    trial,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.25,
                ),
                safe_closure=closure(),
                bundle_path=bundle(root, trial),
            )

            self.assertTrue(decision.ack_permitted)
            self.assertEqual(len(manager.outcome_timeline), 1)
            self.assertEqual(manager.observations, [])
            packet = manager.prepare_ack_packet()

            restored_waiting = supervisor()
            restored_waiting.restore_recovery_snapshot(
                manager.recovery_snapshot()
            )
            self.assertEqual(len(restored_waiting.outcome_timeline), 1)
            self.assertEqual(restored_waiting.observations, [])

            restored_waiting.confirm_ack_consumed(packet)
            self.assertEqual(len(restored_waiting.observations), 1)
            self.assertEqual(
                restored_waiting.observations[0].evaluation.trial_uid,
                trial.trial_uid,
            )

            restored_closed = supervisor()
            restored_closed.restore_recovery_snapshot(
                restored_waiting.recovery_snapshot()
            )
            self.assertEqual(len(restored_closed.observations), 1)

    def test_profile_integer_contract_uses_tp_accel_ones_digit(self) -> None:
        self.assertEqual(execution_profile_integer_id(profile()), 111)
        changed = replace(
            profile(),
            profile_id="nf010-slew010-a050",
            tp_speedj_accel_rad_s2=0.5,
        )
        self.assertEqual(execution_profile_integer_id(changed), 113)
        fixed_live = replace(
            profile(),
            profile_id="nf050-slew050-a050",
            normal_max_rate_rad_s=0.05,
            host_qdot_slew_rad_s2=0.5,
            tp_speedj_accel_rad_s2=0.5,
        )
        self.assertEqual(execution_profile_integer_id(fixed_live), 533)

    def test_governor_requires_a_real_eligible_safe_a_outcome(self) -> None:
        samples = [
            SaturationSample(index * 0.1, normal_filter_limited=True)
            for index in range(110)
        ]
        with self.assertRaisesRegex(RuntimeError, "profile-diagnostic safe A trial"):
            supervisor().begin_governor_probe(samples)

    def test_saturated_profile_diagnostic_stays_out_of_bo_and_starts_governor(
        self,
    ) -> None:
        manager = supervisor()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial_a = manager.next_trial(require_cuda_botorch=False).trial
            decision = self.close_and_ack(
                manager,
                trial_a,
                capture(trial_a, reason=1),
                profile_diagnostic_evaluation(trial_a),
                root,
            )
            self.assertEqual(decision.reason, "governor_profile_diagnostic_ready")
            self.assertEqual(manager.observations, [])
            profile_b, proposal = manager.begin_governor_probe()

        self.assertIsNotNone(profile_b)
        assert profile_b is not None
        self.assertEqual(profile_b.normal_max_rate_rad_s, 0.015)
        self.assertEqual(proposal.layer, "normal_filter_rate")

    def test_transport_ineligible_profile_diagnostic_can_start_governor(self) -> None:
        manager = supervisor()
        trial_a = manager.next_trial(require_cuda_botorch=False).trial
        with tempfile.TemporaryDirectory() as td:
            decision = self.close_and_ack(
                manager,
                trial_a,
                capture(trial_a, reason=1),
                profile_diagnostic_evaluation(
                    trial_a,
                    transport_ineligible=True,
                ),
                Path(td),
            )
        self.assertEqual(decision.reason, "governor_profile_diagnostic_ready")
        self.assertEqual(manager.phase, CampaignPhase.HOME)

    def test_governor_rejects_caller_spoofed_saturation_trigger(self) -> None:
        manager = supervisor()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial_a = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                trial_a,
                capture(trial_a, reason=1),
                evaluation(
                    trial_a,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.5,
                ),
                root,
            )
            spoofed = [SaturationSample(index * 0.1) for index in range(110)]
            with self.assertRaisesRegex(ValueError, "differs from immutable"):
                manager.begin_governor_probe(spoofed)

    def test_t3_governor_b_arm_changes_profile_without_search_attestation(self) -> None:
        manager = supervisor()
        source = seed_t3_outcome(manager, objective=0.5)
        samples = [
            SaturationSample(index * 0.1, normal_filter_limited=True)
            for index in range(110)
        ]
        profile_b, _ = manager.begin_governor_probe(samples)
        self.assertIsNotNone(profile_b)

        trial_b = manager.next_trial(require_cuda_botorch=False).trial

        self.assertEqual(trial_b.candidate, source.candidate)
        self.assertEqual(trial_b.execution_profile, profile_b)
        self.assertIs(
            trial_b.transition.kind,
            TrialTransitionKind.GOVERNOR_PROBE,
        )
        self.assertEqual(
            trial_b.transition.source.trial_uid,
            source.evaluation.trial_uid,
        )
        self.assertIsNone(trial_b.search_attestation)

    def test_t3_infrastructure_recovery_advances_to_untried_candidate(self) -> None:
        manager = supervisor()
        source = seed_t3_outcome(manager)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial = manager.next_trial(require_cuda_botorch=False).trial
            self.assertNotEqual(trial.candidate, source.candidate)
            self.close_and_ack(
                manager,
                trial,
                capture(trial, reason=12),
                evaluation(
                    trial,
                    disposition=TrialDisposition.WAIT_INFRA_READY,
                ),
                root,
            )
            manager.mark_infra_ready()
            next_trial = manager.next_trial(require_cuda_botorch=False).trial

        self.assertNotEqual(next_trial.candidate, trial.candidate)
        self.assertNotEqual(next_trial.candidate_token, trial.candidate_token)
        self.assertIs(next_trial.transition.kind, TrialTransitionKind.FORCE_SEARCH)

    def test_t3_code_fix_advances_to_untried_candidate(self) -> None:
        manager = supervisor()
        source = seed_t3_outcome(manager)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial = manager.next_trial(require_cuda_botorch=False).trial
            self.assertNotEqual(trial.candidate, source.candidate)
            self.close_and_ack(
                manager,
                trial,
                capture(trial, reason=13),
                evaluation(
                    trial,
                    disposition=TrialDisposition.CODE_CONTRACT_BUG,
                ),
                root,
            )
            manager.resume_after_code_change(
                campaign=campaign(epoch=2, fingerprint="9" * 64),
                source_fingerprint="8" * 64,
                config_fingerprint="7" * 64,
            )
            next_trial = manager.next_trial(require_cuda_botorch=False).trial

        self.assertNotEqual(next_trial.candidate, trial.candidate)
        self.assertNotEqual(next_trial.candidate_token, trial.candidate_token)
        self.assertEqual(next_trial.campaign.campaign_epoch, 2)
        self.assertIs(
            next_trial.transition.kind,
            TrialTransitionKind.CODE_EPOCH_SEARCH,
        )
        self.assertEqual(next_trial.transition.source.trial_uid, trial.trial_uid)

    def test_one_eligible_success_finishes_without_confirmation_repeat(self) -> None:
        manager = supervisor()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                first,
                capture(first, reason=1),
                evaluation(first, disposition=TrialDisposition.OBJECTIVE, objective=0.29),
                root,
            )
            self.assertEqual(manager.phase, CampaignPhase.SUCCEEDED)

    def test_codex_batch_policy_does_not_stop_at_first_objective_hit(self) -> None:
        manager = supervisor(selection_policy="codex_batches")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                first,
                capture(first, reason=1),
                evaluation(first, disposition=TrialDisposition.OBJECTIVE, objective=0.29),
                root,
            )
        self.assertEqual(manager.phase, CampaignPhase.HOME)

    def test_infra_recovery_never_repeats_parameter_set(self) -> None:
        manager = supervisor()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            previous = manager.next_trial(require_cuda_botorch=False).trial
            for _ in range(3):
                self.close_and_ack(
                    manager,
                    previous,
                    capture(previous, reason=12),
                    evaluation(previous, disposition=TrialDisposition.WAIT_INFRA_READY),
                    root,
                )
                self.assertEqual(manager.phase, CampaignPhase.WAIT_INFRA_READY)
                manager.mark_infra_ready()
                current = manager.next_trial(require_cuda_botorch=False).trial
                self.assertGreater(current.trial_id, previous.trial_id)
                self.assertNotEqual(current.candidate, previous.candidate)
                self.assertNotEqual(current.candidate_token, previous.candidate_token)
                previous = current

    def test_ineligible_evidence_is_recorded_then_next_candidate_advances(self) -> None:
        manager = supervisor()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = manager.next_trial(require_cuda_botorch=False).trial
            decision = self.close_and_ack(
                manager,
                first,
                capture(first, reason=1),
                evaluation(first, disposition=TrialDisposition.FAIL_CLOSED),
                root,
            )
            self.assertEqual(
                decision.reason,
                "nonparameter_evidence_recorded_no_parameter_repeat",
            )
            self.assertEqual(manager.observations, [])
            next_trial = manager.next_trial(require_cuda_botorch=False).trial
            self.assertNotEqual(next_trial.candidate, first.candidate)
            self.assertNotEqual(next_trial.candidate_token, first.candidate_token)

    def test_code_bug_pauses_until_new_epoch_fingerprint(self) -> None:
        manager = supervisor()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                first,
                capture(first, reason=13),
                evaluation(first, disposition=TrialDisposition.CODE_CONTRACT_BUG),
                root,
            )
            self.assertEqual(manager.phase, CampaignPhase.PAUSED_CODE_BUG)
            with self.assertRaises(ValueError):
                manager.resume_after_code_change(
                    campaign=campaign(),
                    source_fingerprint=SHA_B,
                    config_fingerprint=SHA_C,
                )
            manager.resume_after_code_change(
                campaign=campaign(epoch=2, fingerprint="9" * 64),
                source_fingerprint="8" * 64,
                config_fingerprint="7" * 64,
            )
            next_trial = manager.next_trial(require_cuda_botorch=False).trial
            self.assertEqual(next_trial.campaign.campaign_epoch, 2)
            self.assertNotEqual(next_trial.candidate, first.candidate)
            self.assertNotEqual(next_trial.candidate_token, first.candidate_token)

    def test_missing_bundle_or_safe_closure_never_emits_ack(self) -> None:
        manager = supervisor()
        first = manager.next_trial(require_cuda_botorch=False).trial
        decision = manager.close_trial(
            manifest=replace(
                capture(first, reason=1),
                returned_safe=False,
                safe_closure_evidence=closure(host_dwell_s=0.49),
            ),
            evaluation=evaluation(
                first,
                disposition=TrialDisposition.SAFETY_STOP,
                safe=False,
            ),
            safe_closure=closure(host_dwell_s=0.49),
            bundle_path=None,
        )
        self.assertFalse(decision.ack_permitted)
        self.assertEqual(manager.phase, CampaignPhase.STOPPED_SAFETY)
        with self.assertRaises(RuntimeError):
            manager.ack_bundle()

    def test_runtime_closure_must_match_the_immutable_manifest_proof(self) -> None:
        manager = supervisor()
        first = manager.next_trial(require_cuda_botorch=False).trial
        with self.assertRaisesRegex(ValueError, "immutable capture manifest"):
            manager.close_trial(
                manifest=capture(first, reason=1),
                evaluation=evaluation(
                    first,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.4,
                ),
                safe_closure=closure(host_dwell_s=0.75),
                bundle_path=None,
            )

    def test_safety_guard_stops_and_never_enters_optimizer(self) -> None:
        manager = supervisor()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                first,
                capture(first, reason=5),
                evaluation(first, disposition=TrialDisposition.SAFETY_STOP),
                root,
            )
            self.assertEqual(manager.phase, CampaignPhase.STOPPED_SAFETY)
            self.assertEqual(manager.observations, [])

    def test_code_label_cannot_override_a_safety_terminal_reason(self) -> None:
        manager = supervisor()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial = manager.next_trial(require_cuda_botorch=False).trial
            decision = self.close_and_ack(
                manager,
                trial,
                capture(trial, reason=5),
                evaluation(trial, disposition=TrialDisposition.SAFETY_STOP),
                root,
                failure_cause="code",
            )
        self.assertIs(decision.disposition, TrialDisposition.SAFETY_STOP)
        self.assertEqual(manager.phase, CampaignPhase.STOPPED_SAFETY)
        self.assertFalse(decision.same_candidate_retry_pending)

    def test_governor_keep_creates_plant_epoch_and_cooldown(self) -> None:
        manager = supervisor()
        samples = [
            SaturationSample(index * 0.1, normal_filter_limited=True)
            for index in range(110)
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial_a = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                trial_a,
                capture(trial_a, reason=1),
                evaluation(
                    trial_a,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.5,
                ),
                root,
            )
            candidate, proposal = manager.begin_governor_probe(samples)
            self.assertIsNotNone(candidate)
            self.assertEqual(proposal.layer, "normal_filter_rate")

            trial_b = manager.next_trial(require_cuda_botorch=False).trial
            self.assertEqual(trial_b.candidate, trial_a.candidate)
            self.assertEqual(trial_b.candidate_token, trial_a.candidate_token)
            self.assertEqual(trial_b.execution_profile, candidate)
            self.close_and_ack(
                manager,
                trial_b,
                capture(trial_b, reason=1),
                evaluation(
                    trial_b,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.5,
                    governor_burden=0.6,
                ),
                root,
            )
            forged = AbEvidence(
                burden_a=1.0,
                burden_b=0.59,
                mae_a_n=0.5,
                mae_b_n=0.5,
                tracking_not_worse=True,
                orientation_not_worse=True,
                guards_clean=True,
                safe_closure=True,
            )
            with self.assertRaisesRegex(
                ValueError, "caller burden_b differs from immutable"
            ):
                manager.complete_governor_probe(forged)
            decision = manager.complete_governor_probe(
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
        self.assertEqual(manager.plant_epoch, 2)
        self.assertEqual(manager.cooldown_remaining, 3)

    def test_profile_diagnostic_b_is_compared_from_immutable_evidence(self) -> None:
        manager = supervisor()
        samples = [
            SaturationSample(index * 0.1, normal_filter_limited=True)
            for index in range(110)
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial_a = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                trial_a,
                capture(trial_a, reason=1),
                evaluation(
                    trial_a,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.5,
                ),
                root,
            )
            manager.begin_governor_probe(samples)
            trial_b = manager.next_trial(require_cuda_botorch=False).trial
            failed = profile_diagnostic_evaluation(trial_b)
            decision = self.close_and_ack(
                manager,
                trial_b,
                capture(trial_b, reason=1),
                failed,
                root,
            )
            self.assertEqual(decision.reason, "governor_profile_diagnostic_ready")
            evidence = manager.build_governor_evidence()
            governor_decision = manager.complete_governor_probe(evidence)
        self.assertEqual(manager.phase, CampaignPhase.HOME)
        self.assertTrue(governor_decision.keep)
        self.assertEqual(manager.execution_profile, trial_b.execution_profile)
        self.assertEqual(manager.plant_epoch, 2)
        self.assertEqual(manager.cooldown_remaining, 3)
        self.assertFalse(decision.same_candidate_retry_pending)
        self.assertIsNone(manager.recovery_snapshot().governor_probe)

    def test_ambiguous_profile_probe_reverts_without_a_prime(self) -> None:
        manager = supervisor()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial_a = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                trial_a,
                capture(trial_a, reason=1),
                profile_diagnostic_evaluation(trial_a),
                root,
            )
            manager.begin_governor_probe()
            trial_b = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                trial_b,
                capture(trial_b, reason=1),
                evaluation(
                    trial_b,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.5,
                    governor_burden=0.16,
                ),
                root,
            )
            evidence = AbEvidence(
                burden_a=0.2,
                burden_b=0.16,
                mae_a_n=0.5,
                mae_b_n=0.5,
                tracking_not_worse=True,
                orientation_not_worse=True,
                guards_clean=True,
                safe_closure=True,
            )
            request = manager.complete_governor_probe(evidence)
            self.assertEqual(request.action, "revert")

        self.assertFalse(request.evidence["exact_parameter_set_reuse_allowed"])
        self.assertEqual(len(manager.observations), 1)
        self.assertIsNone(manager.recovery_snapshot().governor_probe)

    def test_caller_cannot_hide_immutable_tracking_regression(self) -> None:
        manager = supervisor()
        samples = [
            SaturationSample(index * 0.1, normal_filter_limited=True)
            for index in range(110)
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial_a = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                trial_a,
                capture(trial_a, reason=1),
                evaluation(
                    trial_a,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.5,
                    tracking_lag_s=0.01,
                ),
                root,
            )
            manager.begin_governor_probe(samples)
            trial_b = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                trial_b,
                capture(trial_b, reason=1),
                evaluation(
                    trial_b,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.5,
                    governor_burden=0.6,
                    tracking_lag_s=0.02,
                ),
                root,
            )
            claimed_clean = AbEvidence(
                burden_a=1.0,
                burden_b=0.6,
                mae_a_n=0.5,
                mae_b_n=0.5,
                tracking_not_worse=True,
                orientation_not_worse=True,
                guards_clean=True,
                safe_closure=True,
            )
            with self.assertRaisesRegex(
                ValueError, "caller tracking_not_worse differs from immutable"
            ):
                manager.complete_governor_probe(claimed_clean)
            reverted = manager.complete_governor_probe(
                replace(
                    claimed_clean,
                    tracking_not_worse=False,
                    lag_improvement_s=-0.01,
                )
            )
        self.assertEqual(reverted.action, "revert")
        self.assertEqual(manager.execution_profile, trial_a.execution_profile)

    def test_governor_rejects_forged_identity_and_ambiguous_reverts(self) -> None:
        manager = supervisor()
        samples = [
            SaturationSample(index * 0.1, normal_filter_limited=True)
            for index in range(110)
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial_a = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                trial_a,
                capture(trial_a, reason=1),
                evaluation(
                    trial_a,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.5,
                ),
                root,
            )
            profile_b, _ = manager.begin_governor_probe(samples)
            assert profile_b is not None
            trial_b = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                trial_b,
                capture(trial_b, reason=1),
                evaluation(
                    trial_b,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.5,
                    governor_burden=0.8,
                ),
                root,
            )
            with self.assertRaisesRegex(RuntimeError, "awaiting assessment"):
                manager.next_trial(require_cuda_botorch=False)
            evidence = AbEvidence(
                burden_a=1.0,
                burden_b=0.8,
                mae_a_n=0.5,
                mae_b_n=0.5,
                tracking_not_worse=True,
                orientation_not_worse=True,
                guards_clean=True,
                safe_closure=True,
            )
            with self.assertRaisesRegex(ValueError, "caller A identity"):
                manager.complete_governor_probe(
                    replace(
                        evidence,
                        identity_a=AbTrialIdentity(
                            "9" * 64,
                            trial_a.candidate.candidate_uid,
                            trial_a.execution_profile.profile_id,
                            trial_a.plant_epoch,
                        ),
                    )
                )
            decision = manager.complete_governor_probe(evidence)
            self.assertEqual(decision.action, "revert")
            self.assertEqual(manager.execution_profile, trial_a.execution_profile)
            self.assertEqual(manager.cooldown_remaining, 3)

            next_trial = manager.next_trial(require_cuda_botorch=False).trial
            self.assertIs(next_trial.transition.kind, TrialTransitionKind.FORCE_SEARCH)
            self.assertEqual(
                next_trial.transition.source.profile_id,
                trial_a.execution_profile.profile_id,
            )
            self.assertEqual(next_trial.transition.source.plant_epoch, trial_a.plant_epoch)
            self.assertNotEqual(next_trial.candidate, trial_a.candidate)

    def test_single_success_finishes_before_governor_probe(self) -> None:
        manager = supervisor()
        samples = [
            SaturationSample(index * 0.1, normal_filter_limited=True)
            for index in range(110)
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            trial_a = manager.next_trial(require_cuda_botorch=False).trial
            self.close_and_ack(
                manager,
                trial_a,
                capture(trial_a, reason=1),
                evaluation(
                    trial_a,
                    disposition=TrialDisposition.OBJECTIVE,
                    objective=0.29,
                ),
                root,
            )
        self.assertEqual(manager.phase, CampaignPhase.SUCCEEDED)
        self.assertIsNone(manager.recovery_snapshot().governor_probe)
        with self.assertRaisesRegex(RuntimeError, "safely at campaign home"):
            manager.begin_governor_probe(samples)

    def test_snapshot_explicitly_disables_budget_and_low_ei_stops(self) -> None:
        snapshot = supervisor().snapshot()
        self.assertFalse(snapshot["trial_budget_stop_enabled"])
        self.assertFalse(snapshot["low_ei_stop_enabled"])


if __name__ == "__main__":
    unittest.main()
