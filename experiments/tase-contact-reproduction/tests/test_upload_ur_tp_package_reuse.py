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
    def test_helper_json_parser_ignores_transport_noise(self) -> None:
        payload = upload.parse_helper_json(
            "transport progress\n\n"
            '{"ok": true, "operation": "readback", "files": []}\n'
        )
        self.assertEqual(payload["operation"], "readback")

    def test_v30_inactive_candidate_allows_manifest_bound_dry_run_without_promotion(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            result = upload.main(
                [
                    "step5d_strict_rnn_ablation_v30",
                    "--dry-run",
                    "--local-dir",
                    str(ROOT / "programs" / "step5" / "step5d"),
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
        self.assertEqual(resolution["controller_dir"], "/programs/andyl/kunwei/step5/archive")
        self.assertEqual(
            resolution["controller_target"],
            "/programs/andyl/kunwei/step5/archive/step5d_strict_rnn_no_contact_p0_v8.urp",
        )
        self.assertEqual(
            resolution["source"],
            "config/current_stage.json#bridge_trigger.no_contact_p0_v8_capture",
        )

    def test_audited_target_override_skips_invalid_table_placeholder(self) -> None:
        out = io.StringIO()
        with (
            patch.object(
                upload,
                "resolve_table_target",
                side_effect=AssertionError("table resolution must be skipped"),
            ),
            redirect_stdout(out),
        ):
            result = upload.main(
                [
                    "step5d_strict_rnn_no_contact_p0_v9",
                    "--dry-run",
                    "--local-dir",
                    str(ROOT / "programs" / "step5" / "step5d"),
                    "--target-dir",
                    "/programs/andyl/kunwei/step5",
                    "--override-table",
                    "--override-reason",
                    "replace local-only placeholder for manifest-bound delivery",
                ]
            )

        self.assertEqual(result, 0)
        manifest = manifest_from_upload_output(out.getvalue())
        self.assertEqual(manifest["target_dir"], "/programs/andyl/kunwei/step5")
        self.assertEqual(manifest["target_source"], "override")

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

    def test_v30_program_specific_local_candidate_marker_is_discovered(self) -> None:
        marker = upload.load_local_candidate_marker(
            ROOT / "programs" / "step5" / "step5d",
            "step5d_strict_rnn_ablation_v30",
        )

        self.assertIsNotNone(marker)
        assert marker is not None
        self.assertTrue(marker["local_only"])
        self.assertTrue(marker["not_delivered"])

    def test_p0_v9_marker_records_verified_controller_readback(self) -> None:
        marker = upload.load_local_candidate_marker(
            ROOT / "programs" / "step5" / "step5d",
            "step5d_strict_rnn_no_contact_p0_v9",
        )

        self.assertIsNotNone(marker)
        assert marker is not None
        self.assertFalse(marker["local_only"])
        self.assertFalse(marker["not_delivered"])
        self.assertTrue(marker["controller_readback_verified"])
        manifest = str(marker["controller_readback_manifest"])
        self.assertTrue(
            manifest.startswith(
                "runs/controller_readback_step5d_strict_rnn_no_contact_p0_v9_"
            )
        )
        self.assertTrue(manifest.endswith("/manifest.json"))
        self.assertTrue((ROOT / manifest).is_file())

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
                    helper_sha256="0" * 64,
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

    def test_full_delivery_uses_only_manifest_bound_helper_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = self._write_triplet(root, "demo_program", "same")
            helper = root / "controller-helper.py"
            helper.touch()
            output = io.StringIO()
            with redirect_stdout(output):
                result = upload.upload_and_readback(
                    files,
                    "demo_program",
                    upload.DEFAULT_CONTROLLER,
                    "/programs/andyl/kunwei/demo",
                    root / "readback",
                    helper=helper,
                    helper_sha256=None,
                    dry_run=True,
                )
            rendered = output.getvalue()

        self.assertEqual(result, {})
        self.assertIn("deploy-triplet", rendered)
        self.assertIn("--confirm-deploy", rendered)
        self.assertIn("readback", rendered)
        self.assertNotIn(" run -- ", rendered)
        self.assertNotIn(" put ", rendered)
        self.assertNotIn(" get ", rendered)

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
                    helper_sha256="0" * 64,
                    local_sha=local_sha,
                )

            self.assertIsNone(result)

    def test_local_candidate_automatically_runs_upload_and_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            program = "step5d_strict_rnn_no_contact_p0_v9"
            target_dir = "/programs/andyl/kunwei/step5"
            source_dir = ROOT / "programs" / "step5" / "step5d"
            local_dir = tmp_path / "local"
            local_dir.mkdir()
            files = {
                ext: local_dir / f"{program}{ext}"
                for ext in upload.EXTENSIONS
            }
            for ext, destination in files.items():
                destination.write_bytes((source_dir / f"{program}{ext}").read_bytes())
            helper = tmp_path / "controller-helper.py"
            helper.write_text("print('fixture')\n", encoding="utf-8")
            helper_digest = upload.sha256(helper)
            self._write_local_candidate_marker(
                local_dir,
                program=program,
                target_dir=target_dir,
                files=files,
            )

            def fake_upload_and_readback(
                package_files,
                _program,
                _controller,
                _target_dir,
                readback_dir,
                *,
                helper,
                helper_sha256,
                dry_run,
            ):
                self.assertFalse(dry_run)
                self.assertIsNotNone(helper)
                self.assertEqual(helper_sha256, helper_digest)
                readback_dir.mkdir(parents=True)
                local_sha = upload.package_sha(package_files)
                for ext, source in package_files.items():
                    (readback_dir / source.name).write_bytes(source.read_bytes())
                return {
                    "local": local_sha,
                    "controller": local_sha,
                    "readback": local_sha,
                }

            out = io.StringIO()
            with (
                patch.object(
                    upload,
                    "upload_and_readback",
                    side_effect=fake_upload_and_readback,
                ) as deploy,
                patch.object(
                    upload,
                    "_verified_owner_dependency",
                    return_value={
                        "owner_id": "ur10e-controller-access",
                        "path": str(helper),
                        "sha256": helper_digest,
                    },
                ),
                redirect_stdout(out),
            ):
                result = upload.main(
                    [
                        program,
                        "--target-dir",
                        target_dir,
                        "--override-table",
                        "--override-reason",
                        "test automatic candidate delivery",
                        "--local-dir",
                        str(local_dir),
                        "--readback-root",
                        str(tmp_path / "readbacks"),
                        "--controller-helper",
                        str(helper),
                        "--controller-helper-sha256",
                        helper_digest,
                    ]
                )

            self.assertEqual(result, 0)
            deploy.assert_called_once()
            manifests = list((tmp_path / "readbacks").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "controller read-back verified")
            marker = upload.load_local_candidate_marker(local_dir, program)
            assert marker is not None
            self.assertFalse(marker["local_only"])
            self.assertFalse(marker["not_delivered"])
            self.assertTrue(marker["controller_readback_verified"])
            self.assertEqual(marker["delivery_mode"], "full_upload_readback")

    def test_helper_requires_exact_sha_and_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper = root / "controller-helper.py"
            helper.write_text("print('fixture')\n", encoding="utf-8")
            digest = upload.sha256(helper)

            with self.assertRaisesRegex(RuntimeError, "SHA-256 is required"):
                upload.helper_cmd(helper, "readback", expected_sha256=None)
            with self.assertRaisesRegex(RuntimeError, "SHA-256 differs"):
                upload.validate_controller_helper(helper, "0" * 64)
            upload.validate_controller_helper(helper, digest)

            alias = root / "helper-alias.py"
            alias.symlink_to(helper)
            with self.assertRaisesRegex(RuntimeError, "missing or unsafe"):
                upload.validate_controller_helper(alias, digest)

    def test_live_helper_rejects_self_reported_pair_not_in_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attested = root / "attested-helper.py"
            attested.write_text("print('attested')\n", encoding="utf-8")
            attested_digest = upload.sha256(attested)
            self_reported = root / "self-reported-helper.py"
            self_reported.write_text("print('self-reported')\n", encoding="utf-8")
            self_reported_digest = upload.sha256(self_reported)
            binding = {
                "owner_id": "ur10e-controller-access",
                "path": str(attested),
                "sha256": attested_digest,
            }

            with patch.object(
                upload,
                "_verified_owner_dependency",
                return_value=binding,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "CLI binding differs from verified runtime attestation",
                ):
                    upload.resolve_live_controller_helper(
                        self_reported,
                        self_reported_digest,
                    )
                self.assertEqual(
                    upload.resolve_live_controller_helper(attested, attested_digest),
                    (attested, attested_digest),
                )
                self.assertEqual(
                    upload.resolve_live_controller_helper(None, None),
                    (attested, attested_digest),
                )
                for requested_helper, requested_sha256 in (
                    (attested, None),
                    (None, attested_digest),
                    (attested, "0" * 64),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "controller helper path and SHA-256 must be supplied together"
                        if (requested_helper is None) != (requested_sha256 is None)
                        else "CLI binding differs from verified runtime attestation",
                    ):
                        upload.resolve_live_controller_helper(
                            requested_helper,
                            requested_sha256,
                        )

    def test_helper_replacement_after_validation_executes_only_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = self._write_triplet(root, "demo_program", "same")
            helper = root / "controller-helper.py"
            helper.write_text("print('first')\n", encoding="utf-8")
            digest = upload.sha256(helper)
            local_sha = upload.package_sha(files)
            snapshots: list[Path] = []
            executed_content: list[str] = []

            def fake_helper_call(command, *, dry_run, capture=False):
                self.assertFalse(dry_run)
                snapshot = Path(command[1])
                snapshots.append(snapshot)
                if not executed_content:
                    helper.write_text("print('replaced')\n", encoding="utf-8")
                executed_content.append(snapshot.read_text(encoding="utf-8"))
                operation = command[2]
                if operation == "deploy-triplet":
                    verified_readback = [
                        {
                            "filename": files[ext].name,
                            "sha256": local_sha[ext],
                        }
                        for ext in upload.EXTENSIONS
                    ]
                    return json.dumps(
                        {"ok": True, "verified_readback": verified_readback}
                    ) + "\n"
                self.assertEqual(operation, "readback")
                output_dir = Path(command[command.index("--output-dir") + 1])
                for source in files.values():
                    (output_dir / source.name).write_bytes(source.read_bytes())
                return '{"ok": true}\n'

            with patch.object(upload, "run", side_effect=fake_helper_call):
                result = upload.upload_and_readback(
                    files,
                    "demo_program",
                    upload.DEFAULT_CONTROLLER,
                    "/programs/andyl/kunwei/demo",
                    root / "readback",
                    helper=helper,
                    helper_sha256=digest,
                    dry_run=False,
                )

            self.assertEqual(result["controller"], local_sha)
            self.assertEqual(
                executed_content,
                ["print('first')\n", "print('first')\n"],
            )
            self.assertTrue(all(snapshot != helper for snapshot in snapshots))
            self.assertTrue(all(not snapshot.exists() for snapshot in snapshots))
            self.assertEqual(
                helper.read_text(encoding="utf-8"),
                "print('replaced')\n",
            )

    def test_readback_only_existing_never_calls_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = self._write_triplet(root, "demo_program", "same")
            helper = root / "controller-helper.py"
            helper.write_text("print('helper')\n", encoding="utf-8")
            digest = upload.sha256(helper)
            local_sha = upload.package_sha(files)
            operations: list[str] = []

            def fake_helper_call(command, *, dry_run, capture=False):
                self.assertFalse(dry_run)
                self.assertTrue(capture)
                operation = command[2]
                operations.append(operation)
                self.assertEqual(operation, "readback")
                output_dir = Path(command[command.index("--output-dir") + 1])
                output_dir.mkdir(parents=True)
                for source in files.values():
                    (output_dir / source.name).write_bytes(source.read_bytes())
                return json.dumps(
                    {
                        "ok": True,
                        "operation": "readback",
                        "files": [
                            {
                                "filename": files[ext].name,
                                "sha256": local_sha[ext],
                            }
                            for ext in upload.EXTENSIONS
                        ],
                    }
                ) + "\n"

            with patch.object(upload, "run", side_effect=fake_helper_call):
                result = upload.readback_only_existing(
                    files,
                    "demo_program",
                    upload.DEFAULT_CONTROLLER,
                    "/programs/andyl/kunwei/demo",
                    root / "readback",
                    helper=helper,
                    helper_sha256=digest,
                )

            self.assertEqual(operations, ["readback"])
            self.assertEqual(
                result,
                {
                    "local": local_sha,
                    "controller": local_sha,
                    "readback": local_sha,
                },
            )

    def test_readback_only_existing_rejects_downloaded_byte_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = self._write_triplet(root, "demo_program", "same")
            helper = root / "controller-helper.py"
            helper.write_text("print('helper')\n", encoding="utf-8")
            digest = upload.sha256(helper)
            local_sha = upload.package_sha(files)
            operations: list[str] = []

            def fake_helper_call(command, *, dry_run, capture=False):
                self.assertFalse(dry_run)
                self.assertTrue(capture)
                operations.append(command[2])
                output_dir = Path(command[command.index("--output-dir") + 1])
                output_dir.mkdir(parents=True)
                for extension, source in files.items():
                    data = source.read_bytes()
                    if extension == ".urp":
                        data += b"drift"
                    (output_dir / source.name).write_bytes(data)
                return json.dumps(
                    {
                        "ok": True,
                        "operation": "readback",
                        "files": [
                            {
                                "filename": files[ext].name,
                                "sha256": local_sha[ext],
                            }
                            for ext in upload.EXTENSIONS
                        ],
                    }
                ) + "\n"

            with (
                patch.object(upload, "run", side_effect=fake_helper_call),
                self.assertRaisesRegex(RuntimeError, "triplet SHA closure differs"),
            ):
                upload.readback_only_existing(
                    files,
                    "demo_program",
                    upload.DEFAULT_CONTROLLER,
                    "/programs/andyl/kunwei/demo",
                    root / "readback",
                    helper=helper,
                    helper_sha256=digest,
                )

            self.assertEqual(operations, ["readback"])

    def test_upload_derives_step5d_p0_target_from_table_without_target_dir(self) -> None:
        out = io.StringIO()

        with redirect_stdout(out):
            result = upload.main(
                [
                    "step5d_strict_rnn_no_contact_p0_v7",
                    "--local-dir",
                    str(ROOT / "programs" / "step5" / "step5d" / "archive"),
                    "--dry-run",
                ]
            )

        self.assertEqual(result, 0)
        manifest = manifest_from_upload_output(out.getvalue())
        self.assertEqual(manifest["target_dir"], "/programs/andyl/kunwei/step5/archive")
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

    def test_manifest_fresh_controller_flag_accepts_full_readback_sha_agreement(self) -> None:
        shas = {
            "local": {".script": "1", ".txt": "2", ".urp": "3"},
            "controller": {".script": "1", ".txt": "2", ".urp": "3"},
            "readback": {".script": "1", ".txt": "2", ".urp": "3"},
        }

        self.assertTrue(upload.triplet_sha_sets_match(shas))
        shas["readback"][".urp"] = "different"
        self.assertFalse(upload.triplet_sha_sets_match(shas))


if __name__ == "__main__":
    unittest.main()
