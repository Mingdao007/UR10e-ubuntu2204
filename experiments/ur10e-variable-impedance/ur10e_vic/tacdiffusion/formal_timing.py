"""Formal-only model-rate evidence for TacDiffusion V4."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Iterable

from .contracts import FORMAL_MODEL_RATE_CANDIDATES_HZ, FORMAL_SAMPLER_STEPS


@dataclass(frozen=True)
class FormalModelTimingEvidence:
    """Timing evidence for the 50-step sampler at 100/50 Hz only."""

    rate_hz: int
    duration_s: float
    p99_latency_s: float
    deadline_misses: int
    nonfinite_outputs: int = 0
    sampler_steps: int = FORMAL_SAMPLER_STEPS
    paced_raw_ticks: bool = True

    def __post_init__(self) -> None:
        if self.rate_hz not in FORMAL_MODEL_RATE_CANDIDATES_HZ:
            raise ValueError("formal rate_hz must be exactly 100 or 50")
        if not math.isfinite(self.duration_s) or self.duration_s <= 0.0:
            raise ValueError("formal timing duration must be finite and positive")
        if not math.isfinite(self.p99_latency_s) or self.p99_latency_s < 0.0:
            raise ValueError("formal p99 latency must be finite and non-negative")
        if self.deadline_misses < 0 or self.nonfinite_outputs < 0:
            raise ValueError("formal timing failure counts must be non-negative")
        if self.sampler_steps != FORMAL_SAMPLER_STEPS or self.paced_raw_ticks is not True:
            raise ValueError("formal timing requires a paced 50-step raw-tick sampler")

    @property
    def period_s(self) -> float:
        return 1.0 / self.rate_hz

    @property
    def accepted(self) -> bool:
        return bool(
            self.deadline_misses == 0
            and self.nonfinite_outputs == 0
            and self.p99_latency_s <= 0.8 * self.period_s
        )

    def payload(self) -> dict[str, object]:
        return {
            "rate_hz": self.rate_hz,
            "duration_s": self.duration_s,
            "p99_latency_s": self.p99_latency_s,
            "deadline_misses": self.deadline_misses,
            "nonfinite_outputs": self.nonfinite_outputs,
            "sampler_steps": self.sampler_steps,
            "paced_raw_ticks": self.paced_raw_ticks,
            "period_s": self.period_s,
            "accepted": self.accepted,
        }


@dataclass(frozen=True)
class FormalModelRateSelection:
    selected_rate_hz: int | None
    evidence_sha256: str
    all_rates_tested: bool
    shadow_only: bool = True
    active_enabled: bool = False


def select_formal_model_rate(
    evidence: Iterable[FormalModelTimingEvidence],
) -> FormalModelRateSelection:
    """Select only from complete formal 100/50 Hz evidence."""

    by_rate: dict[int, FormalModelTimingEvidence] = {}
    for item in evidence:
        if not isinstance(item, FormalModelTimingEvidence):
            raise ValueError("formal selector requires FormalModelTimingEvidence")
        if item.rate_hz in by_rate:
            raise ValueError("formal timing evidence contains a duplicate rate")
        by_rate[item.rate_hz] = item
    ordered_payload = [
        by_rate[rate].payload()
        for rate in FORMAL_MODEL_RATE_CANDIDATES_HZ
        if rate in by_rate
    ]
    encoded = json.dumps(
        ordered_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    evidence_sha256 = hashlib.sha256(encoded).hexdigest()
    all_rates_tested = set(by_rate) == set(FORMAL_MODEL_RATE_CANDIDATES_HZ)
    selected = (
        next(
            (
                rate
                for rate in FORMAL_MODEL_RATE_CANDIDATES_HZ
                if rate in by_rate and by_rate[rate].accepted
            ),
            None,
        )
        if all_rates_tested
        else None
    )
    return FormalModelRateSelection(
        selected_rate_hz=selected,
        evidence_sha256=evidence_sha256,
        all_rates_tested=all_rates_tested,
    )


__all__ = [
    "FormalModelRateSelection",
    "FormalModelTimingEvidence",
    "select_formal_model_rate",
]
