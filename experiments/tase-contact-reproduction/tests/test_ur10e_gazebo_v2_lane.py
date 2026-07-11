#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
TOOLS = ROOT / "tools"
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(TOOLS))

from ur10e_example_controllers import ur10e_gazebo_v2 as lane  # noqa: E402
import build_ur10e_gazebo_v2_evidence as evidence  # noqa: E402
import capture_ur10e_gazebo_v2_native as native_capture  # noqa: E402


CONFIG = PACKAGE / "config"
WORLD = PACKAGE / "worlds" / "ur10e_gazebo_v2_fortress.sdf"
LAUNCH = PACKAGE / "launch" / "ur10e_gazebo_v2_fortress.launch.py"
CONTRACT = ROOT / "config" / "gazebo_v2_lane_contract.json"
TICK_SCHEMA = ROOT / "config" / "schemas" / "ur10e_gazebo_v2_tick_v1.schema.json"


def _robot_fixture() -> str:
    root = ET.Element("robot", {"name": "fixture"})
    for name in ("tool0", lane.EOAT_LINK, lane.ACTIVE_TCP_LINK):
        ET.SubElement(root, "link", {"name": name})
    fixed = ET.SubElement(root, "joint", {"name": lane.EOAT_FIXED_JOINT, "type": "fixed"})
    ET.SubElement(fixed, "parent", {"link": "tool0"})
    ET.SubElement(fixed, "child", {"link": lane.EOAT_LINK})
    tcp_fixed = ET.SubElement(root, "joint", {"name": lane.ACTIVE_TCP_JOINT, "type": "fixed"})
    ET.SubElement(tcp_fixed, "parent", {"link": "tool0"})
    ET.SubElement(tcp_fixed, "child", {"link": lane.ACTIVE_TCP_LINK})
    ET.SubElement(
        tcp_fixed,
        "origin",
        {"xyz": " ".join(str(value) for value in lane.ACTIVE_TCP_OFFSET_TOOL0_M), "rpy": "0 0 0"},
    )
    tcp_gazebo = ET.SubElement(root, "gazebo", {"reference": lane.ACTIVE_TCP_JOINT})
    ET.SubElement(tcp_gazebo, "preserveFixedJoint").text = "true"
    control = ET.SubElement(root, "ros2_control", {"name": "ur", "type": "system"})
    hardware = ET.SubElement(control, "hardware")
    ET.SubElement(hardware, "plugin").text = "ign_ros2_control/IgnitionSystem"
    for name in lane.JOINT_NAMES:
        joint = ET.SubElement(control, "joint", {"name": name})
        ET.SubElement(joint, "command_interface", {"name": "position"})
        ET.SubElement(joint, "command_interface", {"name": "velocity"})
        ET.SubElement(joint, "state_interface", {"name": "position"})
        ET.SubElement(joint, "state_interface", {"name": "velocity"})
    gazebo = ET.SubElement(root, "gazebo")
    ET.SubElement(
        gazebo,
        "plugin",
        {"filename": lane.IGN_ROS2_CONTROL_PLUGIN, "name": "ign_ros2_control::IgnitionROS2ControlPlugin"},
    )
    return ET.tostring(root, encoding="unicode")


def _tick(run_id: str, sequence: int, *, backend: str = "velocity") -> dict[str, object]:
    interface = "velocity" if backend == "velocity" else "effort"
    return {
        "schema": lane.TICK_SCHEMA,
        "run_id": run_id,
        "sequence": sequence,
        "sim_time_s": sequence * 0.002,
        "backend": backend,
        "state": {
            "q_rad": [0.0] * 6,
            "qd_rad_s": [0.0] * 6,
            "tcp_pose_base": [0.0] * 6,
            "tcp_twist_base": [0.0] * 6,
            "native_ft_tcp": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
            "native_contact_count": 1,
            "frame_lineage": {
                "world": "gazebo_world",
                "spawn_root": "base_link",
                "control_base": "base",
                "tool": "tool0",
                "active_tcp": "active_tcp",
            },
        },
        "candidate": {"values": [0.0] * 6, "source": "strict_rnn", "valid": True, "diagnostics": {}},
        "decision": {"action": "accept", "reason": "ok", "accepted": True},
        "command": {"interface": interface, "values": [0.0] * 6, "sequence": sequence, "applied": True},
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _write_valid_png(path: Path, *, width: int = 640, height: int = 360, blank: bool = False) -> None:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    if blank:
        scanlines = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    else:
        scanlines = b"".join(
            b"\x00"
            + b"".join(bytes(((x + y) % 256, (2 * x + y) % 256, (x + 3 * y) % 256)) for x in range(width))
            for y in range(height)
        )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _model_binding_fixture(run_dir: Path, *, backend: str) -> dict[str, object]:
    binding_dir = run_dir / "model_bindings"
    binding_dir.mkdir()
    generated = lane.configure_robot_description(_robot_fixture(), backend=backend)
    generated_path = binding_dir / "generated_robot_description.urdf"
    generated_path.write_text(generated, encoding="utf-8")
    rows: dict[str, dict[str, object]] = {}
    for source_id in sorted(evidence.REQUIRED_BINDING_IDS):
        path = binding_dir / f"{source_id}.txt"
        path.write_text(
            TICK_SCHEMA.read_text(encoding="utf-8") if source_id == "tick_schema" else f"fixture:{source_id}\n",
            encoding="utf-8",
        )
        rows[source_id] = {
            "source_id": source_id,
            "source_path": f"fixture:{source_id}",
            "artifact_path": str(path.relative_to(run_dir)),
            "size": path.stat().st_size,
            "sha256": _sha(path),
        }
    generated_row = {
        "source_id": "generated_urdf",
        "source_path": "generated_in_fixture",
        "artifact_path": str(generated_path.relative_to(run_dir)),
        "size": generated_path.stat().st_size,
        "sha256": _sha(generated_path),
    }
    material = "\n".join(
        f"{source_id}:{row['sha256']}"
        for source_id, row in sorted({**rows, "generated_urdf": generated_row}.items())
    )
    return {
        "schema": "ur10e_gazebo_v2_model_bindings_v1",
        "backend": backend,
        "generated_urdf": generated_row,
        "files": rows,
        "composite_sha256": hashlib.sha256(material.encode()).hexdigest(),
    }


def _capture_artifacts(run_dir: Path, names: list[str]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for name in names:
        path = run_dir / name
        result[name] = {"size": path.stat().st_size, "sha256": _sha(path)}
    return result


class GazeboV2LaneTest(unittest.TestCase):
    def test_robot_description_exports_only_selected_interface_and_attached_sensors(self) -> None:
        for backend, interface in (("velocity", "velocity"), ("effort_surrogate", "effort")):
            with self.subTest(backend=backend):
                configured = lane.configure_robot_description(_robot_fixture(), backend=backend)
                audit = lane.audit_robot_description(configured, backend=backend)
                root = ET.fromstring(configured)
                interfaces = {
                    joint.attrib["name"]: [node.attrib["name"] for node in joint.findall("command_interface")]
                    for joint in root.findall("./ros2_control/joint")
                }
                self.assertTrue(audit["pass"], audit["blockers"])
                active_joint = root.find(f"./joint[@name='{lane.ACTIVE_TCP_JOINT}']")
                self.assertIsNotNone(root.find(f"./link[@name='{lane.ACTIVE_TCP_LINK}']"))
                self.assertIsNotNone(active_joint)
                self.assertEqual(
                    [float(value) for value in active_joint.find("origin").attrib["xyz"].split()],
                    list(lane.ACTIVE_TCP_OFFSET_TOOL0_M),
                )
                self.assertEqual(set(interfaces), set(lane.JOINT_NAMES))
                self.assertTrue(all(values == [interface] for values in interfaces.values()))
                self.assertEqual(
                    root.find("./gazebo/sensor[@name='gazebo_v2_native_contact']/topic").text,
                    lane.NATIVE_CONTACT_TOPIC,
                )
                self.assertEqual(
                    root.find("./gazebo/sensor[@name='gazebo_v2_native_ft']/topic").text,
                    lane.NATIVE_FT_TOPIC,
                )

    def test_abi_preflight_accepts_only_fortress_plugin_identity(self) -> None:
        report = lane.build_abi_preflight(
            ign_path="/usr/bin/ign",
            version_output="6.18.0",
            ign_plugin_paths=[f"/opt/ros/humble/lib/{lane.IGN_ROS2_CONTROL_PLUGIN}"],
            ros_gz_sim_prefix="/opt/ros/humble",
        )
        self.assertTrue(report["pass"], report["blockers"])
        mixed = lane.build_abi_preflight(
            ign_path="/usr/bin/ign",
            version_output="8.14.0",
            ign_plugin_paths=[
                f"/opt/ros/humble/lib/{lane.IGN_ROS2_CONTROL_PLUGIN}",
                f"/opt/ros/humble/lib/{lane.FORBIDDEN_PLUGIN}",
            ],
            ros_gz_sim_prefix="/opt/ros/humble",
        )
        self.assertFalse(mixed["pass"])
        self.assertIn("ign_gazebo_major_not_6", mixed["blockers"])
        self.assertIn("gz_ros2_control_plugin_selected", mixed["blockers"])

    def test_world_and_controllers_are_fixed_rate_fortress_and_mutually_exclusive(self) -> None:
        world = ET.parse(WORLD).getroot().find("./world")
        self.assertIsNotNone(world)
        self.assertEqual(world.findtext("./physics/max_step_size"), "0.0005")
        self.assertEqual(world.findtext("./physics/real_time_update_rate"), "2000")
        plugins = {node.attrib["filename"] for node in world.findall("./plugin")}
        self.assertIn("ignition-gazebo-contact-system", plugins)
        self.assertIn("ignition-gazebo-forcetorque-system", plugins)
        self.assertTrue(all(not name.startswith("gz-sim-") for name in plugins))
        camera_topics = {node.text for node in world.findall(".//sensor[@type='camera']/topic")}
        self.assertEqual(camera_topics, {f"/ur10e/gazebo_v2/camera/{name}" for name in evidence.REQUIRED_VIEWS})

        velocity = yaml.safe_load((CONFIG / "gazebo_v2_velocity_controllers.yaml").read_text(encoding="utf-8"))
        effort = yaml.safe_load(
            (CONFIG / "gazebo_v2_effort_surrogate_controllers.yaml").read_text(encoding="utf-8")
        )
        for payload in (velocity, effort):
            self.assertEqual(payload["controller_manager"]["ros__parameters"]["update_rate"], 500)
        self.assertIn("gazebo_v2_velocity_controller", velocity)
        self.assertNotIn("gazebo_v2_effort_surrogate_controller", velocity)
        self.assertIn("gazebo_v2_effort_surrogate_controller", effort)
        self.assertNotIn("gazebo_v2_velocity_controller", effort)

    def test_contract_schema_and_launch_preserve_claim_boundary(self) -> None:
        contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        schema = json.loads(TICK_SCHEMA.read_text(encoding="utf-8"))
        source = LAUNCH.read_text(encoding="utf-8")
        self.assertEqual(contract["engine"]["required_major"], 6)
        self.assertEqual(contract["rates_hz"]["controller"], 500)
        self.assertFalse(contract["sensor_attachment"]["virtual_surface_force_allowed"])
        self.assertFalse(contract["geometry"]["current_bench_cad_hash_bound"])
        self.assertFalse(contract["geometry"]["mass_cog_inertia_calibrated"])
        self.assertFalse(contract["runtime_implementation"]["gazebo_runtime_pass"])
        self.assertEqual(
            contract["frame_lineage"]["active_tcp_offset_tool0_m"],
            list(lane.ACTIVE_TCP_OFFSET_TOOL0_M),
        )
        self.assertFalse(contract["authorization"]["live_motion_authorized"])
        self.assertEqual(schema["properties"]["schema"]["const"], lane.TICK_SCHEMA)
        self.assertIn('["ign", "gazebo"', source)
        self.assertNotIn('["gz", "sim"', source)
        self.assertIn("selected.controller_name", source)
        self.assertNotIn("/home/andy/ur10e_ros2_ws", source)
        self.assertNotIn("/opt/ros/humble", source)
        self.assertIn('get_package_share_directory("ur10e_bringup")', source)
        self.assertIn('get_package_share_directory("ur_description")', source)

    def test_tick_semantics_fail_closed(self) -> None:
        row = _tick("run-a", 0)
        self.assertEqual(lane.validate_tick_record(row), [])
        rejected = _tick("run-a", 0)
        rejected["candidate"]["source"] = "virtual_surface_force"  # type: ignore[index]
        rejected["candidate"]["valid"] = False  # type: ignore[index]
        rejected["decision"] = {"action": "hold", "reason": "invalid", "accepted": False}
        rejected["command"]["values"] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.001]  # type: ignore[index]
        issues = lane.validate_tick_record(rejected)
        self.assertIn("candidate.virtual_surface_force_forbidden", issues)
        self.assertIn("rejected_command_not_exact_zero", issues)

        not_applied = _tick("run-a", 0)
        not_applied["command"]["applied"] = False  # type: ignore[index]
        self.assertIn("accepted_command_not_applied", lane.validate_tick_record(not_applied))
        mismatch = _tick("run-a", 0)
        mismatch["candidate"]["values"] = [0.001] * 6  # type: ignore[index]
        self.assertIn("accepted_command_candidate_mismatch", lane.validate_tick_record(mismatch))

        extra = _tick("run-a", 0)
        extra["undeclared"] = True
        schema_issues = evidence.validate_tick_json_schema(extra)
        self.assertIn("$.undeclared:additional_property", schema_issues)

    def test_run_id_and_runtime_lineage_are_fail_closed(self) -> None:
        self.assertEqual(native_capture._validate_run_id("gazebo-v2-shared.001"), "gazebo-v2-shared.001")
        with self.assertRaises(ValueError):
            native_capture._validate_run_id("../escape")
        legacy_declarative = {
            "run_id": "r1",
            "runtime_base_link_pose_present": True,
            "frames": {lane.ACTIVE_TCP_LINK: {"parent": "tool0", "source": "contract"}},
        }
        issues = evidence.validate_tf_lineage(
            legacy_declarative,
            run_id="r1",
            generated_urdf_sha256="a" * 64,
        )
        self.assertIn("tf_lineage:schema_mismatch", issues)
        self.assertIn(f"tf_lineage:missing:{lane.ACTIVE_TCP_LINK}", issues)

    def test_contact_ft_pairing_rejects_reuse_window_frame_and_sign(self) -> None:
        contacts = [
            {
                "sim_time_s": stamp,
                "comparison_frame": "base",
                "comparison_convention": "force_on_eoat_along_reaction_normal",
                "comparison_wrench_on_eoat": [0.0, 0.0, force, 0.0, 0.0, 0.0],
                "reaction_normal": [0.0, 0.0, 1.0],
            }
            for stamp, force in ((0.0, 2.0), (0.002, 3.0))
        ]
        one_ft = [
            {
                "sim_time_s": 0.001,
                "comparison_frame": "base",
                "comparison_convention": "force_on_eoat_along_reaction_normal",
                "comparison_wrench_on_eoat": [0.0, 0.0, 2.4, 0.0, 0.0, 0.0],
                "reaction_normal": [0.0, 0.0, 1.0],
            }
        ]
        reused = evidence.contact_ft_correlation(contacts, one_ft, tick_window=(0.0, 0.004))
        self.assertFalse(reused["pass"])
        self.assertIn("unique_same_frame_pair_count_below_2", reused["issues"])

        wrong = [dict(row) for row in one_ft for _ in range(2)]
        wrong[0].update({"sim_time_s": 0.0, "comparison_frame": "tool0"})
        wrong[1].update(
            {
                "sim_time_s": 0.002,
                "comparison_wrench_on_eoat": [0.0, 0.0, -3.6, 0.0, 0.0, 0.0],
            }
        )
        rejected = evidence.contact_ft_correlation(contacts, wrong, tick_window=(0.0, 0.004))
        self.assertFalse(rejected["pass"])
        self.assertTrue(any("comparison_frame_missing_or_mismatch" in value for value in rejected["issues"]))
        self.assertTrue(any("reaction_normal_sign_mismatch" in value for value in rejected["issues"]))

        outside = evidence.contact_ft_correlation(contacts, one_ft, tick_window=(0.003, 0.004))
        self.assertFalse(outside["pass"])
        self.assertTrue(any("outside_tick_window" in value for value in outside["issues"]))

    def test_native_transport_normalizer_preserves_attached_body_and_sensor_contract(self) -> None:
        raw_contact = json.dumps(
            {
                "header": {"stamp": {"sec": "4", "nsec": 2000000}},
                "contact": [
                    {
                        "collision1": {"name": "step5_contact_surface::surface::collision"},
                        "collision2": {
                            "name": f"ur10e_gazebo_v2::{lane.EOAT_LINK}::{lane.EOAT_CONTACT_COLLISION}"
                        },
                        "wrench": [
                            {
                                "body1Wrench": {
                                    "force": {"x": 0, "y": 0, "z": -2},
                                    "torque": {"x": 0, "y": 0, "z": 0},
                                },
                                "body2Wrench": {
                                    "force": {"x": 0, "y": 0, "z": 2},
                                    "torque": {"x": 0, "y": 0, "z": 0.1},
                                },
                            }
                        ],
                    }
                ],
            }
        )
        contacts, contact_issues = native_capture.normalize_contact_messages(raw_contact, run_id="r1")
        raw_ft = json.dumps(
            {
                "header": {"stamp": {"sec": 4, "nsec": 2000000}},
                "force": {"x": 0, "y": 0, "z": 2.4},
                "torque": {"x": 0, "y": 0, "z": 0.1},
            }
        )
        ft_rows, ft_issues = native_capture.normalize_ft_messages(raw_ft, run_id="r1")
        inventory = native_capture.controller_inventory(
            "joint_state_broadcaster joint_state_broadcaster/JointStateBroadcaster active\n"
            "gazebo_v2_velocity_controller velocity_controllers/JointGroupVelocityController active\n"
            "gazebo_v2_effort_surrogate_controller effort_controllers/JointGroupEffortController inactive\n",
            backend="velocity",
        )
        self.assertEqual(contact_issues, [])
        self.assertEqual(ft_issues, [])
        self.assertEqual(contacts[0]["selected_body_side"], 2)
        self.assertEqual(contacts[0]["native_wrench"], [0.0, 0.0, 2.0, 0.0, 0.0, 0.1])
        self.assertEqual(ft_rows[0]["sensor_joint"], lane.EOAT_FIXED_JOINT)
        self.assertTrue(inventory["pass"])
        self.assertFalse(inventory["simultaneous_backend_active"])

    def test_observer_rejects_structurally_valid_blank_png(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gazebo_v2_blank_") as tmp:
            path = Path(tmp) / "blank.png"
            _write_valid_png(path, blank=True)
            metadata = evidence.camera_png_metadata(path)
        self.assertFalse(metadata["valid"])
        self.assertIn("png_content_near_blank", metadata["blockers"])

    def test_model_and_capture_hash_binding_reject_tampering(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gazebo_v2_binding_") as tmp:
            run_dir = Path(tmp)
            bindings = _model_binding_fixture(run_dir, backend="velocity")
            generated = run_dir / str(bindings["generated_urdf"]["artifact_path"])
            generated.write_text(generated.read_text(encoding="utf-8") + "<!-- tampered -->\n", encoding="utf-8")
            issues, _ = evidence.validate_model_bindings(run_dir, bindings, backend="velocity")
            self.assertIn("model_bindings:generated_urdf:size_mismatch", issues)
            self.assertIn("model_bindings:generated_urdf:sha256_mismatch", issues)

            artifact = run_dir / "tick_trace.jsonl"
            artifact.write_text("{}\n", encoding="utf-8")
            row = {"tick_trace.jsonl": {"size": artifact.stat().st_size, "sha256": _sha(artifact)}}
            artifact.write_text("{\"changed\": true}\n", encoding="utf-8")
            artifact_issues = evidence.validate_capture_artifacts(run_dir, row)
        self.assertIn("capture_artifacts:tick_trace.jsonl:size_mismatch", artifact_issues)
        self.assertIn("capture_artifacts:tick_trace.jsonl:sha256_mismatch", artifact_issues)

    def test_same_run_evidence_and_observer_review_positive_fixture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gazebo_v2_lane_test_") as tmp:
            run_dir = Path(tmp)
            run_id = "gazebo-v2-fixture"
            model_bindings = _model_binding_fixture(run_dir, backend="velocity")
            _write_jsonl(run_dir / "tick_trace.jsonl", [_tick(run_id, index) for index in range(3)])
            contacts = [
                {
                    "run_id": run_id,
                    "sim_time_s": stamp,
                    "source": "gazebo_native_contact",
                    "topic": lane.NATIVE_CONTACT_TOPIC,
                    "contact_collision": f"ur10e_gazebo_v2::{lane.EOAT_LINK}::{lane.EOAT_CONTACT_COLLISION}",
                    "surface_collision": "step5_contact_surface::surface::collision",
                    "native_wrench": [0.0, 0.0, force, 0.0, 0.0, 0.0],
                    "comparison_frame": "base",
                    "comparison_convention": "force_on_eoat_along_reaction_normal",
                    "comparison_wrench_on_eoat": [0.0, 0.0, force, 0.0, 0.0, 0.0],
                    "reaction_normal": [0.0, 0.0, 1.0],
                }
                for stamp, force in ((0.0, 2.0), (0.002, 3.0))
            ]
            ft_rows = [
                {
                    "run_id": run_id,
                    "sim_time_s": stamp,
                    "source": "gazebo_native_ft",
                    "topic": lane.NATIVE_FT_TOPIC,
                    "sensor_joint": lane.EOAT_FIXED_JOINT,
                    "frame": "child",
                    "measure_direction": "child_to_parent",
                    "native_wrench": [0.0, 0.0, force * 1.2, 0.0, 0.0, 0.0],
                    "comparison_frame": "base",
                    "comparison_convention": "force_on_eoat_along_reaction_normal",
                    "comparison_wrench_on_eoat": [0.0, 0.0, force * 1.2, 0.0, 0.0, 0.0],
                    "reaction_normal": [0.0, 0.0, 1.0],
                }
                for stamp, force in ((0.0, 2.0), (0.002, 3.0))
            ]
            _write_jsonl(run_dir / "native_contact.jsonl", contacts)
            _write_jsonl(run_dir / "native_ft.jsonl", ft_rows)
            required_entities = [
                "base_link",
                "base",
                "base_link_inertia",
                "shoulder_link",
                "upper_arm_link",
                "forearm_link",
                "wrist_1_link",
                "wrist_2_link",
                "wrist_3_link",
                "flange",
                "tool0",
                lane.EOAT_LINK,
                lane.ACTIVE_TCP_LINK,
            ]
            (run_dir / "tf_lineage.json").write_text(
                json.dumps(
                    {
                        "schema": "ur10e_gazebo_v2_runtime_lineage_v2",
                        "run_id": run_id,
                        "generated_urdf_sha256": model_bindings["generated_urdf"]["sha256"],
                        "pose_info_parse_issues": [],
                        "required_runtime_entities": required_entities,
                        "runtime_entities": {
                            name: {
                                "present": True,
                                "matches": [f"ur10e_gazebo_v2::{name}"],
                                "source": "gazebo_pose_info",
                            }
                            for name in required_entities
                        },
                        "all_required_runtime_entities_present": True,
                        "static_parentage_source": "manifest_bound_generated_urdf_not_runtime_pose_inference",
                    }
                ),
                encoding="utf-8",
            )
            for name in ("native_contact.raw.jsonl", "native_ft.raw.jsonl", "pose_info.raw.jsonl"):
                (run_dir / name).write_text("{}\n", encoding="utf-8")
            views: dict[str, dict[str, object]] = {}
            for name in evidence.REQUIRED_VIEWS:
                path = run_dir / f"{name}.png"
                _write_valid_png(path)
                views[name] = {
                    "path": path.name,
                    "topic": f"/ur10e/gazebo_v2/camera/{name}",
                    "sim_time_s": 0.002,
                    "sha256": _sha(path),
                }
            (run_dir / "camera_manifest.json").write_text(
                json.dumps({"run_id": run_id, "views": views}), encoding="utf-8"
            )
            (run_dir / "observer_manual_checks.json").write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "reviewer": "fixture-independent-observer",
                        "reviewed_at": "2026-07-11T00:00:00Z",
                        "review_lane": "independent_observer",
                        "camera_manifest_sha256": _sha(run_dir / "camera_manifest.json"),
                        "view_sha256": {name: row["sha256"] for name, row in views.items()},
                        "checks": {name: True for name in evidence.REQUIRED_MANUAL_CHECKS},
                    }
                ),
                encoding="utf-8",
            )
            artifact_names = [
                "native_contact.raw.jsonl",
                "native_ft.raw.jsonl",
                "pose_info.raw.jsonl",
                "native_contact.jsonl",
                "native_ft.jsonl",
                "tf_lineage.json",
                "tick_trace.jsonl",
            ] + [
                str(path.relative_to(run_dir)) for path in sorted((run_dir / "model_bindings").iterdir())
            ]
            manifest = {
                "schema": "ur10e_gazebo_v2_capture_manifest_v1",
                "run_id": run_id,
                "backend": "velocity",
                "backend_fidelity": lane.backend_spec("velocity").fidelity,
                "engine_family": "Gazebo Fortress",
                "engine_major": 6,
                "world_name": "ur10e_gazebo_v2_fortress",
                "world_pose_topic_present": True,
                "ros2_control_plugin": lane.IGN_ROS2_CONTROL_PLUGIN,
                "abi_preflight": {
                    "pass": True,
                    "version": [6, 18, 0],
                    "plugin_paths": ["/fixture/libign_ros2_control-system.so"],
                    "spawn_package_prefix": "/fixture/ros_gz_sim",
                },
                "controller_inventory": {
                    "pass": True,
                    "selected": lane.backend_spec("velocity").controller_name,
                    "selected_active": True,
                    "joint_state_broadcaster_active": True,
                    "other_active": False,
                },
                "simultaneous_backend_active": False,
                "normalization_issues": {"contact": [], "ft": []},
                "model_bindings": model_bindings,
                "artifacts": _capture_artifacts(run_dir, artifact_names),
                "runtime_blockers": sorted(evidence.RUNTIME_IMPLEMENTATION_BLOCKERS),
                "gazebo_runtime_pass": False,
            }
            (run_dir / "capture_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            evidence_path, observer_path = evidence.build_evidence(run_dir)
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            observer = json.loads(observer_path.read_text(encoding="utf-8"))

        self.assertTrue(payload["native_contact_ft_same_run_pass"], payload["blockers"])
        self.assertTrue(payload["correlation"]["pass"])
        self.assertTrue(observer["viewer_level_pass"], observer["blockers"])
        self.assertFalse(payload["gazebo_runtime_pass"])
        self.assertEqual(set(payload["runtime_blockers"]), evidence.RUNTIME_IMPLEMENTATION_BLOCKERS)
        self.assertFalse(payload["claim_boundary"]["live_acceptance"])
        self.assertFalse(payload["claim_boundary"]["reproduction_complete"])


if __name__ == "__main__":
    unittest.main()
