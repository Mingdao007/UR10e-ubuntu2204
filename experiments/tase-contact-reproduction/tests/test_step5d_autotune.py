#!/usr/bin/env python3
"""Deterministic tests for Step5d-native autotune contracts and orchestration."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_backend import BACKEND_ID, FrozenFingerprint, Step5dV35Backend  # noqa: E402
from step5d_autotune_contract import (  # noqa: E402
    CandidateReplayEvidence,
    CampaignSpec,
    CaptureArtifactPaths,
    CaptureManifest,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    GovernorDecision,
    SearchAttestation,
    SearchTier,
    TrialDisposition,
    TrialSource,
    TrialSpec,
    TrialTransition,
    TrialTransitionKind,
)
from step5d_autotune_evaluator import (  # noqa: E402
    MalformedEvidenceError,
    evaluate_rows,
    fixed_f0_bin_metrics,
    governor_trigger_metrics,
)
from step5d_autotune_governor import (  # noqa: E402
    AbEvidence,
    AbTrialIdentity,
    SaturationSample,
    assess_ab,
    persistent_saturation,
    profile_orientation_qualified,
    propose_change,
)
from step5d_autotune_optimizer import (  # noqa: E402
    Observation,
    choose_candidate,
    live_trust_region_step,
    one_step_neighbors,
    success_confirmed,
    unlocked_tier,
)
from step5d_autotune_state_machine import (  # noqa: E402
    HostCommand,
    LoopCoordinator,
    SafeClosureEvidence,
    TpLoopState,
    TpPacket,
    classify_terminal_reason,
    host_packet_for_trial,
    verify_transcript,
)
from step5d_autotune_store import CampaignStore, DuplicateTrialError  # noqa: E402
from ur10e_artifact_store import ArtifactRef  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def replay_evidence(trace_sha256: str = "2" * 64) -> CandidateReplayEvidence:
    return CandidateReplayEvidence(
        engine_id="step5d_v35_candidate_bound_exact_replay_v1",
        replayed_rows=30_000,
        accepted_rows=30_000,
        active_bounds_rows=0,
        structural_failure_rows=0,
        rnn_residual_max=5e-4,
        rnn_oracle_qdot_delta_max_rad_s=5e-7,
        qdot_max_abs_rad_s=0.02,
        slew_violation_max_rad_s=0.0,
        trace_artifact_ref=ArtifactRef(
            trace_sha256, 10, f"sha256/{trace_sha256}"
        ),
        report_artifact_ref=ArtifactRef(
            "3" * 64, 20, f"sha256/{'3' * 64}"
        ),
    )


def campaign() -> CampaignSpec:
    return CampaignSpec(
        campaign_id="synthetic-step5d",
        campaign_epoch=1,
        campaign_fingerprint=SHA_A,
        f0_shadow_reaction_normal_base=(0.0, 0.0, 1.0),
    )


def profile() -> ExecutionProfile:
    return ExecutionProfile("nf010-slew010-a010", 0.010)


def trial(
    *,
    trial_id: int = 1,
    token: int = 101,
    candidate: ForceCandidate | None = None,
    search_attestation: SearchAttestation | None = None,
) -> TrialSpec:
    selected = candidate or ForceCandidate()
    if selected == ForceCandidate():
        transition = TrialTransition(TrialTransitionKind.BASELINE)
    else:
        if search_attestation is not None:
            source_candidate = search_attestation.from_candidate
            source_trial_uid = search_attestation.source_trial_uid
        else:
            source_trial_uid = hashlib.sha256(
                f"synthetic-source-{trial_id}".encode("utf-8")
            ).hexdigest()
            if not math.isclose(selected.log2_p, 0.0, abs_tol=1e-9):
                source_candidate = ForceCandidate.from_log2(
                    p=selected.log2_p
                    - math.copysign(0.25, selected.log2_p),
                    damping=selected.log2_damping,
                    i=selected.log2_i if selected.i_mode == "positive" else 0.0,
                    i_off=selected.i_mode == "off",
                    filter_tau=selected.log2_filter_tau,
                )
            elif not math.isclose(selected.log2_damping, 0.0, abs_tol=1e-9):
                source_candidate = ForceCandidate.from_log2(
                    p=selected.log2_p,
                    damping=selected.log2_damping
                    - math.copysign(0.25, selected.log2_damping),
                    i=selected.log2_i if selected.i_mode == "positive" else 0.0,
                    i_off=selected.i_mode == "off",
                    filter_tau=selected.log2_filter_tau,
                )
            elif not math.isclose(
                selected.log2_filter_tau, 0.0, abs_tol=1e-9
            ):
                source_candidate = ForceCandidate.from_log2(
                    p=selected.log2_p,
                    damping=selected.log2_damping,
                    i=selected.log2_i if selected.i_mode == "positive" else 0.0,
                    i_off=selected.i_mode == "off",
                    filter_tau=selected.log2_filter_tau
                    - math.copysign(0.25, selected.log2_filter_tau),
                )
            elif selected.i_mode == "off":
                source_candidate = ForceCandidate.from_log2(
                    p=selected.log2_p,
                    damping=selected.log2_damping,
                    i=0.0,
                    filter_tau=selected.log2_filter_tau,
                )
            else:
                source_candidate = ForceCandidate.from_log2(
                    p=selected.log2_p,
                    damping=selected.log2_damping,
                    i=selected.log2_i
                    - math.copysign(0.25, selected.log2_i),
                    filter_tau=selected.log2_filter_tau,
                )
        transition = TrialTransition(
            TrialTransitionKind.FORCE_SEARCH,
            source=TrialSource(
                trial_uid=source_trial_uid,
                candidate=source_candidate,
                profile_id=profile().profile_id,
                plant_epoch=1,
                campaign_id=campaign().campaign_id,
                campaign_epoch=campaign().campaign_epoch,
                campaign_fingerprint=campaign().campaign_fingerprint,
                backend_id=BACKEND_ID,
                source_fingerprint=SHA_B,
                config_fingerprint=SHA_C,
            ),
        )
    return TrialSpec(
        campaign=campaign(),
        trial_id=trial_id,
        candidate_token=token,
        command_seq=trial_id,
        plant_epoch=1,
        candidate=selected,
        execution_profile=profile(),
        backend_id=BACKEND_ID,
        source_fingerprint=SHA_B,
        config_fingerprint=SHA_C,
        transition=transition,
        search_attestation=search_attestation,
    )


def capture(spec: TrialSpec, *, reason: int = 1, returned_safe: bool = True) -> CaptureManifest:
    closure_proof = safe_closure(**({} if returned_safe else {"host_dwell_s": 0.49}))
    return CaptureManifest(
        trial_uid=spec.trial_uid,
        backend_id=spec.backend_id,
        source_fingerprint_pre=spec.source_fingerprint,
        source_fingerprint_post=spec.source_fingerprint,
        config_fingerprint_pre=spec.config_fingerprint,
        config_fingerprint_post=spec.config_fingerprint,
        candidate_token=spec.candidate_token,
        terminal_reason=reason,
        host_cause=None,
        csv_sha256="d" * 64,
        metadata_sha256="e" * 64,
        terminal_manifest_sha256="f" * 64,
        completion_marker=True,
        cadence_ok=True,
        feedback_fresh=True,
        rnn_oracle_aligned=True,
        safety_normal=True,
        returned_safe=returned_safe,
        immutable_bundle_written=True,
        stage25_complete_s=60.0,
        safe_closure_evidence=closure_proof,
    )


def capture_artifacts(
    root: Path,
    spec: TrialSpec,
    *,
    csv_bytes: bytes = b"shared physical capture\n",
) -> tuple[CaptureManifest, CaptureArtifactPaths]:
    directory = root / "artifacts" / spec.trial_uid
    directory.mkdir(parents=True)
    csv_path = directory / "capture.csv"
    metadata_path = directory / "metadata.json"
    terminal_path = directory / "terminal_manifest.json"
    csv_path.write_bytes(csv_bytes)
    identity = {
        "trial_uid": spec.trial_uid,
        "backend_id": spec.backend_id,
        "candidate_token": spec.candidate_token,
    }
    metadata_path.write_text(json.dumps(identity) + "\n", encoding="utf-8")
    terminal_path.write_text(
        json.dumps(
            {
                **identity,
                "terminal_reason": 1,
                "safe_closure_evidence": safe_closure().payload(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = replace(
        capture(spec),
        csv_sha256=digest(csv_path),
        metadata_sha256=digest(metadata_path),
        terminal_manifest_sha256=digest(terminal_path),
    )
    return manifest, CaptureArtifactPaths(
        csv_path=csv_path,
        metadata_path=metadata_path,
        terminal_manifest_path=terminal_path,
    )


def evaluation(spec: TrialSpec, objective: float, *, eligible: bool = True) -> Evaluation:
    return Evaluation(
        trial_uid=spec.trial_uid,
        backend_id=spec.backend_id,
        eligible=eligible,
        disposition=TrialDisposition.OBJECTIVE if eligible else TrialDisposition.FAIL_CLOSED,
        objective_mae_n=objective if eligible else None,
        force_bias_n=0.0 if eligible else None,
        force_std_n=objective if eligible else None,
        coverage_12_plus_minus_1_ratio=1.0 if eligible else None,
        complete_bins=550 if eligible else 0,
        safe_closure=True,
    )


def force_rows(*, missing_bin: int | None = None, load_n: float = 12.0) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for index in range(550):
        if index == missing_bin:
            continue
        qd0 = ((index % 11) - 5) * 0.001
        rows.append(
            {
                "stage": 25.0,
                "path_time_s": 5.0 + index * 0.1 + 0.05,
                "force_b_x_n": 0.0,
                "force_b_y_n": 0.0,
                "force_b_z_n": load_n,
                "_step5d_contact_orientation_error_rad": 0.01,
                "_step5d_normal_rate_limiter_active": 0.0,
                "_step5d_qdot_slew_limiter_active": 0.0,
                "step4e_cmd_vx_m_s": qd0,
                "step4e_cmd_vy_m_s": 0.0,
                "step4e_cmd_vz_m_s": 0.0,
                "step4e_cmd_wx_rad_s": 0.0,
                "step4e_cmd_wy_rad_s": 0.0,
                "step4e_cmd_wz_rad_s": 0.0,
                "ur_actual_qd_0": qd0,
                "ur_actual_qd_1": 0.0,
                "ur_actual_qd_2": 0.0,
                "ur_actual_qd_3": 0.0,
                "ur_actual_qd_4": 0.0,
                "ur_actual_qd_5": 0.0,
                "ur_actual_qdd_0": 0.0,
                "ur_actual_qdd_1": 0.0,
                "ur_actual_qdd_2": 0.0,
                "ur_actual_qdd_3": 0.0,
                "ur_actual_qdd_4": 0.0,
                "ur_actual_qdd_5": 0.0,
                "ur_kinematics_dt_s": 0.002,
            }
        )
    return rows


def safe_closure(**updates) -> SafeClosureEvidence:
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


class ContractTest(unittest.TestCase):
    def test_log2_mapping_tiers_and_native_mapping(self) -> None:
        seed = ForceCandidate()
        self.assertEqual(seed.log2_p, 0.0)
        self.assertEqual(seed.log2_damping, 0.0)
        self.assertEqual(seed.native_mapping, {"Md": 1000.0, "kf": 0.01, "Bd": 7000.0})
        edge = ForceCandidate.from_log2(p=2.0, damping=-2.0, i=2.0)
        self.assertTrue(edge.within_tier(SearchTier.T3))
        self.assertFalse(edge.within_tier(SearchTier.T2))
        self.assertAlmostEqual(edge.force_p_gain, 0.004)
        self.assertAlmostEqual(edge.force_damping, 1.75)

    def test_capture_and_evaluation_mapping_payloads_are_defensively_frozen(self) -> None:
        external_metrics = {"nested": {"rows": [1, 2]}}
        result = replace(evaluation(trial(), 0.2), metrics=external_metrics)
        external_metrics["nested"]["rows"].append(3)
        self.assertEqual(result.metrics["nested"]["rows"], (1, 2))
        with self.assertRaises(TypeError):
            result.metrics["nested"]["new"] = True

        external_evidence = {"capture": {"hashes": ["a", "b"]}}
        manifest = replace(capture(trial()), evidence=external_evidence)
        external_evidence["capture"]["hashes"].append("c")
        self.assertEqual(manifest.evidence["capture"]["hashes"], ("a", "b"))
        with self.assertRaises(TypeError):
            manifest.evidence["capture"]["extra"] = "mutated"

    def test_i_zero_is_categorical_and_alpha_is_rejected(self) -> None:
        candidate = ForceCandidate.from_log2(p=0.0, damping=0.0, i_off=True)
        self.assertEqual(candidate.i_mode, "off")
        with self.assertRaises(ValueError):
            _ = candidate.log2_i
        with self.assertRaisesRegex(ValueError, "normal_filter_alpha"):
            ForceCandidate.from_payload({"normal_filter_alpha": 0.55})

    def test_filter_tau_is_a_log2_candidate_dimension_with_legacy_default(self) -> None:
        legacy = ForceCandidate.from_payload(
            {
                "force_p_gain": 0.001,
                "force_i_gain": 0.00001,
                "force_damping": 7.0,
            }
        )
        self.assertEqual(legacy.normal_filter_tau_s, 0.35)
        self.assertNotIn("normal_filter_tau_s", legacy.payload())
        faster = ForceCandidate.from_log2(
            p=0.0,
            damping=0.0,
            i=0.0,
            filter_tau=-1.0,
        )
        self.assertAlmostEqual(faster.normal_filter_tau_s, 0.175)
        self.assertEqual(faster.log2_filter_tau, -1.0)
        self.assertEqual(faster.payload()["normal_filter_tau_s"], 0.175)

    def test_profiles_bind_live_and_offline_ladders(self) -> None:
        self.assertTrue(ExecutionProfile("nf020-slew010-a010", 0.020).live_eligible)
        with self.assertRaisesRegex(ValueError, "offline_only"):
            ExecutionProfile("nf030-offline", 0.030)
        offline = ExecutionProfile("nf030-offline", 0.030, live_eligible=False)
        self.assertFalse(offline.live_eligible)
        with self.assertRaisesRegex(ValueError, "fixed at .5"):
            ExecutionProfile("nf010-slew010-a010", 0.010, qdot_cap_rad_s=0.2)
        with self.assertRaisesRegex(ValueError, "canonically encode"):
            ExecutionProfile("same-profile-id", 0.010)

    def test_trial_uid_is_path_independent_and_identity_complete(self) -> None:
        first = trial()
        second = trial()
        self.assertEqual(first.trial_uid, second.trial_uid)
        payload = first.payload()
        self.assertNotIn("run_dir", payload)
        self.assertEqual(payload["backend_id"], BACKEND_ID)
        self.assertEqual(payload["source_fingerprint"], SHA_B)
        self.assertEqual(payload["config_fingerprint"], SHA_C)


class StateMachineTest(unittest.TestCase):
    def test_safe_closure_thresholds(self) -> None:
        self.assertTrue(safe_closure().returned_safe)
        self.assertFalse(safe_closure(host_dwell_s=0.49).returned_safe)
        self.assertFalse(safe_closure(host_safety_mode="PROTECTIVE_STOP").returned_safe)
        self.assertFalse(safe_closure(host_tcp_linear_speed_m_s=0.0011).returned_safe)

    def test_failure_disposition_does_not_blame_parameters(self) -> None:
        self.assertEqual(
            classify_terminal_reason(12, host_cause=None, safe_closure=True, eligible_evidence=False),
            TrialDisposition.WAIT_INFRA_READY,
        )
        self.assertEqual(
            classify_terminal_reason(13, host_cause=None, safe_closure=True, eligible_evidence=False),
            TrialDisposition.CODE_CONTRACT_BUG,
        )
        self.assertEqual(
            classify_terminal_reason(4, host_cause=None, safe_closure=True, eligible_evidence=False),
            TrialDisposition.FAIL_CLOSED,
        )
        self.assertEqual(
            classify_terminal_reason(5, host_cause=None, safe_closure=True, eligible_evidence=False),
            TrialDisposition.SAFETY_STOP,
        )
        self.assertEqual(
            classify_terminal_reason(2, host_cause=None, safe_closure=False, eligible_evidence=False),
            TrialDisposition.MANUAL_RECOVERY,
        )

    def test_transcript_requires_exact_echo_and_safe_sequence(self) -> None:
        spec = trial()
        host = host_packet_for_trial(spec, command=HostCommand.ARM, execution_profile_id=10)
        packets = [
            TpPacket(0, 0, TpLoopState.READY_HOME, 0, 0, 0, 0),
            *[
                TpPacket(1, 1, state, 101, 1 if state >= TpLoopState.TERMINAL else 0, 10, 1)
                for state in (
                    TpLoopState.ARMED,
                    TpLoopState.RUN,
                    TpLoopState.TERMINAL,
                    TpLoopState.RETRACT,
                    TpLoopState.RETURN,
                    TpLoopState.HOME_VERIFY,
                    TpLoopState.WAIT_ACK,
                )
            ],
        ]
        self.assertEqual(verify_transcript(host, packets), (True, ()))
        bad = list(packets)
        bad[-1] = replace(bad[-1], consumed_command_seq=2)
        self.assertFalse(verify_transcript(host, bad)[0])

    def test_loop_cannot_rearm_before_bundle_and_safe_closure(self) -> None:
        coordinator = LoopCoordinator()
        coordinator.arm(trial())
        coordinator.observe_state(TpLoopState.TERMINAL)
        disposition = coordinator.close_trial(
            reason=1,
            host_cause=None,
            evidence=safe_closure(),
            eligible_evidence=True,
            immutable_bundle_written=True,
        )
        self.assertEqual(disposition, TrialDisposition.OBJECTIVE)
        with self.assertRaisesRegex(RuntimeError, "already active"):
            coordinator.arm(trial(trial_id=2, token=102))
        coordinator.ack_bundle()
        coordinator.arm(trial(trial_id=2, token=102))


class EvaluatorTest(unittest.TestCase):
    def test_fixed_f0_uses_exact_550_bins(self) -> None:
        metrics = fixed_f0_bin_metrics(force_rows(), reaction_normal_base=(0.0, 0.0, 1.0))
        self.assertEqual(metrics["complete_bins"], 550)
        self.assertAlmostEqual(metrics["mae_n"], 0.0)
        incomplete = fixed_f0_bin_metrics(
            force_rows(missing_bin=17), reaction_normal_base=(0.0, 0.0, 1.0)
        )
        self.assertEqual(incomplete["complete_bins"], 549)
        self.assertNotIn("mae_n", incomplete)

    def test_governor_trigger_is_derived_from_immutable_trace_rows(self) -> None:
        rows = force_rows()
        for index, row in enumerate(rows):
            row["_step5d_normal_rate_limiter_active"] = float(index % 5 == 0)
        spec = trial()
        result = evaluate_rows(spec, capture(spec), rows)
        trigger = result.metrics["governor"]["trigger"]
        self.assertEqual(trigger["source"], "immutable_trial_csv")
        self.assertTrue(trigger["normal_filter_persistent"])
        self.assertFalse(trigger["qdot_persistent"])
        self.assertFalse(trigger["host_slew_persistent"])

        direct = governor_trigger_metrics(
            rows,
            tp_accel_limit_rad_s2=0.1,
            tracking=result.metrics["governor"]["tracking"],
        )
        self.assertEqual(dict(trigger), direct)

    def test_stage25_evidence_requires_present_consistent_finite_aliases(self) -> None:
        missing = force_rows()
        missing[0].pop("stage")
        with self.assertRaisesRegex(MalformedEvidenceError, "missing stage evidence"):
            fixed_f0_bin_metrics(
                missing,
                reaction_normal_base=(0.0, 0.0, 1.0),
            )

        conflicting = force_rows()
        conflicting[0]["_step5d_expected_stage"] = 20.0
        with self.assertRaisesRegex(MalformedEvidenceError, "conflicting stage aliases"):
            fixed_f0_bin_metrics(
                conflicting,
                reaction_normal_base=(0.0, 0.0, 1.0),
            )

        malformed = force_rows()
        malformed[0]["_step5d_expected_stage"] = float("nan")
        with self.assertRaisesRegex(MalformedEvidenceError, "stage is not finite"):
            fixed_f0_bin_metrics(
                malformed,
                reaction_normal_base=(0.0, 0.0, 1.0),
            )

    def test_evaluator_accepts_only_complete_safe_trial(self) -> None:
        spec = trial()
        result = evaluate_rows(spec, capture(spec), force_rows(load_n=12.2))
        self.assertTrue(result.eligible, result)
        self.assertAlmostEqual(result.objective_mae_n or 0.0, 0.2)
        before_bundle = evaluate_rows(
            spec,
            replace(capture(spec), immutable_bundle_written=False),
            force_rows(load_n=12.2),
        )
        self.assertTrue(before_bundle.eligible, before_bundle)
        self.assertNotIn("immutable_bundle_missing", before_bundle.structural_failures)
        short = evaluate_rows(spec, replace(capture(spec), stage25_complete_s=55.0), force_rows())
        self.assertFalse(short.eligible)
        self.assertIn("stage25_shorter_than_60s", short.structural_failures)

        unqualified_rows = force_rows(load_n=12.2)
        for row in unqualified_rows:
            row["_step5d_contact_orientation_error_rad"] = 0.04
        unqualified = evaluate_rows(spec, capture(spec), unqualified_rows)
        self.assertTrue(unqualified.eligible)
        self.assertNotIn(
            "orientation_profile_unqualified", unqualified.structural_failures
        )
        self.assertFalse(unqualified.metrics["profile"]["orientation_qualified"])
        self.assertAlmostEqual(unqualified.objective_mae_n or 0.0, 0.2)

        transport_diagnostic = evaluate_rows(
            spec,
            replace(capture(spec), cadence_ok=False, feedback_fresh=False),
            unqualified_rows,
        )
        self.assertFalse(transport_diagnostic.eligible)
        self.assertEqual(
            transport_diagnostic.metrics["governor"][
                "nontrainable_profile_diagnostic"
            ]["failure_scope"],
            "governor_profile_nontrainable",
        )
        self.assertEqual(
            transport_diagnostic.metrics["governor"][
                "nontrainable_profile_diagnostic"
            ]["structural_failures"],
            (
                "cadence_failed",
                "feedback_failed",
            ),
        )

        missing_saturation_rows = force_rows()
        missing_saturation_rows[0].pop("_step5d_normal_rate_limiter_active")
        missing_saturation = evaluate_rows(
            spec, capture(spec), missing_saturation_rows
        )
        self.assertFalse(missing_saturation.eligible)
        self.assertFalse(
            missing_saturation.metrics["profile"][
                "angular_saturation_evidence_complete"
            ]
        )

        missing_orientation_rows = force_rows()
        missing_orientation_rows[0].pop(
            "_step5d_contact_orientation_error_rad"
        )
        missing_orientation = evaluate_rows(
            spec, capture(spec), missing_orientation_rows
        )
        self.assertFalse(missing_orientation.eligible)
        self.assertFalse(
            missing_orientation.metrics["profile"][
                "orientation_evidence_complete"
            ]
        )

        missing_tracking_rows = force_rows()
        missing_tracking_rows[0].pop("ur_actual_qd_0")
        missing_tracking = evaluate_rows(
            spec, capture(spec), missing_tracking_rows
        )
        self.assertFalse(missing_tracking.eligible)
        self.assertIn(
            "qd_tracking_evidence_incomplete",
            missing_tracking.structural_failures,
        )
        self.assertNotIn("mae_n", missing_tracking.metrics["objective"])

    def test_malformed_evidence_is_quarantinable_not_exception(self) -> None:
        spec = trial()
        result = evaluate_rows(spec, capture(spec), [{"stage": 25.0, "path_time_s": "bad"}])
        self.assertFalse(result.eligible)
        self.assertTrue(result.metrics["quarantine_required"])
        safety = evaluate_rows(
            spec,
            replace(capture(spec), terminal_reason=5),
            [{"stage": 25.0, "path_time_s": "bad"}],
        )
        self.assertIs(safety.disposition, TrialDisposition.SAFETY_STOP)
        infrastructure = evaluate_rows(
            spec,
            replace(capture(spec), terminal_reason=12),
            [{"stage": 25.0, "path_time_s": "bad"}],
        )
        self.assertIs(
            infrastructure.disposition, TrialDisposition.WAIT_INFRA_READY
        )


class OptimizerTest(unittest.TestCase):
    def test_trust_region_changes_one_coordinate_by_quarter_octave(self) -> None:
        seed = ForceCandidate()
        neighbors = one_step_neighbors(seed, SearchTier.T1)
        self.assertEqual(len(neighbors), 6)
        self.assertTrue(all(live_trust_region_step(seed, candidate) for candidate in neighbors))
        tau_neighbors = sorted(
            candidate.log2_filter_tau
            for candidate in neighbors
            if candidate.normal_filter_tau_s != seed.normal_filter_tau_s
        )
        self.assertEqual(len(tau_neighbors), 2)
        self.assertAlmostEqual(tau_neighbors[0], seed.log2_filter_tau - 0.25)
        self.assertAlmostEqual(tau_neighbors[1], seed.log2_filter_tau + 0.25)
        diagonal = ForceCandidate.from_log2(p=0.25, damping=0.25, i=0.0)
        self.assertFalse(live_trust_region_step(seed, diagonal))

    def test_tier2_unlock_and_single_success(self) -> None:
        spec = trial()
        candidates = [ForceCandidate(), *one_step_neighbors(ForceCandidate(), SearchTier.T1)]
        observations: list[Observation] = []
        for index in range(6):
            candidate = candidates[index % len(candidates)]
            if index in {0, 5}:
                candidate = ForceCandidate()
            obs_spec = trial(trial_id=index + 1, token=100 + index, candidate=candidate)
            observations.append(Observation(candidate, evaluation(obs_spec, 0.5), profile().profile_id, 1))
        self.assertEqual(unlocked_tier(observations), SearchTier.T2)
        first = trial(trial_id=20, token=220)
        confirmed = [
            Observation(first.candidate, evaluation(first, 0.29), profile().profile_id, 1),
        ]
        self.assertTrue(success_confirmed(confirmed, profile_id=profile().profile_id, plant_epoch=1))

    def test_tier_unlock_never_mixes_profile_or_plant_epoch(self) -> None:
        other_profile = "nf015-slew010-a010"
        observations = [
            Observation(
                ForceCandidate(),
                evaluation(trial(trial_id=1, token=301), 0.7),
                profile().profile_id,
                1,
            )
        ]
        for index in range(6):
            observations.append(
                Observation(
                    ForceCandidate(),
                    evaluation(trial(trial_id=10 + index, token=400 + index), 0.5),
                    other_profile,
                    2,
                )
            )
        _, details = choose_candidate(
            observations,
            profile_id=profile().profile_id,
            plant_epoch=1,
            require_cuda_botorch=False,
        )
        self.assertEqual(details["tier"], SearchTier.T1.value)

    def test_nonfinite_objective_never_enters_optimizer(self) -> None:
        spec = trial()
        with self.assertRaisesRegex(ValueError, "finite"):
            replace(evaluation(spec, 0.5), objective_mae_n=float("nan"))


class GovernorTest(unittest.TestCase):
    def samples(self, **flags) -> list[SaturationSample]:
        return [SaturationSample(index * 0.1, **flags) for index in range(110)]

    def identity(
        self,
        label: str,
        profile_id: str,
        *,
        plant_epoch: int = 3,
        candidate_uid: str | None = None,
    ) -> AbTrialIdentity:
        return AbTrialIdentity(
            trial_uid=hashlib.sha256(label.encode("utf-8")).hexdigest(),
            force_candidate_uid=candidate_uid or ForceCandidate().candidate_uid,
            profile_id=profile_id,
            plant_epoch=plant_epoch,
        )

    def ab_evidence(
        self,
        profile_a: ExecutionProfile,
        profile_b: ExecutionProfile,
        **updates,
    ) -> AbEvidence:
        payload = {
            "burden_a": 1.0,
            "burden_b": 0.6,
            "mae_a_n": 0.5,
            "mae_b_n": 0.55,
            "tracking_not_worse": True,
            "orientation_not_worse": True,
            "guards_clean": True,
            "safe_closure": True,
            "identity_a": self.identity("trial-a", profile_a.profile_id),
            "identity_b": self.identity("trial-b", profile_b.profile_id),
        }
        payload.update(updates)
        return AbEvidence(**payload)

    def test_persistent_trigger_and_lag_alone(self) -> None:
        saturated = self.samples(normal_filter_limited=True)
        self.assertTrue(persistent_saturation(saturated, lambda row: row.normal_filter_limited))
        lag_only = self.samples(lag_s=0.0119, correlation=0.984)
        next_profile, decision = propose_change(profile(), lag_only, plant_epoch=1, cooldown_remaining=0)
        self.assertIsNone(next_profile)
        self.assertEqual(decision.action, "hold")

    def test_saturation_fraction_requires_two_complete_independent_windows(self) -> None:
        full = [
            SaturationSample(index * 0.1, normal_filter_limited=index % 5 == 0)
            for index in range(100)
        ]
        partial = full[:50] + [
            SaturationSample(5.0 + index * 0.1, normal_filter_limited=index % 5 == 0)
            for index in range(10)
        ]
        self.assertTrue(persistent_saturation(full, lambda row: row.normal_filter_limited))
        self.assertFalse(persistent_saturation(partial, lambda row: row.normal_filter_limited))
        sparse = [
            SaturationSample(0.0, normal_filter_limited=True),
            SaturationSample(100.0, normal_filter_limited=True),
        ]
        self.assertFalse(
            persistent_saturation(sparse, lambda row: row.normal_filter_limited)
        )
        split_runs = [
            SaturationSample(index * 0.1, normal_filter_limited=True)
            for index in range(20)
        ] + [
            SaturationSample(3.0 + index * 0.1, normal_filter_limited=True)
            for index in range(20)
        ]
        self.assertFalse(
            persistent_saturation(split_runs, lambda row: row.normal_filter_limited)
        )

    def test_normal_rate_then_host_headroom_probe(self) -> None:
        candidate, decision = propose_change(
            profile(), self.samples(normal_filter_limited=True), plant_epoch=1, cooldown_remaining=0
        )
        self.assertIsNotNone(candidate)
        self.assertAlmostEqual(candidate.normal_max_rate_rad_s, 0.015)
        self.assertEqual(candidate.profile_id, "nf015-slew010-a010")
        self.assertEqual(decision.layer, "normal_filter_rate")
        candidate, decision = propose_change(
            profile(), self.samples(host_slew_limited=True), plant_epoch=1, cooldown_remaining=0
        )
        self.assertIsNotNone(candidate)
        self.assertAlmostEqual(candidate.tp_speedj_accel_rad_s2, 0.2)
        self.assertEqual(decision.reason, "tp_headroom_probe_before_host_slew_increase")

    def test_qdot_is_telemetry_only_and_profile_gate(self) -> None:
        candidate, decision = propose_change(
            profile(), self.samples(qdot_limited=True), plant_epoch=1, cooldown_remaining=0
        )
        self.assertIsNone(candidate)
        self.assertTrue(decision.evidence["qdot_saturation_telemetry_only"])
        self.assertTrue(
            profile_orientation_qualified(
                {
                    "orientation_error_p95_rad": 0.035,
                    "orientation_error_max_rad": 0.049,
                    "angular_saturation_duty": 0.04,
                }
            )
        )

    def test_ab_keep_increments_plant_epoch(self) -> None:
        profile_a = profile()
        profile_b = ExecutionProfile("nf015-slew010-a010", 0.015)
        decision = assess_ab(
            layer="normal_filter_rate",
            profile_a=profile_a,
            profile_b=profile_b,
            evidence=self.ab_evidence(profile_a, profile_b),
            plant_epoch=3,
        )
        self.assertTrue(decision.keep)
        self.assertEqual(decision.plant_epoch_after, 4)
        self.assertEqual(
            decision.evidence["force_candidate_uid"],
            ForceCandidate().candidate_uid,
        )
        self.assertEqual(decision.evidence["from_profile_id"], profile_a.profile_id)
        self.assertEqual(decision.evidence["to_profile_id"], profile_b.profile_id)
        self.assertEqual(decision.evidence["plant_epoch"], 3)

    def test_ab_identity_mismatch_fails_closed_and_ambiguous_reverts(self) -> None:
        profile_a = profile()
        profile_b = ExecutionProfile("nf015-slew010-a010", 0.015)
        base = self.ab_evidence(profile_a, profile_b)

        mismatches = {
            "missing_identity": replace(base, identity_b=None),
            "candidate": replace(
                base,
                identity_b=self.identity(
                    "trial-b",
                    profile_b.profile_id,
                    candidate_uid="d" * 64,
                ),
            ),
            "profile": replace(
                base,
                identity_b=self.identity("trial-b", "wrong-profile"),
            ),
            "plant_epoch": replace(
                base,
                identity_b=self.identity(
                    "trial-b", profile_b.profile_id, plant_epoch=4
                ),
            ),
            "trial_uid": replace(base, identity_b=base.identity_a),
        }
        for name, evidence in mismatches.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                assess_ab(
                    layer="normal_filter_rate",
                    profile_a=profile_a,
                    profile_b=profile_b,
                    evidence=evidence,
                    plant_epoch=3,
                )

        ambiguous = self.ab_evidence(profile_a, profile_b, burden_b=0.8)
        request = assess_ab(
            layer="normal_filter_rate",
            profile_a=profile_a,
            profile_b=profile_b,
            evidence=ambiguous,
            plant_epoch=3,
        )
        self.assertEqual(request.action, "revert")
        self.assertFalse(request.keep)
        self.assertFalse(request.evidence["a_prime_required"])
        self.assertFalse(request.evidence["exact_parameter_set_reuse_allowed"])


class StoreAndBackendTest(unittest.TestCase):
    def test_store_rejects_same_physical_csv_for_second_trial(self) -> None:
        first = trial()
        second = trial(trial_id=2, token=202)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = CampaignStore(root / "store")
            store.initialize({"campaign": "synthetic"})
            first_capture, first_paths = capture_artifacts(root, first)
            store.write_trial_bundle(
                first,
                first_capture,
                evaluation(first, 0.5),
                artifact_paths=first_paths,
            )
            second_capture, second_paths = capture_artifacts(root, second)
            with self.assertRaises(DuplicateTrialError):
                store.write_trial_bundle(
                    second,
                    second_capture,
                    evaluation(second, 0.4),
                    artifact_paths=second_paths,
                )
            self.assertEqual(len(store.read_history()), 1)

    def test_history_malformed_row_is_durably_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignStore(Path(tmpdir))
            store.initialize({"campaign": "synthetic"})
            store.history_path.write_text("not-json\n", encoding="utf-8")
            self.assertEqual(store.read_history(), [])
            self.assertEqual(len(list(store.quarantine_dir.glob("*.json"))), 1)

    def test_backend_preparation_has_no_alpha_and_preserves_tangential_seam(self) -> None:
        spec = trial()
        frozen = FrozenFingerprint(
            backend_id=BACKEND_ID,
            git_commit="0" * 40,
            source_fingerprint=spec.source_fingerprint,
            config_fingerprint=spec.config_fingerprint,
            composite_fingerprint=spec.campaign.campaign_fingerprint,
            source_files={},
            config_files={},
            v35_package_sha256={".script": SHA_A, ".txt": SHA_B, ".urp": SHA_C},
            controller_readback_manifest="manifest.json",
            controller_readback_manifest_sha256=SHA_A,
            rnn_contract={},
            force_frame_contract_sha256=SHA_B,
        )
        prepared = Step5dV35Backend(ROOT).prepare_trial(spec, frozen)
        self.assertNotIn("BRIDGE_NORMAL_FILTER_ALPHA", prepared.environment)
        self.assertNotIn("BRIDGE_FORCE_P_GAIN", prepared.environment)
        self.assertEqual(prepared.environment["STEP5D_AUTOTUNE_FORCE_P"], "0.001")
        self.assertEqual(
            prepared.environment["STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S"], "0.01"
        )
        self.assertIn("--step5d-autotune-campaign-epoch", prepared.runner_arguments)
        self.assertIn("--step5d-autotune-execution-profile-id", prepared.runner_arguments)

    def test_backend_preparation_preserves_nontrivial_candidate_floats_exactly(self) -> None:
        spec = trial(
            candidate=ForceCandidate.from_log2(p=-0.25, damping=0.25, i=0.25)
        )
        frozen = FrozenFingerprint(
            backend_id=BACKEND_ID,
            git_commit="0" * 40,
            source_fingerprint=spec.source_fingerprint,
            config_fingerprint=spec.config_fingerprint,
            composite_fingerprint=spec.campaign.campaign_fingerprint,
            source_files={},
            config_files={},
            v35_package_sha256={".script": SHA_A, ".txt": SHA_B, ".urp": SHA_C},
            controller_readback_manifest="manifest.json",
            controller_readback_manifest_sha256=SHA_A,
            rnn_contract={},
            force_frame_contract_sha256=SHA_B,
        )
        prepared = Step5dV35Backend(ROOT).prepare_trial(spec, frozen)
        for name, expected in (
            ("STEP5D_AUTOTUNE_FORCE_P", spec.candidate.force_p_gain),
            ("STEP5D_AUTOTUNE_FORCE_I", spec.candidate.force_i_gain),
            ("STEP5D_AUTOTUNE_FORCE_DAMPING", spec.candidate.force_damping),
        ):
            self.assertEqual(float(prepared.environment[name]), expected)

    def test_backend_preparation_requires_frozen_f0_shadow_normal(self) -> None:
        spec = replace(
            trial(),
            campaign=CampaignSpec(
                campaign_id="synthetic-step5d",
                campaign_epoch=1,
                campaign_fingerprint=SHA_A,
            ),
        )
        frozen = FrozenFingerprint(
            backend_id=BACKEND_ID,
            git_commit="0" * 40,
            source_fingerprint=spec.source_fingerprint,
            config_fingerprint=spec.config_fingerprint,
            composite_fingerprint=spec.campaign.campaign_fingerprint,
            source_files={},
            config_files={},
            v35_package_sha256={".script": SHA_A, ".txt": SHA_B, ".urp": SHA_C},
            controller_readback_manifest="manifest.json",
            controller_readback_manifest_sha256=SHA_A,
            rnn_contract={},
            force_frame_contract_sha256=SHA_B,
        )
        with self.assertRaisesRegex(ValueError, "F0 shadow reaction normal"):
            Step5dV35Backend(ROOT).prepare_trial(spec, frozen)

    def test_backend_offline_freeze_uses_canonical_artifact_store_and_live_fails_closed(self) -> None:
        backend = Step5dV35Backend(ROOT)
        frozen = backend.freeze_fingerprint()
        self.assertEqual(
            frozen.controller_readback_manifest_sha256,
            "f0c15e18b55b898e4e271e6928fc47e4b8cc43f509eb49ed07fb3a55677aeb26",
        )
        offline = backend.preflight(offline=True)
        self.assertTrue(offline.ok, offline.blockers)
        self.assertTrue(offline.controller_readback_sha_closed)
        self.assertTrue(offline.controller_readback_verified)
        self.assertTrue(
            offline.evidence["baseline_controller_readback_sha_closed"]
        )
        live = backend.preflight(offline=False)
        self.assertFalse(live.ok)
        self.assertNotIn(
            "autotune_controller_delivery_and_fresh_readback_required",
            live.blockers,
        )
        self.assertTrue(live.evidence["selected_release_current"])
        self.assertNotIn("selected_release_must_be_current_and_active", live.blockers)
        self.assertIn("campaign_execution_context_missing_or_mismatched", live.blockers)

    def test_autotune_readback_flags_cannot_spoof_sha_authoritative_closure(self) -> None:
        closed, evidence = Step5dV35Backend(ROOT)._campaign_readback_closure(
            {
                "status": "controller_readback_verified",
                "controller_uploaded": True,
                "controller_readback_verified": True,
                "sha256": {".script": SHA_A, ".txt": SHA_B, ".urp": SHA_C},
            }
        )
        self.assertFalse(closed)
        self.assertFalse(evidence["campaign_readback_sha_closed"])
        self.assertEqual(
            evidence["campaign_readback_failure"],
            "artifact_locator_missing_or_invalid",
        )

    def test_backend_binds_t3_search_attestation_into_runner_environment(self) -> None:
        source = ForceCandidate.from_log2(p=1.5, damping=0.0, i=0.0)
        target = ForceCandidate.from_log2(p=1.75, damping=0.0, i=0.0)
        proof = SearchAttestation(
            profile_id=profile().profile_id,
            plant_epoch=1,
            source_trial_uid="1" * 64,
            latest_trace_sha256="2" * 64,
            replay_source_fingerprint=SHA_B,
            replay_config_fingerprint=SHA_C,
            from_candidate=source,
            to_candidate=target,
            next_candidate_uid=target.candidate_uid,
            outward_axis="p",
            outward_direction=1,
            replay_evidence=replay_evidence(),
        )
        spec = trial(candidate=target, search_attestation=proof)
        frozen = FrozenFingerprint(
            backend_id=BACKEND_ID,
            git_commit="0" * 40,
            source_fingerprint=spec.source_fingerprint,
            config_fingerprint=spec.config_fingerprint,
            composite_fingerprint=spec.campaign.campaign_fingerprint,
            source_files={},
            config_files={},
            v35_package_sha256={".script": SHA_A, ".txt": SHA_B, ".urp": SHA_C},
            controller_readback_manifest="manifest.json",
            controller_readback_manifest_sha256=SHA_A,
            rnn_contract={},
            force_frame_contract_sha256=SHA_B,
        )
        with mock.patch(
            "step5d_autotune_backend.verify_candidate_bound_search_attestation"
        ):
            environment = Step5dV35Backend(ROOT).prepare_trial(spec, frozen).environment
        self.assertEqual(
            environment["STEP5D_AUTOTUNE_SEARCH_ATTESTATION_UID"],
            proof.attestation_uid,
        )
        self.assertEqual(
            environment["STEP5D_AUTOTUNE_REPLAY_TRACE_SHA256"],
            proof.latest_trace_sha256,
        )

    def test_backend_preflight_classifies_unreadable_campaign_stage(self) -> None:
        backend = Step5dV35Backend(ROOT)
        spec = trial()
        frozen = FrozenFingerprint(
            backend_id=BACKEND_ID,
            git_commit="0" * 40,
            source_fingerprint=spec.source_fingerprint,
            config_fingerprint=spec.config_fingerprint,
            composite_fingerprint=spec.campaign.campaign_fingerprint,
            source_files={},
            config_files={},
            v35_package_sha256={".script": SHA_A, ".txt": SHA_B, ".urp": SHA_C},
            controller_readback_manifest="manifest.json",
            controller_readback_manifest_sha256=SHA_A,
            rnn_contract={},
            force_frame_contract_sha256=SHA_B,
        )
        with (
            mock.patch.object(backend, "freeze_fingerprint", return_value=frozen),
            mock.patch.object(backend, "_stage_row", side_effect=ValueError("missing")),
        ):
            result = backend.preflight(offline=True)
        self.assertFalse(result.ok)
        self.assertIn(
            "campaign_stage_delivery_unreadable:ValueError:missing",
            result.blockers,
        )


if __name__ == "__main__":
    unittest.main()
