#!/usr/bin/env python3
"""Validate Step5d import boundaries without arbitrary size/module limits."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
V3 = TOOLS / "step5d_autotune_v3"
SCHEMA = "step5d.autotune-v3/layer-boundary-report-v1"
SHARED_FILES = {
    V3 / "optimizer_wire.py",
    V3 / "public_state.py",
    V3 / "shared_contracts.py",
}
OPTIMIZER_FILES = {
    V3 / "optimizer_payloads.py",
    V3 / "optimizer_worker.py",
    TOOLS / "step5d_autotune_optimizer.py",
    TOOLS / "step5d_autotune_r008_policy.py",
}
ADAPTER_FILES = {V3 / "optimizer_protocol.py"}
DEPLOYMENT_FILES = {
    V3 / "runtime_environment.py",
    V3 / "runtime_installation.py",
}
ONLINE_FILES = (
    {
        path
        for path in V3.glob("*.py")
        if path
        not in SHARED_FILES | OPTIMIZER_FILES | ADAPTER_FILES | DEPLOYMENT_FILES
    }
    | {
        TOOLS / "run_step5d_autotune_v3_live.py",
        TOOLS / "step5d_bridge_status.py",
    }
)
LAYERS = {
    "shared": SHARED_FILES,
    "optimizer": OPTIMIZER_FILES,
    "adapter": ADAPTER_FILES,
    "deployment": DEPLOYMENT_FILES,
    "online": ONLINE_FILES,
}
SHARED_FORBIDDEN_IMPORTS = {
    "botorch",
    "cupy",
    "gpytorch",
    "os",
    "pathlib",
    "rclpy",
    "socket",
    "subprocess",
    "torch",
}
ONLINE_FORBIDDEN_IMPORTS = {"botorch", "gpytorch", "torch"}
OPTIMIZER_FORBIDDEN_IMPORT_PARTS = {
    "controller",
    "dashboard",
    "rclpy",
    "rtde",
    "sensor",
}
SHARED_FORBIDDEN_CALLS = {
    "__import__",
    "compile",
    "eval",
    "exec",
    "open",
}
ALLOWED_LAYER_EDGES = {
    "shared": set(),
    "optimizer": {"shared", "optimizer", "deployment"},
    "adapter": {"shared", "optimizer", "adapter", "deployment"},
    "deployment": {"shared", "deployment"},
    "online": {"shared", "optimizer", "adapter", "deployment", "online"},
}


class BoundaryError(RuntimeError):
    pass


def _module_name(path: Path) -> str:
    if path.parent == V3:
        return f"step5d_autotune_v3.{path.stem}"
    return path.stem


def _imports(tree: ast.AST, current: str) -> set[str]:
    result: set[str] = set()
    package = current.rsplit(".", 1)[0] if "." in current else ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                prefix_parts = package.split(".") if package else []
                keep = max(0, len(prefix_parts) - node.level + 1)
                prefix = ".".join(prefix_parts[:keep])
                module = ".".join(
                    value
                    for value in (prefix, node.module or "")
                    if value
                )
            else:
                module = node.module or ""
            if module:
                result.add(module)
    return result


def _call_names(tree: ast.AST) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            result.add(function.id)
        elif isinstance(function, ast.Attribute):
            result.add(function.attr)
    return result


def _layer_index() -> tuple[dict[Path, str], dict[str, Path]]:
    by_path: dict[Path, str] = {}
    by_module: dict[str, Path] = {}
    for layer, paths in LAYERS.items():
        for path in paths:
            resolved = path.resolve()
            if resolved in by_path:
                raise BoundaryError(f"file is assigned twice: {path}")
            by_path[resolved] = layer
            by_module[_module_name(path)] = resolved
    return by_path, by_module


def _match_local_module(
    imported: str,
    by_module: Mapping[str, Path],
) -> Path | None:
    matches = [
        (name, path)
        for name, path in by_module.items()
        if imported == name or imported.startswith(f"{name}.")
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item[0]))[1]


def _cycle(graph: Mapping[Path, set[Path]]) -> list[Path] | None:
    visiting: set[Path] = set()
    visited: set[Path] = set()
    stack: list[Path] = []

    def walk(node: Path) -> list[Path] | None:
        if node in visiting:
            index = stack.index(node)
            return stack[index:] + [node]
        if node in visited:
            return None
        visiting.add(node)
        stack.append(node)
        for target in sorted(graph.get(node, set())):
            found = walk(target)
            if found is not None:
                return found
        stack.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in sorted(graph):
        found = walk(node)
        if found is not None:
            return found
    return None


def validate(root: Path = ROOT) -> dict[str, Any]:
    if root.resolve() != ROOT.resolve():
        raise BoundaryError("validator must run against its repository root")
    by_path, by_module = _layer_index()
    violations: list[str] = []
    graph: dict[Path, set[Path]] = {path: set() for path in by_path}
    layer_graph: dict[str, set[str]] = {layer: set() for layer in LAYERS}
    metrics: dict[str, dict[str, int]] = {
        layer: {"modules": 0, "source_lines": 0} for layer in LAYERS
    }
    for path, layer in sorted(by_path.items(), key=lambda item: str(item[0])):
        if not path.is_file():
            violations.append(f"missing:{path.relative_to(ROOT)}")
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            violations.append(f"syntax:{path.relative_to(ROOT)}:{exc}")
            continue
        metrics[layer]["modules"] += 1
        metrics[layer]["source_lines"] += len(source.splitlines())
        imports = _imports(tree, _module_name(path))
        top_levels = {name.split(".", 1)[0] for name in imports}
        if layer == "shared":
            forbidden = sorted(top_levels & SHARED_FORBIDDEN_IMPORTS)
            if forbidden:
                violations.append(
                    f"shared_forbidden_import:{path.relative_to(ROOT)}:{','.join(forbidden)}"
                )
            project_imports = sorted(
                name
                for name in imports
                if name.startswith("step5d") or name.startswith("ur10e_")
            )
            if project_imports:
                violations.append(
                    f"shared_project_import:{path.relative_to(ROOT)}:{','.join(project_imports)}"
                )
            calls = sorted(_call_names(tree) & SHARED_FORBIDDEN_CALLS)
            if calls:
                violations.append(
                    f"shared_side_effect_call:{path.relative_to(ROOT)}:{','.join(calls)}"
                )
        if layer in {"online", "adapter"}:
            forbidden = sorted(top_levels & ONLINE_FORBIDDEN_IMPORTS)
            if forbidden:
                violations.append(
                    f"online_optimizer_import:{path.relative_to(ROOT)}:{','.join(forbidden)}"
                )
        if layer == "optimizer":
            forbidden = sorted(
                name
                for name in imports
                if any(
                    part in name.lower()
                    for part in OPTIMIZER_FORBIDDEN_IMPORT_PARTS
                )
            )
            if forbidden:
                violations.append(
                    f"optimizer_online_import:{path.relative_to(ROOT)}:{','.join(forbidden)}"
                )
        for imported in imports:
            target = _match_local_module(imported, by_module)
            if target is None or target == path:
                continue
            graph[path].add(target)
            target_layer = by_path[target]
            if target_layer != layer:
                layer_graph[layer].add(target_layer)
            if target_layer not in ALLOWED_LAYER_EDGES[layer]:
                violations.append(
                    "layer_backedge:"
                    f"{layer}:{path.relative_to(ROOT)}"
                    f"->{target_layer}:{target.relative_to(ROOT)}"
                )
    cycle = _cycle(layer_graph)
    if cycle is not None:
        violations.append("cross_layer_cycle:" + "->".join(cycle))
    return {
        "schema": SCHEMA,
        "ok": not violations,
        "policy": {
            "global_module_limit": None,
            "global_line_limit": None,
            "module_counts_are_report_only": True,
            "zero_cross_layer_cycles": True,
        },
        "metrics": metrics,
        "violations": violations,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = validate(args.root)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.write_text(encoded, encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
