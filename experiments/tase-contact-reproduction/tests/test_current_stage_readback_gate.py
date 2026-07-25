#!/usr/bin/env python3
"""Offline tests for current-stage controller read-back verification."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_current_stage_readback as gate  # noqa: E402


PROGRAM = "step5d_strict_rnn_liveprep_v123"
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
            "v123_controller_readback_verified": True,
            "v123_controller_readback_manifest": manifest_rel,
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


def write_case_with_manifest_status(root: Path, status: str) -> None:
    write_case(root)
    manifest_path = root / f"runs/controller_readback_{PROGRAM}_fixture/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = status
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


class CurrentStageReadbackGateTest(unittest.TestCase):
    def test_immutable_release_pointer_resolves_tp_program_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = "step5d_strict_rnn_autotune_v3"
            tp_program = f"{release}_r123"
            manifest = {
                "schema": "step5d.autotune-v3/release-manifest-v3",
                "identity": {
                    "release_stage_id": release,
                    "program_id": tp_program,
                },
            }
            encoded = json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            manifest_path = root / "config/step5d/releases/fixture/manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_bytes(encoded)
            pointer = {
                "schema": "step5d.autotune-v3/current-release-pointer-v1",
                "manifest_path": str(manifest_path.relative_to(root)),
                "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
            }
            (root / "config/step5d/current.json").write_text(
                json.dumps(pointer),
                encoding="utf-8",
            )

            self.assertEqual(
                gate.selected_tp_program(root, {}, release),
                tp_program,
            )

    def test_immutable_release_pointer_rejects_manifest_sha_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "config/step5d/releases/fixture/manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "identity": {
                            "release_stage_id": "step5d_strict_rnn_autotune_v3",
                            "program_id": "step5d_strict_rnn_autotune_v3_r123",
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "config/step5d/current.json").write_text(
                json.dumps(
                    {
                        "schema": "step5d.autotune-v3/current-release-pointer-v1",
                        "manifest_path": str(manifest_path.relative_to(root)),
                        "manifest_sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "SHA-256 differs"):
                gate.selected_tp_program(
                    root,
                    {},
                    "step5d_strict_rnn_autotune_v3",
                )

    def test_per_campaign_readback_resolves_durable_delivery_basis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_case(root)
            current = json.loads(
                (root / "config/current_stage.json").read_text(encoding="utf-8")
            )
            source = root / current["evidence"]["v123_controller_readback_manifest"]
            receipt = json.loads(source.read_text(encoding="utf-8"))
            prior = root / "config/step5d/delivery-bases/prior.json"
            prior.parent.mkdir(parents=True)
            receipt.update(
                {
                    "delivery_mode": "full_upload_readback",
                    "readback_source": "fresh_controller_get",
                    "fresh_controller_sha_verified": True,
                    "fresh_controller_checked_at": "2026-07-02T17:28:39+08:00",
                }
            )
            prior.write_text(json.dumps(receipt), encoding="utf-8")
            source.write_text(
                json.dumps(
                    {
                        "schema": "step5d.autotune.controller-readback/v3",
                        "status": "controller read-back verified",
                        "verified": True,
                        "program": PROGRAM,
                        "controller_target": f"{TARGET_DIR}/{PROGRAM}.urp",
                        "triplet_sha256": SHA,
                        "fresh_get_evidence": "per_campaign_delivery_observation",
                    }
                ),
                encoding="utf-8",
            )
            release = SimpleNamespace(
                program_id=PROGRAM,
                controller_target=f"{TARGET_DIR}/{PROGRAM}.urp",
                artifact_sha256=SHA,
            )
            basis = {
                "prior_full_readback_receipt": {
                    "path": str(prior.relative_to(root)),
                    "sha256": hashlib.sha256(prior.read_bytes()).hexdigest(),
                }
            }
            with mock.patch.object(
                gate,
                "load_current_release_for_compatible_readback",
                return_value=release,
            ), mock.patch.object(
                gate,
                "load_delivery_basis",
                return_value=(root / "config/step5d/delivery-bases/basis.json", basis),
            ):
                result = gate.verify(root, PROGRAM)

            self.assertTrue(result["ok"])
            self.assertEqual(result["delivery_mode"], "full_upload_readback")

    def test_per_campaign_readback_rejects_stale_prior_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_case(root)
            current = json.loads(
                (root / "config/current_stage.json").read_text(encoding="utf-8")
            )
            source = root / current["evidence"]["v123_controller_readback_manifest"]
            prior = root / "config/step5d/delivery-bases/prior.json"
            prior.parent.mkdir(parents=True)
            receipt = json.loads(source.read_text(encoding="utf-8"))
            receipt.update(
                {
                    "delivery_mode": "full_upload_readback",
                    "readback_source": "retained_snapshot",
                    "fresh_controller_sha_verified": True,
                    "fresh_controller_checked_at": "2026-07-02T17:28:39+08:00",
                }
            )
            prior.write_text(json.dumps(receipt), encoding="utf-8")
            source.write_text(
                json.dumps(
                    {
                        "schema": "step5d.autotune.controller-readback/v3",
                        "status": "controller read-back verified",
                        "verified": True,
                        "program": PROGRAM,
                        "controller_target": f"{TARGET_DIR}/{PROGRAM}.urp",
                        "triplet_sha256": SHA,
                        "fresh_get_evidence": "per_campaign_delivery_observation",
                    }
                ),
                encoding="utf-8",
            )
            basis = {
                "prior_full_readback_receipt": {
                    "path": str(prior.relative_to(root)),
                    "sha256": hashlib.sha256(prior.read_bytes()).hexdigest(),
                }
            }
            release = SimpleNamespace(
                program_id=PROGRAM,
                controller_target=f"{TARGET_DIR}/{PROGRAM}.urp",
                artifact_sha256=SHA,
            )
            with mock.patch.object(
                gate,
                "load_current_release_for_compatible_readback",
                return_value=release,
            ), mock.patch.object(
                gate,
                "load_delivery_basis",
                return_value=(root / "config/step5d/delivery-bases/basis.json", basis),
            ):
                with self.assertRaisesRegex(RuntimeError, "fresh controller GET"):
                    gate.verify(root, PROGRAM)

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

    def test_local_package_verified_manifest_cannot_be_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_case_with_manifest_status(root, "local package verified")
            with self.assertRaisesRegex(RuntimeError, "manifest status is local package verified"):
                gate.verify(root, PROGRAM)

    def test_dry_run_manifest_cannot_be_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_case_with_manifest_status(root, "dry-run")
            with self.assertRaisesRegex(RuntimeError, "manifest status is dry-run"):
                gate.verify(root, PROGRAM)


if __name__ == "__main__":
    unittest.main()
