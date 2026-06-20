#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
TOOLS = ROOT / "tools"
PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PACKAGE))

import build_p2_contact_correlation_audit as contact_audit  # noqa: E402
import build_p2_eoat_collision_inventory as p2_inventory  # noqa: E402
import capture_p2_gazebo_contact_pair_log as capture  # noqa: E402
from ur10e_example_controllers import canonical_wrench_contract as contract  # noqa: E402


def _raw_contacts_json_line() -> str:
    return json.dumps(
        {
            "header": {"stamp": {"sec": "2", "nsec": 345000000}},
            "contact": [
                {
                    "header": {"stamp": {"sec": "2", "nsec": 345000000}},
                    "collision1": {
                        "name": "real_aligned_eoat_visual_stack::eoat_contact_pad_link::eoat_contact_pad_collision"
                    },
                    "collision2": {"name": "step5_contact_surface::surface::collision"},
                    "position": [{"x": -0.48, "y": -0.22, "z": 0.01}],
                }
            ],
        }
    )


def _simulated_ft_observation() -> dict[str, object]:
    return {
        "schema": "ur10e_canonical_simulated_ft_runtime_observation_v1",
        "mode": "offline_ros2_runtime_observation",
        "claim_tier": "simulated_ft",
        "force_source": contract.SOURCE_SIMULATED_FT,
        "observed_counts": {
            "canonical_wrench": 5,
            "simulated_ft_wrench": 5,
            "simulated_ft_status": 5,
            "contact_state": 5,
            "controller_status": 5,
            "run_metadata": 1,
        },
        "evidence_fields_present": {
            "stamp": True,
            "frame_id": True,
            "source": True,
            "status": True,
            "baseline": True,
            "log_evidence": True,
        },
    }


class P2GazeboContactPairCaptureTest(unittest.TestCase):
    def test_contact_witness_world_contains_expected_collision_sensor_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_contact_world_test_") as tmp:
            world = Path(tmp) / "p2_contact_witness.sdf"
            capture.write_contact_witness_world(world)
            source = world.read_text(encoding="utf-8")

        self.assertIn("gz::sim::systems::Contact", source)
        self.assertIn("real_aligned_eoat_visual_stack", source)
        self.assertIn("eoat_contact_pad_collision", source)
        self.assertIn("step5_contact_surface", source)
        self.assertIn("p2_contact_pair_sensor", source)
        self.assertIn(capture.DEFAULT_CONTACT_TOPIC, source)

    def test_contact_witness_world_validates_with_ign_sdf(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_contact_world_test_") as tmp:
            world = Path(tmp) / "p2_contact_witness.sdf"
            capture.write_contact_witness_world(world)
            completed = subprocess.run(["ign", "sdf", "-k", str(world)], check=False, capture_output=True, text=True)

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_raw_contacts_json_lines_convert_to_contact_pair_log_v1(self) -> None:
        payload = capture.contact_pair_log_from_json_lines(
            [_raw_contacts_json_line()],
            topic=capture.DEFAULT_CONTACT_TOPIC,
            world_path="/tmp/p2_contact_witness.sdf",
            raw_jsonl_path="/tmp/topic_stdout.jsonl",
        )

        self.assertEqual(payload["schema"], "ur10e_gazebo_contact_pair_log_v1")
        self.assertEqual(payload["source"], "gazebo_contact_sensor_topic")
        self.assertEqual(payload["claim_tier"], "physical Gazebo collision/contact physics blocked/not_proven_contact_pair_only")
        self.assertEqual(payload["topic"], capture.DEFAULT_CONTACT_TOPIC)
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["stamp_s"], 2.345)
        self.assertIn("eoat_contact_pad_collision", payload["rows"][0]["collision1"])
        self.assertEqual(payload["rows"][0]["collision2"], "step5_contact_surface::surface::collision")
        self.assertEqual(payload["rows"][0]["position_m"], [-0.48, -0.22, 0.01])
        self.assertEqual(payload["rows"][0]["normal"], [0.0, 0.0, 1.0])
        self.assertEqual(payload["rows"][0]["normal_source"], "derived_from_static_contact_surface_normal")
        self.assertEqual(payload["rows"][0]["contact_count"], 1)

    def test_contact_pair_log_feeds_p2_audit_but_does_not_close_wrench_gate(self) -> None:
        contact_payload = capture.contact_pair_log_from_json_lines(
            [_raw_contacts_json_line()],
            topic=capture.DEFAULT_CONTACT_TOPIC,
            world_path="/tmp/p2_contact_witness.sdf",
            raw_jsonl_path="/tmp/topic_stdout.jsonl",
        )
        audit = contact_audit.build_audit(
            p2_inventory_payload=p2_inventory.build_inventory(generated_at="2026-06-21T02:45:00+08:00"),
            wrench_payload=_simulated_ft_observation(),
            contact_pair_payload=contact_payload,
            generated_at="2026-06-21T02:45:00+08:00",
        )

        self.assertTrue(audit["physical_gazebo_contact_gate"]["contact_pair_log_evidence"])
        self.assertFalse(audit["physical_gazebo_contact_gate"]["wrench_contact_correlation"])
        self.assertFalse(audit["physical_gazebo_contact_gate"]["force_contact_physics_proven"])
        self.assertIn("wrench_source_not_gazebo_contact", audit["known_blockers"])


if __name__ == "__main__":
    unittest.main()
