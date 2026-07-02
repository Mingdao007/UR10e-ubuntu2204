#!/usr/bin/env python3
"""Offline tests for TP package read-back reuse safety."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import upload_ur_tp_package as upload  # noqa: E402


class UploadUrTpPackageReuseTest(unittest.TestCase):
    def test_upload_validator_checks_installation_relative_path(self) -> None:
        program = "step5d_strict_rnn_liveprep_v19"
        target_dir = "/programs/andyl/kunwei/step5"
        files = {
            ext: ROOT / "programs" / "step5" / f"{program}{ext}"
            for ext in upload.EXTENSIONS
        }

        result = upload.validate_package(
            files,
            program,
            target_dir,
            require_exact_cached_script=True,
        )

        self.assertEqual(result["installation_relative_path"], "../../../default")

    def _write_triplet(self, root: Path, program: str, payload_prefix: str) -> dict[str, Path]:
        files = {ext: root / f"{program}{ext}" for ext in upload.EXTENSIONS}
        for ext, path in files.items():
            path.write_bytes(f"{payload_prefix}:{ext}\n".encode("utf-8"))
        return files

    def _write_manifest(
        self,
        readback_root: Path,
        *,
        program: str,
        controller: str,
        target_dir: str,
        files: dict[str, Path],
    ) -> Path:
        readback_dir = readback_root / f"controller_readback_{program}_20260702_120000"
        readback_dir.mkdir(parents=True)
        for ext, source in files.items():
            (readback_dir / source.name).write_bytes(source.read_bytes())
        local_sha = upload.package_sha(files)
        manifest = {
            "status": "controller read-back verified",
            "controller": controller,
            "target_dir": target_dir,
            "validation": {
                "program": program,
                "target_dir": target_dir,
                "script_sha256": local_sha[".script"],
                "txt_sha256": local_sha[".txt"],
                "urp_sha256": local_sha[".urp"],
            },
            "sha256": {
                "local": local_sha,
                "controller": local_sha,
                "readback": local_sha,
            },
        }
        manifest_path = readback_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path

    def test_reuses_prior_readback_only_after_fresh_controller_sha_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            program = "demo_program"
            target_dir = "/programs/andyl/kunwei/demo"
            local_dir = tmp_path / "local"
            local_dir.mkdir()
            files = self._write_triplet(local_dir, program, "same")
            local_sha = upload.package_sha(files)
            readback_root = tmp_path / "runs"
            prior_manifest = self._write_manifest(
                readback_root,
                program=program,
                controller=upload.DEFAULT_CONTROLLER,
                target_dir=target_dir,
                files=files,
            )
            readback_dir = readback_root / f"controller_readback_{program}_20260702_120100"
            remote_sha = {
                upload.controller_path(target_dir, files[ext].name): local_sha[ext]
                for ext in upload.EXTENSIONS
            }

            with patch.object(upload, "remote_sha256", return_value=remote_sha) as remote:
                result = upload.reuse_readback_if_remote_sha_matches(
                    files,
                    program,
                    upload.DEFAULT_CONTROLLER,
                    target_dir,
                    readback_root,
                    readback_dir,
                    helper=Path("/unused/controller-helper.py"),
                    local_sha=local_sha,
                )

            self.assertIsNotNone(result)
            assert result is not None
            shas, reused_from = result
            self.assertEqual(reused_from, prior_manifest)
            self.assertEqual(shas["local"], local_sha)
            self.assertEqual(shas["controller"], local_sha)
            self.assertEqual(shas["readback"], local_sha)
            self.assertTrue((readback_dir / f"{program}.urp").is_file())
            remote.assert_called_once()

    def test_remote_sha_mismatch_falls_back_to_full_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            program = "demo_program"
            target_dir = "/programs/andyl/kunwei/demo"
            local_dir = tmp_path / "local"
            local_dir.mkdir()
            files = self._write_triplet(local_dir, program, "same")
            local_sha = upload.package_sha(files)
            readback_root = tmp_path / "runs"
            self._write_manifest(
                readback_root,
                program=program,
                controller=upload.DEFAULT_CONTROLLER,
                target_dir=target_dir,
                files=files,
            )
            remote_sha = {
                upload.controller_path(target_dir, files[ext].name): local_sha[ext]
                for ext in upload.EXTENSIONS
            }
            remote_sha[upload.controller_path(target_dir, files[".urp"].name)] = "0" * 64

            with patch.object(upload, "remote_sha256", return_value=remote_sha):
                result = upload.reuse_readback_if_remote_sha_matches(
                    files,
                    program,
                    upload.DEFAULT_CONTROLLER,
                    target_dir,
                    readback_root,
                    readback_root / f"controller_readback_{program}_20260702_120100",
                    helper=Path("/unused/controller-helper.py"),
                    local_sha=local_sha,
                )

            self.assertIsNone(result)

    def test_reuse_manifest_records_fresh_controller_sha_basis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            readback_dir = Path(tmp) / "readback"
            readback_dir.mkdir()
            basis = Path(tmp) / "prior" / "manifest.json"
            shas = {
                "local": {".script": "1" * 64, ".txt": "2" * 64, ".urp": "3" * 64},
                "controller": {".script": "1" * 64, ".txt": "2" * 64, ".urp": "3" * 64},
                "readback": {".script": "1" * 64, ".txt": "2" * 64, ".urp": "3" * 64},
            }

            upload.write_manifest(
                readback_dir,
                controller=upload.DEFAULT_CONTROLLER,
                target_dir="/programs/andyl/kunwei/demo",
                local_dir=Path("programs/demo"),
                validation={"program": "demo_program"},
                shas=shas,
                dry_run=False,
                delivery_mode="content_addressed_reuse",
                reused_from_manifest=basis,
                fresh_controller_sha_verified=True,
                readback_source="prior_full_readback",
            )

            manifest = json.loads((readback_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["delivery_mode"], "content_addressed_reuse")
            self.assertEqual(manifest["skip_basis_manifest"], str(basis))
            self.assertTrue(manifest["fresh_controller_sha_verified"])
            self.assertEqual(manifest["readback_source"], "prior_full_readback")
            self.assertIn("fresh_controller_checked_at", manifest)


if __name__ == "__main__":
    unittest.main()
