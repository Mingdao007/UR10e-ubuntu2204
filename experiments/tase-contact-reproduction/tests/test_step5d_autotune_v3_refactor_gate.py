from __future__ import annotations

import hashlib
import json
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
        self.assertEqual(report["protected_source_count"], 13)
        self.assertEqual(report["approved_orchestration_variant_count"], 10)
        self.assertLessEqual(
            report["runtime_budget"]["module_count"],
            report["runtime_budget"]["module_limit"],
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

    def test_runtime_budget_counts_every_flat_python_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "tools" / "step5d_autotune_v3"
            runtime.mkdir(parents=True)
            for index in range(gate.RUNTIME_MODULE_LIMIT + 1):
                (runtime / f"module_{index}.py").write_text("VALUE = 1\n", encoding="utf-8")
            (runtime / "escaped.py").symlink_to(runtime / "module_0.py")
            (runtime / "payload.bin").write_bytes(b"not budgeted")
            issues, report = gate.runtime_budget(root)
            self.assertEqual(report["module_count"], gate.RUNTIME_MODULE_LIMIT + 1)
            self.assertIn(
                f"v3_runtime_module_budget_exceeded:{gate.RUNTIME_MODULE_LIMIT + 1}>{gate.RUNTIME_MODULE_LIMIT}",
                issues,
            )
            self.assertTrue(any(item.startswith("v3_runtime_symlink_forbidden:") for item in issues))
            self.assertTrue(any(item.startswith("v3_runtime_unbudgeted_file:") for item in issues))

    def test_certification_owner_has_an_independent_bounded_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "tools" / "step5d_autotune_v3"
            runtime.mkdir(parents=True)
            certification = runtime / gate.CERTIFICATION_MODULE
            certification.write_text(
                "\n".join("VALUE = 1" for _ in range(gate.CERTIFICATION_LOC_LIMIT + 1)),
                encoding="utf-8",
            )
            issues, report = gate.runtime_budget(root)
            self.assertEqual(report["module_count"], 0)
            self.assertEqual(report["certification_module_count"], 1)
            self.assertIn(
                f"v3_certification_loc_budget_exceeded:"
                f"{gate.CERTIFICATION_LOC_LIMIT + 1}>{gate.CERTIFICATION_LOC_LIMIT}",
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

    def test_matrix_readiness_cannot_restore_authorization_or_skip_validation(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        readiness = payload["operator_readiness_gate"]
        readiness["user_confirmation_required"] = True
        readiness["user_authorization_required"] = True
        readiness["transition_order"].remove("deterministic_tests")
        readiness["ready_to_execute_requires"].remove("deterministic_tests_pass")
        issues = gate.matrix_issues(payload)
        self.assertIn("test_matrix_user_confirmation_not_disabled", issues)
        self.assertIn("test_matrix_user_authorization_not_disabled", issues)
        self.assertIn("test_matrix_readiness_transition_order_mismatch", issues)
        self.assertIn("test_matrix_ready_to_execute_requirements_mismatch", issues)

    def test_obsolete_hil_launch_permit_cannot_be_reintroduced(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        payload["hil_launch_permit_gate"] = {"scope": "hil_full_bridge_hold"}
        self.assertIn(
            "test_matrix_obsolete_hil_launch_permit_present",
            gate.matrix_issues(payload),
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
        self.assertIn("STEP5D_V3_HERMETIC_PARSER_CI", workflow)
        self.assertIn("python3 -m step5d_v3_parser_ci_stubs", workflow)
        self.assertNotIn("docker run", workflow)
        self.assertNotIn("large_ursim", workflow)
        self.assertNotIn("hil_no_motion", workflow)


if __name__ == "__main__":
    unittest.main()
