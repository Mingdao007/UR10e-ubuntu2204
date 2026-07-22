"""Offline-first orchestration primitives for Step5d autotune v3."""

from typing import Any

_LAUNCHER_EXPORTS = {
    "build_bridge_argv",
    "check_effective_config",
    "parse_effective_config",
    "validate_contract",
    "validate_effective_config",
    "validate_raw_argv",
}
_PROFILE_EXPORTS = {
    "ContractViolation",
    "control_fingerprint",
    "load_contract",
    "normalize_candidate",
    "validate_candidate",
}


def __getattr__(name: str) -> Any:
    if name in _PROFILE_EXPORTS:
        from . import profile

        return getattr(profile, name)
    if name in _LAUNCHER_EXPORTS:
        from . import launcher

        return getattr(launcher, name)
    raise AttributeError(name)

__all__ = [
    "ContractViolation",
    "build_bridge_argv",
    "check_effective_config",
    "control_fingerprint",
    "load_contract",
    "normalize_candidate",
    "parse_effective_config",
    "validate_candidate",
    "validate_contract",
    "validate_effective_config",
    "validate_raw_argv",
]
