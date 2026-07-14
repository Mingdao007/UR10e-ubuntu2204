#!/usr/bin/env python3
"""Offline tests for TP package read-back reuse safety."""

from __future__ import annotations

import json
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import upload_ur_tp_package as upload  # noqa: E402
import build_step5d_liveprep as liveprep  # noqa: E402


def manifest_from_upload_output(output: str) -> dict:
    marker = '{\n  "status":'
    start = output.rfind(marker)
    if start < 0:
        raise AssertionError(f"upload manifest JSON not found in output:\n{output}")
    return json.loads(output[start:])


class UploadUrTpPackageReuseTest(unittest.TestCase):
    def test_v30_inactive_candidate_allows_manifest_bound_dry_run_without_promotion(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            result = upload.main(
                [
                    "step5d_strict_rnn_ablation_v30",
                    "--dry-run",
                    "--local-dir",
                    str(ROOT / "programs" / "step5" / "step5d"),
                    "--controller-helper",
                    str(ROOT / "tools" / "upload_ur_tp_package.py"),
                ]
            )

        self.assertEqual(result, 0)
        manifest = manifest_from_upload_output(out.getvalue())
        self.assertEqual(manifest["target_dir"], "/programs/andyl/kunwei/step5")
        self.assertEqual(manifest["delivery_mode"], "dry-run")
        self.assertIn("promoted_from_local_candidate", manifest)
        self.assertIn("no program start", manifest["safety_boundary"])
        self.assertIn("no live bridge", manifest["safety_boundary"])

    def test_p0_v8_target_resolves_from_current_planned_capture_target(self) -> None:
        resolution = upload.resolve_table_target(
            "step5d_strict_rnn_no_contact_p0_v8",
            root=ROOT,
            local_dir=ROOT / "programs" / "step5" / "step5d",
        )

        self.assertIsNotNone(resolution)
        assert resolution is not None
        self.assertEqual(resolution["controller_dir"], "/programs/andyl/kunwei/step5")
        self.assertEqual(
            resolution["controller_target"],
            "/programs/andyl/kunwei/step5/step5d_strict_rnn_no_contact_p0_v8.urp",
        )
        self.assertEqual(
            resolution["source"],
            "config/current_stage.json#bridge_trigger.no_contact_p0_v8_capture",
        )

    def test_other_offline_candidate_stays_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "step5_stage_table.json").write_text(
                json.dumps(
                    {
                        "stages": [
                            {
                                "id": "step5d_future_offline_candidate",
                                "package_delivery": {"status": "local_offline_candidate_only"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "inactive offline candidate"):
                upload.enforce_offline_candidate_delivery_block(
                    "step5d_future_offline_candidate",
                    root=root,
                )

    def test_v30_non_dry_delivery_requires_inactive_prelive_flag(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--allow-local-candidate-promote"):
            upload.main(
                [
                    "step5d_strict_rnn_ablation_v30",
                    "--local-dir",
                    str(ROOT / "programs" / "step5" / "step5d"),
                ]
            )

    def test_v30_program_specific_local_candidate_marker_is_discovered(self) -> None:
        marker = upload.load_local_candidate_marker(
            ROOT / "programs" / "step5" / "step5d",
            "step5d_strict_rnn_ablation_v30",
        )

        self.assertIsNotNone(marker)
        assert marker is not None
        self.assertTrue(marker["local_only"])
        self.assertTrue(marker["not_delivered"])

    def test_upload_validator_checks_installation_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            built = liveprep.write_outputs(
                "2026-07-06T0100HKT_STEP5D_STRICT_RNN_ABLATION_V27",
                "2026-07-03T01:00:00+08:00",
                output_dir=output_dir,
                local_only=True,
            )
            files = {ext: Path(built[ext.lstrip(".")]) for ext in upload.EXTENSIONS}

            result = upload.validate_package(
                files,
                liveprep.PROGRAM_NAME,
                liveprep.CONTROLLER_DIR,
                require_exact_cached_script=True,
            )

        self.assertEqual(result["installation_relative_path"], "../../../default")

    def test_upload_validator_checks_step5d_v25_ablation_semantics(self) -> None:
        program = "step5d_strict_rnn_ablation_v25"
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp) / "v25"
            liveprep.write_outputs(
                "2026-07-03T0100HKT_STEP5D_STRICT_RNN_ABLATION_V25",
                "2026-07-03T01:00:00+08:00",
                output_dir=local_dir,
                local_only=True,
                program=program,
            )
            files = {
                ext: local_dir / f"{program}{ext}"
                for ext in upload.EXTENSIONS
            }

            result = upload.validate_package(
                files,
                program,
                liveprep.CONTROLLER_DIR,
                require_exact_cached_script=True,
            )
            script_text = files[".script"].read_text(encoding="utf-8")

        self.assertEqual(result["program"], program)
        self.assertEqual(result["target_dir"], liveprep.CONTROLLER_DIR)
        self.assertIn("v25 preload overrides in 40/41/42/44/46/47", script_text)
        self.assertIn("local cartesian_angular_cap_rad_s = 0.150", script_text)
        self.assertIn("write_output_float_register(35, 25.95)", script_text)
        self.assertIn("local register_clear_required_s = 0.006", script_text)
        self.assertIn("local register_clear_zero_tol = 0.000500", script_text)
        self.assertNotIn("qdot_clear_cap_rad_s", script_text)
        self.assertIn("multimode_executor_and_guard_only", script_text)
        self.assertIn("local cartesian_layout_code = 523.000", script_text)
        self.assertIn("local joint_layout_code = 524.000", script_text)
        self.assertIn("speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, cmd_wz]", script_text)
        self.assertIn("speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]", script_text)
        self.assertIn("# SAFETY: raw normal guard 25 N, force norm guard 25 N, torque guard 4.0 Nm.", script_text)

    def test_upload_validator_accepts_step5d_v25_ablation_candidate(self) -> None:
        program = "step5d_strict_rnn_ablation_v25"
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            built = liveprep.write_outputs(
                "2026-07-03T0100HKT_STEP5D_STRICT_RNN_ABLATION_V25",
                "2026-07-03T01:00:00+08:00",
                output_dir=output_dir,
                local_only=True,
                program=program,
            )
            files = {
                ".script": Path(built["script"]),
                ".txt": Path(built["txt"]),
                ".urp": Path(built["urp"]),
            }

            result = upload.validate_package(
                files,
                program,
                liveprep.CONTROLLER_DIR,
                require_exact_cached_script=True,
            )

        self.assertEqual(result["program"], program)
        self.assertEqual(result["target_dir"], liveprep.CONTROLLER_DIR)

    def test_upload_validator_checks_step5d_v26_ablation_semantics(self) -> None:
        program = "step5d_strict_rnn_ablation_v26"
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp) / "v26"
            liveprep.write_outputs(
                "2026-07-03T0100HKT_STEP5D_STRICT_RNN_ABLATION_V26",
                "2026-07-03T01:00:00+08:00",
                output_dir=local_dir,
                local_only=True,
                program=program,
            )
            files = {
                ext: local_dir / f"{program}{ext}"
                for ext in upload.EXTENSIONS
            }

            result = upload.validate_package(
                files,
                program,
                liveprep.CONTROLLER_DIR,
                require_exact_cached_script=True,
            )
            script_text = files[".script"].read_text(encoding="utf-8")
            txt_text = files[".txt"].read_text(encoding="utf-8")

        self.assertEqual(result["program"], program)
        self.assertEqual(result["target_dir"], liveprep.CONTROLLER_DIR)
        self.assertIn("v26 preload overrides in 40/41/42/44/46/47", script_text)
        self.assertIn("local cartesian_angular_cap_rad_s = 0.015", script_text)
        self.assertIn("v26 first live mode defaults to speedl_cartesian_oracle", txt_text)
        self.assertIn("joint-feasibility-scaled", txt_text)
        self.assertNotIn("step5d_strict_rnn_ablation_v25", script_text + txt_text)

    def _write_triplet(self, root: Path, program: str, payload_prefix: str) -> dict[str, Path]:
        files = {ext: root / f"{program}{ext}" for ext in upload.EXTENSIONS}
        for ext, path in files.items():
            path.write_bytes(f"{payload_prefix}:{ext}\n".encode("utf-8"))
        return files

    def _write_local_candidate_marker(
        self,
        local_dir: Path,
        *,
        program: str,
        target_dir: str,
        files: dict[str, Path],
    ) -> Path:
        marker = {
            "schema": "ur_tp_local_candidate_v1",
            "status": "local package verified",
            "local_only": True,
            "not_delivered": True,
            "program": program,
            "target_dir": target_dir,
            "sha256": upload.package_sha(files),
            "semantic_fingerprint": "abc123",
            "stamp": "2026-07-02T1200HKT_DEMO",
        }
        marker_path = local_dir / upload.LOCAL_CANDIDATE_MARKER
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        return marker_path

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
            with patch.object(
                upload,
                "readback_controller_sha256",
                return_value=local_sha,
            ) as remote:
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
            with patch.object(upload, "readback_controller_sha256", return_value=None):
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

    def test_local_only_candidate_blocks_non_dry_upload_without_promote_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            program = "demo_program"
            target_dir = "/programs/andyl/kunwei/demo"
            local_dir = tmp_path / "local"
            local_dir.mkdir()
            files = self._write_triplet(local_dir, program, "candidate")
            self._write_local_candidate_marker(
                local_dir,
                program=program,
                target_dir=target_dir,
                files=files,
            )

            with self.assertRaisesRegex(RuntimeError, "refusing to upload local-only TP candidate"):
                upload.main(
                    [
                        program,
                        "--target-dir",
                        target_dir,
                        "--override-table",
                        "--override-reason",
                        "offline demo fixture",
                        "--local-dir",
                        str(local_dir),
                    ]
                )

    def test_upload_derives_step5d_p0_target_from_table_without_target_dir(self) -> None:
        out = io.StringIO()

        with redirect_stdout(out):
            result = upload.main(
                [
                    "step5d_strict_rnn_no_contact_p0_v7",
                    "--local-dir",
                    str(ROOT / "programs" / "step5" / "step5d"),
                    "--dry-run",
                ]
            )

        self.assertEqual(result, 0)
        manifest = manifest_from_upload_output(out.getvalue())
        self.assertEqual(manifest["target_dir"], "/programs/andyl/kunwei/step5")
        self.assertEqual(manifest["target_source"], "table")
        self.assertEqual(manifest["target_resolution"]["row_id"], "step5d_strict_rnn_no_contact_p0_v7")

    def test_upload_rejects_explicit_target_dir_without_override(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "explicit --target-dir requires --override-table"):
            upload.main(
                [
                    "step5d_strict_rnn_no_contact_p0_v7",
                    "--local-dir",
                    str(ROOT / "programs" / "step5" / "step5d"),
                    "--target-dir",
                    "/programs/andyl/kunwei/step5/step5d",
                    "--dry-run",
                ]
            )

    def test_upload_override_requires_reason_and_records_manifest_reason(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--override-reason is required"):
            upload.main(
                [
                    "step5d_strict_rnn_no_contact_p0_v7",
                    "--local-dir",
                    str(ROOT / "programs" / "step5" / "step5d"),
                    "--target-dir",
                    "/programs/andyl/kunwei/step5",
                    "--override-table",
                    "--dry-run",
                ]
            )

        out = io.StringIO()
        with redirect_stdout(out):
            result = upload.main(
                [
                    "step5d_strict_rnn_no_contact_p0_v7",
                    "--local-dir",
                    str(ROOT / "programs" / "step5" / "step5d"),
                    "--target-dir",
                    "/programs/andyl/kunwei/step5",
                    "--override-table",
                    "--override-reason",
                    "operator recovery drill",
                    "--dry-run",
                ]
            )

        self.assertEqual(result, 0)
        manifest = manifest_from_upload_output(out.getvalue())
        self.assertEqual(manifest["target_source"], "override")
        self.assertEqual(manifest["target_override_reason"], "operator recovery drill")

    def test_local_only_candidate_marker_validates_exact_triplet_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            program = "demo_program"
            target_dir = "/programs/andyl/kunwei/demo"
            local_dir = tmp_path / "local"
            local_dir.mkdir()
            files = self._write_triplet(local_dir, program, "candidate")
            self._write_local_candidate_marker(
                local_dir,
                program=program,
                target_dir=target_dir,
                files=files,
            )
            marker = upload.load_local_candidate_marker(local_dir)
            assert marker is not None
            files[".urp"].write_bytes(b"changed\n")

            with self.assertRaisesRegex(RuntimeError, "local candidate marker sha256 does not match"):
                upload.validate_local_candidate_marker(
                    marker,
                    files=files,
                    program=program,
                    target_dir=target_dir,
                    local_sha=upload.package_sha(files),
                )

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
