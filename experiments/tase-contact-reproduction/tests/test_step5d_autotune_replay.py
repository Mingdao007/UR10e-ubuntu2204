from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import (  # noqa: E402
    CampaignSpec,
    CaptureManifest,
    ExecutionProfile,
    ForceCandidate,
    SafeClosureEvidence,
    SearchTier,
    TrialSource,
    TrialSpec,
    TrialTransition,
    TrialTransitionKind,
)
from step5d_autotune_replay import (  # noqa: E402
    ProcessCleanupLedger,
    axial_frontier,
    build_candidate_bound_search_attestation,
    cadence_eligible,
    delay_replay,
    managed_helper_process,
    rate_limited_orientation_replay,
    synthetic_safe_transcript,
    tier_corner_candidates,
    transcript_eligible,
    verify_candidate_bound_search_attestation,
    v34_replay_cadence_evidence,
)
from step5d_autotune_state_machine import HostCommand, HostPacket  # noqa: E402


class Step5dAutotuneReplayTest(unittest.TestCase):
    def test_v34_remains_cadence_ineligible_without_tp_consumption_echo(self) -> None:
        eligible, failures = cadence_eligible(v34_replay_cadence_evidence())
        self.assertFalse(eligible)
        self.assertIn("missing_tp_consumption_echo", failures)

    def test_delay_sweep_measures_zero_to_twenty_ms(self) -> None:
        time = np.arange(2048) * 0.002
        command = np.sin(2.0 * np.pi * 1.7 * time) + 0.3 * np.sin(
            2.0 * np.pi * 5.3 * time
        )
        for delay_ms in (0, 2, 4, 8, 12, 16, 20):
            with self.subTest(delay_ms=delay_ms):
                result = delay_replay(command, delay_s=delay_ms / 1000.0)
                self.assertAlmostEqual(result.measured_lag_s, delay_ms / 1000.0)
                self.assertGreater(result.correlation, 0.999)
                self.assertLess(result.nrmse, 1e-10)

    def test_filter_profiles_cover_live_and_offline_rate_ladders(self) -> None:
        time = np.arange(30000) * 0.002
        target = 0.012 * np.sin(2.0 * np.pi * 0.05 * time)
        for rate in (0.010, 0.015, 0.020, 0.030):
            with self.subTest(rate=rate):
                result = rate_limited_orientation_replay(
                    target, rate_limit_rad_s=rate
                )
                self.assertLessEqual(result.max_error_rad, 0.05)
        self.assertEqual(rate, 0.030)  # explicit offline profile is exercised

    def test_persistent_drift_fails_orientation_qualification(self) -> None:
        time = np.arange(30000) * 0.002
        target = 0.030 * time
        result = rate_limited_orientation_replay(target, rate_limit_rad_s=0.010)
        self.assertFalse(result.qualified)
        self.assertGreater(result.saturation_duty, 0.05)

    def test_direction_reversal_is_replayed_without_gross_error(self) -> None:
        time = np.arange(30000) * 0.002
        target = 0.01 * np.sin(2.0 * np.pi * 0.08 * time)
        result = rate_limited_orientation_replay(target, rate_limit_rad_s=0.020)
        self.assertGreater(result.direction_reversal_count, 2)
        self.assertLess(result.max_error_rad, 0.05)

    def test_tier_corners_and_quarter_octave_frontiers(self) -> None:
        expected_corner_counts = {SearchTier.T1: 4, SearchTier.T2: 12, SearchTier.T3: 12}
        for tier, count in expected_corner_counts.items():
            with self.subTest(tier=tier):
                corners = tier_corner_candidates(tier)
                self.assertEqual(len(corners), count)
                self.assertTrue(all(candidate.within_tier(tier) for candidate in corners))
                frontier = axial_frontier(axis="p", direction=1, tier=tier)
                self.assertEqual(
                    len(frontier) - 1,
                    int(round(tier.p_d_radius_octaves / 0.25)),
                )

    def test_synthetic_transcript_rejects_stale_token_or_sequence(self) -> None:
        host = HostPacket(1, 2, HostCommand.ARM, 3, 111, 4)
        packets = synthetic_safe_transcript(host)
        self.assertTrue(transcript_eligible(host, packets))
        self.assertFalse(
            transcript_eligible(host, (*packets[:-1], replace(packets[-1], candidate_token_echo=99)))
        )
        self.assertFalse(
            transcript_eligible(host, (*packets[:-1], replace(packets[-1], consumed_command_seq=3)))
        )

    def test_process_cleanup_ledger_fails_closed_on_survivor(self) -> None:
        ledger = ProcessCleanupLedger()
        ledger.launch(101)
        ledger.launch(102)
        ledger.terminate(101)
        with self.assertRaisesRegex(RuntimeError, "102"):
            ledger.assert_closed()
        ledger.terminate(102)
        ledger.assert_closed()

    def test_real_helper_process_group_is_closed_on_exception(self) -> None:
        process = None
        with self.assertRaisesRegex(RuntimeError, "synthetic replay failure"):
            with managed_helper_process(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            ) as process:
                self.assertIsNone(process.poll())
                raise RuntimeError("synthetic replay failure")
        self.assertIsNotNone(process)
        self.assertIsNotNone(process.poll())

    def test_production_attestation_binds_trace_candidate_context_and_report_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            trace_path = temporary_root / "bridge.csv"
            trace_path.write_bytes(b"immutable candidate-bound source trace\n")
            trace_sha256 = hashlib.sha256(trace_path.read_bytes()).hexdigest()
            source = ForceCandidate.from_log2(p=1.5, damping=0.0, i=0.0)
            target = ForceCandidate.from_log2(p=1.75, damping=0.0, i=0.0)
            profile = ExecutionProfile("nf010-slew010-a010", 0.010)
            source_campaign = CampaignSpec(
                campaign_id="candidate-replay",
                campaign_epoch=1,
                campaign_fingerprint="a" * 64,
            )
            source_trial = TrialSpec(
                campaign=source_campaign,
                trial_id=8,
                candidate_token=9,
                command_seq=10,
                plant_epoch=3,
                candidate=source,
                execution_profile=profile,
                backend_id="step5d_v35_native_backend_v1",
                source_fingerprint="b" * 64,
                config_fingerprint="c" * 64,
                transition=TrialTransition(
                    TrialTransitionKind.FORCE_SEARCH,
                    source=TrialSource(
                        trial_uid=hashlib.sha256(
                            b"candidate-replay-source"
                        ).hexdigest(),
                        candidate=ForceCandidate.from_log2(
                            p=1.25,
                            damping=0.0,
                            i=0.0,
                        ),
                        profile_id=profile.profile_id,
                        plant_epoch=3,
                        campaign_id=source_campaign.campaign_id,
                        campaign_epoch=source_campaign.campaign_epoch,
                        campaign_fingerprint=source_campaign.campaign_fingerprint,
                        backend_id="step5d_v35_native_backend_v1",
                        source_fingerprint="b" * 64,
                        config_fingerprint="c" * 64,
                    ),
                ),
            )
            closure = SafeClosureEvidence(
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
            source_manifest = CaptureManifest(
                trial_uid=source_trial.trial_uid,
                backend_id=source_trial.backend_id,
                source_fingerprint_pre=source_trial.source_fingerprint,
                source_fingerprint_post=source_trial.source_fingerprint,
                config_fingerprint_pre=source_trial.config_fingerprint,
                config_fingerprint_post=source_trial.config_fingerprint,
                candidate_token=source_trial.candidate_token,
                terminal_reason=1,
                host_cause=None,
                csv_sha256=trace_sha256,
                metadata_sha256="d" * 64,
                terminal_manifest_sha256="e" * 64,
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
            measured = {
                "replayed_rows": 30_000,
                "accepted_rows": 30_000,
                "active_bounds_rows": 0,
                "structural_failure_rows": 0,
                "rnn_residual_max": 5e-4,
                "rnn_oracle_qdot_delta_max_rad_s": 5e-7,
                "qdot_max_abs_rad_s": 0.02,
                "slew_violation_max_rad_s": 0.0,
            }
            store = temporary_root / "artifact-store"
            with mock.patch(
                "step5d_autotune_replay._run_candidate_bound_exact_replay",
                return_value=measured,
            ):
                proof = build_candidate_bound_search_attestation(
                    root=ROOT,
                    source_trial=source_trial,
                    source_manifest=source_manifest,
                    trace_path=trace_path,
                    to_candidate=target,
                    artifact_store_override=store,
                )
            self.assertEqual(proof.latest_trace_sha256, trace_sha256)
            self.assertEqual(proof.source_trial_uid, source_trial.trial_uid)
            self.assertEqual(proof.from_candidate, source)
            self.assertEqual(proof.to_candidate, target)
            self.assertEqual(proof.profile_id, profile.profile_id)
            self.assertEqual(proof.plant_epoch, 3)
            self.assertEqual(proof.replay_source_fingerprint, "b" * 64)
            self.assertEqual(proof.replay_config_fingerprint, "c" * 64)
            self.assertTrue(proof.exact_replay_passed)
            encoded = json.dumps(proof.payload(), sort_keys=True)
            self.assertNotIn("exact_rnn_replay_passed", encoded)
            trace_object, report_object = verify_candidate_bound_search_attestation(
                proof,
                root=ROOT,
                execution_profile=profile,
                artifact_store_override=store,
            )
            self.assertEqual(trace_object.read_bytes(), trace_path.read_bytes())
            report = json.loads(report_object.read_text(encoding="utf-8"))
            self.assertEqual(
                report["bindings"]["next_candidate_uid"], target.candidate_uid
            )
            report_object.chmod(0o644)
            report_object.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "artifact (size|sha256) mismatch"):
                verify_candidate_bound_search_attestation(
                    proof,
                    root=ROOT,
                    execution_profile=profile,
                    artifact_store_override=store,
                )


if __name__ == "__main__":
    unittest.main()
