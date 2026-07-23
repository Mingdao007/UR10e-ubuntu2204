"""Minimal pure-logic package for Step5d remote control."""

import importlib

from .core import (
    RemoteControlResult,
    RemoteControlState,
    RemoteControlStateMachine,
    RemotePhase,
    ReactionNormalFilter,
    PreloadGate,
    apply_linear_caps,
    compute_outer_force_terms,
    cycloid_xy_reference,
    expect_exact_keys,
    joint_omega_bounds,
    load_r012_config,
    normalize_vector,
    rotate_toward,
)

__all__ = [
    "core",
    "engine",
    "primitives",
    "RemoteControlResult",
    "RemoteControlState",
    "RemoteControlStateMachine",
    "RemotePhase",
    "ReactionNormalFilter",
    "PreloadGate",
    "apply_linear_caps",
    "compute_outer_force_terms",
    "cycloid_xy_reference",
    "expect_exact_keys",
    "joint_omega_bounds",
    "load_r012_config",
    "normalize_vector",
    "rotate_toward",
]


def __getattr__(name: str):
    if name in {"core", "engine", "primitives"}:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | {"core", "engine", "primitives"})
