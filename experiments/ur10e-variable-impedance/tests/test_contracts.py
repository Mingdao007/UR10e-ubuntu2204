from __future__ import annotations

import unittest

from ur10e_vic.contracts import (
    ArtifactBinding,
    BackendCommand,
    ClaimState,
    ImpedanceObservation,
    ImpedanceProposal,
    PoseSample,
    RunManifest,
)

from helpers import observation


class ContractTests(unittest.TestCase):
    def test_quaternion_is_normalized_and_sign_canonicalized(self) -> None:
        positive = PoseSample((0, 0, 0), (2, 0, 0, 0))
        negative = PoseSample((0, 0, 0), (-2, 0, 0, 0))
        self.assertEqual(positive, negative)
        self.assertEqual(positive.quaternion_wxyz, (1.0, 0.0, 0.0, 0.0))

    def test_observation_requires_exact_history(self) -> None:
        source = observation()
        with self.assertRaisesRegex(ValueError, "16 PoseSample"):
            ImpedanceObservation(
                **{
                    **source.__dict__,
                    "pose_history": source.pose_history[:-1],
                }
            )

    def test_proposal_rejects_negative_stiffness(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive semidefinite"):
            ImpedanceProposal(
                generated_at_s=0.0,
                s_zft=PoseSample((0, 0, 0), (1, 0, 0, 0)),
                stiffness=(-1, 1, 1, 1, 1, 1),
                damping=(1,) * 6,
                confidence=1.0,
                age_s=0.0,
                source="fixed",
                model_hash="",
            )

        with self.assertRaisesRegex(ValueError, "confidence > 0"):
            ImpedanceProposal(
                generated_at_s=0.0,
                s_zft=PoseSample((0, 0, 0), (1, 0, 0, 0)),
                stiffness=(1,) * 6,
                damping=(1,) * 6,
                confidence=0.0,
                age_s=0.0,
                source="fixed",
                model_hash="",
                valid=True,
            )

    def test_command_requires_exactly_one_backend_vector(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            BackendCommand(
                backend="bad",
                backend_fidelity="surrogate",
                mode="command",
                qdot_rad_s=(0,) * 6,
                torque_nm=(0,) * 6,
            )

    def test_manifest_preserves_claim_boundaries(self) -> None:
        package = ArtifactBinding("package", "offline.zip", "a" * 64)
        manifest = RunManifest(
            experiment_id="vic-offline",
            controller_software="unprobed",
            profile="fixed",
            backend_fidelity="surrogate",
            execution_mode="offline_only",
            claim_level="offline_scaffold",
            artifacts=(package,),
        )
        self.assertFalse(manifest.live_authorized)
        with self.assertRaisesRegex(ValueError, "require surrogate_live"):
            RunManifest(
                experiment_id="bad",
                controller_software="5.11",
                profile="fixed",
                backend_fidelity="surrogate",
                execution_mode="active",
                claim_level="true_torque_live",
                artifacts=(package,),
                live_authorized=True,
            )

        offline = ClaimState(
            package_status="offline_scaffold_ready",
            authorization_status="not_requested",
            run_status="offline_completed",
            acceptance_status="not_evaluated",
            reproduction_status="not_claimed",
        )
        self.assertEqual(offline.reproduction_status, "not_claimed")
        with self.assertRaisesRegex(ValueError, "live authorization"):
            ClaimState(
                package_status="package_accepted",
                authorization_status="not_requested",
                run_status="live_completed",
                acceptance_status="not_evaluated",
                reproduction_status="not_claimed",
            )
        with self.assertRaisesRegex(ValueError, "dataset and checkpoint"):
            RunManifest(
                experiment_id="bad-dbil",
                controller_software="unprobed",
                profile="dbil_shadow",
                backend_fidelity="surrogate",
                execution_mode="shadow",
                claim_level="shadow_evidence",
                artifacts=(package,),
            )


if __name__ == "__main__":
    unittest.main()
