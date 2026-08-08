"""Thin r005 adapter over the lineage-neutral managed control runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from step5d_managed_runtime import (
    CONTROL_PROFILE,
    MANIFEST_SCHEMA,
    ManagedControlBinding,
    ManagedRuntimeError,
    _pointer_row,
    _probe_control_interpreter,
    _regular_executable,
    launch_managed_module,
    resolve_managed_control_runtime,
)

from .contracts import load_contract


ROOT = Path(__file__).resolve().parents[2]
CONTROL_RUNTIME_SCHEMA = MANIFEST_SCHEMA
ControlRuntimeError = ManagedRuntimeError
ControlRuntimeBinding = ManagedControlBinding


def resolve_control_runtime(
    *,
    source_environment: Mapping[str, str] | None = None,
    pointer_loader: Callable[..., Mapping[str, Any]] | None = None,
    environment_builder: Callable[..., Mapping[str, str]] | None = None,
    dependency_probe: Callable[..., Mapping[str, Any]] | None = None,
    runtime_contract_loader: Callable[[], Mapping[str, Any]] | None = None,
) -> ControlRuntimeBinding:
    """Resolve r005's manifest-declared control lane through the V3 profile."""

    try:
        manifest = load_contract().runtime_manifest
        return resolve_managed_control_runtime(
            manifest,
            source_environment=source_environment,
            pointer_loader=pointer_loader,
            environment_builder=environment_builder,
            dependency_probe=dependency_probe,
            runtime_contract_loader=runtime_contract_loader,
        )
    except ManagedRuntimeError:
        raise
    except Exception as exc:
        raise ControlRuntimeError(
            "CONTROL_RUNTIME_INVALID",
            f"r005 managed control adapter failed closed: {exc}",
        ) from exc


def launch_control_module(
    module: str,
    args: Sequence[str] = (),
    *,
    binding: ControlRuntimeBinding | None = None,
) -> int:
    """Launch only a module allowlisted by the manifest's route table."""

    selected = resolve_control_runtime() if binding is None else binding
    return launch_managed_module(module, args, binding=selected)


__all__ = [
    "CONTROL_PROFILE",
    "CONTROL_RUNTIME_SCHEMA",
    "ControlRuntimeBinding",
    "ControlRuntimeError",
    "launch_control_module",
    "resolve_control_runtime",
]
