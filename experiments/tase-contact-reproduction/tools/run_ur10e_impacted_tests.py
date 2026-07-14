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


def _command_identity(command: list[str], *, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
    }


def environment_binding(*, python: Path, root: Path, dependency_map: Path) -> dict[str, Any]:
    """Bind cache reuse to the interpreter, dependencies, GPU and fixture closure."""
    mapping = json.loads(dependency_map.read_text(encoding="utf-8"))
    patterns = mapping.get("cache_external_fixture_globs")
    fixture_declaration_present = isinstance(patterns, list) and all(
        isinstance(pattern, str) and pattern for pattern in patterns
    )
    fixture_closure: dict[str, list[dict[str, Any]]] = {}
    for pattern in patterns if fixture_declaration_present else []:
        rows = []
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                rows.append({
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": path.stat().st_size,
                })
        fixture_closure[pattern] = rows

    resolved_python = python.resolve()
    # importlib.metadata raises for absent optional packages, so use a small explicit loop.
    probe = (
        "import importlib.metadata as m,json,sys\n"
        "versions={}\n"
        "for n in ('pytest','pytest-xdist','cupy','cupy-cuda11x','cupy-cuda12x','numpy'):\n"
        "  try: versions[n]=m.version(n)\n"
        "  except m.PackageNotFoundError: versions[n]=None\n"
        "print(json.dumps({'executable':sys.executable,'version':sys.version,'packages':versions},sort_keys=True))\n"
    )
    python_probe = _command_identity([str(resolved_python), "-c", probe], cwd=root)
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,name,driver_version,memory.total",
             "--format=csv,noheader,nounits"],
            cwd=root, text=True, capture_output=True, check=False, timeout=5.0,
        )
        gpu_identity = {
            "exit_code": gpu.returncode,
            "stdout": gpu.stdout.strip(),
            "stderr_sha256": hashlib.sha256(gpu.stderr.encode()).hexdigest(),
        }
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        gpu_identity = {"exit_code": None, "unavailable": type(exc).__name__}
    return {
        "schema_version": "ur10e_validation_environment_binding_v1",
        "python": {
            "requested": str(python),
            "resolved": str(resolved_python),
            "sha256": hashlib.sha256(resolved_python.read_bytes()).hexdigest(),
            "probe": python_probe,
        },
        "git_head": _command_identity(["git", "rev-parse", "HEAD"], cwd=root),
        "git_tree": _command_identity(["git", "rev-parse", "HEAD^{tree}"], cwd=root),
        "gpu": gpu_identity,
        "gpu_environment": {
            key: os.environ.get(key) for key in ("CUDA_VISIBLE_DEVICES", "UR10E_RNN_GPU_DEVICE")
        },
        "fixture_declaration_present": fixture_declaration_present,
        "external_fixture_closure": fixture_closure,
    }


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
        "UR10E_TEST_PYTHON": str(python.resolve()),
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
    environment = environment_binding(
        python=Path(os.path.abspath(args.python)), root=root,
        dependency_map=args.dependency_map.resolve(),
    )
    cache_reuse_eligible = environment["fixture_declaration_present"] is True
    composite = digest({
        "source_fingerprint": selection["source_fingerprint"],
        "decision_digest": decision["decision_digest"],
        "current_stage_sha256": decision["source_bindings"]["current_stage_sha256"],
        "stage_table_sha256": decision["source_bindings"]["stage_table_sha256"],
        "execution_scope": "full" if args.full_suite else "impacted",
        "environment_binding": environment,
    })
    cache = args.cache_root.resolve() / composite / args.execution
    if cache_reuse_eligible and not args.no_reuse and (cache / "validation_manifest.json").is_file():
        cached = json.loads((cache / "validation_manifest.json").read_text())
        if (cached.get("passed") is True
                and cached.get("composite_fingerprint") == composite
                and cached.get("environment_binding") == environment
                and cached.get("cache_reuse_eligible") is True):
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
        "environment_binding": environment,
        "cache_reuse_eligible": cache_reuse_eligible,
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
    if passed and cache_reuse_eligible:
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            shutil.rmtree(cache)
        shutil.copytree(output, cache)
    print(manifest_path)
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
