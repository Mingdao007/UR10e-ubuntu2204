import importlib.util
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "run_tacdiffusion_synthetic_canary.py"
SPEC = importlib.util.spec_from_file_location("tacdiffusion_synthetic_canary", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TacDiffusionSyntheticCanaryTests(unittest.TestCase):
    def test_shadow_logical_horizon_is_command_invariant_and_fail_closed(self) -> None:
        result = MODULE.run_canary(ticks=1_000, model_rate_hz=200)
        self.assertTrue(result["deterministic_test_pass"])
        self.assertEqual(result["accepted_ratio"], 1.0)
        self.assertEqual(result["shadow_command_mismatches"], 0)
        self.assertGreater(result["max_diagnostic_filter_abs"], 0.0)
        self.assertEqual(result["stale_fault_reason"], "model_stale_over_two_periods")
        self.assertEqual(result["model_active_fault_reason"], "model_active_not_authorized")
        self.assertFalse(result["simulation_run"])
        self.assertFalse(result["hardware_run"])
        self.assertFalse(result["controller_verified"])

    def test_retained_60s_logical_evidence_is_immutable(self) -> None:
        evidence_path = ROOT / "evidence" / "tacdiffusion_synthetic_canary_60s.json"
        evidence_bytes = evidence_path.read_bytes()
        evidence = json.loads(evidence_bytes)
        self.assertEqual(
            hashlib.sha256(evidence_bytes).hexdigest(),
            "f09222ca9405e54e4713c46a71bea6704a37451c35885aebf7f4d56fc17fdd2c",
        )
        self.assertTrue(evidence["deterministic_test_pass"])
        self.assertEqual(evidence["logical_ticks"], 30_000)
        self.assertEqual(evidence["logical_duration_s"], 60.0)
        self.assertFalse(evidence["simulation_run"])
        for sha256 in evidence["source_bindings"].values():
            self.assertRegex(sha256, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
