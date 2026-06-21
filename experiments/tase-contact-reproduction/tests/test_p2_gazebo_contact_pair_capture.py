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
import verify_p2_gz_contact_wrench_evidence as gz_wrench_verifier  # noqa: E402
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


def _raw_gz_contacts_json_line_with_native_wrench() -> str:
    return json.dumps(
        {
            "header": {"stamp": {"sec": "4", "nsec": 250000000}},
            "contact": [
                {
                    "header": {"stamp": {"sec": "4", "nsec": 250000000}},
                    "collision1": {"name": "step5_contact_surface::surface::collision"},
                    "collision2": {
                        "name": "real_aligned_eoat_visual_stack::eoat_contact_pad_link::eoat_contact_pad_collision"
                    },
                    "position": [{"x": 0.0, "y": 0.0, "z": 0.01}],
                    "normal": [{"x": 0.0, "y": 0.0, "z": 1.0}],
                    "depth": [1.57e-08],
                    "wrench": [
                        {
                            "body1Wrench": {
                                "force": {"x": 0.0, "y": 0.0, "z": 0.4905},
                                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
                            },
                            "body2Wrench": {
                                "force": {"x": 0.0, "y": 0.0, "z": -0.4905},
                                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
                            },
                        }
                    ],
                }
            ],
        }
    )


def _raw_gz_eoat_sensor_contacts_json_line_with_native_wrench() -> str:
    return json.dumps(
        {
            "header": {"stamp": {"sec": "5", "nsec": 250000000}},
            "contact": [
                {
                    "header": {"stamp": {"sec": "5", "nsec": 250000000}},
                    "collision1": {
                        "name": "real_aligned_eoat_visual_stack::eoat_contact_pad_link::eoat_contact_pad_collision"
                    },
                    "collision2": {"name": "step5_contact_surface::surface::collision"},
                    "position": [{"x": 0.0, "y": 0.0, "z": 0.01}],
                    "normal": [{"x": 0.0, "y": 0.0, "z": 1.0}],
                    "depth": [1.57e-08],
                    "wrench": [
                        {
                            "body1Wrench": {
                                "force": {"x": 0.0, "y": 0.0, "z": 0.4905},
                                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
                            },
                            "body2Wrench": {
                                "force": {"x": 0.0, "y": 0.0, "z": -0.4905},
                                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
                            },
                        }
                    ],
                }
            ],
        }
    )


def _raw_gz_eoat_sensor_contacts_json_line_with_strong_native_wrench() -> str:
    return json.dumps(
        {
            "header": {"stamp": {"sec": "5", "nsec": 250000000}},
            "contact": [
                {
                    "header": {"stamp": {"sec": "5", "nsec": 250000000}},
                    "collision1": {
                        "name": "real_aligned_eoat_visual_stack::eoat_contact_pad_link::eoat_contact_pad_collision"
                    },
                    "collision2": {"name": "step5_contact_surface::surface::collision"},
                    "position": [{"x": 0.0, "y": 0.0, "z": 0.01}],
                    "normal": [{"x": 0.0, "y": 0.0, "z": 1.0}],
                    "depth": [0.001],
                    "wrench": [
                        {
                            "body1Wrench": {
                                "force": {"x": 0.0, "y": 0.0, "z": 2.1},
                                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
                            },
                            "body2Wrench": {
                                "force": {"x": 0.0, "y": 0.0, "z": -2.1},
                                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
                            },
                        }
                    ],
                }
            ],
        }
    )


def _raw_gz_surface_sensor_contacts_json_line_with_strong_native_wrench() -> str:
    return json.dumps(
        {
            "header": {"stamp": {"sec": "5", "nsec": 250000000}},
            "contact": [
                {
                    "header": {"stamp": {"sec": "5", "nsec": 250000000}},
                    "collision1": {"name": "step5_contact_surface::surface::collision"},
                    "collision2": {
                        "name": "real_aligned_eoat_visual_stack::eoat_contact_pad_link::eoat_contact_pad_collision"
                    },
                    "position": [{"x": 0.0, "y": 0.0, "z": 0.01}],
                    "normal": [{"x": 0.0, "y": 0.0, "z": 1.0}],
                    "depth": [0.001],
                    "wrench": [
                        {
                            "body1Wrench": {
                                "force": {"x": 0.0, "y": 0.0, "z": 2.1},
                                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
                            },
                            "body2Wrench": {
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


def _verified_base_frame_gazebo_total_contact_wrench_pair_log() -> dict[str, object]:
    payload = _verified_base_frame_gazebo_contact_wrench_pair_log()
    row = payload["rows"][0]
    row["contact_count"] = 2
    row["native_gazebo_contact_wrench"]["raw_wrench_count"] = 2
    row["native_gazebo_contact_wrench"]["raw_wrench_index"] = 0
    row["native_gazebo_contact_wrench"]["selected_body"] = "body_1_wrench"
    row["native_gazebo_contact_wrench"]["frame_policy"] = "pretransformed_to_base"
    row["raw_gazebo_contact_wrench_count"] = 2
    row["raw_gazebo_contact_wrenches"] = [
        {
            "body1Wrench": {
                "force": {"x": 0.0, "y": 0.0, "z": 2.1},
                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
            "body2Wrench": {
                "force": {"x": 0.0, "y": 0.0, "z": -2.1},
                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
        },
        {
            "body1Wrench": {
                "force": {"x": 0.0, "y": 0.0, "z": 1.4},
                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
            "body2Wrench": {
                "force": {"x": 0.0, "y": 0.0, "z": -1.4},
                "torque": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
        },
    ]
    return payload


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


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _gz_contact_payloads_for_verifier(tmp_path: Path, *, include_base_frame: bool = True) -> tuple[Path, Path, Path]:
    contact_world = tmp_path / "contact" / "p2_contact_witness.sdf"
    surface_world = tmp_path / "surface" / "p2_contact_witness.sdf"
    baseline_world = tmp_path / "baseline" / "p2_contact_witness.sdf"
    capture.write_contact_witness_world(contact_world, sensor_collision_role="eoat", include_base_frame=include_base_frame)
    capture.write_contact_witness_world(surface_world, sensor_collision_role="surface", include_base_frame=include_base_frame)
    capture.write_contact_witness_world(
        baseline_world,
        sensor_collision_role="eoat",
        eoat_static=True,
        eoat_pose_z=0.2,
        include_base_frame=include_base_frame,
    )

    eoat_payload = capture.contact_pair_log_from_json_lines(
        [_raw_gz_eoat_sensor_contacts_json_line_with_strong_native_wrench()],
        topic=capture.DEFAULT_CONTACT_TOPIC,
        world_path=str(contact_world),
        raw_jsonl_path=str(tmp_path / "contact" / "contact_topic_stdout.jsonl"),
        transport="gz",
        sensor_collision_role="eoat",
    )
    surface_payload = capture.contact_pair_log_from_json_lines(
        [_raw_gz_surface_sensor_contacts_json_line_with_strong_native_wrench()],
        topic=capture.DEFAULT_CONTACT_TOPIC,
        world_path=str(surface_world),
        raw_jsonl_path=str(tmp_path / "surface" / "contact_topic_stdout.jsonl"),
        transport="gz",
        sensor_collision_role="surface",
    )
    baseline_raw = tmp_path / "baseline" / "contact_topic_stdout.jsonl"
    baseline_raw.parent.mkdir(parents=True, exist_ok=True)
    baseline_raw.write_text("", encoding="utf-8")
    baseline_payload = capture.contact_pair_log_from_json_lines(
        [],
        topic=capture.DEFAULT_CONTACT_TOPIC,
        world_path=str(baseline_world),
        raw_jsonl_path=str(baseline_raw),
        transport="gz",
        sensor_collision_role="eoat",
        baseline_mode="no_contact_static_elevated_eoat",
    )
    baseline_payload["capture"] = {
        "allow_no_messages": True,
        "topic_timeout_expired": True,
        "eoat_static": True,
        "eoat_pose_z": 0.2,
    }

    eoat_path = _write_json(tmp_path / "contact" / "p2_gazebo_contact_pair_log.json", eoat_payload)
    surface_path = _write_json(tmp_path / "surface" / "p2_gazebo_contact_pair_log.json", surface_payload)
    baseline_path = _write_json(tmp_path / "baseline" / "p2_gazebo_contact_pair_log.json", baseline_payload)
    return eoat_path, surface_path, baseline_path


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

    def test_contact_witness_world_can_place_sensor_on_eoat_collision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_contact_world_test_") as tmp:
            world = Path(tmp) / "p2_contact_witness.sdf"
            capture.write_contact_witness_world(world, sensor_collision_role="eoat")
            source = world.read_text(encoding="utf-8")

        self.assertIn("<collision>eoat_contact_pad_collision</collision>", source)
        self.assertNotIn("<collision>collision</collision>", source)

    def test_contact_witness_world_validates_with_ign_sdf(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_contact_world_test_") as tmp:
            world = Path(tmp) / "p2_contact_witness.sdf"
            capture.write_contact_witness_world(world)
            completed = subprocess.run(["ign", "sdf", "-k", str(world)], check=False, capture_output=True, text=True)

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_gz_transport_uses_gz_sim_and_topic_commands(self) -> None:
        gazebo_cmd, topic_cmd = capture._transport_commands(
            Path("/tmp/p2_contact_witness.sdf"),
            topic=capture.DEFAULT_CONTACT_TOPIC,
            max_messages=3,
            transport="gz",
        )

        self.assertEqual(gazebo_cmd[:3], ["gz", "sim", "-r"])
        self.assertEqual(topic_cmd[:3], ["gz", "topic", "-e"])
        self.assertIn("--json-output", topic_cmd)

    def test_raw_contacts_json_lines_convert_to_contact_pair_log_v1(self) -> None:
        payload = capture.contact_pair_log_from_json_lines(
            [_raw_contacts_json_line()],
            topic=capture.DEFAULT_CONTACT_TOPIC,
            world_path="/tmp/p2_contact_witness.sdf",
            raw_jsonl_path="/tmp/topic_stdout.jsonl",
        )

        self.assertEqual(payload["schema"], "ur10e_gazebo_contact_pair_log_v1")
        self.assertEqual(payload["source"], "gazebo_contact_sensor_topic")
        self.assertEqual(payload["sensor_collision_role"], "surface")
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

    def test_contact_pair_log_preserves_stage_observation_metadata(self) -> None:
        payload = capture.contact_pair_log_from_json_lines(
            [_raw_contacts_json_line()],
            topic="/ur10e/contact/gazebo/step5b/contacts",
            world_path="/tmp/p2_contact_witness.sdf",
            raw_jsonl_path="/tmp/topic_stdout.jsonl",
            stage_id="step5b",
            observation_id="stage-step5b-witness-001",
            time_window={
                "start": "2026-06-21T19:05:00+08:00",
                "end": "2026-06-21T19:05:10+08:00",
                "clock_source": "/clock",
            },
            observation_scope=capture.STANDALONE_P2_OBSERVATION_SCOPE,
        )

        self.assertEqual(payload["stage_id"], "step5b")
        self.assertEqual(payload["observation_id"], "stage-step5b-witness-001")
        self.assertEqual(payload["time_window"]["clock_source"], "/clock")
        self.assertEqual(payload["observation_scope"], "standalone_p2_contact_witness")

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

    def test_gz_contacts_json_lines_preserve_camelcase_wrench_as_untransformed_evidence(self) -> None:
        payload = capture.contact_pair_log_from_json_lines(
            [_raw_gz_contacts_json_line_with_native_wrench()],
            topic=capture.DEFAULT_CONTACT_TOPIC,
            world_path="/tmp/p2_contact_witness.sdf",
            raw_jsonl_path="/tmp/topic_stdout.jsonl",
            transport="gz",
            sensor_collision_role="eoat",
        )

        row = payload["rows"][0]
        native = row["native_gazebo_contact_wrench"]
        self.assertEqual(payload["sim_transport"], "gz")
        self.assertEqual(payload["sensor_collision_role"], "eoat")
        self.assertEqual(row["normal_source"], "gazebo_contact_message_normal")
        self.assertEqual(row["depth_m"], 1.57e-08)
        self.assertEqual(native["source_schema"], "gz.msgs.Contact.contact.wrench")
        self.assertEqual(native["selected_body"], "body_2_wrench")
        self.assertEqual(native["selected_body_field"], "body2Wrench")
        self.assertEqual(native["selected_body_collision"], "collision2")
        self.assertEqual(native["force_n"], [0.0, 0.0, -0.4905])
        self.assertEqual(native["other_body"], "body_1_wrench")
        self.assertEqual(native["other_body_field"], "body1Wrench")
        self.assertEqual(native["other_force_n"], [0.0, 0.0, 0.4905])
        self.assertLess(native["selected_force_dot_contact_normal_n"], 0.0)
        self.assertTrue(native["wrench_stamp_evidence"])
        self.assertEqual(native["frame_id"], "gazebo_contact_message_native_frame")
        self.assertEqual(native["status"], "raw_untransformed")

    def test_gz_eoat_sensor_contacts_select_body1_positive_normal_wrench(self) -> None:
        payload = capture.contact_pair_log_from_json_lines(
            [_raw_gz_eoat_sensor_contacts_json_line_with_native_wrench()],
            topic=capture.DEFAULT_CONTACT_TOPIC,
            world_path="/tmp/p2_contact_witness.sdf",
            raw_jsonl_path="/tmp/topic_stdout.jsonl",
            transport="gz",
            sensor_collision_role="eoat",
        )

        row = payload["rows"][0]
        native = row["native_gazebo_contact_wrench"]
        self.assertEqual(row["collision1"], "real_aligned_eoat_visual_stack::eoat_contact_pad_link::eoat_contact_pad_collision")
        self.assertEqual(native["selected_body"], "body_1_wrench")
        self.assertEqual(native["selected_body_field"], "body1Wrench")
        self.assertEqual(native["selected_body_collision"], "collision1")
        self.assertEqual(native["force_n"], [0.0, 0.0, 0.4905])
        self.assertGreater(native["selected_force_dot_contact_normal_n"], 0.0)

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
        self.assertEqual(payload["blocker_summary"]["missing_native_gazebo_wrench_or_force_vector"], 1)
        self.assertEqual(payload["evidence_contract"]["accepted_frame_id"], "base")
        self.assertIn(
            "inferred force from contact position/normal/depth",
            payload["evidence_contract"]["forbidden_force_sources"],
        )
        self.assertEqual(len(payload["row_diagnostics"]), 1)
        self.assertFalse(payload["row_diagnostics"][0]["native_wrench_present"])
        self.assertFalse(payload["row_diagnostics"][0]["verified_native_wrench"])
        self.assertIn("missing_native_gazebo_wrench_or_force_vector", payload["row_diagnostics"][0]["blockers"])
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
        self.assertTrue(payload["row_diagnostics"][0]["native_wrench_present"])
        self.assertEqual(payload["row_diagnostics"][0]["native_wrench_frame_id"], "gazebo_contact_message_native_frame")
        self.assertEqual(payload["row_diagnostics"][0]["native_wrench_status"], "raw_untransformed")
        self.assertFalse(payload["row_diagnostics"][0]["verified_native_wrench"])
        self.assertIn("missing_base_frame_transform_evidence", payload["row_diagnostics"][0]["blockers"])

    def test_untransformed_gz_native_wrench_log_refuses_physical_trace(self) -> None:
        contact_payload = capture.contact_pair_log_from_json_lines(
            [_raw_gz_contacts_json_line_with_native_wrench()],
            topic=capture.DEFAULT_CONTACT_TOPIC,
            world_path="/tmp/p2_contact_witness.sdf",
            raw_jsonl_path="/tmp/topic_stdout.jsonl",
            transport="gz",
        )
        payload = wrench_adapter.build_wrench_trace_or_report(
            contact_payload,
            generated_at="2026-06-21T06:50:00+08:00",
        )

        self.assertFalse(payload["trace_written"])
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertEqual(payload["native_wrench_row_count"], 1)
        self.assertEqual(payload["native_wrench_source_class"], contract.SOURCE_GAZEBO_CONTACT)
        self.assertEqual(payload["verified_native_wrench_row_count"], 0)
        self.assertIn("missing_base_frame_transform_evidence", payload["blockers"])
        self.assertIn("native_wrench_status_not_valid", payload["blockers"])
        self.assertIn("no_positive_normal_load_from_native_gazebo_wrench", payload["blockers"])
        self.assertEqual(payload["row_diagnostics"][0]["native_wrench_source_schema"], "gz.msgs.Contact.contact.wrench")
        self.assertEqual(payload["row_diagnostics"][0]["native_wrench_force_source_class"], contract.SOURCE_GAZEBO_CONTACT)
        self.assertEqual(payload["row_diagnostics"][0]["native_wrench_baseline_policy"], "gazebo_contact_zero_no_contact_baseline_unverified")

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
        self.assertFalse(payload["total_contact_wrench_proven"])
        self.assertEqual(
            payload["wrench_aggregation_policy"],
            "single_native_contact_point_wrench_sample_no_total_contact_wrench_claim",
        )
        self.assertIn("single_contact_point_wrench_sample", trace["rows"][0]["diagnostic_flags"])
        self.assertIn("total_contact_wrench_not_proven", trace["rows"][0]["diagnostic_flags"])

    def test_verified_raw_components_build_total_contact_wrench_trace(self) -> None:
        payload = wrench_adapter.build_wrench_trace_or_report(
            _verified_base_frame_gazebo_total_contact_wrench_pair_log(),
            generated_at="2026-06-21T03:10:00+08:00",
        )

        self.assertTrue(payload["trace_written"])
        self.assertEqual(payload["claim_tier"], "physical Gazebo collision/contact physics")
        self.assertTrue(payload["total_contact_wrench_proven"])
        self.assertEqual(payload["wrench_aggregation_policy"], "total_contact_wrench")
        self.assertEqual(payload["total_contact_wrench_row_count"], 1)
        self.assertEqual(payload["total_contact_wrench_blockers"], [])
        trace = payload["wrench_trace"]
        self.assertEqual(trace["rows"][0]["normal_load_n"], 3.5)
        self.assertEqual(trace["rows"][0]["quality"], "gazebo_contact_total_native_wrench")
        self.assertIn("total_contact_wrench", trace["rows"][0]["diagnostic_flags"])
        self.assertIn("total_contact_wrench_component_count=2", trace["rows"][0]["diagnostic_flags"])

    def test_standalone_total_wrench_trace_forbids_stage_or_same_run_upgrade(self) -> None:
        payload = wrench_adapter.build_wrench_trace_or_report(
            _verified_base_frame_gazebo_total_contact_wrench_pair_log(),
            generated_at="2026-06-21T03:10:00+08:00",
            observation_scope=capture.STANDALONE_P2_OBSERVATION_SCOPE,
        )

        self.assertTrue(payload["total_contact_wrench_proven"])
        self.assertEqual(payload["observation_scope"], "standalone_p2_contact_witness")
        self.assertIn("per-stage physical Gazebo contact upgrade", payload["forbidden_claim"])
        self.assertIn("same-run dual-sensor/integrated binding upgrade", payload["forbidden_claim"])

    def test_stage_scoped_wrench_adapter_write_uses_custom_filenames_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="stage_wrench_adapter_test_") as tmp:
            tmp_path = Path(tmp)
            contact_path = _write_json(
                tmp_path / "stage_contact_pair_log.json",
                _verified_base_frame_gazebo_total_contact_wrench_pair_log(),
            )
            report_path = wrench_adapter.write_wrench_trace_or_report(
                tmp_path / "adapter",
                contact_pair_path=contact_path,
                generated_at="2026-06-21T17:12:00+08:00",
                source_topic="/ur10e/contact/gazebo/step5b/wrench",
                report_filename="stage_contact_wrench_adapter.json",
                trace_filename="stage_contact_wrench_trace.json",
                stage_id="step5b",
                observation_id="stage-step5b-dual-sensor-fixture-001",
            )
            payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report_path.name, "stage_contact_wrench_adapter.json")
        self.assertEqual(payload["stage_id"], "step5b")
        self.assertEqual(payload["observation_id"], "stage-step5b-dual-sensor-fixture-001")
        self.assertEqual(payload["source_topic"], "/ur10e/contact/gazebo/step5b/wrench")
        self.assertTrue(payload["total_contact_wrench_proven"])
        self.assertEqual(Path(payload["wrench_trace_path"]).name, "stage_contact_wrench_trace.json")

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

    def test_gz_eoat_wrench_verifier_builds_adapter_ready_physical_trace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_gz_wrench_verifier_test_") as tmp:
            tmp_path = Path(tmp)
            eoat_path, surface_path, baseline_path = _gz_contact_payloads_for_verifier(tmp_path)
            report_path = gz_wrench_verifier.write_verified_contact_pair_or_report(
                tmp_path / "verified",
                eoat_contact_pair_path=eoat_path,
                surface_contact_pair_path=surface_path,
                baseline_contact_pair_path=baseline_path,
                generated_at="2026-06-21T07:05:00+08:00",
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            verified_path = Path(report["outputs"]["verified_contact_pair_path"])
            cross_check = json.loads(Path(report["outputs"]["cross_check_path"]).read_text(encoding="utf-8"))
            verified_payload = json.loads(verified_path.read_text(encoding="utf-8"))
            adapter_payload = wrench_adapter.build_wrench_trace_or_report(
                verified_payload,
                generated_at="2026-06-21T07:05:00+08:00",
            )
            audit = contact_audit.build_audit(
                p2_inventory_payload=p2_inventory.build_inventory(generated_at="2026-06-21T07:05:00+08:00"),
                wrench_payload=adapter_payload,
                contact_pair_payload=verified_payload,
                generated_at="2026-06-21T07:05:00+08:00",
            )

        self.assertEqual(report["claim_tier"], "physical Gazebo collision/contact physics")
        self.assertEqual(report["verified_row_count"], 1)
        self.assertEqual(report["blockers"], [])
        self.assertTrue(
            report["claim_boundary_gate"][
                "surface_eoat_cross_check_is_cross_run_repeatability_not_concurrent_observation"
            ]
        )
        self.assertFalse(report["claim_boundary_gate"]["total_contact_wrench_proven"])
        self.assertEqual(cross_check["comparison_scope"], "cross_run_deterministic_repeatability")
        self.assertFalse(cross_check["same_run_concurrent_observation"])
        self.assertEqual(verified_payload["rows"][0]["native_gazebo_contact_wrench"]["frame_id"], "base")
        self.assertEqual(
            verified_payload["rows"][0]["native_gazebo_contact_wrench"]["baseline_policy"],
            "gazebo_contact_zero_no_contact_baseline",
        )
        self.assertEqual(
            verified_payload["rows"][0]["native_gazebo_contact_wrench"]["wrench_aggregation_policy"],
            "raw_components_preserved_for_adapter_total_wrench_verification",
        )
        self.assertTrue(adapter_payload["trace_written"])
        self.assertEqual(adapter_payload["claim_tier"], "physical Gazebo collision/contact physics")
        self.assertTrue(adapter_payload["total_contact_wrench_proven"])
        self.assertEqual(adapter_payload["wrench_aggregation_policy"], "total_contact_wrench")
        self.assertTrue(audit["physical_gazebo_contact_gate"]["force_contact_physics_proven"])

    def test_gz_eoat_wrench_verifier_blocks_when_base_frame_is_not_geometric(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_gz_wrench_verifier_test_") as tmp:
            tmp_path = Path(tmp)
            eoat_path, surface_path, baseline_path = _gz_contact_payloads_for_verifier(
                tmp_path,
                include_base_frame=False,
            )
            report_path = gz_wrench_verifier.write_verified_contact_pair_or_report(
                tmp_path / "verified",
                eoat_contact_pair_path=eoat_path,
                surface_contact_pair_path=surface_path,
                baseline_contact_pair_path=baseline_path,
                generated_at="2026-06-21T07:05:00+08:00",
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["claim_tier"], "visual_only")
        self.assertIsNone(report["outputs"]["verified_contact_pair_path"])
        self.assertIn("missing_ur10e_base_frame_model", report["blockers"])

    def test_gz_eoat_wrench_verifier_blocks_without_independent_baseline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_gz_wrench_verifier_test_") as tmp:
            tmp_path = Path(tmp)
            eoat_path, surface_path, baseline_path = _gz_contact_payloads_for_verifier(tmp_path)
            baseline_payload = json.loads(baseline_path.read_text(encoding="utf-8"))
            baseline_payload["baseline_mode"] = None
            baseline_path.write_text(json.dumps(baseline_payload, indent=2), encoding="utf-8")
            report_path = gz_wrench_verifier.write_verified_contact_pair_or_report(
                tmp_path / "verified",
                eoat_contact_pair_path=eoat_path,
                surface_contact_pair_path=surface_path,
                baseline_contact_pair_path=baseline_path,
                generated_at="2026-06-21T07:05:00+08:00",
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["claim_tier"], "visual_only")
        self.assertIsNone(report["outputs"]["verified_contact_pair_path"])
        self.assertIn("baseline_mode_not_no_contact_static_elevated_eoat", report["blockers"])

    def test_gz_eoat_wrench_verifier_blocks_missing_baseline_raw_log_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_gz_wrench_verifier_test_") as tmp:
            tmp_path = Path(tmp)
            eoat_path, surface_path, baseline_path = _gz_contact_payloads_for_verifier(tmp_path)
            baseline_payload = json.loads(baseline_path.read_text(encoding="utf-8"))
            baseline_payload["raw_jsonl_path"] = str(tmp_path / "baseline" / "missing_raw.jsonl")
            baseline_path.write_text(json.dumps(baseline_payload, indent=2), encoding="utf-8")
            report_path = gz_wrench_verifier.write_verified_contact_pair_or_report(
                tmp_path / "verified",
                eoat_contact_pair_path=eoat_path,
                surface_contact_pair_path=surface_path,
                baseline_contact_pair_path=baseline_path,
                generated_at="2026-06-21T07:05:00+08:00",
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["claim_tier"], "visual_only")
        self.assertIsNone(report["outputs"]["verified_contact_pair_path"])
        self.assertIn("baseline_raw_topic_log_not_empty", report["blockers"])

    def test_gz_eoat_wrench_verifier_blocks_surface_eoat_cross_check_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_gz_wrench_verifier_test_") as tmp:
            tmp_path = Path(tmp)
            eoat_path, surface_path, baseline_path = _gz_contact_payloads_for_verifier(tmp_path)
            surface_payload = json.loads(surface_path.read_text(encoding="utf-8"))
            surface_payload["rows"][0]["native_gazebo_contact_wrench"]["other_force_n"] = [0.0, 0.0, 99.0]
            surface_path.write_text(json.dumps(surface_payload, indent=2), encoding="utf-8")
            report_path = gz_wrench_verifier.write_verified_contact_pair_or_report(
                tmp_path / "verified",
                eoat_contact_pair_path=eoat_path,
                surface_contact_pair_path=surface_path,
                baseline_contact_pair_path=baseline_path,
                generated_at="2026-06-21T07:05:00+08:00",
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["claim_tier"], "visual_only")
        self.assertIsNone(report["outputs"]["verified_contact_pair_path"])
        self.assertIn("no_verified_eoat_contact_wrench_rows", report["blockers"])
        self.assertIn("eoat_body1_not_matching_surface_body1", report["rejected_rows"][0]["blockers"])

    def test_gz_eoat_wrench_verifier_filters_below_floor_rows_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_gz_wrench_verifier_test_") as tmp:
            tmp_path = Path(tmp)
            eoat_path, surface_path, baseline_path = _gz_contact_payloads_for_verifier(tmp_path)
            eoat_payload = json.loads(eoat_path.read_text(encoding="utf-8"))
            weak_payload = capture.contact_pair_log_from_json_lines(
                [_raw_gz_eoat_sensor_contacts_json_line_with_native_wrench()],
                topic=capture.DEFAULT_CONTACT_TOPIC,
                world_path=eoat_payload["world_path"],
                raw_jsonl_path=eoat_payload["raw_jsonl_path"],
                transport="gz",
                sensor_collision_role="eoat",
            )
            eoat_payload["rows"].extend(weak_payload["rows"])
            eoat_payload["row_count"] = len(eoat_payload["rows"])
            eoat_path.write_text(json.dumps(eoat_payload, indent=2), encoding="utf-8")
            report_path = gz_wrench_verifier.write_verified_contact_pair_or_report(
                tmp_path / "verified",
                eoat_contact_pair_path=eoat_path,
                surface_contact_pair_path=surface_path,
                baseline_contact_pair_path=baseline_path,
                generated_at="2026-06-21T07:05:00+08:00",
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            verified_payload = json.loads(Path(report["outputs"]["verified_contact_pair_path"]).read_text(encoding="utf-8"))

        self.assertEqual(report["claim_tier"], "physical Gazebo collision/contact physics")
        self.assertEqual(report["verified_row_count"], 1)
        self.assertEqual(verified_payload["row_count"], 1)
        self.assertIn("normal_load_below_floor", report["rejected_rows"][0]["blockers"])


if __name__ == "__main__":
    unittest.main()
