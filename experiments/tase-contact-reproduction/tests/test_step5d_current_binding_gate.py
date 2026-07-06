#!/usr/bin/env python3
"""Tests for the Step5d current binding gate."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_current_binding as gate  # noqa: E402


PROGRAM = "step5d_strict_rnn_liveprep_v99"
TARGET_DIR = "/programs/andyl/kunwei/step5"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_binding_fixture(
    root: Path,
    *,
    stage_active: bool = True,
    live_motion_authorized: bool = True,
    strict_rnn_p0_required: bool = False,
    strict_rnn_p0_passed: bool = False,
) -> None:
    local_stem = root / "programs" / "step5" / PROGRAM
    local_stem.parent.mkdir(parents=True)
    contents = {
        ".script": b"def codex_step5d_strict_rnn_liveprep_v99():\nend\n",
        ".txt": b"step5d fixture\n",
        ".urp": b"fixture urp bytes\n",
    }
    sha = {}
    for ext, data in contents.items():
        local_stem.with_suffix(ext).write_bytes(data)
        sha[ext] = _sha256(data)

    manifest_rel = f"runs/controller_readback_{PROGRAM}_fixture/manifest.json"
    manifest_path = root / manifest_rel
    manifest_path.parent.mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    current = {
        "current_stage_id": PROGRAM,
        "program": PROGRAM,
        "stage_table_path": "config/step5_stage_table.json",
        "controller_target": f"{TARGET_DIR}/{PROGRAM}.urp",
        "controller_script": f"{TARGET_DIR}/{PROGRAM}.script",
        "local_triplet": f"programs/step5/{PROGRAM}",
        "status": f"{PROGRAM}_controller_readback_verified_pending_live_bridge_run",
        "delivery_manifest": manifest_rel,
        "sha256": sha,
        "bridge_trigger": {
            "bridge_has_started": False,
            "live_motion_authorized": live_motion_authorized,
            "zero_ftsensor_authorized": False,
            "required_before_live": [],
        },
    }
    manifest = {
        "status": "controller read-back verified",
        "target_dir": TARGET_DIR,
        "validation": {
            "program": PROGRAM,
            "target_dir": TARGET_DIR,
            "installation_relative_path": "../../../default",
            "script_node_path": f"{TARGET_DIR}/{PROGRAM}.script",
            "script_sha256": sha[".script"],
            "txt_sha256": sha[".txt"],
            "urp_sha256": sha[".urp"],
        },
        "sha256": {
            "local": sha,
            "controller": sha,
            "readback": sha,
        },
    }
    stage_table = {
        "stages": [
            {
                "id": PROGRAM,
                "active": stage_active,
                "blocked": False,
                "acceptance": {
                    "strict_rnn_no_contact_p0_required_before_live": strict_rnn_p0_required,
                    "strict_rnn_no_contact_p0_passed": strict_rnn_p0_passed,
                },
            }
        ]
    }
    (root / "config" / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
    (root / "config" / "step5_stage_table.json").write_text(json.dumps(stage_table), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


class Step5dCurrentBindingGateTest(unittest.TestCase):
    def test_current_stage_readback_and_runtime_binding_pass(self) -> None:
        result = gate.verify_binding(ROOT)

        self.assertTrue(result["ok"])
        self.assertTrue(
            result["program"].startswith(("step5d_strict_rnn_liveprep_", "step5d_strict_rnn_ablation_"))
        )
        self.assertIn("controller_readback_step5d_strict_rnn_", result["manifest"])
        self.assertEqual(result["runtime_interface"]["program"], result["program"])
        self.assertEqual(len(result["local_triplet"]), 3)
        self.assertEqual(result["stage_table"]["id"], result["program"])
        self.assertTrue(result["stage_table"]["active"])
        self.assertFalse(result["stage_table"]["blocked"])

    def test_local_triplet_sha_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(root)
            result = gate.verify_binding(root, PROGRAM)
            self.assertTrue(result["ok"])

            (root / "programs" / "step5" / f"{PROGRAM}.script").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "local triplet sha256"):
                gate.verify_binding(root, PROGRAM)

    def test_inactive_stage_table_row_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(root, stage_active=False)
            with self.assertRaisesRegex(RuntimeError, "stage table row .* is not active"):
                gate.verify_binding(root, PROGRAM)

    def test_live_bridge_authorization_requires_current_stage_live_motion_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(root, live_motion_authorized=False)

            with self.assertRaisesRegex(RuntimeError, "live motion is not authorized"):
                gate.verify_live_bridge_authorization(root, PROGRAM, "speedl_cartesian_oracle")

    def test_speedj_rnn_live_authorization_requires_no_contact_p0_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(
                root,
                live_motion_authorized=True,
                strict_rnn_p0_required=True,
                strict_rnn_p0_passed=False,
            )

            with self.assertRaisesRegex(RuntimeError, "no-contact P0"):
                gate.verify_live_bridge_authorization(root, PROGRAM, "speedj_rnn_live")

    def test_speedj_rnn_live_authorization_passes_after_no_contact_p0(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(
                root,
                live_motion_authorized=True,
                strict_rnn_p0_required=True,
                strict_rnn_p0_passed=True,
            )

            result = gate.verify_live_bridge_authorization(root, PROGRAM, "speedj_rnn_live")

        self.assertTrue(result["ok"])
        self.assertEqual(result["stage25_control_mode"], "speedj_rnn_live")
        self.assertTrue(result["live_motion_authorized"])
        self.assertTrue(result["strict_rnn_no_contact_p0_passed"])

    def test_speedl_live_authorization_does_not_require_strict_rnn_p0(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(
                root,
                live_motion_authorized=True,
                strict_rnn_p0_required=True,
                strict_rnn_p0_passed=False,
            )

            result = gate.verify_live_bridge_authorization(root, PROGRAM, "speedl_cartesian_oracle")

        self.assertTrue(result["ok"])
        self.assertEqual(result["stage25_control_mode"], "speedl_cartesian_oracle")


if __name__ == "__main__":
    unittest.main()
