#!/usr/bin/env python3
"""Select impacted UR10e tests and content-address their dependencies."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = ROOT / "config/ur10e_test_dependency_map_v1.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def changed_paths(root: Path, base_ref: str | None = None) -> list[str]:
    paths: set[str] = set()
    if base_ref:
        completed = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"], cwd=root,
            text=True, capture_output=True, check=False,
        )
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or f"cannot diff {base_ref}")
        paths.update(completed.stdout.splitlines())
    for args in (["git", "diff", "--name-only"], ["git", "diff", "--cached", "--name-only"]):
        completed = subprocess.run(args, cwd=root, text=True, capture_output=True, check=True)
        paths.update(completed.stdout.splitlines())
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    paths.update(untracked.stdout.splitlines())
    prefix = "experiments/tase-contact-reproduction/"
    return sorted(path[len(prefix):] if path.startswith(prefix) else path for path in paths if path)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(path == pattern or fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _test_file(node: str) -> str:
    return node.split("::", 1)[0]


def select(*, root: Path, paths: list[str], dependency_map: Path = DEFAULT_MAP,
           full_suite: bool = False) -> dict[str, Any]:
    mapping = json.loads(dependency_map.read_text(encoding="utf-8"))
    tests = set(mapping["always_run"])
    all_tests = sorted(
        path.relative_to(root).as_posix() for path in (root / "tests").glob("test_*.py")
    )
    matched_paths: set[str] = set()
    if full_suite:
        tests.update(all_tests)
    else:
        for path in paths:
            if path.startswith("tests/test_") and path.endswith(".py"):
                tests.add(path)
                matched_paths.add(path)
        for rule in mapping["rules"]:
            matched = {path for path in paths if _matches(path, rule["paths"])}
            if matched:
                matched_paths.update(matched)
                tests.update(rule["tests"])
    missing = sorted(test for test in tests if not (root / _test_file(test)).is_file())
    if missing:
        raise ValueError(f"dependency map selected missing tests: {missing}")
    grouped: dict[str, str] = {}
    for group, patterns in mapping.get("resource_groups", {}).items():
        for test in tests:
            if _matches(_test_file(test), patterns):
                grouped[test] = group
    selected = sorted(tests)
    parallel = [test for test in selected if test not in grouped]
    serial = [test for test in selected if test in grouped]
    dependency_files = sorted(
        set([_test_file(test) for test in selected] + mapping["validators"] + paths)
    )
    file_hashes = {
        path: sha(root / path)
        for path in dependency_files
        if (root / path).is_file()
    }
    fingerprint_payload = {
        "dependency_map_sha256": sha(dependency_map),
        "changed_paths": sorted(paths),
        "selected_tests": selected,
        "skipped_tests": sorted(set(all_tests) - {_test_file(test) for test in selected}),
        "matched_changed_paths": sorted(matched_paths),
        "unmapped_changed_paths": sorted(set(paths) - matched_paths),
        "dependency_hashes": file_hashes,
        "full_suite": full_suite,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": "ur10e_test_selection_v1",
        "dependency_map_version": mapping["schema_version"],
        "dependency_map_sha256": sha(dependency_map),
        "changed_paths": sorted(paths),
        "always_run_tests": sorted(mapping["always_run"]),
        "selected_tests": selected,
        "skipped_tests": sorted(set(all_tests) - {_test_file(test) for test in selected}),
        "matched_changed_paths": sorted(matched_paths),
        "unmapped_changed_paths": sorted(set(paths) - matched_paths),
        "parallel_tests": parallel,
        "serial_tests": serial,
        "resource_groups": {test: grouped[test] for test in sorted(grouped)},
        "validators": mapping["validators"],
        "source_fingerprint": fingerprint,
        "full_suite": full_suite,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dependency-map", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--base-ref")
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument("--changed-paths-file", type=Path)
    parser.add_argument("--full-suite", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    paths = list(args.changed_path)
    if args.changed_paths_file:
        paths.extend(line.strip() for line in args.changed_paths_file.read_text().splitlines() if line.strip())
    if not paths:
        paths = changed_paths(args.root.resolve(), args.base_ref)
    payload = select(root=args.root.resolve(), paths=sorted(set(paths)),
                     dependency_map=args.dependency_map.resolve(), full_suite=args.full_suite)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
