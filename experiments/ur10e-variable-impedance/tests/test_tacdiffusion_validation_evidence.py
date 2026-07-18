import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TacDiffusionValidationEvidenceTests(unittest.TestCase):
    def test_offline_validation_rehashes_sources_and_retains_stage_boundary(self) -> None:
        evidence = json.loads(
            (ROOT / "evidence" / "tacdiffusion_offline_validation.json").read_text()
        )
        self.assertEqual(evidence["schema"], "ur10e_tacdiffusion_offline_validation_v1")
        self.assertEqual(evidence["highest_stage"], "deterministic_tested")
        self.assertFalse(evidence["simulation_run"])
        self.assertFalse(evidence["ursim_run"])
        self.assertFalse(evidence["hardware_run"])
        self.assertFalse(evidence["controller_verified"])
        self.assertFalse(evidence["model_active_enabled"])
        self.assertFalse(evidence["live_motion_authorized"])
        self.assertGreaterEqual(
            evidence["validation"]["variable_impedance_test_count"], 85
        )
        self.assertTrue(evidence["validation"]["torch_model_smoke_passed"])
        self.assertTrue(evidence["validation"]["external_dbil_artifacts_rehashed"])
        for binding in evidence["source_bindings"]:
            path = ROOT / binding["path"]
            self.assertTrue(path.is_file(), binding["path"])
            self.assertEqual(
                binding["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )


if __name__ == "__main__":
    unittest.main()
