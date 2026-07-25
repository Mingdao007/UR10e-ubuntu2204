import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TacDiffusionValidationEvidenceTests(unittest.TestCase):
    def test_historical_offline_validation_is_immutable_and_retains_stage_boundary(self) -> None:
        evidence_path = ROOT / "evidence" / "tacdiffusion_offline_validation.json"
        evidence_bytes = evidence_path.read_bytes()
        evidence = json.loads(evidence_bytes)
        self.assertEqual(
            hashlib.sha256(evidence_bytes).hexdigest(),
            "1dda54b6b45243cf75d229068c6d191ee1fcd227fc99aa58e06798cc17e2d96d",
        )
        self.assertEqual(evidence["schema"], "ur10e_tacdiffusion_offline_validation_v2")
        self.assertEqual(evidence["highest_stage"], "deterministic_tested")
        self.assertFalse(evidence["simulation_run"])
        self.assertFalse(evidence["ursim_run"])
        self.assertFalse(evidence["hardware_run"])
        self.assertFalse(evidence["controller_verified"])
        self.assertFalse(evidence["model_active_enabled"])
        self.assertFalse(evidence["live_motion_authorized"])
        self.assertGreaterEqual(evidence["validation"]["variable_impedance_test_count"], 100)
        self.assertTrue(evidence["validation"]["torch_2_11_cpu_fixture_smoke_passed"])
        self.assertTrue(evidence["validation"]["torch_fixture_train_resume_evaluate_passed"])
        self.assertTrue(evidence["validation"]["ursim_5_26_fake_transport_nonpromotion_passed"])
        self.assertTrue(evidence["validation"]["expert_manifest_v2_artifact_graph_passed"])
        self.assertFalse(evidence["validation"]["external_dbil_artifacts_rehashed_this_round"])
        self.assertFalse(evidence["formal_dataset_collected"])
        self.assertFalse(evidence["formal_checkpoint_trained"])
        for binding in evidence["source_bindings"]:
            path = ROOT / binding["path"]
            self.assertTrue(path.is_file(), binding["path"])
            self.assertRegex(binding["sha256"], r"^[0-9a-f]{64}$")

    def test_remote_headless_mainline_evidence_rehashes_current_sources_and_blocks_live(self) -> None:
        evidence = json.loads(
            (ROOT / "evidence" / "tacdiffusion_remote_headless_offline_validation_v1.json").read_text()
        )
        self.assertEqual(
            evidence["schema"],
            "ur10e_tacdiffusion_remote_headless_offline_validation/v1",
        )
        self.assertEqual(
            evidence["highest_stage"],
            "offline_ursim_protocol_no_motion_tested",
        )
        self.assertTrue(all(value is False for value in evidence["boundaries"].values()))
        self.assertFalse(evidence["promotion"]["active_allowed"])
        self.assertFalse(evidence["promotion"]["shadow_artifacts_complete"])
        self.assertEqual(
            evidence["validation"]["cuda_bounded_training_and_sampler"]["selected_rate_hz"],
            50,
        )
        self.assertEqual(
            evidence["validation"]["virtual_campaign"]["continuous_episodes"],
            50,
        )
        self.assertEqual(
            evidence["validation"]["virtual_campaign"]["isolated_recovery_cycles"],
            100,
        )
        for binding in evidence["source_bindings"]:
            path = ROOT / binding["path"]
            self.assertTrue(path.is_file(), binding["path"])
            self.assertEqual(
                binding["sha256"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
                binding["path"],
            )


if __name__ == "__main__":
    unittest.main()
