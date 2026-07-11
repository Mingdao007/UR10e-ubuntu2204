from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = EXPERIMENT_ROOT.parents[1]
TOOLS = EXPERIMENT_ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import build_ursim_preparation_manifest as builder  # noqa: E402
import verify_ursim_preparation_manifest as verifier  # noqa: E402


class URSimPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec_path = EXPERIMENT_ROOT / "config" / "ursim_lane_spec_v1.json"
        self.spec = json.loads(self.spec_path.read_text(encoding="utf-8"))

    def test_build_is_deterministic_and_both_lanes_remain_unavailable(self) -> None:
        first = builder.build_manifest(self.spec_path, REPOSITORY_ROOT)
        second = builder.build_manifest(self.spec_path, REPOSITORY_ROOT)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "blocked")
        self.assertEqual(first["availability"], "unavailable")
        self.assertFalse(first["protocol_execution_performed"])
        self.assertEqual(
            first["claims"],
            {
                "p0_ursim_protocol_pass": False,
                "direct_torque_ursim_software_pass": False,
            },
        )
        self.assertEqual(
            tuple(lane["id"] for lane in first["lanes"]), builder.LANE_IDS
        )
        self.assertEqual(
            tuple(lane["polyscope_version"]["value"] for lane in first["lanes"]),
            ("5.11.9.1010452", "5.23.0"),
        )
        for lane in first["lanes"]:
            self.assertEqual(lane["status"], "blocked")
            self.assertEqual(lane["runtime_status"], "unavailable")
            self.assertFalse(lane["protocol_pass"])
            self.assertIsNone(lane["image"]["digest"])
            self.assertIsNone(lane["image"]["reference"])
            self.assertTrue(all(lane["static_contract"].values()))
            self.assertEqual(
                set(lane["blockers"]),
                {
                    "container_service_inactive",
                    "digest_bound_ursim_image_absent",
                    "ursim_runtime_evidence_absent",
                },
            )

    def test_p0_lane_reuses_package_runtime_and_layout524_contracts(self) -> None:
        manifest = builder.build_manifest(self.spec_path, REPOSITORY_ROOT)
        lane = manifest["lanes"][0]
        self.assertTrue(lane["static_contract"]["package_triplet_validator_reused"])
        self.assertTrue(lane["static_contract"]["runtime_contract_reused"])
        self.assertTrue(lane["static_contract"]["layout524_joint_only"])
        self.assertTrue(
            lane["static_contract"]["layout524_payload_registers_declared"]
        )
        self.assertTrue(lane["static_contract"]["heartbeat_stale_guard_static"])
        self.assertTrue(
            lane["static_contract"]["deadline_overrun_zero_hold_static"]
        )
        self.assertTrue(lane["static_contract"]["bounded_speedj_tick_static"])
        self.assertTrue(lane["static_contract"]["speedj_safe_exit_static"])
        self.assertIn("urscript_parser_load", lane["required_runtime_checks"])
        self.assertIn("rtde_register_roundtrip", lane["required_runtime_checks"])
        self.assertIn("speedj_safe_exit", lane["required_runtime_checks"])

    def test_523_lane_reuses_vic_layout_version_and_packet_oracles(self) -> None:
        manifest = builder.build_manifest(self.spec_path, REPOSITORY_ROOT)
        lane = manifest["lanes"][1]
        self.assertTrue(
            lane["static_contract"]["polyscope_version_parser_static"]
        )
        self.assertTrue(lane["static_contract"]["vic_layout_validator_reused"])
        self.assertTrue(lane["static_contract"]["sequence_oracle_static"])
        self.assertTrue(
            lane["static_contract"]["heartbeat_sequence_commit_static"]
        )
        self.assertTrue(lane["static_contract"]["zero_damping_startup_static"])
        self.assertTrue(lane["static_contract"]["zero_damping_safe_exit_static"])
        self.assertTrue(
            lane["static_contract"]["direct_torque_template_uninvoked"]
        )
        self.assertIn(
            "direct_torque_urscript_parser_load", lane["required_runtime_checks"]
        )
        self.assertIn("zero_damping_safe_exit", lane["required_runtime_checks"])

    def test_verifier_accepts_only_exact_deterministic_blocked_manifest(self) -> None:
        manifest = builder.build_manifest(self.spec_path, REPOSITORY_ROOT)
        result = verifier.verify_manifest(
            manifest, self.spec_path, REPOSITORY_ROOT
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["availability"], "unavailable")

        promoted = deepcopy(manifest)
        promoted["claims"]["p0_ursim_protocol_pass"] = True
        promoted["lanes"][0]["protocol_pass"] = True
        promoted["lanes"][0]["status"] = "passed"
        with self.assertRaisesRegex(ValueError, "claims must remain false"):
            verifier.verify_manifest(promoted, self.spec_path, REPOSITORY_ROOT)

        invented_runtime = deepcopy(manifest)
        invented_runtime["protocol_execution_performed"] = True
        with self.assertRaisesRegex(ValueError, "cannot claim protocol execution"):
            verifier.verify_manifest(
                invented_runtime, self.spec_path, REPOSITORY_ROOT
            )

    def test_missing_image_cannot_be_replaced_by_an_unverified_digest(self) -> None:
        mutated = deepcopy(self.spec)
        mutated["lanes"][0]["image"] = {
            "digest_required": True,
            "digest": "sha256:" + "a" * 64,
            "reference": "unverified/ursim@sha256:" + "a" * 64,
            "availability": "claimed",
        }
        with self.assertRaisesRegex(ValueError, "records the absent image"):
            builder.validate_spec(mutated)

    def test_binding_hash_and_path_drift_fail_closed(self) -> None:
        unsafe = deepcopy(self.spec)
        unsafe["lanes"][0]["bindings"][0]["path"] = "../escape.script"
        with self.assertRaisesRegex(ValueError, "unsafe binding path"):
            builder.validate_spec(unsafe)

        drifted = deepcopy(self.spec)
        drifted["lanes"][0]["bindings"][0]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory(dir=EXPERIMENT_ROOT) as directory:
            candidate = Path(directory) / "drifted-spec.json"
            candidate.write_text(json.dumps(drifted), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "binding hash drifted"):
                builder.build_manifest(candidate, REPOSITORY_ROOT)

    def test_schema_itself_forbids_protocol_promotion(self) -> None:
        schema = json.loads(
            (
                EXPERIMENT_ROOT
                / "config"
                / "schemas"
                / "ursim_preparation_manifest_v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(
            schema["properties"]["protocol_execution_performed"]["const"]
        )
        self.assertFalse(
            schema["properties"]["claims"]["properties"]
            ["p0_ursim_protocol_pass"]["const"]
        )
        self.assertFalse(
            schema["properties"]["claims"]["properties"]
            ["direct_torque_ursim_software_pass"]["const"]
        )
        lane = schema["$defs"]["lane"]["properties"]
        self.assertFalse(lane["protocol_pass"]["const"])
        self.assertEqual(lane["runtime_status"]["const"], "unavailable")
        self.assertEqual(lane["image"]["properties"]["digest"]["type"], "null")

    def test_tools_have_no_container_or_live_mutation_surface(self) -> None:
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                TOOLS / "build_ursim_preparation_manifest.py",
                TOOLS / "verify_ursim_preparation_manifest.py",
            )
        )
        for forbidden in (
            "import subprocess",
            "from subprocess",
            "import socket",
            "docker start",
            "systemctl start",
            "usermod",
            "sendall(",
        ):
            self.assertNotIn(forbidden, sources)


if __name__ == "__main__":
    unittest.main()
