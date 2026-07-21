"""Resolve repository-owned Python dependencies of the production bridge path."""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath
from typing import Iterable


PRODUCTION_EXPERIMENT_SEEDS = frozenset(
    {
        "scripts/step5d-autotune-v3.sh",
        "tools/build_step5d_autotune_tp_v3.py",
        "tools/preflight_step5d_autotune_v3.py",
        "tools/run_step5d_autotune_campaign.py",
        "tools/run_step5d_autotune_v3_bridge.py",
        "tools/run_step5d_autotune_v3_live.py",
        "tools/run_step5d_autotune_v3_qualification.py",
        "tools/run_step5d_autotune_v3_tp_transaction.py",
        "tools/step5d_autotune_v3/cli.py",
    }
)
PRODUCTION_REPOSITORY_ASSETS = frozenset(
    {
        "experiments/archive/legacy/tase-mujoco-reproduction-2026-05-23/"
        "assets/mjcf/ur10e_nominal.xml",
    }
)


def _module_name(path: Path, experiment_root: Path, repository_root: Path) -> str | None:
    roots = (
        experiment_root / "tools",
        repository_root / "experiments/sensor-integration/kunwei-kwr75b/tools",
        repository_root / "src/ur10e_experiment_runtime",
    )
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)
    return None


def _module_files(module: str, search_roots: Iterable[Path]) -> set[Path]:
    if not module:
        return set()
    parts = module.split(".")
    found: set[Path] = set()
    for root in search_roots:
        leaf = root.joinpath(*parts)
        candidates = (leaf.with_suffix(".py"), leaf / "__init__.py")
        for candidate in candidates:
            if candidate.is_file() and not candidate.is_symlink():
                found.add(candidate.resolve(strict=True))
        for index in range(1, len(parts)):
            package_init = root.joinpath(*parts[:index], "__init__.py")
            if package_init.is_file() and not package_init.is_symlink():
                found.add(package_init.resolve(strict=True))
    return found


def _imports(
    path: Path,
    *,
    experiment_root: Path,
    repository_root: Path,
    search_roots: tuple[Path, ...],
) -> set[Path]:
    tree = ast.parse(path.read_bytes(), filename=str(path))
    current_module = _module_name(path, experiment_root, repository_root)
    current_package = ""
    if current_module:
        current_package = (
            current_module
            if path.name == "__init__.py"
            else current_module.rpartition(".")[0]
        )
    result: set[Path] = set()
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package_parts = current_package.split(".") if current_package else []
                keep = len(package_parts) - (node.level - 1)
                if keep < 0:
                    continue
                prefix = package_parts[:keep]
                base_parts = [*prefix, *(node.module or "").split(".")]
                base = ".".join(part for part in base_parts if part)
            else:
                base = node.module or ""
            if base:
                modules.append(base)
            modules.extend(
                ".".join(part for part in (base, alias.name) if part)
                for alias in node.names
                if alias.name != "*"
            )
        for module in modules:
            result.update(_module_files(module, search_roots))
    return result


def production_source_closure(
    experiment_root: Path,
) -> tuple[frozenset[str], frozenset[str]]:
    experiment = experiment_root.resolve(strict=True)
    repository = experiment.parents[1]
    search_roots = (
        experiment / "tools",
        repository / "experiments/sensor-integration/kunwei-kwr75b/tools",
        repository / "src/ur10e_experiment_runtime",
    )
    pending: list[Path] = []
    for relative in PRODUCTION_EXPERIMENT_SEEDS:
        path = experiment / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"production source seed is missing or unsafe: {relative}")
        pending.append(path.resolve(strict=True))
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        if path.suffix == ".py":
            pending.extend(
                _imports(
                    path,
                    experiment_root=experiment,
                    repository_root=repository,
                    search_roots=search_roots,
                )
                - visited
            )
    for relative in PRODUCTION_REPOSITORY_ASSETS:
        path = repository / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"production runtime asset is missing or unsafe: {relative}")
        visited.add(path.resolve(strict=True))
    experiment_paths: set[str] = set()
    repository_paths: set[str] = set()
    for path in visited:
        try:
            relative = path.relative_to(experiment)
        except ValueError:
            try:
                relative = path.relative_to(repository)
            except ValueError as exc:
                raise RuntimeError(f"production source escapes repository: {path}") from exc
            repository_paths.add(PurePosixPath(relative).as_posix())
        else:
            experiment_paths.add(PurePosixPath(relative).as_posix())
    return frozenset(experiment_paths), frozenset(repository_paths)


__all__ = [
    "PRODUCTION_EXPERIMENT_SEEDS",
    "PRODUCTION_REPOSITORY_ASSETS",
    "production_source_closure",
]
