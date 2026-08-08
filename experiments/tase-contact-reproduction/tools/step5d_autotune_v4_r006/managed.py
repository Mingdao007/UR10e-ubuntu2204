"""Managed optimizer/runtime and Remote startup read-only seams."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from step5d_managed_runtime import ManagedRuntimeManifest, load_runtime_manifest

from .contracts import ROOT, RUNTIME_MANIFEST_PATH
from .optimizer import ManagedOptimizer, ManagedOptimizerBinding


@dataclass(frozen=True)
class RemoteStartupReceipt:
    """Offline receipt shape; constructing it performs no Remote action."""

    route: str = "Remote"
    live_action_performed: bool = False
    controller_connected: bool = False
    dashboard_action_performed: bool = False


@dataclass(frozen=True)
class R006ManagedRuntime:
    manifest: ManagedRuntimeManifest
    optimizer: ManagedOptimizerBinding
    remote_startup: RemoteStartupReceipt


def resolve_managed_runtime(
    *,
    manifest_path: Path = RUNTIME_MANIFEST_PATH,
    optimizer_resolver=None,
) -> R006ManagedRuntime:
    manifest = load_runtime_manifest(manifest_path, root=ROOT)
    binding = ManagedOptimizer(optimizer_resolver).resolve()
    return R006ManagedRuntime(manifest, binding, RemoteStartupReceipt())


__all__ = ["R006ManagedRuntime", "RemoteStartupReceipt", "resolve_managed_runtime"]
