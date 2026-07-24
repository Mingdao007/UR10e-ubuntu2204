from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GIT_ROOT = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import validate_step5d_autotune_v3_refactor as gate  # noqa: E402


VALID_DECLARATION = f"""
## Change class

- [x] `behavior_preserving`
- [ ] `behavior_changing`
- Frozen baseline: `{gate.FROZEN_COMMIT}`

## Allowed deltas

- v3 orchestration and diagnostics only; frozen v1 control bytes remain unchanged.

## Rollback

```bash
git revert --no-edit deadbeef
```

## Validation commands

```bash
python3 tools/validate_step5d_autotune_v3_refactor.py --repository-only
python3 -m pytest -q tests/test_step5d_autotune_v3_refactor_gate.py
```
"""


class Step5dAutotuneV3RefactorGateTest(unittest.TestCase):
    def test_repository_contract_is_green(self) -> None:
        report = gate.validate_repository(ROOT)
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["protected_source_count"], 12)
        self.assertIn(
            "tools/step5d_autotune_v3/governance.py",
            report["runtime_surface"]["python_modules"],
        )
        self.assertEqual(
            report["active_surface_schema"],
            "step5d.autotune-v3/active-surface-v3",
        )

    def test_complete_pr_declaration_is_accepted(self) -> None:
        self.assertEqual(gate.declaration_issues(VALID_DECLARATION), [])

    def test_real_replace_language_is_not_a_template_placeholder(self) -> None:
        declaration = VALID_DECLARATION.replace(
            "v3 orchestration and diagnostics only; frozen v1 control bytes remain unchanged.",
            "Replace user-side HIL routing with a machine-bound startup gate.",
        )

        self.assertEqual(gate.declaration_issues(declaration), [])
        self.assertTrue(gate._placeholder("- TODO: describe the allowed delta"))
        self.assertTrue(gate._placeholder("git revert --no-edit <commit>"))

    def test_declaration_requires_one_class_and_exact_baseline(self) -> None:
        broken = VALID_DECLARATION.replace("[ ] `behavior_changing`", "[x] `behavior_changing`")
        broken = broken.replace(gate.FROZEN_COMMIT, "0" * 40)
        issues = gate.declaration_issues(broken)
        self.assertIn("declaration_change_class_count:2", issues)
        self.assertIn("declaration_frozen_baseline_mismatch", issues)

    def test_declaration_requires_delta_rollback_and_test_commands(self) -> None:
        skeleton = f"""
## Change class
- [x] `behavior_preserving`
- Frozen baseline: `{gate.FROZEN_COMMIT}`
## Allowed deltas
<!-- describe -->
## Rollback
```bash
# command
```
## Validation commands
```bash
# command
```
"""
        issues = gate.declaration_issues(skeleton)
        self.assertIn("declaration_allowed_deltas_missing", issues)
        self.assertIn("declaration_rollback_command_missing", issues)
        self.assertIn("declaration_test_command_missing", issues)

    def test_protected_source_gate_detects_worktree_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "v3 gate test"], cwd=repo, check=True)
            protected = repo / "protected.txt"
            protected.write_bytes(b"frozen\n")
            subprocess.run(["git", "add", "protected.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
            expected = {"protected.txt": hashlib.sha256(b"frozen\n").hexdigest()}
            self.assertEqual(
                gate.protected_source_issues(repo, baseline_ref="HEAD", expected=expected),
                [],
            )
            protected.write_bytes(b"drift\n")
            issues = gate.protected_source_issues(repo, baseline_ref="HEAD", expected=expected)
            self.assertTrue(any(item.startswith("protected_source_hash_mismatch:") for item in issues))
            self.assertIn("protected_source_not_zero_diff:protected.txt", issues)

    def test_runtime_surface_rejects_symlinks_non_python_files_and_nesting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "tools" / "step5d_autotune_v3"
            runtime.mkdir(parents=True)
            (runtime / "valid.py").write_text("VALUE = 1\n", encoding="utf-8")
            (runtime / "escaped.py").symlink_to(runtime / "valid.py")
            (runtime / "payload.bin").write_bytes(b"not source")
            nested = runtime / "nested"
            nested.mkdir()
            (nested / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

            issues, report = gate.runtime_surface_issues(root)

            self.assertEqual(
                report["python_modules"],
                ["tools/step5d_autotune_v3/valid.py"],
            )
            self.assertTrue(any(item.startswith("v3_runtime_symlink_forbidden:") for item in issues))
            self.assertTrue(
                any(item.startswith("v3_runtime_non_python_file_forbidden:") for item in issues)
            )
            self.assertTrue(
                any(item.startswith("v3_runtime_nested_module_forbidden:") for item in issues)
            )

    def test_active_surface_requires_v3_manifest_and_classifies_runtime_modules(self) -> None:
        payload = json.loads(gate.DEFAULT_ACTIVE_SURFACE.read_text(encoding="utf-8"))
        runtime_modules = set(gate.runtime_surface_issues(ROOT)[1]["python_modules"])
        self.assertEqual(
            gate.active_surface_issues(
                ROOT,
                payload,
                runtime_modules=runtime_modules,
            ),
            [],
        )

        payload["release_truth"]["manifest_schema"] = (
            "step5d.autotune-v3/release-manifest-v2"
        )
        payload["active_orchestration_paths"].remove(
            "tools/step5d_autotune_v3/runtime_environment.py"
        )
        issues = gate.active_surface_issues(
            ROOT,
            payload,
            runtime_modules=runtime_modules,
        )
        self.assertIn("active_surface_release_manifest_schema_mismatch", issues)
        self.assertIn(
            "active_surface_runtime_module_unclassified:"
            "tools/step5d_autotune_v3/runtime_environment.py",
            issues,
        )
        self.assertIn(
            "active_surface_required_runtime_not_active:"
            "tools/step5d_autotune_v3/runtime_environment.py",
            issues,
        )

    def test_matrix_has_only_hermetic_ci_lanes(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        self.assertEqual(gate.matrix_issues(payload), [])
        payload["lanes"]["small"]["network_allowed"] = True
        payload["lanes"]["medium"]["motion_allowed"] = True
        issues = gate.matrix_issues(payload)
        self.assertIn("test_matrix_ci_lane_not_hermetic:small:network_allowed", issues)
        self.assertIn("test_matrix_ci_lane_not_hermetic:medium:motion_allowed", issues)

    def test_b1_governance_separates_evidence_and_rejects_duplicate_test_paths(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        self.assertEqual(gate.content_governance_issues(ROOT, payload), [])
        duplicate = payload["lanes"]["small"]["commands"][0][-1]
        payload["lanes"]["medium"]["commands"][0].append(duplicate)
        self.assertIn(
            f"test_matrix_duplicate_test_path:{duplicate}:small:medium",
            gate.content_governance_issues(ROOT, payload),
        )

    def test_authoritative_bridge_gate_is_fail_closed_and_fully_classified(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        bridge = payload["authoritative_bridge_gate"]
        classified = bridge["classified_test_files"]
        bridge["canonical_launcher"] = "scripts/legacy.sh"
        bridge["unclassified_failure_policy"] = "ignore"
        bridge["acceptance_path"].remove("next_arm_acknowledged")
        classified["active"].remove("tests/test_step5d_autotune_v3_batch_producer.py")
        classified["active"].append("tests/test_step5d_uncommanded.py")
        classified["obsolete"].append("tests/test_step5d_runtime_gate.py")
        payload["lanes"]["small"]["commands"][0].append(
            "tests/test_failure_to_guard.py"
        )
        classified["unrelated"].remove("tests/test_failure_to_guard.py")

        issues = gate.matrix_issues(payload)
        self.assertIn(
            "test_matrix_authoritative_bridge_field_mismatch:canonical_launcher",
            issues,
        )
        self.assertIn(
            "test_matrix_authoritative_bridge_field_mismatch:unclassified_failure_policy",
            issues,
        )
        self.assertIn("test_matrix_authoritative_acceptance_path_mismatch", issues)
        self.assertIn("test_matrix_authoritative_active_set_mismatch", issues)
        self.assertIn("test_matrix_authoritative_obsolete_set_mismatch", issues)
        self.assertIn(
            "test_matrix_classification_overlap:active:obsolete:"
            "tests/test_step5d_runtime_gate.py",
            issues,
        )
        self.assertIn(
            "test_matrix_active_test_not_commanded:tests/test_step5d_uncommanded.py",
            issues,
        )
        self.assertIn(
            "test_matrix_commanded_test_unclassified:tests/test_failure_to_guard.py",
            issues,
        )
        self.assertIn(
            "test_matrix_obsolete_test_commanded:tests/test_step5d_runtime_gate.py",
            issues,
        )

    def test_authoritative_commands_exclude_classified_unrelated_tests(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        payload["lanes"]["small"]["commands"][0].append(
            "tests/test_failure_to_guard.py"
        )

        self.assertIn(
            "test_matrix_unrelated_test_commanded:tests/test_failure_to_guard.py",
            gate.matrix_issues(payload),
        )

    def test_repository_v3_test_discovery_blocks_unclassified_files(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            relevant = tests / "test_new_bridge_regression.py"
            relevant.write_text("STEP5D_V3 = True\n", encoding="utf-8")
            (tests / "test_unrelated.py").write_text("VALUE = 1\n", encoding="utf-8")

            issues = gate.matrix_issues(payload, root=root)

        self.assertIn(
            "test_matrix_repository_v3_test_unclassified:"
            "tests/test_new_bridge_regression.py",
            issues,
        )
        self.assertFalse(
            any("tests/test_unrelated.py" in issue for issue in issues),
            issues,
        )

    def test_obsolete_operator_and_hil_gates_cannot_be_reintroduced(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        payload["operator_readiness_gate"] = {}
        payload["hil_launch_permit_gate"] = {"scope": "hil_full_bridge_hold"}
        issues = gate.matrix_issues(payload)
        self.assertIn(
            "test_matrix_obsolete_operator_readiness_gate_present",
            issues,
        )
        self.assertIn(
            "test_matrix_obsolete_hil_launch_permit_present",
            issues,
        )

    def test_local_installed_runtime_gate_is_serial_and_never_hosted(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        installed = payload["local_installed_runtime_gate"]
        installed["ci"] = True
        installed["serial"] = False
        self.assertIn(
            "test_matrix_local_installed_runtime_gate_mismatch",
            gate.matrix_issues(payload),
        )

        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        payload["lanes"]["small"]["commands"][0].append(
            "tests/test_step5d_autotune_v3_installed_runtime.py"
        )
        self.assertIn(
            "test_matrix_installed_runtime_leaked_into_ci:small",
            gate.matrix_issues(payload),
        )

    def test_matrix_loader_requires_material_incident_fixtures(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            matrix = Path(directory) / "config" / "matrix.json"
            matrix.parent.mkdir()
            matrix.write_text(json.dumps(payload), encoding="utf-8")
            _, issues = gate.load_matrix(matrix)
        self.assertTrue(
            any(item.startswith("test_matrix_incident_fixture_unavailable:") for item in issues),
            issues,
        )

    def test_separate_evidence_requires_hash_binding_and_rejects_false_hil_pass(self) -> None:
        payload = json.loads(gate.DEFAULT_EVIDENCE.read_text(encoding="utf-8"))
        payload["lanes"]["large_ursim"]["result_sha256"] = "0" * 64
        payload["lanes"]["large_ursim"]["cleanup_completed"] = False
        payload["lanes"]["hil_no_motion"]["status"] = "pass"
        issues = gate.evidence_issues(payload, root=ROOT)
        self.assertIn(
            "test_evidence_pass_field:large_ursim:result_sha256",
            issues,
        )
        self.assertIn(
            "test_evidence_pass_field:large_ursim:cleanup_completed",
            issues,
        )
        self.assertIn("test_evidence_false_pass:hil_no_motion", issues)

    def test_workflow_runs_only_explicit_hermetic_fast_tests(self) -> None:
        workflow = (GIT_ROOT / ".github" / "workflows" / "step5d-autotune-v3.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("tools/run_step5d_autotune_v3_test_matrix.py", workflow)
        self.assertIn("--lanes small medium --workers auto", workflow)
        self.assertIn("uv==0.9.30", workflow)
        self.assertIn("uv sync --frozen --only-group test-hermetic", workflow)
        self.assertIn(".venv/bin/python", workflow)
        self.assertIn('"src/ur10e_experiment_runtime/**"', workflow)
        self.assertIn(
            '"experiments/tase-contact-reproduction/tools/step5d_autotune_*.py"',
            workflow,
        )
        self.assertIn(
            '"experiments/tase-contact-reproduction/tools/run_step5d_autotune_*.py"',
            workflow,
        )
        self.assertIn(
            '"experiments/tase-contact-reproduction/config/step5d/**"',
            workflow,
        )
        self.assertIn(
            '"experiments/tase-contact-reproduction/tests/test_step5d_autotune*.py"',
            workflow,
        )
        self.assertIn("tests/test_step5d_release_contract.py", workflow)
        self.assertIn("docker run --rm --network none --read-only", workflow)
        self.assertIn("dst=/workspace,readonly", workflow)
        self.assertNotIn("STEP5D_V3_HERMETIC_PARSER_CI", workflow)
        self.assertNotIn("step5d_v3_parser_ci_stubs", workflow)
        self.assertNotIn("sudo install", workflow)
        self.assertNotIn("/opt/ros/humble/share/ur_description", workflow)
        self.assertNotIn("large_ursim", workflow)
        self.assertNotIn("hil_no_motion", workflow)

    def test_repository_release_contract_probe_needs_no_private_skill(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m", "pytest", "-q",
                "tests/test_step5d_release_contract.py",
            ],
            cwd=ROOT,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": "tools:../../src/ur10e_experiment_runtime",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("3 passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
