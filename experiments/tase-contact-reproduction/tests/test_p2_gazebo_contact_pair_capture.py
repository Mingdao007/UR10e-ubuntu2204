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
import build_gazebo_contact_wrench_trace as wrench_adapter  # noqa: E402
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


def _raw_contacts_json_line_with_native_wrench() -> str:
    return json.dumps(
        {
            "header": {"stamp": {"sec": "3", "nsec": 125000000}},
            "contact": [
                {
                    "header": {"stamp": {"sec": "3", "nsec": 125000000}},
                    "collision1": {
                        "name": "real_aligned_eoat_visual_stack::eoat_contact_pad_link::eoat_contact_pad_collision"
                    },
                    "collision2": {"name": "step5_contact_surface::surface::collision"},
                    "position": [{"x": 0.0, "y": 0.0, "z": 0.01}],
                    "normal": [{"x": 0.0, "y": 0.0, "z": 1.0}],
                    "depth": [0.001],
                    "wrench": [
                        {
                            "header": {"stamp": {"sec": "3", "nsec": 125000000}},
                            "body_1_name": "real_aligned_eoat_visual_stack::eoat_contact_pad_link",
                            "body_2_name": "step5_contact_surface::surface",
                            "body_1_wrench": {
                                "force": {"x": 0.0, "y": 0.0, "z": 2.1},
                                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
                            },
                            "body_2_wrench": {
                                "force": {"x": 0.0, "y": 0.0, "z": -2.1},
                                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
                            },
                        }
                    ],
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


def _verified_base_frame_gazebo_contact_wrench_pair_log() -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_contact_pair_log_v1",
        "source": "gazebo_contact_sensor_topic",
        "claim_tier": "visual_only",
        "topic": capture.DEFAULT_CONTACT_TOPIC,
        "world_path": "/tmp/p2_contact_witness.sdf",
        "raw_jsonl_path": "/tmp/topic_stdout.jsonl",
        "row_count": 1,
        "rows": [
            {
                "stamp_s": 1.25,
                "stamp_evidence": True,
                "collision1": "real_aligned_eoat_visual_stack::eoat_contact_pad_link::eoat_contact_pad_collision",
                "collision2": "step5_contact_surface::surface::collision",
                "position_m": [0.0, 0.0, 0.01],
                "normal": [0.0, 0.0, 1.0],
                "normal_source": "gazebo_contact_message_normal",
                "contact_count": 4,
                "native_gazebo_contact_wrench": {
                    "force_n": [0.0, 0.0, 2.1],
                    "torque_nm": [0.0, 0.0, 0.0],
                    "source": "gazebo_contact_message_wrench",
                    "source_schema": "ignition.msgs.Contact.contact.wrench",
                    "force_source_class": contract.SOURCE_GAZEBO_CONTACT,
                    "selected_body": "body_1_wrench",
                    "selected_body_collision": "collision1",
                    "selected_body_role": "eoat",
                    "measured_contact_wrench": True,
                    "commanded_force": False,
                    "frame_id": "base",
                    "frame_policy": "pretransformed_to_base",
                    "frame_transform_evidence": {
                        "source": "offline_transform_fixture",
                        "from_frame": "gazebo_contact_message_native_frame",
                        "to_frame": "base",
                        "stamp_s": 1.25,
                        "artifact_path": "/tmp/gazebo_contact_wrench_transform.json",
                    },
                    "status": "valid",
                    "baseline_policy": "gazebo_contact_zero_no_contact_baseline",
                    "wrench_stamp_s": 1.25,
                    "wrench_stamp_evidence": True,
                },
            }
        ],
    }


def _simulated_ft_force_embedded_in_contact_pair_log() -> dict[str, object]:
    payload = _verified_base_frame_gazebo_contact_wrench_pair_log()
    row = payload["rows"][0]
    row.pop("native_gazebo_contact_wrench")
    row["native_wrench"] = {
        "force_n": [0.0, 0.0, 2.1],
        "torque_nm": [0.0, 0.0, 0.0],
        "source": contract.SOURCE_SIMULATED_FT,
    }
    return payload


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
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertEqual(payload["topic"], capture.DEFAULT_CONTACT_TOPIC)
        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["rows"][0]["stamp_s"], 2.345)
        self.assertIn("eoat_contact_pad_collision", payload["rows"][0]["collision1"])
        self.assertEqual(payload["rows"][0]["collision2"], "step5_contact_surface::surface::collision")
        self.assertEqual(payload["rows"][0]["position_m"], [-0.48, -0.22, 0.01])
        self.assertEqual(payload["rows"][0]["normal"], [0.0, 0.0, 1.0])
        self.assertEqual(payload["rows"][0]["normal_source"], "derived_from_static_contact_surface_normal")
        self.assertEqual(payload["rows"][0]["contact_count"], 1)
        self.assertTrue(payload["rows"][0]["stamp_evidence"])

    def test_raw_contacts_json_lines_preserve_gazebo_contact_wrench_as_untransformed_evidence(self) -> None:
        payload = capture.contact_pair_log_from_json_lines(
            [_raw_contacts_json_line_with_native_wrench()],
            topic=capture.DEFAULT_CONTACT_TOPIC,
            world_path="/tmp/p2_contact_witness.sdf",
            raw_jsonl_path="/tmp/topic_stdout.jsonl",
        )

        native = payload["rows"][0]["native_gazebo_contact_wrench"]
        self.assertEqual(native["source"], "gazebo_contact_message_wrench")
        self.assertEqual(native["source_schema"], "ignition.msgs.Contact.contact.wrench")
        self.assertEqual(native["force_source_class"], contract.SOURCE_GAZEBO_CONTACT)
        self.assertEqual(native["selected_body"], "body_1_wrench")
        self.assertEqual(native["selected_body_collision"], "collision1")
        self.assertEqual(native["selected_body_role"], "eoat")
        self.assertTrue(native["measured_contact_wrench"])
        self.assertFalse(native["commanded_force"])
        self.assertEqual(native["force_n"], [0.0, 0.0, 2.1])
        self.assertEqual(native["raw_wrench_index"], 0)
        self.assertEqual(native["raw_wrench_count"], 1)
        self.assertTrue(native["wrench_stamp_evidence"])
        self.assertEqual(native["frame_id"], "gazebo_contact_message_native_frame")
        self.assertFalse(native["frame_transform_evidence"])

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

    def test_contact_pair_only_log_refuses_gazebo_contact_wrench_trace(self) -> None:
        contact_payload = capture.contact_pair_log_from_json_lines(
            [_raw_contacts_json_line()],
            topic=capture.DEFAULT_CONTACT_TOPIC,
            world_path="/tmp/p2_contact_witness.sdf",
            raw_jsonl_path="/tmp/topic_stdout.jsonl",
        )
        payload = wrench_adapter.build_wrench_trace_or_report(
            contact_payload,
            generated_at="2026-06-21T03:10:00+08:00",
        )

        self.assertEqual(payload["schema"], "ur10e_gazebo_contact_wrench_adapter_report_v1")
        self.assertFalse(payload["trace_written"])
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertEqual(payload["target_claim_tier"], "physical Gazebo collision/contact physics")
        self.assertIn("missing_native_gazebo_wrench_or_force_vector", payload["blockers"])
        self.assertIsNone(payload["wrench_trace"])

    def test_untransformed_native_gazebo_wrench_log_refuses_physical_trace(self) -> None:
        contact_payload = capture.contact_pair_log_from_json_lines(
            [_raw_contacts_json_line_with_native_wrench()],
            topic=capture.DEFAULT_CONTACT_TOPIC,
            world_path="/tmp/p2_contact_witness.sdf",
            raw_jsonl_path="/tmp/topic_stdout.jsonl",
        )
        payload = wrench_adapter.build_wrench_trace_or_report(
            contact_payload,
            generated_at="2026-06-21T03:10:00+08:00",
        )

        self.assertFalse(payload["trace_written"])
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertEqual(payload["native_wrench_row_count"], 1)
        self.assertEqual(payload["verified_native_wrench_row_count"], 0)
        self.assertIn("missing_base_frame_transform_evidence", payload["blockers"])

    def test_simulated_ft_or_commanded_force_inside_contact_log_is_refused(self) -> None:
        payload = wrench_adapter.build_wrench_trace_or_report(
            _simulated_ft_force_embedded_in_contact_pair_log(),
            generated_at="2026-06-21T03:10:00+08:00",
        )

        self.assertFalse(payload["trace_written"])
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertIn("missing_verified_gazebo_contact_wrench_provenance", payload["blockers"])

    def test_legacy_force_contamination_blocks_even_verified_native_wrench(self) -> None:
        contact_payload = _verified_base_frame_gazebo_contact_wrench_pair_log()
        contact_payload["rows"][0]["force_n"] = [0.0, 0.0, 2.1]

        payload = wrench_adapter.build_wrench_trace_or_report(
            contact_payload,
            generated_at="2026-06-21T03:10:00+08:00",
        )

        self.assertFalse(payload["trace_written"])
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertIn("legacy_force_field_contamination", payload["blockers"])

    def test_malformed_or_extra_invalid_rows_block_all_physical_trace(self) -> None:
        contact_payload = _verified_base_frame_gazebo_contact_wrench_pair_log()
        contact_payload["rows"].append("not-a-contact-row")

        payload = wrench_adapter.build_wrench_trace_or_report(
            contact_payload,
            generated_at="2026-06-21T03:10:00+08:00",
        )

        self.assertFalse(payload["trace_written"])
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertIn("malformed_contact_pair_row", payload["blockers"])

    def test_wrench_body_side_mismatch_blocks_physical_trace(self) -> None:
        contact_payload = _verified_base_frame_gazebo_contact_wrench_pair_log()
        native = contact_payload["rows"][0]["native_gazebo_contact_wrench"]
        native["selected_body"] = "body_2_wrench"
        native["selected_body_collision"] = "collision2"

        payload = wrench_adapter.build_wrench_trace_or_report(
            contact_payload,
            generated_at="2026-06-21T03:10:00+08:00",
        )

        self.assertFalse(payload["trace_written"])
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertIn("wrench_body_collision_mismatch", payload["blockers"])

    def test_missing_timestamp_evidence_blocks_physical_trace(self) -> None:
        contact_payload = _verified_base_frame_gazebo_contact_wrench_pair_log()
        row = contact_payload["rows"][0]
        row["stamp_evidence"] = False
        row["native_gazebo_contact_wrench"]["wrench_stamp_evidence"] = False

        payload = wrench_adapter.build_wrench_trace_or_report(
            contact_payload,
            generated_at="2026-06-21T03:10:00+08:00",
        )

        self.assertFalse(payload["trace_written"])
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertIn("missing_timestamped_native_wrench", payload["blockers"])

    def test_boolean_transform_attestation_blocks_physical_trace(self) -> None:
        contact_payload = _verified_base_frame_gazebo_contact_wrench_pair_log()
        contact_payload["rows"][0]["native_gazebo_contact_wrench"]["frame_transform_evidence"] = True

        payload = wrench_adapter.build_wrench_trace_or_report(
            contact_payload,
            generated_at="2026-06-21T03:10:00+08:00",
        )

        self.assertFalse(payload["trace_written"])
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertIn("missing_base_frame_transform_evidence", payload["blockers"])

    def test_verified_gazebo_wrench_contact_log_builds_canonical_trace(self) -> None:
        payload = wrench_adapter.build_wrench_trace_or_report(
            _verified_base_frame_gazebo_contact_wrench_pair_log(),
            generated_at="2026-06-21T03:10:00+08:00",
        )

        self.assertTrue(payload["trace_written"])
        self.assertEqual(payload["claim_tier"], "physical Gazebo collision/contact physics")
        trace = payload["wrench_trace"]
        self.assertEqual(trace["schema"], contract.TRACE_SCHEMA)
        self.assertEqual(trace["force_source"], contract.SOURCE_GAZEBO_CONTACT)
        self.assertEqual(trace["rows"][0]["source"], contract.SOURCE_GAZEBO_CONTACT)
        self.assertEqual(trace["rows"][0]["status"], "valid")
        self.assertEqual(trace["rows"][0]["contact_state"], "contact")
        self.assertEqual(trace["rows"][0]["normal_load_n"], 2.1)

    def test_verified_gazebo_wrench_trace_can_close_correlation_gate(self) -> None:
        adapter_payload = wrench_adapter.build_wrench_trace_or_report(
            _verified_base_frame_gazebo_contact_wrench_pair_log(),
            generated_at="2026-06-21T03:10:00+08:00",
        )
        audit = contact_audit.build_audit(
            p2_inventory_payload=p2_inventory.build_inventory(generated_at="2026-06-21T03:10:00+08:00"),
            wrench_payload=adapter_payload,
            contact_pair_payload=_verified_base_frame_gazebo_contact_wrench_pair_log(),
            generated_at="2026-06-21T03:10:00+08:00",
        )

        self.assertTrue(audit["physical_gazebo_contact_gate"]["contact_pair_log_evidence"])
        self.assertTrue(audit["physical_gazebo_contact_gate"]["wrench_contact_correlation"])
        self.assertTrue(audit["physical_gazebo_contact_gate"]["force_contact_physics_proven"])


if __name__ == "__main__":
    unittest.main()
