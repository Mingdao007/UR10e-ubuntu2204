#!/usr/bin/env python3
"""Lightweight mailbox contract shared by experiment and optimizer sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from step5d_autotune_contract import TrialSpec


class FingerprintBinding(Protocol):
    source_fingerprint: str
    config_fingerprint: str
    composite_fingerprint: str


@dataclass(frozen=True)
class PreparedFingerprint:
    source_fingerprint: str
    config_fingerprint: str
    composite_fingerprint: str


@dataclass(frozen=True)
class PreparedTrial:
    trial: TrialSpec
    frozen: FingerprintBinding
    environment: Mapping[str, str]
    runner_arguments: tuple[str, ...]
    trial_overlay: Mapping[str, Any] | None = None
    trial_overlay_sha256: str | None = None
    batch_row_index: int | None = None
    occurrence_uid: str | None = None
    transport_candidate_uid: str | None = None
    control_candidate_uid: str | None = None
