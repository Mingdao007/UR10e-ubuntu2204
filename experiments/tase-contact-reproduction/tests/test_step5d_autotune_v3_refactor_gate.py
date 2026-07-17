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
        self.assertEqual(report["protected_source_count"], 22)
        self.assertEqual(report["approved_orchestration_variant_count"], 1)
        self.assertLessEqual(
            report["runtime_budget"]["module_count"],
            report["runtime_budget"]["module_limit"],
        )

    def test_complete_pr_declaration_is_accepted(self) -> None:
        self.assertEqual(gate.declaration_issues(VALID_DECLARATION), [])

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

    def test_matrix_has_all_lanes_and_fail_closed_realistic_policies(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        self.assertEqual(gate.matrix_issues(payload), [])
        payload["lanes"]["large_ursim"]["arm_allowed"] = True
        payload["lanes"]["hil_no_motion"]["serial"] = False
        payload["lanes"]["large_ursim"]["robot_network_allowed"] = True
        payload["lanes"]["large_ursim"]["dashboard_commands_allowed"].append("play")
        issues = gate.matrix_issues(payload)
        self.assertIn("test_matrix_realistic_lane_policy:large_ursim:arm_allowed", issues)
        self.assertIn("test_matrix_realistic_lane_policy:hil_no_motion:serial", issues)
        self.assertIn(
            "test_matrix_ursim_restricted_network_policy:robot_network_allowed",
            issues,
        )
        self.assertIn("test_matrix_ursim_dashboard_allowlist_drift", issues)

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

    def test_matrix_realistic_pass_requires_hash_bound_evidence(self) -> None:
        payload = json.loads(gate.DEFAULT_MATRIX.read_text(encoding="utf-8"))
        payload["lanes"]["large_ursim"]["immutable_result_sha256"] = "0" * 64
        payload["lanes"]["large_ursim"]["cleanup_completed"] = False
        payload["lanes"]["hil_no_motion"]["execution_status"] = "pass"
        issues = gate.matrix_issues(payload)
        self.assertIn(
            "test_matrix_realistic_lane_pass_evidence:large_ursim:immutable_result_sha256",
            issues,
        )
        self.assertIn(
            "test_matrix_realistic_lane_pass_evidence:large_ursim:cleanup_completed",
            issues,
        )
        self.assertIn("test_matrix_realistic_lane_false_pass:hil_no_motion", issues)

    def test_workflow_runs_only_explicit_hermetic_fast_tests(self) -> None:
        workflow = (GIT_ROOT / ".github" / "workflows" / "step5d-autotune-v3.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("tests/test_step5d_autotune_v3_contract.py", workflow)
        self.assertIn("tests/test_step5d_autotune_v3_refactor_gate.py", workflow)
        self.assertIn("tests/test_step5d_autotune_v3_service.py", workflow)
        self.assertIn("tests/test_step5d_autotune_v3_acceptance.py", workflow)
        self.assertIn("tests/test_step5d_autotune_recovery.py", workflow)
        self.assertIn("tests/test_step5d_autotune_journal.py", workflow)
        self.assertIn("tests/test_step5d_autotune_v3_attempt_ledger.py", workflow)
        self.assertIn("tests/test_step5d_autotune_v3_artifacts.py", workflow)
        self.assertIn("tests/test_step5d_autotune_v3_g10.py", workflow)
        self.assertIn("tests/test_step5d_autotune_v3_ursim_hold.py", workflow)
        self.assertIn("STEP5D_V3_HERMETIC_PARSER_CI", workflow)
        self.assertNotIn("docker run", workflow)
        self.assertNotIn("large_ursim", workflow)
        self.assertNotIn("hil_no_motion", workflow)


if __name__ == "__main__":
    unittest.main()
