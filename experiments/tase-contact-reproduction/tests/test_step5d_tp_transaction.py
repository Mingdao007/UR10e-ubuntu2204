#!/usr/bin/env python3
from __future__ import annotations
import hashlib
import json
import sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import run_step5d_tp_transaction as coordinator  # noqa: E402

class Step5dTpTransactionTest(unittest.TestCase):
    CANDIDATE_SHA = {extension: extension.encode().hex().ljust(64, "0")[:64] for extension in (".script", ".txt", ".urp")}

    def candidate_preflight(self):
        return mock.Mock(package_sha256=self.CANDIDATE_SHA)

    def args(self, root: Path) -> list[str]:
        return [
            "step5d_x",
            "--root", str(root),
            "--local-dir", str(root / "programs"),
            "--artifact-store", str(root / "artifacts"),
        ]

    def test_dry_run_passes_one_resolved_readback_root_without_locking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(coordinator.upload, "_main", return_value=0) as upload, \
             mock.patch.object(coordinator, "acquire_controller_mutation_locks") as lock:
            root = Path(tmp)
            self.assertEqual(coordinator.main([*self.args(root), "--dry-run"]), 0)
        lock.assert_not_called()
        upload_args = upload.call_args.args[0]
        self.assertEqual(upload_args.count("--readback-root"), 1)
        self.assertIn(str((root / "runs").resolve()), upload_args)
        self.assertNotIn("None", upload_args)

    def test_success_holds_one_lock_scope_for_all_three_stages(self) -> None:
        events: list[str] = []
        calls: dict[str, list[str]] = {}

        def upload_exact(args: list[str]) -> int:
            events.append("upload")
            token = args[args.index("--upload-transaction-id") + 1]
            result_path = Path(args[args.index("--manifest-path-output") + 1])
            readback_root = Path(args[args.index("--readback-root") + 1])
            manifest = readback_root / "controller_readback_step5d_x_exact/manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"upload_transaction_id": token}) + "\n")
            result_path.write_text(json.dumps({
                "schema_version": "ur10e_upload_result_v1",
                "upload_transaction_id": token,
                "manifest_path": str(manifest.resolve()),
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            }) + "\n")
            calls["manifest"] = [str(manifest.resolve())]
            return 0

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(coordinator, "acquire_controller_mutation_locks", side_effect=lambda: events.append("lock") or [object()]), \
             mock.patch.object(coordinator.fast_gate, "preflight_controller_candidate", return_value=self.candidate_preflight()), \
             mock.patch.object(coordinator.fast_gate, "preflight_fresh_readback", return_value=({}, "transaction")), \
             mock.patch.object(coordinator.upload, "_main", side_effect=upload_exact), \
             mock.patch.object(coordinator.promote, "_main", side_effect=lambda args: calls.setdefault("promote", args) and events.append("promote") or 0), \
             mock.patch.object(coordinator.readback, "main", side_effect=lambda args: calls.setdefault("readback", args) and events.append("readback") or 0), \
             mock.patch.object(coordinator.fast_gate, "promote_controller_verified", side_effect=lambda **kwargs: calls.setdefault("canonical", [str(kwargs["readback_manifest"])]) and events.append("canonical") or Path(tmp)), \
             mock.patch.object(coordinator, "release_controller_mutation_locks", side_effect=lambda _: events.append("release")):
            self.assertEqual(coordinator.main(self.args(Path(tmp))), 0)
        self.assertEqual(events, ["lock", "upload", "promote", "readback", "canonical", "release"])
        exact = calls["manifest"][0]
        self.assertEqual(calls["promote"][calls["promote"].index("--manifest") + 1], exact)
        self.assertEqual(calls["readback"][calls["readback"].index("--manifest") + 1], exact)
        self.assertEqual(calls["canonical"], [exact])

    def test_custom_root_and_stale_future_mtime_cannot_redirect_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / "runs/controller_readback_step5d_x_stale/manifest.json"
            stale.parent.mkdir(parents=True)
            stale.write_text("{}\n")
            custom = root / "custom-readback"
            exact: list[Path] = []

            def upload_exact(args: list[str]) -> int:
                token = args[args.index("--upload-transaction-id") + 1]
                result_path = Path(args[args.index("--manifest-path-output") + 1])
                manifest = custom / "controller_readback_step5d_x_new/manifest.json"
                manifest.parent.mkdir(parents=True)
                manifest.write_text(json.dumps({"upload_transaction_id": token}) + "\n")
                result_path.write_text(json.dumps({
                    "schema_version": "ur10e_upload_result_v1",
                    "upload_transaction_id": token,
                    "manifest_path": str(manifest.resolve()),
                    "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                }) + "\n")
                exact.append(manifest.resolve())
                return 0

            seen: list[Path] = []
            args = [*self.args(root), "--readback-root", str(custom)]
            with mock.patch.object(coordinator, "acquire_controller_mutation_locks", return_value=[object()]), \
                 mock.patch.object(coordinator.fast_gate, "preflight_controller_candidate", return_value=self.candidate_preflight()), \
                 mock.patch.object(coordinator.fast_gate, "preflight_fresh_readback", return_value=({}, "transaction")), \
                 mock.patch.object(coordinator.upload, "_main", side_effect=upload_exact), \
                 mock.patch.object(coordinator.promote, "_main", side_effect=lambda values: seen.append(Path(values[values.index("--manifest") + 1])) or 0), \
                 mock.patch.object(coordinator.readback, "main", side_effect=lambda values: seen.append(Path(values[values.index("--manifest") + 1])) or 0), \
                 mock.patch.object(coordinator.fast_gate, "promote_controller_verified", side_effect=lambda **kwargs: seen.append(kwargs["readback_manifest"]) or Path(tmp)), \
                 mock.patch.object(coordinator, "release_controller_mutation_locks"):
                self.assertEqual(coordinator.main(args), 0)
            self.assertEqual(seen, exact * 3)
            self.assertNotIn(stale.resolve(), seen)

    def test_external_readback_root_is_rejected_before_upload_or_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as external, \
             mock.patch.object(coordinator.upload, "_main") as upload, \
             mock.patch.object(coordinator, "acquire_controller_mutation_locks") as lock:
            with self.assertRaisesRegex(RuntimeError, "inside the experiment root"):
                coordinator.main([
                    *self.args(Path(tmp)),
                    "--readback-root", external,
                ])
        upload.assert_not_called()
        lock.assert_not_called()

    def test_candidate_precondition_failure_cannot_lock_or_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(
                 coordinator.fast_gate,
                 "preflight_controller_candidate",
                 side_effect=ValueError("candidate bytes drift"),
             ), \
             mock.patch.object(coordinator.upload, "_main") as upload, \
             mock.patch.object(coordinator, "acquire_controller_mutation_locks") as lock:
            with self.assertRaisesRegex(ValueError, "candidate bytes drift"):
                coordinator.main(self.args(Path(tmp)))
        upload.assert_not_called()
        lock.assert_not_called()

    def test_failure_stops_following_stages_and_releases(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(coordinator.fast_gate, "preflight_controller_candidate", return_value=self.candidate_preflight()), \
             mock.patch.object(coordinator, "acquire_controller_mutation_locks", return_value=[object()]), \
             mock.patch.object(coordinator.upload, "_main", return_value=7), \
             mock.patch.object(coordinator.promote, "_main") as promote, \
             mock.patch.object(coordinator.readback, "main") as readback, \
             mock.patch.object(coordinator, "release_controller_mutation_locks", side_effect=lambda _: events.append("release")):
            self.assertEqual(coordinator.main(self.args(Path(tmp))), 7)
        promote.assert_not_called()
        readback.assert_not_called()
        self.assertEqual(events, ["release"])

if __name__ == "__main__":
    unittest.main()
