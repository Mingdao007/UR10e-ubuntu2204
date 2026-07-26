from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import step5d_fast_gate as gate  # noqa: E402
from step5d_workflow_state import WorkflowStateError, verify_current  # noqa: E402


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


class Step5dFastGateTest(unittest.TestCase):
    @staticmethod
    def _decision_snapshot() -> dict:
        return {
            "decision_digest": "d" * 64,
            "source_bindings": {
                "current_stage_sha256": "1" * 64,
                "stage_table_sha256": "2" * 64,
            },
        }

    def _candidate(
        self,
        root: Path,
        *,
        high: bool = False,
        no_upload: bool = False,
        reused: bool = False,
        validation_updates: dict | None = None,
        add_unvalidated_change: bool = False,
        omit_always_run: bool = False,
    ) -> Path:
        program = "step5d_demo_v1"
        local = root / "package"
        local.mkdir()
        for extension in gate.EXTENSIONS:
            (local / f"{program}{extension}").write_bytes(f"demo:{extension}".encode())
        validation_root = Path(tempfile.mkdtemp(prefix="step5d-fast-gate-validation-"))
        self.addCleanup(shutil.rmtree, validation_root, True)
        validation = validation_root / "validation_manifest.json"
        dependency_map = root / "config/ur10e_test_dependency_map_v1.json"
        always_run = ["tests/test_demo.py"]
        if omit_always_run:
            always_run.append("tests/test_always.py")
        write_json(dependency_map, {
            "schema_version": "ur10e_test_dependency_map_v1",
            "always_run": always_run,
            "rules": [],
            "resource_groups": {},
            "validators": ["tools/validator.py"],
        })
        test_file = root / "tests/test_demo.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def test_demo():\n    assert True\n", encoding="utf-8")
        if omit_always_run:
            (root / "tests/test_always.py").write_text(
                "def test_always():\n    assert True\n", encoding="utf-8"
            )
        validator_file = root / "tools/validator.py"
        validator_file.parent.mkdir(parents=True)
        validator_file.write_text("raise SystemExit(0)\n", encoding="utf-8")
        (root / ".gitignore").write_text("runs/\nartifact-store/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Step5d gate test"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture baseline"], cwd=root, check=True)
        test_file.write_text("def test_demo():\n    assert True\n# changed\n", encoding="utf-8")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        comparison = {
            "source": "workspace_head",
            "base_ref": "HEAD",
            "head_ref": "HEAD",
            "merge_base": head,
        }
        changed_paths = ["tests/test_demo.py"]
        selection_snapshot = gate.select_impacted(
            root=root,
            paths=changed_paths,
            dependency_map=dependency_map,
            full_suite=False,
            comparison=comparison,
        )
        if omit_always_run:
            omitted = "tests/test_always.py"
            selection_snapshot["always_run_tests"].remove(omitted)
            selection_snapshot["selected_tests"].remove(omitted)
            selection_snapshot["parallel_tests"].remove(omitted)
            selection_snapshot["skipped_tests"].append(omitted)
            selection_snapshot["skipped_tests"].sort()
            selection_snapshot["dependency_hashes"].pop(omitted)
            fingerprint_payload = {
                "dependency_map_sha256": selection_snapshot["dependency_map_sha256"],
                "changed_paths": selection_snapshot["changed_paths"],
                "selected_tests": selection_snapshot["selected_tests"],
                "skipped_tests": selection_snapshot["skipped_tests"],
                "matched_changed_paths": selection_snapshot["matched_changed_paths"],
                "unmapped_changed_paths": selection_snapshot["unmapped_changed_paths"],
                "dependency_hashes": selection_snapshot["dependency_hashes"],
                "full_suite": False,
                "comparison": comparison,
            }
            selection_snapshot["source_fingerprint"] = gate._digest(fingerprint_payload)
        dependency_hashes = selection_snapshot["dependency_hashes"]
        selected_tests = selection_snapshot["selected_tests"]
        skipped_tests = selection_snapshot["skipped_tests"]
        matched_changed_paths = selection_snapshot["matched_changed_paths"]
        unmapped_changed_paths = selection_snapshot["unmapped_changed_paths"]
        source_bindings = {
            "current_stage_sha256": "1" * 64,
            "stage_table_sha256": "2" * 64,
        }
        source_fingerprint = selection_snapshot["source_fingerprint"]
        environment_binding = {
            "schema_version": "ur10e_validation_environment_binding_v1",
            "fixture_declaration_present": True,
            "external_fixture_closure": {},
            "python": {"sha256": "4" * 64},
            "git_head": {"exit_code": 0},
            "git_tree": {"exit_code": 0},
        }
        composite_fingerprint = gate._digest({
            "source_fingerprint": source_fingerprint,
            "decision_digest": "d" * 64,
            "current_stage_sha256": source_bindings["current_stage_sha256"],
            "stage_table_sha256": source_bindings["stage_table_sha256"],
            "execution_scope": "impacted",
            "environment_binding": environment_binding,
        })
        decision_snapshot = {
            "decision_digest": "d" * 64,
            "source_bindings": source_bindings,
        }
        write_json(validation.parent / "test_selection.json", selection_snapshot)
        write_json(validation.parent / "user_decision_manifest.json", decision_snapshot)
        results = [
            {
                "name": "pytest_demo",
                "command": [sys.executable, "-m", "pytest", "-q", "tests/test_demo.py"],
                "status": "passed",
                "exit_code": 0,
            },
            {
                "name": "validator",
                "command": [sys.executable, "tools/validator.py"],
                "status": "passed",
                "exit_code": 0,
            },
        ]
        for result in results:
            (validation.parent / f"{result['name']}.stdout.log").write_text("passed\n")
            (validation.parent / f"{result['name']}.stderr.log").write_text("")
        payload = {
            "schema_version": "ur10e_impacted_validation_manifest_v1",
            "passed": True,
            "full_suite": False,
            "unmapped_changed_paths": unmapped_changed_paths,
            "comparison": comparison,
            "decision_digest": "d" * 64,
            "source_fingerprint": source_fingerprint,
            "composite_fingerprint": composite_fingerprint,
            "environment_binding": environment_binding,
            "dependency_map_sha256": gate.sha256(dependency_map),
            "dependency_hashes": dependency_hashes,
            "changed_paths": changed_paths,
            "matched_changed_paths": matched_changed_paths,
            "selected_tests": selected_tests,
            "skipped_tests": skipped_tests,
            "always_run_tests": selection_snapshot["always_run_tests"],
            "results": results,
            "reused": reused,
            "cache_role": "development_acceleration_only",
            "cache_reuse_acceptance_eligible": False,
            "cached_outputs": [],
        }
        payload["cached_outputs"] = [
            {
                "path": path.relative_to(validation.parent).as_posix(),
                "sha256": gate.sha256(path),
                "size": path.stat().st_size,
            }
            for path in sorted(validation.parent.rglob("*"))
            if path.is_file()
        ]
        if validation_updates:
            payload.update(validation_updates)
        payload["manifest_core_sha256"] = gate._manifest_core_digest(payload)
        write_json(validation, payload)
        if add_unvalidated_change:
            omitted = root / "tools/omitted_change.py"
            omitted.parent.mkdir(parents=True, exist_ok=True)
            omitted.write_text("# omitted from validation\n", encoding="utf-8")
        with patch.object(gate, "build_snapshot", return_value=decision_snapshot):
            return gate.create_candidate(
                root=root,
                program=program,
                parent_program="step5d_demo_v0",
                local_dir=local,
                validation_manifest=validation,
                semantic_delta="demo delta",
                changed_contracts=["force_frame"] if high else ["reporting"],
                owners=["bridge"],
                no_upload=no_upload,
            )

    def _readback(self, root: Path, candidate_path: Path) -> Path:
        candidate = json.loads(candidate_path.read_text())
        program = candidate["program"]
        directory = root / "runs" / f"controller_readback_{program}_fixture"
        directory.mkdir(parents=True)
        for extension in gate.EXTENSIONS:
            source = Path(candidate["package_source"]) / f"{program}{extension}"
            (directory / source.name).write_bytes(source.read_bytes())
        write_json(directory / "manifest.json", {
            "status": "controller read-back verified",
            "delivery_mode": "full_upload_readback",
            "readback_source": "fresh_controller_get",
            "upload_transaction_id": "1" * 32,
            "controller": "fixture-controller",
            "target_dir": "/programs/fixture",
            "validation": {
                "program": program,
                "target_dir": "/programs/fixture",
                "script_node_path": f"/programs/fixture/{program}.script",
                "script_sha256": candidate["package_sha256"][".script"],
                "txt_sha256": candidate["package_sha256"][".txt"],
                "urp_sha256": candidate["package_sha256"][".urp"],
            },
            "sha256": {
                "local": candidate["package_sha256"],
                "controller": candidate["package_sha256"],
                "readback": candidate["package_sha256"],
            },
        })
        return directory / "manifest.json"

    def _promote(self, **kwargs):
        with patch.object(gate, "build_snapshot", return_value=self._decision_snapshot()):
            return gate.promote_controller_verified(**kwargs)

    def test_low_risk_candidate_to_controller_verified_skips_review_full_suite_and_timing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = self._candidate(root)
            readback = self._readback(root, candidate)
            self._promote(
                root=root,
                program="step5d_demo_v1",
                readback_manifest=readback,
                store=root / "artifact-store",
            )
            status = verify_current(root=root, store=root / "artifact-store")
            payload = json.loads(candidate.read_text())
            self.assertEqual(payload["review"]["requirement"], "0+0")
            self.assertFalse(payload["impacted_validation"]["full_suite"])
            self.assertEqual(status["state"], "controller_verified")
            self.assertFalse(status["live_ready"])

    def test_explicit_no_upload_stays_candidate_and_cannot_claim_complete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = self._candidate(root, no_upload=True)
            current = json.loads((root / gate.CURRENT).read_text())
            self.assertEqual(json.loads(candidate.read_text())["upload_policy"], "explicit_no_upload")
            self.assertEqual(current["state"], "candidate")
            self.assertFalse(current["claims"]["new_version_complete"])
            readback = self._readback(root, candidate)
            with self.assertRaisesRegex(WorkflowStateError, "explicit_no_upload"):
                self._promote(
                    root=root,
                    program="step5d_demo_v1",
                    readback_manifest=readback,
                    store=root / "artifact-store",
                )

    def test_development_cache_reuse_cannot_create_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(WorkflowStateError, "fresh non-reused"):
                self._candidate(Path(td), reused=True)

    def test_cache_eligibility_self_assertion_and_non_boolean_reuse_cannot_create_candidate(self) -> None:
        cases = (
            {"reused": True, "cache_reuse_acceptance_eligible": True},
            {"reused": "true", "cache_reuse_acceptance_eligible": False},
            {"reused": False, "cache_reuse_acceptance_eligible": True},
            {"reused": False, "cache_role": "acceptance_evidence"},
        )
        for updates in cases:
            with self.subTest(updates=updates), tempfile.TemporaryDirectory() as td:
                with self.assertRaisesRegex(WorkflowStateError, "fresh non-reused"):
                    self._candidate(Path(td), validation_updates=updates)

    def test_malformed_validation_schema_fingerprint_comparison_and_results_fail_closed(self) -> None:
        cases = {
            "schema": {"schema_version": "unknown"},
            "source": {"source_fingerprint": "not-a-digest"},
            "composite": {"composite_fingerprint": "3" * 64},
            "comparison": {"comparison": {"source": "explicit"}},
            "results": {"results": [{
                "name": "pytest_demo",
                "command": ["python", "-m", "pytest", "tests/test_demo.py"],
                "status": "failed",
                "exit_code": 1,
            }]},
            "validator_omitted": {"results": [{
                "name": "pytest_demo",
                "command": [sys.executable, "-m", "pytest", "-q", "tests/test_demo.py"],
                "status": "passed",
                "exit_code": 0,
            }]},
            "environment": {"environment_binding": {"schema_version": "self_asserted"}},
        }
        for name, updates in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(WorkflowStateError):
                    self._candidate(Path(td), validation_updates=updates)

    def test_validation_cannot_omit_an_actual_git_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(WorkflowStateError, "authoritative git diff"):
                self._candidate(Path(td), add_unvalidated_change=True)

    def test_validation_cannot_omit_dependency_map_always_run_test(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(WorkflowStateError, "authoritative dependency map"):
                self._candidate(Path(td), omit_always_run=True)

    def test_preupload_freeze_rejects_dependency_drift_after_candidate_creation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = self._candidate(root)
            (root / "tests/test_demo.py").write_text("# changed again after candidate freeze\n")
            with patch.object(gate, "build_snapshot", return_value=self._decision_snapshot()):
                with self.assertRaises(WorkflowStateError):
                    gate.preflight_controller_candidate(
                        root=root,
                        program="step5d_demo_v1",
                        local_dir=Path(json.loads(candidate.read_text())["package_source"]),
                    )

    def test_frozen_candidate_survives_expected_legacy_decision_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = self._candidate(root)
            readback = self._readback(root, candidate)
            with patch.object(gate, "build_snapshot", return_value=self._decision_snapshot()):
                frozen = gate.preflight_controller_candidate(
                    root=root,
                    program="step5d_demo_v1",
                    local_dir=Path(json.loads(candidate.read_text())["package_source"]),
                )
            write_json(root / "config/current_stage.json", {"mutated_by": "legacy_promotion"})
            write_json(root / "config/step5_stage_table.json", {"mutated_by": "legacy_promotion"})
            with patch.object(gate, "build_snapshot", side_effect=AssertionError("must not re-freeze")):
                gate.promote_controller_verified(
                    root=root,
                    program="step5d_demo_v1",
                    readback_manifest=readback,
                    store=root / "artifact-store",
                    candidate_preflight=frozen,
                )
            self.assertEqual(
                json.loads((root / gate.CURRENT).read_text())["state"],
                "controller_verified",
            )

    def test_full_suite_must_be_exact_false_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(WorkflowStateError, "impacted validation"):
                self._candidate(Path(td), validation_updates={"full_suite": "false"})

    def test_high_risk_runs_at_most_one_settled_review_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = self._candidate(root, high=True)
            readback = self._readback(root, candidate)
            with self.assertRaisesRegex(WorkflowStateError, "requires one settled"):
                self._promote(
                    root=root,
                    program="step5d_demo_v1",
                    readback_manifest=readback,
                    store=root / "artifact-store",
                )
            self._promote(
                root=root,
                program="step5d_demo_v1",
                readback_manifest=readback,
                store=root / "artifact-store",
                review_status="skipped_unavailable_1+0",
            )
            manifest = json.loads(
                (root / "config/step5d/manifests/step5d_demo_v1/controller_verification.json").read_text()
            )
            self.assertEqual(manifest["review_status"], "skipped_unavailable_1+0")

    def test_reuse_claim_mismatch_or_byte_tamper_cannot_promote(self) -> None:
        for mutation in (
            "reuse", "mismatch", "byte_tamper", "candidate_tamper",
            "wrong_program", "wrong_target",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                candidate = self._candidate(root)
                readback = self._readback(root, candidate)
                payload = json.loads(readback.read_text())
                if mutation == "reuse":
                    payload["readback_source"] = "prior_full_readback"
                    payload["delivery_mode"] = "content_addressed_reuse"
                else:
                    if mutation == "mismatch":
                        payload["sha256"]["readback"][".txt"] = "0" * 64
                    elif mutation == "byte_tamper":
                        (readback.parent / "step5d_demo_v1.script").write_bytes(b"tampered")
                    elif mutation == "wrong_program":
                        payload["validation"]["program"] = "different-program"
                    elif mutation == "wrong_target":
                        payload["target_dir"] = "/programs/different"
                    else:
                        candidate_payload = json.loads(candidate.read_text())
                        package_source = Path(candidate_payload["package_source"])
                        (package_source / "step5d_demo_v1.script").write_bytes(b"tampered")
                write_json(readback, payload)
                with self.assertRaises(WorkflowStateError):
                    self._promote(
                        root=root,
                        program="step5d_demo_v1",
                        readback_manifest=readback,
                        store=root / "artifact-store",
                    )


if __name__ == "__main__":
    unittest.main()
