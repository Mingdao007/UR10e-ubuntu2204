#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import run_step5b_gz_transport_probe as probe  # noqa: E402


class Step5bGzTransportProbeTest(unittest.TestCase):
    def test_probe_world_uses_step5b_topic_and_nonformal_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step5b_probe_world_test_") as tmp:
            world_path, manifest_path = probe.build_probe_world(Path(tmp), no_contact=False)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tree = ET.parse(world_path)
            root = tree.getroot()

        topics = [element.text for element in root.findall(".//sensor[@type='contact']/contact/topic")]
        models = {element.attrib["name"] for element in root.findall(".//model")}
        self.assertIn(probe.TOPIC, topics)
        self.assertIn(probe.PROBE_MODEL_NAME, models)
        self.assertIn("ur10e_base_frame", models)
        self.assertEqual(manifest["stage_id"], "step5b")
        self.assertEqual(manifest["observation_scope"], probe.OBSERVATION_SCOPE)
        self.assertEqual(manifest["claim_tier"], "visual_only")
        self.assertFalse(manifest["step5b_attempt_spent"])
        self.assertFalse(manifest["formal_step5b_attempt"])

    def test_no_contact_probe_world_marks_negative_control(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step5b_probe_world_test_") as tmp:
            world_path, manifest_path = probe.build_probe_world(Path(tmp), no_contact=True)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tree = ET.parse(world_path)
            static = tree.getroot().find(f".//model[@name='{probe.PROBE_MODEL_NAME}']/static")

        self.assertEqual(manifest["probe_mode"], "no_contact_negative_control")
        self.assertIsNotNone(static)
        self.assertEqual(static.text, "true")

    def test_transport_commands_separate_server_and_subscriber(self) -> None:
        world = Path("/tmp/step5b_probe.sdf")
        self.assertEqual(probe.server_command("gz", world), ["gz", "sim", "-r", "-s", str(world)])
        self.assertEqual(probe.server_command("ignition", world), ["ign", "gazebo", "-r", "-s", str(world)])
        self.assertEqual(
            probe.subscriber_command("gz", topic=probe.TOPIC, max_messages=2),
            ["gz", "topic", "-e", "-t", probe.TOPIC, "-n", "2", "--json-output"],
        )
        self.assertEqual(
            probe.subscriber_command("ignition", topic=probe.TOPIC, max_messages=2),
            ["ign", "topic", "-e", "-t", probe.TOPIC, "-n", "2", "--json-output"],
        )

    def test_gazebo_env_contains_package_resource_parent(self) -> None:
        env = probe.gazebo_env()
        entries = env["GZ_SIM_RESOURCE_PATH"].split(":")
        self.assertIn(str(probe.WORKSPACE / "install/ur10e_example_controllers/share"), entries)

    def test_transport_comparison_requires_gz_native_and_negative_control(self) -> None:
        comparison = probe.build_transport_comparison(
            {
                "gz_server_gz_subscriber": {"native_wrench_row_count": 1, "row_count": 1},
                "ignition_server_ignition_subscriber": {"native_wrench_row_count": 0, "row_count": 1},
                "gz_server_ignition_subscriber": {"native_wrench_row_count": 0, "row_count": 0},
                "ignition_server_gz_subscriber": {"native_wrench_row_count": 0, "row_count": 0},
                "gz_server_gz_subscriber_no_contact": {"native_wrench_row_count": 0, "row_count": 0},
            }
        )

        self.assertTrue(comparison["m2_static_probe_native_exposure_pass"])
        self.assertTrue(comparison["native_wrench_exposed_on_step5b_topic_by_gz_server_gz_subscriber"])
        self.assertFalse(comparison["m5_go_allowed"])
        self.assertIn("transform_evidence_not_yet_verified", comparison["m5_go_blockers"])
        self.assertFalse(comparison["step5b_attempt_spent"])

    def test_transport_comparison_blocks_without_gz_native_rows(self) -> None:
        comparison = probe.build_transport_comparison(
            {
                "gz_server_gz_subscriber": {"native_wrench_row_count": 0, "row_count": 1},
                "ignition_server_ignition_subscriber": {"native_wrench_row_count": 0, "row_count": 1},
                "gz_server_ignition_subscriber": {"native_wrench_row_count": 0, "row_count": 0},
                "ignition_server_gz_subscriber": {"native_wrench_row_count": 0, "row_count": 0},
                "gz_server_gz_subscriber_no_contact": {"native_wrench_row_count": 0, "row_count": 0},
            }
        )

        self.assertFalse(comparison["m2_static_probe_native_exposure_pass"])
        self.assertIn("gz_server_gz_subscriber_native_rows_gt_zero", comparison["blockers"])

    def test_transport_comparison_blocks_cross_transport_rows(self) -> None:
        comparison = probe.build_transport_comparison(
            {
                "gz_server_gz_subscriber": {"native_wrench_row_count": 1, "row_count": 1},
                "ignition_server_ignition_subscriber": {"native_wrench_row_count": 0, "row_count": 1},
                "gz_server_ignition_subscriber": {"native_wrench_row_count": 0, "row_count": 1},
                "ignition_server_gz_subscriber": {"native_wrench_row_count": 0, "row_count": 0},
                "gz_server_gz_subscriber_no_contact": {"native_wrench_row_count": 0, "row_count": 0},
            }
        )

        self.assertFalse(comparison["m2_static_probe_native_exposure_pass"])
        self.assertIn("gz_server_ignition_subscriber_rows_zero", comparison["blockers"])

    def test_required_shape_writes_step5b_gz_native_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step5b_probe_shape_test_") as tmp:
            root = Path(tmp)
            gz_dir = root / "cases" / "gz_server_gz_subscriber"
            gz_dir.mkdir(parents=True)
            stdout = gz_dir / "contact_topic_stdout.jsonl"
            stdout.write_text('{"contact": []}\n', encoding="utf-8")
            contact_log = gz_dir / "gazebo_contact_pair_log.json"
            contact_log.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "native_gazebo_contact_wrench": {
                                    "source": "gazebo_contact_message_wrench",
                                }
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            summaries = {
                "gz_server_gz_subscriber": {
                    "artifact_path": str(contact_log),
                    "stdout_path": str(stdout),
                    "native_wrench_row_count": 1,
                },
                "ignition_server_ignition_subscriber": {
                    "stdout_path": str(stdout),
                    "native_wrench_row_count": 0,
                },
            }
            comparison = probe.build_transport_comparison(
                {
                    **summaries,
                    "gz_server_gz_subscriber_no_contact": {"row_count": 0, "native_wrench_row_count": 0},
                }
            )
            probe.write_required_shape(root, summaries, comparison)
            summary = json.loads((root / "step5b_gz_capture" / "step5b_gz_native_wrench_summary.json").read_text())
            comparison_written = (root / "transport_baseline" / "transport_comparison.json").is_file()

        self.assertEqual(summary["stage_id"], "step5b")
        self.assertEqual(summary["topic"], probe.TOPIC)
        self.assertEqual(summary["native_wrench_row_count"], 1)
        self.assertFalse(summary["accepted_for_step5b_physical_contact"])
        self.assertTrue(comparison_written)

    def test_m3_wrench_integration_builds_total_contact_adapter_trace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step5b_probe_m3_test_") as tmp:
            root = Path(tmp)
            positive_world, _positive_manifest = probe.build_probe_world(root / "worlds", no_contact=False)
            negative_world, _negative_manifest = probe.build_probe_world(root / "worlds", no_contact=True)
            positive_raw = root / "positive" / "contact_topic_stdout.jsonl"
            baseline_raw = root / "baseline" / "contact_topic_stdout.jsonl"
            positive_raw.parent.mkdir(parents=True)
            baseline_raw.parent.mkdir(parents=True)
            positive_raw.write_text('{"contact": []}\n', encoding="utf-8")
            baseline_raw.write_text("", encoding="utf-8")
            positive_path = root / "positive" / "gazebo_contact_pair_log.json"
            baseline_path = root / "baseline" / "gazebo_contact_pair_log.json"
            positive_path.write_text(
                json.dumps(_positive_contact_payload(str(positive_world), str(positive_raw)), indent=2) + "\n",
                encoding="utf-8",
            )
            baseline_path.write_text(
                json.dumps(_baseline_contact_payload(str(negative_world), str(baseline_raw)), indent=2) + "\n",
                encoding="utf-8",
            )
            manifest = probe.build_m3_adapter_and_manifest(
                output_dir=root,
                positive_contact_path=positive_path,
                baseline_contact_path=baseline_path,
                generated_at="2026-06-22T11:05:00+08:00",
            )
            adapter = json.loads(Path(manifest["adapter"]["path"]).read_text(encoding="utf-8"))
            verified = json.loads(Path(manifest["verification"]["verified_contact_pair_path"]).read_text(encoding="utf-8"))
            trace = json.loads(Path(manifest["adapter"]["trace_path"]).read_text(encoding="utf-8"))
            correlation = json.loads(
                Path(manifest["required_shape"]["step5b_correlation"]["correlation_path"]).read_text(
                    encoding="utf-8"
                )
            )
            semantics = json.loads(
                Path(manifest["required_shape"]["step5b_semantics"]["sign_frame_magnitude_audit_path"]).read_text(
                    encoding="utf-8"
                )
            )
            required_shape_files_exist = (
                Path(manifest["required_shape"]["step5b_total_wrench"]["summary_path"]).is_file()
                and Path(manifest["required_shape"]["step5b_correlation"]["correlation_path"]).is_file()
                and Path(manifest["required_shape"]["step5b_semantics"]["sign_frame_magnitude_audit_path"]).is_file()
            )

        self.assertTrue(manifest["m3_wrench_integration_pass"])
        self.assertEqual(manifest["formal_step5b_acceptance_claim_tier"], "visual_only")
        self.assertIn("not formal Step5b physical acceptance proof", manifest["allowed_claim"])
        self.assertTrue(adapter["trace_written"])
        self.assertTrue(adapter["total_contact_wrench_proven"])
        self.assertEqual(adapter["formal_step5b_acceptance_claim_tier"], "visual_only")
        self.assertEqual(adapter["formal_step5b_acceptance_status"], "blocked")
        self.assertIn("not formal Step5b physical acceptance proof", adapter["allowed_claim"])
        self.assertEqual(adapter["wrench_aggregation_policy"], "total_contact_wrench")
        native = verified["rows"][0]["native_gazebo_contact_wrench"]
        self.assertEqual(native["frame_id"], "base")
        self.assertEqual(native["frame_policy"], "verified_world_to_base_identity_from_step5b_transport_probe_sdf")
        self.assertEqual(native["baseline_policy"], "gazebo_contact_zero_no_contact_baseline")
        self.assertEqual(native["selected_body_role"], "eoat")
        self.assertEqual(native["selected_body"], "body_2_wrench")
        self.assertEqual(verified["rows"][0]["normal"], [0.0, 0.0, 1.0])
        self.assertEqual(trace["rows"][0]["reaction_normal"], [0.0, 0.0, 1.0])
        self.assertEqual(trace["rows"][0]["approach_normal"], [-0.0, -0.0, -1.0])
        self.assertEqual(correlation["formal_step5b_acceptance_claim_tier"], "visual_only")
        self.assertIn("not formal Step5b physical acceptance proof", correlation["allowed_claim"])
        self.assertEqual(semantics["selected_body_role"], "eoat")
        self.assertEqual(semantics["formal_step5b_acceptance_claim_tier"], "visual_only")
        self.assertIn(
            "transform_evidence_is_sdf_identity_assumption_not_independent_gazebo_frame_measurement",
            semantics["formal_readiness_blockers"],
        )
        self.assertTrue(required_shape_files_exist)

    def test_m3_wrench_integration_blocks_nonempty_baseline_raw_log(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step5b_probe_m3_test_") as tmp:
            root = Path(tmp)
            positive_world, _positive_manifest = probe.build_probe_world(root / "worlds", no_contact=False)
            negative_world, _negative_manifest = probe.build_probe_world(root / "worlds", no_contact=True)
            positive_raw = root / "positive" / "contact_topic_stdout.jsonl"
            baseline_raw = root / "baseline" / "contact_topic_stdout.jsonl"
            positive_raw.parent.mkdir(parents=True)
            baseline_raw.parent.mkdir(parents=True)
            positive_raw.write_text('{"contact": []}\n', encoding="utf-8")
            baseline_raw.write_text('{"unexpected": true}\n', encoding="utf-8")
            positive_path = root / "positive" / "gazebo_contact_pair_log.json"
            baseline_path = root / "baseline" / "gazebo_contact_pair_log.json"
            positive_path.write_text(
                json.dumps(_positive_contact_payload(str(positive_world), str(positive_raw)), indent=2) + "\n",
                encoding="utf-8",
            )
            baseline_path.write_text(
                json.dumps(_baseline_contact_payload(str(negative_world), str(baseline_raw)), indent=2) + "\n",
                encoding="utf-8",
            )
            manifest = probe.build_m3_adapter_and_manifest(
                output_dir=root,
                positive_contact_path=positive_path,
                baseline_contact_path=baseline_path,
                generated_at="2026-06-22T11:05:00+08:00",
            )

        self.assertFalse(manifest["m3_wrench_integration_pass"])
        self.assertIn("baseline_raw_topic_log_not_empty", manifest["blockers"])


def _positive_contact_payload(world_path: str, raw_path: str) -> dict[str, object]:
    return {
        "schema": probe.contact_capture.CONTACT_LOG_SCHEMA,
        "generated_at": "2026-06-22T11:04:00+08:00",
        "mode": "offline_nonformal_step5b_transport_probe_capture",
        "source": "gazebo_contact_sensor_topic",
        "sim_transport": "gz",
        "server_transport": "gz",
        "subscriber_transport": "gz",
        "sensor_collision_role": "surface",
        "selected_contact_body_role": "surface",
        "claim_tier": "visual_only",
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "topic": probe.TOPIC,
        "world_path": world_path,
        "raw_jsonl_path": raw_path,
        "row_count": 1,
        "parse_issues": [],
        "stage_id": probe.STAGE_ID,
        "observation_id": "fixture-gz-server-gz-subscriber",
        "observation_scope": probe.OBSERVATION_SCOPE,
        "time_window": {
            "start": "2026-06-22T11:04:00+08:00",
            "end": "2026-06-22T11:04:01+08:00",
            "clock_source": "fixture",
        },
        "probe_mode": "forced_contact_positive_probe",
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
        "capture": {"topic_timeout_expired": False, "allow_no_messages": False},
        "rows": [
            {
                "stamp_s": 0.125,
                "stamp_evidence": True,
                "collision1": "step5_contact_surface::surface::collision",
                "collision2": f"{probe.PROBE_MODEL_NAME}::{probe.PROBE_LINK_NAME}::{probe.PROBE_COLLISION_NAME}",
                "position_m": [0.0, 0.0, 0.0],
                "normal": [0.0, 0.0, -1.0],
                "normal_source": "gazebo_contact_message_normal",
                "contact_count": 2,
                "depth_m": 0.001,
                "raw_line_index": 0,
                "raw_contact_index": 0,
                "topic": probe.TOPIC,
                "native_gazebo_contact_wrench": {
                    "source": "gazebo_contact_message_wrench",
                    "source_schema": "gz.msgs.Contact.contact.wrench",
                    "force_source_class": "gazebo_contact",
                    "selected_body": "body_1_wrench",
                    "selected_body_field": "body1Wrench",
                    "selected_body_collision": "collision1",
                    "selected_body_role": "surface",
                    "other_body": "body_2_wrench",
                    "other_body_field": "body2Wrench",
                    "raw_wrench_index": 0,
                    "raw_wrench_count": 2,
                    "measured_contact_wrench": True,
                    "commanded_force": False,
                    "force_n": [0.0, 0.0, -2.0],
                    "torque_nm": [0.0, 0.0, 0.0],
                    "selected_force_dot_contact_normal_n": 2.0,
                    "other_force_n": [0.0, 0.0, 2.0],
                    "other_torque_nm": [0.0, 0.0, 0.0],
                    "other_force_dot_contact_normal_n": -2.0,
                    "wrench_stamp_s": 0.125,
                    "wrench_stamp_evidence": True,
                    "frame_id": "gazebo_contact_message_native_frame",
                    "frame_policy": "native_gazebo_contact_frame_untransformed",
                    "frame_transform_evidence": False,
                    "status": "raw_untransformed",
                    "baseline_policy": "gazebo_contact_zero_no_contact_baseline_unverified",
                },
                "raw_gazebo_contact_wrench_count": 2,
                "raw_gazebo_contact_wrenches": [
                    {"body1Wrench": {"force": {"z": -2.0}}, "body2Wrench": {"force": {"z": 2.0}}},
                    {"body1Wrench": {"force": {"z": -3.0}}, "body2Wrench": {"force": {"z": 3.0}}},
                ],
            }
        ],
    }


def _baseline_contact_payload(world_path: str, raw_path: str) -> dict[str, object]:
    return {
        "schema": probe.contact_capture.CONTACT_LOG_SCHEMA,
        "generated_at": "2026-06-22T11:04:02+08:00",
        "mode": "offline_nonformal_step5b_transport_probe_capture",
        "source": "gazebo_contact_sensor_topic",
        "sim_transport": "gz",
        "server_transport": "gz",
        "subscriber_transport": "gz",
        "sensor_collision_role": "surface",
        "selected_contact_body_role": "surface",
        "claim_tier": "visual_only",
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "topic": probe.TOPIC,
        "world_path": world_path,
        "raw_jsonl_path": raw_path,
        "row_count": 0,
        "parse_issues": [],
        "stage_id": probe.STAGE_ID,
        "observation_id": "fixture-gz-server-gz-subscriber-no-contact",
        "observation_scope": probe.OBSERVATION_SCOPE,
        "probe_mode": "no_contact_negative_control",
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
        "capture": {"topic_timeout_expired": True, "allow_no_messages": True},
        "rows": [],
    }


if __name__ == "__main__":
    unittest.main()
