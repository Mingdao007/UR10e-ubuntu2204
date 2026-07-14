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
FAIL_CLOSED_PREFIXES = ("tools/", "scripts/", "config/", "programs/", "tests/")
FAIL_CLOSED_ROOT_FILES = {"check.sh", "pytest.ini", "requirements-test.txt"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_lines(root: Path, args: list[str], *, check: bool = True) -> list[str]:
    completed = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False,
    )
    if check and completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed.stdout.splitlines() if completed.returncode == 0 else []


def resolve_comparison(root: Path, base_ref: str | None = None,
                       head_ref: str = "HEAD") -> dict[str, str]:
    if base_ref:
        bases = _git_lines(root, ["merge-base", base_ref, head_ref])
        if not bases:
            raise ValueError(f"cannot resolve merge-base for {base_ref} and {head_ref}")
        return {"base_ref": base_ref, "head_ref": head_ref, "merge_base": bases[0], "source": "explicit"}
    upstream = _git_lines(
        root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], check=False,
    )
    if upstream:
        bases = _git_lines(root, ["merge-base", upstream[0], head_ref])
        if bases:
            return {
                "base_ref": upstream[0], "head_ref": head_ref,
                "merge_base": bases[0], "source": "upstream_merge_base",
            }
    # A dirty worktree has an explicit immutable comparison boundary at HEAD.
    dirty = _git_lines(root, ["status", "--porcelain"], check=False)
    if dirty:
        head = _git_lines(root, ["rev-parse", head_ref])
        return {"base_ref": head_ref, "head_ref": head_ref, "merge_base": head[0], "source": "workspace_head"}
    raise ValueError(
        "impact selection needs --base-ref or an upstream merge-base; clean tree has no comparison scope"
    )


def changed_scope(root: Path, base_ref: str | None = None,
                  head_ref: str = "HEAD") -> tuple[list[str], dict[str, str]]:
    comparison = resolve_comparison(root, base_ref, head_ref)
    paths: set[str] = set()
    paths.update(_git_lines(root, ["diff", "--name-only", f"{comparison['merge_base']}...{head_ref}"]))
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
    normalized = sorted(path[len(prefix):] if path.startswith(prefix) else path for path in paths if path)
    if not normalized:
        raise ValueError("impact comparison resolved but contains no changed paths")
    return normalized, comparison


def changed_paths(root: Path, base_ref: str | None = None,
                  head_ref: str = "HEAD") -> list[str]:
    return changed_scope(root, base_ref, head_ref)[0]


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(path == pattern or fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _test_file(node: str) -> str:
    return node.split("::", 1)[0]


def select(*, root: Path, paths: list[str], dependency_map: Path = DEFAULT_MAP,
           full_suite: bool = False, comparison: dict[str, str] | None = None) -> dict[str, Any]:
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
    unmapped = sorted(set(paths) - matched_paths)
    unmapped_code = [
        path for path in unmapped
        if path in FAIL_CLOSED_ROOT_FILES or path.startswith(FAIL_CLOSED_PREFIXES)
    ]
    if unmapped_code and not full_suite:
        raise ValueError(
            "dependency map has unmapped code paths; add an explicit rule or use "
            f"--full-suite: {unmapped_code}"
        )
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
        "unmapped_changed_paths": unmapped,
        "dependency_hashes": file_hashes,
        "full_suite": full_suite,
        "comparison": comparison or {"source": "explicit_paths"},
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
        "unmapped_changed_paths": unmapped,
        "dependency_hashes": file_hashes,
        "parallel_tests": parallel,
        "serial_tests": serial,
        "resource_groups": {test: grouped[test] for test in sorted(grouped)},
        "validators": mapping["validators"],
        "source_fingerprint": fingerprint,
        "full_suite": full_suite,
        "comparison": comparison or {"source": "explicit_paths"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dependency-map", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--base-ref")
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument("--changed-paths-file", type=Path)
    parser.add_argument("--full-suite", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    paths = list(args.changed_path)
    if args.changed_paths_file:
        paths.extend(line.strip() for line in args.changed_paths_file.read_text().splitlines() if line.strip())
    comparison = {"source": "explicit_paths"}
    if not paths:
        paths, comparison = changed_scope(args.root.resolve(), args.base_ref, args.head_ref)
    payload = select(root=args.root.resolve(), paths=sorted(set(paths)),
                     dependency_map=args.dependency_map.resolve(), full_suite=args.full_suite,
                     comparison=comparison)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
