import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import validate_cross_step_parameter_table as validator  # noqa: E402


class CrossStepParameterTableTest(unittest.TestCase):
    def test_cross_step_parameter_table_contract_passes(self) -> None:
        self.assertEqual([], validator.validate(ROOT))

    def test_live_startup_gates_are_not_cacheable(self) -> None:
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        profile = validator.dotted_get(table, "startup_gate_profiles.prepared_fast_bridge_v1")
        live_gates = [gate for gate in profile["gates"] if gate.get("liveness_required") is True]

        self.assertGreaterEqual(len(live_gates), 5)
        self.assertTrue(all(gate.get("cacheable") is False for gate in live_gates))


if __name__ == "__main__":
    unittest.main()
