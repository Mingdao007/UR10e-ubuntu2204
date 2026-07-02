#!/usr/bin/env python3
"""Offline tests for current-stage controller read-back verification."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_current_stage_readback as gate  # noqa: E402


PROGRAM = "step5d_strict_rnn_liveprep_v19"
TARGET_DIR = "/programs/andyl/kunwei/step5"
SHA = {
    ".script": "1" * 64,
    ".txt": "2" * 64,
    ".urp": "3" * 64,
}


def write_case(root: Path, *, delivery_mode: str | None = None, include_fresh: bool = True) -> None:
    manifest_rel = f"runs/controller_readback_{PROGRAM}_fixture/manifest.json"
    (root / "config").mkdir(parents=True)
    manifest_path = root / manifest_rel
    manifest_path.parent.mkdir(parents=True)
    current = {
        "current_stage_id": PROGRAM,
        "program": PROGRAM,
        "controller_target": f"{TARGET_DIR}/{PROGRAM}.urp",
        "controller_script": f"{TARGET_DIR}/{PROGRAM}.script",
        "status": f"{PROGRAM}_controller_readback_verified_pending_live_bridge_run",
        "evidence": {
            "v19_controller_readback_verified": True,
            "v19_controller_readback_manifest": manifest_rel,
            "sha256": SHA,
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
            "script_sha256": SHA[".script"],
            "txt_sha256": SHA[".txt"],
            "urp_sha256": SHA[".urp"],
        },
        "sha256": {
            "local": SHA,
            "controller": SHA,
            "readback": SHA,
        },
    }
    if delivery_mode is not None:
        manifest["delivery_mode"] = delivery_mode
    if delivery_mode == "content_addressed_reuse":
        manifest["skip_basis_manifest"] = "runs/controller_readback_prior/manifest.json"
        manifest["readback_source"] = "prior_full_readback"
        if include_fresh:
            manifest["fresh_controller_sha_verified"] = True
            manifest["fresh_controller_checked_at"] = "2026-07-02T17:28:39+08:00"
    (root / "config" / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


class CurrentStageReadbackGateTest(unittest.TestCase):
    def test_legacy_manifest_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_case(root)
            result = gate.verify(root, PROGRAM)
            self.assertTrue(result["ok"])
            self.assertEqual(result["delivery_mode"], "legacy_full_upload_readback")

    def test_content_addressed_reuse_requires_fresh_controller_sha_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_case(root, delivery_mode="content_addressed_reuse", include_fresh=False)
            with self.assertRaisesRegex(RuntimeError, "fresh_controller_sha_verified"):
                gate.verify(root, PROGRAM)

    def test_content_addressed_reuse_passes_with_fresh_controller_sha_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_case(root, delivery_mode="content_addressed_reuse", include_fresh=True)
            result = gate.verify(root, PROGRAM)
            self.assertEqual(result["delivery_mode"], "content_addressed_reuse")


if __name__ == "__main__":
    unittest.main()
