#!/usr/bin/env python3
"""Deterministically guard tests against direct retry-forever supervisors."""

from __future__ import annotations

import argparse
import ast
import time
from pathlib import Path


REGISTERED_SUPERVISORS = frozenset(
    {
        ("run_step5d_autotune_v3_live", "run"),
        ("run_step5d_autotune_v3_live", "main"),
        ("run_step5d_autotune_campaign", "run"),
        ("run_step5d_autotune_campaign", "main"),
    }
)
SUBPROCESS_CLEANUP_METHODS = frozenset(
    {"wait", "communicate", "terminate", "kill", "_terminate"}
)


def _module_names(tree: ast.AST) -> dict[str, tuple[str, str | None]]:
    names: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {name for name, _ in REGISTERED_SUPERVISORS}:
                    names[alias.asname or alias.name] = (alias.name, None)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if (node.module, alias.name) in REGISTERED_SUPERVISORS:
                    names[alias.asname or alias.name] = (node.module, alias.name)
    return names


def _subprocess_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    modules: set[str] = set()
    popen_symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name == "Popen":
                    popen_symbols.add(alias.asname or alias.name)
    return modules, popen_symbols


def _is_popen_call(
    node: ast.AST, subprocess_modules: set[str], popen_symbols: set[str]
) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        return (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id in subprocess_modules
            and node.func.attr == "Popen"
        )
    return isinstance(node.func, ast.Name) and node.func.id in popen_symbols


def _assigned_popen_objects(tree: ast.AST) -> dict[str, ast.Call]:
    subprocess_modules, popen_symbols = _subprocess_names(tree)
    bindings: dict[str, ast.Call] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not _is_popen_call(node.value, subprocess_modules, popen_symbols):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = node.value
    return bindings


def _call_has_deadline(node: ast.Call) -> bool:
    return any(
        keyword.arg in {"deadline", "timeout", "timeout_s"}
        for keyword in node.keywords
    )


def _object_lifecycle_calls(tree: ast.AST, object_name: str) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            if (
                node.func.attr in SUBPROCESS_CLEANUP_METHODS
                and (
                    (
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id == object_name
                    )
                    or any(
                        isinstance(argument, ast.Name) and argument.id == object_name
                        for argument in node.args
                    )
                )
            ):
                calls.append(node)
        elif isinstance(node.func, ast.Name) and node.func.id in SUBPROCESS_CLEANUP_METHODS:
            if any(
                isinstance(argument, ast.Name) and argument.id == object_name
                for argument in node.args
            ):
                calls.append(node)
    return calls


def _has_deadline_lifecycle(tree: ast.AST) -> bool:
    source_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    has_subprocess = any(
        isinstance(node, ast.Import) and any(alias.name == "subprocess" for alias in node.names)
        or isinstance(node, ast.ImportFrom) and node.module == "subprocess"
        for node in ast.walk(tree)
    )
    has_deadline = any(
        keyword.arg in {"deadline", "timeout", "timeout_s"}
        for node in source_calls
        for keyword in node.keywords
    ) or any(
        isinstance(node, ast.Name) and node.id in {"deadline", "timeout", "timeout_s"}
        for node in ast.walk(tree)
    )
    return has_subprocess and has_deadline


def _line(node: ast.AST) -> int:
    return int(getattr(node, "lineno", 0))


def _explicit_single_session(node: ast.Call) -> bool:
    return any(
        isinstance(child, ast.Call)
        and any(
            keyword.arg == "single_session"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in child.keywords
        )
        for child in ast.walk(node)
    )


def issues_for_file(path: Path) -> list[str]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return [f"{path}:parse:{exc}"]
    if not (
        any(module_name in source for module_name, _ in REGISTERED_SUPERVISORS)
        or "subprocess" in source
    ):
        return []
    aliases = _module_names(tree)
    issues: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            module = node.func.value.id if isinstance(node.func.value, ast.Name) else None
            module_name = aliases.get(module, ("", None))[0] if module else ""
            target = (module_name, node.func.attr)
        elif isinstance(node.func, ast.Name):
            module_name, symbol = aliases.get(node.func.id, ("", None))
            target = (module_name, symbol or "")
        else:
            target = ("", "")
        if target not in REGISTERED_SUPERVISORS:
            continue
        finite_prepare_only = any(
            isinstance(argument, ast.Constant)
            and argument.value == "--prepare-only"
            for argument in ast.walk(node)
        )
        finite_single_session = _explicit_single_session(node)
        if (
            not finite_prepare_only
            and not finite_single_session
        ):
            issues.append(
                f"{path}:{_line(node)}:direct_retry_supervisor_without_deadline:{target[0]}.{target[1]}"
            )
    if _has_deadline_lifecycle(tree):
        for object_name, popen_call in _assigned_popen_objects(tree).items():
            lifecycle_calls = _object_lifecycle_calls(tree, object_name)
            if not lifecycle_calls:
                issues.append(
                    f"{path}:{_line(popen_call)}:subprocess_lifecycle_has_no_cleanup:{object_name}"
                )
            elif not any(_call_has_deadline(call) for call in lifecycle_calls):
                issues.append(
                    f"{path}:{_line(popen_call)}:subprocess_lifecycle_has_no_deadline:{object_name}"
                )
    return issues


def _budget_exceeded(
    started_cpu: float,
    started_wall: float,
    timeout_s: float,
) -> bool:
    cpu_elapsed = time.process_time() - started_cpu
    wall_elapsed = time.monotonic() - started_wall
    # CPU is authoritative; requiring both clocks to cross the boundary keeps
    # scheduler descheduling from consuming the AST scan budget.
    return cpu_elapsed > timeout_s and wall_elapsed > timeout_s


def validate(root: Path, *, timeout_s: float = 2.0) -> list[str]:
    started_cpu = time.process_time()
    started_wall = time.monotonic()
    issues: list[str] = []
    for path in sorted((root / "tests").glob("test_*.py")):
        issues.extend(issues_for_file(path))
        if _budget_exceeded(started_cpu, started_wall, timeout_s):
            return [f"AST supervisor validator exceeded {timeout_s:.3f}s"]
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    issues = validate(args.root.resolve())
    for issue in issues:
        print(issue)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
