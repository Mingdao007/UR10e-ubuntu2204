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


def _has_deadline_lifecycle(tree: ast.AST) -> bool:
    source_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    has_subprocess = any(
        isinstance(node, ast.Import) and any(alias.name == "subprocess" for alias in node.names)
        or isinstance(node, ast.ImportFrom) and node.module == "subprocess"
        for node in ast.walk(tree)
    )
    has_deadline = any(
        keyword.arg in {"timeout", "timeout_s"}
        for node in source_calls
        for keyword in node.keywords
    ) or any(
        isinstance(node, ast.Name) and node.id in {"deadline", "timeout", "timeout_s"}
        for node in ast.walk(tree)
    )
    return has_subprocess and has_deadline


def _line(node: ast.AST) -> int:
    return int(getattr(node, "lineno", 0))


def issues_for_file(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return [f"{path}:parse:{exc}"]
    aliases = _module_names(tree)
    guarded = _has_deadline_lifecycle(tree)
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
        finite_prepare_only = any(
            isinstance(argument, ast.Constant)
            and argument.value == "--prepare-only"
            for argument in ast.walk(node)
        )
        if target in REGISTERED_SUPERVISORS and not guarded and not finite_prepare_only:
            issues.append(
                f"{path}:{_line(node)}:direct_retry_supervisor_without_deadline:{target[0]}.{target[1]}"
            )
    if guarded:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "Popen" and not any(
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr in {
                        "wait", "communicate", "terminate", "kill", "_terminate"
                    }
                    for child in ast.walk(tree)
                ):
                    issues.append(f"{path}:{_line(node)}:subprocess_lifecycle_has_no_cleanup")
    return issues


def validate(root: Path, *, timeout_s: float = 2.0) -> list[str]:
    started = time.monotonic()
    issues: list[str] = []
    for path in sorted((root / "tests").glob("test_*.py")):
        issues.extend(issues_for_file(path))
        if time.monotonic() - started > timeout_s:
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
