#!/usr/bin/env python3
"""Focused regression tests for the v32 stage-aware transport candidate."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402
import step5d_runtime_interface as runtime  # noqa: E402
import verify_step5d_contact_v32 as package_gate  # noqa: E402


PROGRAM = "step5d_strict_rnn_ablation_v32"


class Step5dV32StageAwareTest(unittest.TestCase):
    def test_stage_row_uses_stage_aware_protocol_not_global_layout_gate(self) -> None:
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        row = next(item for item in table["stages"] if item.get("id") == PROGRAM)

        self.assertEqual(row["runtime_profile"]["wire_protocol"], "stage_aware_joint_v1")
        self.assertNotIn("joint_layout_code", row["runtime_profile"])
        self.assertEqual(row["wire_protocol"]["stage25_05"], "latch_ready_state_33")
        self.assertEqual(row["wire_protocol"]["stage25_3"], "preload_state_521")
        self.assertEqual(row["wire_protocol"]["stage25_95"], "zero_invalid_clear_state_522")
        self.assertEqual(row["wire_protocol"]["joint_marker_internal"], 524)
        self.assertTrue(row["wire_protocol"]["layout_is_not_a_global_publish_gate"])

    def test_transport_classifies_each_tp_stage_before_joint_validation(self) -> None:
        scaffold = {"step4e_cmd_valid": 1.0, "step4e_controller_state": 33.0}
        preload = {"step4e_cmd_valid": 1.0, "step4e_controller_state": 521.0}
        clear = {
            **{name: 0.0 for name in bridge.BRIDGE_INPUT_NAMES[:6]},
            "step4e_cmd_valid": 0.0,
            "step4e_controller_state": 522.0,
        }
        joint = {"step4e_cmd_valid": 1.0, "step4e_controller_state": 524.0}

        self.assertEqual(
            bridge.step5d_wire_packet_kind(scaffold, robot_stage=25.05, stop_dominant=False),
            "scaffold",
        )
        self.assertEqual(
            bridge.step5d_wire_packet_kind(preload, robot_stage=25.3, stop_dominant=False),
            "preload",
        )
        self.assertEqual(
            bridge.step5d_wire_packet_kind(clear, robot_stage=25.95, stop_dominant=False),
            "qdot_clear",
        )
        self.assertEqual(
            bridge.step5d_wire_packet_kind(joint, robot_stage=25.0, stop_dominant=False),
            "joint",
        )

    def test_runtime_defaults_match_permissive_guard_v2(self) -> None:
        interface = runtime.resolve_runtime_interface(program=PROGRAM, root=ROOT, env={})
        profile = interface.hard_contract["runtime_profile"]

        self.assertEqual(profile["wire_protocol"], "stage_aware_joint_v1")
        self.assertEqual(profile["joint_marker_internal"], 524.0)
        self.assertEqual(profile["qdot_cap_rad_s"], 0.5)
        self.assertFalse(profile["force_guards_enabled"])
        self.assertFalse(profile["cartesian_speed_guards_enabled"])
        self.assertFalse(profile["normal_motion_guards_enabled"])
        self.assertEqual(interface.bridge_defaults.sensor_stale_s, 2.0)
        self.assertEqual(interface.hard_contract["stage25_runtime_limit_s"], 75.0)

    def test_package_and_operator_are_bound_without_cached_preflight(self) -> None:
        result = package_gate.verify(ROOT)
        wrapper = (ROOT / "scripts" / "step5d-strict-rnn-contact-v32.sh").read_text(encoding="utf-8")
        operator = (ROOT / "scripts" / "bridge-line-operator.sh").read_text(encoding="utf-8")

        self.assertTrue(result["ok"], result["failed"])
        self.assertIn('STEP5D_SENSOR_STALE_S="2.0"', wrapper)
        self.assertIn('STEP5D_QDOT_LIMIT_RAD_S="0.500"', wrapper)
        self.assertIn("step5d_strict_rnn_ablation_v32", operator)
        self.assertNotIn("LONG_CHECK_CACHE", operator)
        self.assertNotIn("require_rtde_quick_probe", operator)


if __name__ == "__main__":
    unittest.main()
