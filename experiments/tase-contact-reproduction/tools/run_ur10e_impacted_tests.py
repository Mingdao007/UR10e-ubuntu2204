#!/usr/bin/env python3
"""Run one changed-file-aware UR10e validation scope with safe result reuse."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ur10e_decision_manifest import build_snapshot
from ur10e_impact_selector import DEFAULT_MAP, ROOT, changed_paths, select


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run_command(name: str, command: list[str], *, root: Path, env: dict[str, str],
                output: Path) -> dict[str, Any]:
    started_at, started = now(), time.monotonic()
    completed = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, check=False)
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{name}.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (output / f"{name}.stderr.log").write_text(completed.stderr, encoding="utf-8")
    return {
        "name": name,
        "command": command,
        "started_at": started_at,
        "ended_at": now(),
        "elapsed_s": time.monotonic() - started,
        "exit_code": completed.returncode,
        "status": "passed" if completed.returncode == 0 else "failed",
    }


def pytest_command(python: Path, tests: list[str], *, workers: int, parallel: bool) -> list[str]:
    command = [str(python), "-m", "pytest", "-q", *tests]
    if parallel and len(tests) >= 8 and workers > 1:
        command.extend(["-p", "xdist.plugin", "-n", str(workers), "--dist", "loadgroup"])
    return command


def execute(selection: dict[str, Any], *, python: Path, root: Path, output: Path,
            mode: str, workers: int) -> list[dict[str, Any]]:
    env = os.environ.copy()
    env.update({
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    })
    validators = [
        (Path(path).stem, [str(python), path]) for path in selection["validators"]
    ]
    parallel_tests = selection["parallel_tests"]
    resource_tests: dict[str, list[str]] = {}
    for test in selection["serial_tests"]:
        resource_tests.setdefault(selection["resource_groups"][test], []).append(test)
    results: list[dict[str, Any]] = []
    resource_jobs = [
        (
            f"pytest_resource_{group}",
            pytest_command(python, tests, workers=1, parallel=False),
        )
        for group, tests in sorted(resource_tests.items())
    ]
    if mode == "serial":
        for name, command in validators:
            results.append(run_command(name, command, root=root, env=env, output=output))
        if parallel_tests:
            results.append(run_command(
                "pytest_parallel_scope_serial", pytest_command(python, parallel_tests, workers=1, parallel=False),
                root=root, env=env, output=output,
            ))
        for name, command in resource_jobs:
            results.append(run_command(name, command, root=root, env=env, output=output))
    else:
        jobs = validators[:]
        if parallel_tests:
            jobs.append((
                "pytest_parallel_scope",
                pytest_command(python, parallel_tests, workers=workers, parallel=True),
            ))
        # Each resource group is internally serial, while independent CPU/GPU/lock
        # groups can overlap with the pure-CPU fan-out.
        jobs.extend(resource_jobs)
        with ThreadPoolExecutor(max_workers=len(jobs) or 1) as pool:
            futures = [
                pool.submit(run_command, name, command, root=root, env=env, output=output)
                for name, command in jobs
            ]
            results.extend(future.result() for future in futures)
    return sorted(results, key=lambda row: row["name"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--dependency-map", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--base-ref")
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument("--changed-paths-file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, default=Path("/tmp/ur10e-test-evidence-cache"))
    parser.add_argument("--execution", choices=("serial", "dag"), default="dag")
    parser.add_argument("--workers", type=int, default=14)
    parser.add_argument("--full-suite", action="store_true")
    parser.add_argument("--no-reuse", action="store_true")
    args = parser.parse_args(argv)
    root, output = args.root.resolve(), args.output_dir.resolve()
    if output.exists():
        parser.error(f"output already exists: {output}")
    paths = list(args.changed_path)
    if args.changed_paths_file:
        paths.extend(line.strip() for line in args.changed_paths_file.read_text().splitlines() if line.strip())
    if not paths:
        paths = changed_paths(root, args.base_ref)
    selection = select(
        root=root, paths=sorted(set(paths)), dependency_map=args.dependency_map.resolve(),
        full_suite=args.full_suite,
    )
    decision = build_snapshot(root=root)
    composite = digest({
        "source_fingerprint": selection["source_fingerprint"],
        "decision_digest": decision["decision_digest"],
        "current_stage_sha256": decision["source_bindings"]["current_stage_sha256"],
        "stage_table_sha256": decision["source_bindings"]["stage_table_sha256"],
        "execution_scope": "full" if args.full_suite else "impacted",
    })
    cache = args.cache_root.resolve() / composite / args.execution
    if not args.no_reuse and (cache / "validation_manifest.json").is_file():
        cached = json.loads((cache / "validation_manifest.json").read_text())
        if cached.get("passed") is True and cached.get("composite_fingerprint") == composite:
            shutil.copytree(cache, output)
            manifest_path = output / "validation_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest.update({"reused": True, "reused_from": str(cache), "reused_at": now()})
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            print(manifest_path)
            return 0
    output.mkdir(parents=True)
    (output / "test_selection.json").write_text(json.dumps(selection, indent=2, sort_keys=True) + "\n")
    (output / "user_decision_manifest.json").write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n")
    started = time.monotonic()
    results = execute(
        selection, python=Path(os.path.abspath(args.python)), root=root, output=output,
        mode=args.execution, workers=max(1, args.workers),
    )
    passed = all(row["exit_code"] == 0 for row in results)
    manifest = {
        "schema_version": "ur10e_impacted_validation_manifest_v1",
        "execution": args.execution,
        "full_suite": args.full_suite,
        "dependency_map_version": selection["dependency_map_version"],
        "changed_paths": selection["changed_paths"],
        "selected_tests": selection["selected_tests"],
        "always_run_tests": selection["always_run_tests"],
        "serial_resource_tests": selection["serial_tests"],
        "source_fingerprint": selection["source_fingerprint"],
        "decision_digest": decision["decision_digest"],
        "composite_fingerprint": composite,
        "reused": False,
        "skipped_tests": selection["skipped_tests"],
        "matched_changed_paths": selection["matched_changed_paths"],
        "unmapped_changed_paths": selection["unmapped_changed_paths"],
        "results": results,
        "wall_time_s": time.monotonic() - started,
        "passed": passed,
        "claim_scope": "full_suite" if args.full_suite else "focused_impacted_validation",
    }
    manifest_path = output / "validation_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if passed:
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            shutil.rmtree(cache)
        shutil.copytree(output, cache)
    print(manifest_path)
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
