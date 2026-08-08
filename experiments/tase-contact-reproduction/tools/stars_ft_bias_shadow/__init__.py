"""Offline STARS-lite F/T bias shadow (read-only replay; never tare / never live)."""

from __future__ import annotations

from .contact_gate import GateDecision, GateMode, decide_gate
from .estimator import GatedEmaBiasEstimator
from .replay import (
    ShadowReplayResult,
    cold_read_validate,
    replay_run_dir,
    validate_cold_read,
    validate_completion_receipt,
)

__all__ = [
    "GateDecision",
    "GateMode",
    "GatedEmaBiasEstimator",
    "ShadowReplayResult",
    "decide_gate",
    "replay_run_dir",
    "validate_cold_read",
    "validate_completion_receipt",
    "cold_read_validate",
]

__version__ = "0.2.0-hardened"
