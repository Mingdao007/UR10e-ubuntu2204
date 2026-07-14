from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from ur10e_impact_selector import changed_paths, select  # noqa: E402
from run_ur10e_impacted_tests import main as run_impacted  # noqa: E402


class Ur10eImpactSelectorTest(unittest.TestCase):
    def test_selects_impacted_plus_always_run_and_resource_groups(self) -> None:
        result = select(root=ROOT, paths=["tools/ur10e_parallel.py"])
        self.assertIn("tests/test_ur10e_parallel.py", result["selected_tests"])
        self.assertIn("tests/test_project_check_entrypoint.py", result["always_run_tests"])
        self.assertIn("tests/test_cross_step_parameter_table.py", result["serial_tests"])
        self.assertEqual(result["dependency_map_version"], "ur10e_test_dependency_map_v1")

    def test_unrelated_docs_run_only_cheap_invariants(self) -> None:
        result = select(root=ROOT, paths=["docs/offline-note.md"])
        self.assertEqual(result["selected_tests"], result["always_run_tests"])

    def test_changed_paths_include_untracked_files(self) -> None:
        paths = changed_paths(ROOT)
        self.assertIn("tools/ur10e_impact_selector.py", paths)
        self.assertIn("config/ur10e_test_dependency_map_v1.json", paths)

    def test_content_addressed_pass_is_reused_for_unchanged_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "tests").mkdir()
            (root / "tools").mkdir()
            (root / "tests/test_cheap.py").write_text("def test_ok():\n    assert True\n")
            (root / "tools/cheap_validator.py").write_text("raise SystemExit(0)\n")
            decisions = {"offline_only": True}
            current = {
                "current_stage_id": "offline",
                "workflow_state": "liveprep_blocked",
                "p0_v8_candidate": {
                    "canary_policy": {
                        "mode": "direct_single_duration",
                        "direct_duration_s": 60.0,
                    }
                },
            }
            table = {"stages": [{"id": "step5d_strict_rnn_no_contact_p0_v8", "duration_s": 60.0}]}
            dependency_map = {
                "schema_version": "test_map_v1",
                "always_run": ["tests/test_cheap.py"],
                "rules": [],
                "resource_groups": {},
                "validators": ["tools/cheap_validator.py"],
            }
            for path, payload in (
                ("config/ur10e_user_decisions_v1.json", decisions),
                ("config/current_stage.json", current),
                ("config/step5_stage_table.json", table),
                ("config/dependency.json", dependency_map),
            ):
                (root / path).write_text(json.dumps(payload) + "\n")
            cache = root / "cache"
            first = root / "first"
            second = root / "second"
            common = [
                "--root", str(root), "--python", sys.executable,
                "--dependency-map", str(root / "config/dependency.json"),
                "--cache-root", str(cache), "--execution", "serial",
                "--changed-path", "docs/note.md",
            ]
            self.assertEqual(run_impacted([*common, "--output-dir", str(first)]), 0)
            self.assertEqual(run_impacted([*common, "--output-dir", str(second)]), 0)
            first_manifest = json.loads((first / "validation_manifest.json").read_text())
            second_manifest = json.loads((second / "validation_manifest.json").read_text())
            self.assertFalse(first_manifest["reused"])
            self.assertTrue(second_manifest["reused"])
            self.assertEqual(first_manifest["composite_fingerprint"], second_manifest["composite_fingerprint"])
            self.assertEqual(first_manifest["selected_tests"], second_manifest["selected_tests"])


if __name__ == "__main__":
    unittest.main()
