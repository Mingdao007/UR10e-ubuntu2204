from __future__ import annotations

import sys
import json
import subprocess
import tempfile
import threading
import time
import unittest
import venv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from ur10e_impact_selector import changed_paths, select  # noqa: E402
from run_ur10e_impacted_tests import (  # noqa: E402
    atomic_publish_cache,
    cache_key_lock,
    environment_binding,
    execute,
    main as run_impacted,
)


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

    def test_unmapped_code_fails_closed_instead_of_running_only_invariants(self) -> None:
        with self.assertRaisesRegex(ValueError, "unmapped code paths"):
            select(root=ROOT, paths=["tools/not-yet-mapped-control.py"])

    def test_control_and_outer_loop_sources_have_explicit_impact_rules(self) -> None:
        control = select(root=ROOT, paths=["tools/step5d_control_contract.py"])
        outer = select(root=ROOT, paths=["tools/step5d_paper_outer_loop.py"])
        self.assertIn("tests/test_step5d_v30_control_contract.py", control["selected_tests"])
        self.assertIn("tests/test_step5d_paper_outer_loop.py", outer["selected_tests"])

    def test_autotune_edit_loop_uses_narrow_component_rules(self) -> None:
        supervisor = select(
            root=ROOT, paths=["tools/step5d_autotune_supervisor.py"]
        )
        builder = select(
            root=ROOT, paths=["tools/build_step5d_autotune_tp.py"]
        )
        runner = select(
            root=ROOT, paths=["tools/run_step5d_autotune_campaign.py"]
        )
        publisher = select(
            root=ROOT, paths=["tools/publish_step5d_autotune_plot.py"]
        )

        self.assertIn(
            "tests/test_step5d_autotune_supervisor.py",
            supervisor["selected_tests"],
        )
        self.assertNotIn(
            "tests/test_step5d_autotune_store.py", supervisor["selected_tests"]
        )
        self.assertIn("tests/test_step5d_autotune_tp.py", builder["selected_tests"])
        self.assertNotIn(
            "tests/test_step5d_autotune_journal.py", builder["selected_tests"]
        )
        self.assertIn(
            "tests/test_step5d_autotune_campaign_runner.py",
            runner["selected_tests"],
        )
        self.assertIn(
            "tests/test_publish_step5d_autotune_plot.py",
            publisher["selected_tests"],
        )

    def test_autotune_v2_sources_tests_and_tp_verifier_share_explicit_rule(self) -> None:
        v2_tests = {
            "tests/test_step5d_autotune_v2_core.py",
            "tests/test_step5d_autotune_v2_entrypoint.py",
            "tests/test_step5d_autotune_v2_hardening.py",
            "tests/test_step5d_autotune_v2_legacy.py",
            "tests/test_step5d_autotune_v2_report.py",
            "tests/test_step5d_autotune_v2_service.py",
            "tests/test_step5d_autotune_v2_supervisor.py",
            "tests/test_step5d_autotune_v2_tp_watchdog.py",
            "tests/test_step5d_autotune_v2_transport.py",
        }
        changed_paths = [
            "tools/step5d_autotune_v2/bridge.py",
            *sorted(v2_tests),
            "tools/verify_step5d_tp_watchdog_diff.py",
            "tools/generate_step5_docs.py",
            "config/step5/current.json",
            "config/step5/tp_watchdog_v2.json",
            "config/step5/tp_watchdog_blocks/host_heartbeat_fail_closed_v1.script",
        ]

        for changed_path in changed_paths:
            with self.subTest(changed_path=changed_path):
                result = select(root=ROOT, paths=[changed_path])
                self.assertEqual(result["unmapped_changed_paths"], [])
                self.assertTrue(v2_tests.issubset(result["selected_tests"]))

        self.assertEqual(result["exclusive_throughput_groups"], ["timing_sensitive"])
        self.assertEqual(
            result["resource_groups"]["tests/test_step5d_autotune_v2_hardening.py"],
            "timing_sensitive",
        )
        self.assertEqual(
            result["resource_groups"]["tests/test_step5d_autotune_v2_service.py"],
            "timing_sensitive",
        )

    def test_p0_v8_controller_readback_evidence_has_explicit_impact_rule(self) -> None:
        result = select(
            root=ROOT,
            paths=[
                "runs/controller_readback_step5d_strict_rnn_no_contact_p0_v8_20260714/manifest.json"
            ],
        )
        self.assertEqual(result["unmapped_changed_paths"], [])
        self.assertIn("tests/test_current_stage_readback_gate.py", result["selected_tests"])

    def test_changed_paths_include_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "UR10e test"], cwd=root, check=True)
            (root / "tracked.txt").write_text("tracked\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
            (root / "tools").mkdir()
            (root / "tools/untracked.py").write_text("# untracked\n")
            paths = changed_paths(root)
        self.assertEqual(paths, ["tools/untracked.py"])

    def test_clean_tree_without_base_or_upstream_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "UR10e test"], cwd=root, check=True)
            (root / "tracked.txt").write_text("tracked\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
            with self.assertRaisesRegex(ValueError, "needs --base-ref or an upstream"):
                changed_paths(root)

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
                "cache_external_fixture_globs": ["config/**/*.json"],
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
            third = root / "third"
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
            (root / "config/fixture.json").write_text('{"changed": true}\n')
            self.assertEqual(run_impacted([*common, "--output-dir", str(third)]), 0)
            third_manifest = json.loads((third / "validation_manifest.json").read_text())
            self.assertFalse(third_manifest["reused"])
            self.assertNotEqual(
                second_manifest["composite_fingerprint"], third_manifest["composite_fingerprint"]
            )

    def test_environment_binding_probes_lexical_venv_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dependency_map = root / "dependency.json"
            dependency_map.write_text(
                json.dumps({"cache_external_fixture_globs": []}) + "\n",
                encoding="utf-8",
            )
            venv_root = root / "validation-venv"
            venv.EnvBuilder(with_pip=False, system_site_packages=True).create(venv_root)
            invoked_python = venv_root / "bin/python"

            binding = environment_binding(
                python=invoked_python,
                root=root,
                dependency_map=dependency_map,
            )

            python_identity = binding["python"]
            runtime = python_identity["runtime"]
            self.assertEqual(python_identity["invoked"], str(invoked_python))
            self.assertEqual(
                python_identity["probe"]["command"][0], str(invoked_python)
            )
            self.assertEqual(runtime["executable"], str(invoked_python))
            self.assertEqual(runtime["prefix"], str(venv_root))
            self.assertEqual(
                Path(runtime["real_executable"]),
                Path(python_identity["resolved"]),
            )
            self.assertIsInstance(runtime["packages"], dict)

    def test_dag_runs_exclusive_throughput_group_after_parallel_jobs(self) -> None:
        selection = {
            "validators": ["tools/fixture_validator.py"],
            "parallel_tests": [f"tests/test_parallel_{index}.py" for index in range(8)],
            "serial_tests": ["tests/test_gpu.py", "tests/test_timing.py"],
            "resource_groups": {
                "tests/test_gpu.py": "gpu",
                "tests/test_timing.py": "timing_sensitive",
            },
            "exclusive_throughput_groups": ["timing_sensitive"],
        }
        intervals: dict[str, tuple[float, float]] = {}
        lock = threading.Lock()

        def fake_run_command(
            name: str,
            command: list[str],
            *,
            root: Path,
            env: dict[str, str],
            output: Path,
        ) -> dict[str, object]:
            del root, env, output
            started = time.monotonic()
            time.sleep(0.03)
            ended = time.monotonic()
            with lock:
                intervals[name] = (started, ended)
            return {
                "name": name,
                "command": command,
                "exit_code": 0,
                "status": "passed",
            }

        with tempfile.TemporaryDirectory() as td, mock.patch(
            "run_ur10e_impacted_tests.run_command", side_effect=fake_run_command
        ):
            results = execute(
                selection,
                python=Path(sys.executable),
                root=Path(td),
                output=Path(td) / "output",
                mode="dag",
                workers=4,
            )

        exclusive_name = "pytest_resource_timing_sensitive"
        nonexclusive_names = {
            "fixture_validator",
            "pytest_parallel_scope",
            "pytest_resource_gpu",
        }
        self.assertEqual({row["name"] for row in results}, nonexclusive_names | {exclusive_name})
        self.assertGreaterEqual(
            intervals[exclusive_name][0],
            max(intervals[name][1] for name in nonexclusive_names),
        )

    def test_tampered_cached_log_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "tests").mkdir()
            (root / "tools").mkdir()
            (root / "tests/test_cheap.py").write_text("def test_ok():\n    assert True\n")
            (root / "tools/cheap_validator.py").write_text("raise SystemExit(0)\n")
            payloads = {
                "config/ur10e_user_decisions_v1.json": {"offline_only": True},
                "config/current_stage.json": {
                    "current_stage_id": "offline",
                    "workflow_state": "blocked",
                    "p0_v8_candidate": {
                        "canary_policy": {
                            "mode": "direct_single_duration",
                            "direct_duration_s": 60.0,
                        }
                    },
                },
                "config/step5_stage_table.json": {
                    "stages": [
                        {
                            "id": "step5d_strict_rnn_no_contact_p0_v8",
                            "duration_s": 60.0,
                        }
                    ]
                },
                "config/dependency.json": {
                    "schema_version": "test_map_v1",
                    "always_run": ["tests/test_cheap.py"],
                    "rules": [],
                    "resource_groups": {},
                    "validators": ["tools/cheap_validator.py"],
                    "cache_external_fixture_globs": ["config/**/*.json"],
                },
            }
            for relative, payload in payloads.items():
                (root / relative).write_text(json.dumps(payload) + "\n")
            common = [
                "--root", str(root), "--python", sys.executable,
                "--dependency-map", str(root / "config/dependency.json"),
                "--cache-root", str(root / "cache"), "--execution", "serial",
                "--changed-path", "docs/note.md",
            ]
            first, second = root / "first", root / "second"
            third, fourth = root / "third", root / "fourth"
            self.assertEqual(run_impacted([*common, "--output-dir", str(first)]), 0)
            cache_manifest = next((root / "cache").rglob("validation_manifest.json"))
            (cache_manifest.parent / "cheap_validator.stdout.log").write_text("tampered\n")
            self.assertEqual(run_impacted([*common, "--output-dir", str(second)]), 0)
            manifest = json.loads((second / "validation_manifest.json").read_text())
            self.assertFalse(manifest["reused"])
            self.assertFalse(manifest["cache_reuse_acceptance_eligible"])

            cache_manifest = next((root / "cache").rglob("validation_manifest.json"))
            cached = json.loads(cache_manifest.read_text())
            cached["selected_tests"] = ["tampered/test.py"]
            cache_manifest.write_text(json.dumps(cached) + "\n")
            self.assertEqual(run_impacted([*common, "--output-dir", str(third)]), 0)
            self.assertFalse(json.loads((third / "validation_manifest.json").read_text())["reused"])

            cache_manifest = next((root / "cache").rglob("validation_manifest.json"))
            (cache_manifest.parent / "unexpected-extra.log").write_text("extra\n")
            self.assertEqual(run_impacted([*common, "--output-dir", str(fourth)]), 0)
            self.assertFalse(json.loads((fourth / "validation_manifest.json").read_text())["reused"])

    def test_per_key_lock_and_atomic_cache_publish_never_mix_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            (first / "identity.txt").write_text("first\n")
            (first / "first-only.txt").write_text("first\n")
            (second / "identity.txt").write_text("second\n")
            (second / "second-only.txt").write_text("second\n")
            cache = root / "cache" / ("a" * 64) / "serial"

            def publish(source: Path) -> None:
                with cache_key_lock(cache):
                    atomic_publish_cache(source, cache)

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(publish, (first, second)))
            identity = (cache / "identity.txt").read_text().strip()
            if identity == "first":
                self.assertTrue((cache / "first-only.txt").is_file())
                self.assertFalse((cache / "second-only.txt").exists())
            else:
                self.assertEqual(identity, "second")
                self.assertTrue((cache / "second-only.txt").is_file())
                self.assertFalse((cache / "first-only.txt").exists())
            self.assertEqual(list(cache.parent.glob(".serial.publish-*")), [])
            self.assertEqual(list(cache.parent.glob(".serial.stale-*")), [])


if __name__ == "__main__":
    unittest.main()
