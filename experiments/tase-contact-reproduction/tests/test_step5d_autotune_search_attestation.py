#!/usr/bin/env python3
"""Strict Step5d search-attestation and outcome-timeline tests."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import (  # noqa: E402
    CandidateReplayEvidence,
    CampaignSpec,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    SearchAttestation as ContractSearchAttestation,
    SearchTier,
    TrialDisposition,
    TrialSource,
    TrialSpec,
    TrialTransition,
    TrialTransitionKind,
)
from ur10e_artifact_store import ArtifactRef  # noqa: E402
from step5d_autotune_optimizer import (  # noqa: E402
    OutcomeRecord,
    SearchAttestation,
    choose_candidate,
    live_trust_region_step,
    unlocked_tier,
)


PROFILE_ID = "nf010-slew010-a010"
SOURCE_FINGERPRINT = "b" * 64
CONFIG_FINGERPRINT = "c" * 64


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def outcome(
    index: int,
    candidate: ForceCandidate,
    objective: float | None,
    *,
    structural_failures: tuple[str, ...] = (),
) -> OutcomeRecord:
    eligible = objective is not None and not structural_failures
    evaluation = Evaluation(
        trial_uid=digest(f"trial-{index}"),
        backend_id="step5d-v35-native",
        eligible=eligible,
        disposition=(
            TrialDisposition.OBJECTIVE if eligible else TrialDisposition.FAIL_CLOSED
        ),
        objective_mae_n=objective if eligible else None,
        force_bias_n=0.0 if eligible else None,
        force_std_n=0.1 if eligible else None,
        coverage_12_plus_minus_1_ratio=1.0 if eligible else None,
        complete_bins=550 if eligible else 0,
        safe_closure=True,
        structural_failures=structural_failures,
    )
    return OutcomeRecord(
        candidate,
        evaluation,
        PROFILE_ID,
        1,
        latest_trace_sha256=digest(f"trace-{index}"),
    )


def t3_ready_timeline() -> list[OutcomeRecord]:
    points = (
        (0.0, 1.40),
        (0.0, 1.42),
        (0.5, 1.25),
        (1.0, 1.00),
        (1.25, 0.80),
        (1.5, 0.60),
        (0.5, 1.20),
        (1.5, 0.63),
    )
    return [
        outcome(
            index,
            ForceCandidate.from_log2(p=p_coordinate, damping=0.0, i=0.0),
            objective,
        )
        for index, (p_coordinate, objective) in enumerate(points, start=1)
    ]


def attestation(
    timeline: list[OutcomeRecord],
    target: ForceCandidate,
    *,
    direction: int = 1,
    profile_id: str = PROFILE_ID,
) -> SearchAttestation:
    latest = timeline[-1]
    trace_sha256 = latest.latest_trace_sha256 or ""
    return SearchAttestation(
        profile_id=profile_id,
        plant_epoch=latest.plant_epoch,
        source_trial_uid=latest.evaluation.trial_uid,
        latest_trace_sha256=trace_sha256,
        replay_source_fingerprint=SOURCE_FINGERPRINT,
        replay_config_fingerprint=CONFIG_FINGERPRINT,
        from_candidate=latest.candidate,
        to_candidate=target,
        next_candidate_uid=target.candidate_uid,
        outward_axis="p",
        outward_direction=direction,
        replay_evidence=CandidateReplayEvidence(
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
                digest(f"report-{target.candidate_uid}"),
                20,
                f"sha256/{digest(f'report-{target.candidate_uid}')}",
            ),
        ),
    )


def campaign_spec() -> CampaignSpec:
    return CampaignSpec(
        campaign_id="search-attestation",
        campaign_epoch=1,
        campaign_fingerprint="a" * 64,
    )


def force_search_transition(
    source: OutcomeRecord,
    *,
    profile_id: str = PROFILE_ID,
    plant_epoch: int = 1,
) -> TrialTransition:
    campaign = campaign_spec()
    return TrialTransition(
        TrialTransitionKind.FORCE_SEARCH,
        source=TrialSource(
            trial_uid=source.evaluation.trial_uid,
            candidate=source.candidate,
            profile_id=profile_id,
            plant_epoch=plant_epoch,
            campaign_id=campaign.campaign_id,
            campaign_epoch=campaign.campaign_epoch,
            campaign_fingerprint=campaign.campaign_fingerprint,
            backend_id="step5d-v35-native",
            source_fingerprint=SOURCE_FINGERPRINT,
            config_fingerprint=CONFIG_FINGERPRINT,
        ),
    )


class SearchAttestationTest(unittest.TestCase):
    def test_t3_trial_identity_contains_canonical_fingerprint_bound_attestation(self) -> None:
        timeline = t3_ready_timeline()
        target = ForceCandidate.from_log2(p=1.75, damping=0.0, i=0.0)
        proof = attestation(timeline, target)
        self.assertEqual(
            ContractSearchAttestation.from_payload(proof.payload()), proof
        )
        transition = force_search_transition(timeline[-1])
        spec = TrialSpec(
            campaign=campaign_spec(),
            trial_id=9,
            candidate_token=4,
            command_seq=11,
            plant_epoch=1,
            candidate=target,
            execution_profile=ExecutionProfile(PROFILE_ID, 0.010),
            backend_id="step5d-v35-native",
            source_fingerprint=SOURCE_FINGERPRINT,
            config_fingerprint=CONFIG_FINGERPRINT,
            transition=transition,
            search_attestation=proof,
        )
        self.assertEqual(spec.payload()["search_attestation"], proof.payload())
        failed_proof = replace(
            proof,
            replay_evidence=replace(
                proof.replay_evidence,
                slew_violation_max_rad_s=1e-4,
            ),
        )
        with self.assertRaisesRegex(ValueError, "exact RNN/oracle/slew replay must pass"):
            TrialSpec(
                campaign=spec.campaign,
                trial_id=10,
                candidate_token=4,
                command_seq=12,
                plant_epoch=1,
                candidate=target,
                execution_profile=spec.execution_profile,
                backend_id=spec.backend_id,
                source_fingerprint=spec.source_fingerprint,
                config_fingerprint=spec.config_fingerprint,
                transition=transition,
                search_attestation=failed_proof,
            )
        with self.assertRaisesRegex(ValueError, "requires.*attestation"):
            TrialSpec(
                campaign=spec.campaign,
                trial_id=11,
                candidate_token=4,
                command_seq=13,
                plant_epoch=1,
                candidate=target,
                execution_profile=spec.execution_profile,
                backend_id=spec.backend_id,
                source_fingerprint=spec.source_fingerprint,
                config_fingerprint=spec.config_fingerprint,
                transition=transition,
            )

    def test_real_three_point_chain_and_objective_repeat_reach_t3(self) -> None:
        timeline = t3_ready_timeline()
        target = ForceCandidate.from_log2(p=1.75, damping=0.0, i=0.0)
        proof = attestation(timeline, target)

        self.assertEqual(unlocked_tier(timeline), SearchTier.T2)
        self.assertEqual(
            unlocked_tier(
                timeline,
                pending_candidate=target,
                search_attestation=proof,
            ),
            SearchTier.T3,
        )
        selected, details = choose_candidate(
            timeline,
            profile_id=PROFILE_ID,
            plant_epoch=1,
            require_cuda_botorch=False,
            search_attestations=(proof,),
        )
        self.assertEqual(selected, target)
        self.assertTrue(live_trust_region_step(timeline[-1].candidate, selected))
        self.assertEqual(details["tier"], SearchTier.T3.value)
        self.assertEqual(details["search_attestation_uid"], proof.attestation_uid)

    def test_wrong_candidate_proof_cannot_unlock_or_select_t3(self) -> None:
        timeline = t3_ready_timeline()
        target = ForceCandidate.from_log2(p=1.75, damping=0.0, i=0.0)
        wrong_target = ForceCandidate.from_log2(p=1.25, damping=0.0, i=0.0)
        wrong_proof = attestation(timeline, wrong_target, direction=-1)

        self.assertEqual(
            unlocked_tier(
                timeline,
                pending_candidate=target,
                search_attestation=wrong_proof,
            ),
            SearchTier.T2,
        )
        selected, details = choose_candidate(
            timeline,
            profile_id=PROFILE_ID,
            plant_epoch=1,
            require_cuda_botorch=False,
            search_attestations=(wrong_proof,),
        )
        self.assertNotEqual(selected, target)
        self.assertEqual(details["tier"], SearchTier.T2.value)
        with self.assertRaisesRegex(ValueError, "next_candidate_uid"):
            SearchAttestation(
                profile_id=PROFILE_ID,
                plant_epoch=1,
                source_trial_uid=timeline[-1].evaluation.trial_uid,
                latest_trace_sha256=timeline[-1].latest_trace_sha256 or "",
                replay_source_fingerprint=SOURCE_FINGERPRINT,
                replay_config_fingerprint=CONFIG_FINGERPRINT,
                from_candidate=timeline[-1].candidate,
                to_candidate=target,
                next_candidate_uid=wrong_target.candidate_uid,
                outward_axis="p",
                outward_direction=1,
                replay_evidence=wrong_proof.replay_evidence,
            )

    def test_recent_structural_failure_relocks_complete_timeline(self) -> None:
        timeline = t3_ready_timeline()
        target = ForceCandidate.from_log2(p=1.75, damping=0.0, i=0.0)
        proof = attestation(timeline, target)
        timeline.append(
            outcome(
                9,
                timeline[-1].candidate,
                None,
                structural_failures=("rnn_oracle_alignment_invalid",),
            )
        )

        self.assertEqual(
            unlocked_tier(
                timeline,
                pending_candidate=target,
                search_attestation=proof,
            ),
            SearchTier.T1,
        )
        with self.assertRaisesRegex(RuntimeError, "no untried candidate"):
            choose_candidate(
                timeline,
                profile_id=PROFILE_ID,
                plant_epoch=1,
                require_cuda_botorch=False,
                search_attestations=(proof,),
            )

    def test_every_actual_transition_changes_at_most_one_coordinate(self) -> None:
        seed = ForceCandidate()
        explored = ForceCandidate.from_log2(p=-0.25, damping=0.0, i=0.0)
        timeline = [outcome(1, seed, 0.8), outcome(2, explored, 1.0)]
        selected, details = choose_candidate(
            timeline,
            profile_id=PROFILE_ID,
            plant_epoch=1,
            require_cuda_botorch=False,
        )
        self.assertNotIn(selected, {seed, explored})
        self.assertTrue(live_trust_region_step(explored, selected))
        self.assertEqual(details["selection"], "duplicate_rejected_unseen_neighbor")

    def test_t3_frontier_advances_pointwise_after_outer_incumbent_repeat(self) -> None:
        timeline = t3_ready_timeline()
        first_outer = ForceCandidate.from_log2(p=1.75, damping=0.0, i=0.0)
        timeline.extend(
            [
                outcome(9, first_outer, 0.50),
                outcome(10, first_outer, 0.52),
            ]
        )
        second_outer = ForceCandidate.from_log2(p=2.0, damping=0.0, i=0.0)
        proof = attestation(timeline, second_outer)
        self.assertEqual(
            unlocked_tier(
                timeline,
                pending_candidate=second_outer,
                search_attestation=proof,
            ),
            SearchTier.T3,
        )
        selected, details = choose_candidate(
            timeline,
            profile_id=PROFILE_ID,
            plant_epoch=1,
            require_cuda_botorch=False,
            search_attestations=(proof,),
        )
        self.assertEqual(selected, second_outer)
        self.assertTrue(live_trust_region_step(first_outer, selected))
        self.assertEqual(details["search_attestation_uid"], proof.attestation_uid)

    def test_new_plant_epoch_replicates_anchor_and_t3_requires_fresh_proof(self) -> None:
        new_profile_id = "nf015-slew010-a010"
        old_anchor = replace(
            outcome(
                1,
                ForceCandidate.from_log2(p=1.25, damping=0.0, i=0.0),
                0.7,
            ),
            plant_epoch=1,
        )
        selected, details = choose_candidate(
            [old_anchor],
            profile_id=new_profile_id,
            plant_epoch=2,
            require_cuda_botorch=False,
        )
        self.assertEqual(selected, old_anchor.candidate)
        self.assertEqual(details["selection"], "plant_epoch_anchor_replication")

        outer_anchor = replace(
            old_anchor,
            candidate=ForceCandidate.from_log2(p=1.75, damping=0.0, i=0.0),
        )
        selected, details = choose_candidate(
            [outer_anchor],
            profile_id=new_profile_id,
            plant_epoch=2,
            require_cuda_botorch=False,
        )
        self.assertEqual(selected, outer_anchor.candidate)
        self.assertEqual(details["selection"], "plant_epoch_anchor_replication")
        self.assertNotIn("search_attestation_uid", details)

        new_epoch_timeline = [
            replace(
                item,
                profile_id=new_profile_id,
                plant_epoch=2,
                evaluation=replace(
                    item.evaluation,
                    trial_uid=digest(f"new-epoch-{index}"),
                ),
                latest_trace_sha256=digest(f"new-epoch-trace-{index}"),
            )
            for index, item in enumerate(t3_ready_timeline(), start=1)
        ]
        first_outer = outer_anchor.candidate
        new_epoch_timeline.extend(
            [
                replace(
                    outcome(9, first_outer, 0.50),
                    profile_id=new_profile_id,
                    plant_epoch=2,
                ),
                replace(
                    outcome(10, first_outer, 0.52),
                    profile_id=new_profile_id,
                    plant_epoch=2,
                ),
            ]
        )
        target = ForceCandidate.from_log2(p=2.0, damping=0.0, i=0.0)
        stale_proof = attestation([outer_anchor], target)
        with self.assertRaisesRegex(RuntimeError, "no untried candidate"):
            choose_candidate(
                [outer_anchor, *new_epoch_timeline],
                profile_id=new_profile_id,
                plant_epoch=2,
                require_cuda_botorch=False,
                search_attestations=(stale_proof,),
            )

        proof = attestation(
            new_epoch_timeline,
            target,
            profile_id=new_profile_id,
        )
        selected, details = choose_candidate(
            [outer_anchor, *new_epoch_timeline],
            profile_id=new_profile_id,
            plant_epoch=2,
            require_cuda_botorch=False,
            search_attestations=(proof,),
        )
        self.assertEqual(selected, target)
        self.assertEqual(details["search_attestation_uid"], proof.attestation_uid)


if __name__ == "__main__":
    unittest.main()
