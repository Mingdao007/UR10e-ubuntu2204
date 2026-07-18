"""Independent model-rate acceptance for the UR10e 500 Hz control loop."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Iterable

from .contracts import MODEL_RATE_CANDIDATES_HZ


@dataclass(frozen=True)
class ModelTimingEvidence:
    rate_hz: int
    duration_s: float
    p99_latency_s: float
    deadline_misses: int
    nonfinite_outputs: int = 0

    def __post_init__(self) -> None:
        if self.rate_hz not in MODEL_RATE_CANDIDATES_HZ:
            raise ValueError("rate_hz must be 50, 100, 200, or 500")
        if not math.isfinite(self.duration_s) or self.duration_s < 60.0:
            raise ValueError("timing evidence must cover at least 60 seconds")
        if not math.isfinite(self.p99_latency_s) or self.p99_latency_s < 0.0:
            raise ValueError("p99_latency_s must be finite and non-negative")
        if self.deadline_misses < 0 or self.nonfinite_outputs < 0:
            raise ValueError("timing failure counts must be non-negative")

    @property
    def period_s(self) -> float:
        return 1.0 / self.rate_hz

    @property
    def accepted(self) -> bool:
        return (
            self.deadline_misses == 0
            and self.nonfinite_outputs == 0
            and self.p99_latency_s <= 0.8 * self.period_s
        )

    def payload(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "deadline_misses": self.deadline_misses,
            "duration_s": self.duration_s,
            "nonfinite_outputs": self.nonfinite_outputs,
            "p99_latency_s": self.p99_latency_s,
            "period_s": self.period_s,
            "rate_hz": self.rate_hz,
        }


@dataclass(frozen=True)
class ModelRateSelection:
    selected_rate_hz: int | None
    evidence_sha256: str
    all_rates_tested: bool
    shadow_only: bool = True


def select_highest_accepted_model_rate(
    evidence: Iterable[ModelTimingEvidence],
) -> ModelRateSelection:
    """Select only after complete independent 60 s evidence for all four rates."""

    by_rate: dict[int, ModelTimingEvidence] = {}
    for item in evidence:
        if item.rate_hz in by_rate:
            raise ValueError("timing evidence contains a duplicate rate")
        by_rate[item.rate_hz] = item
    required = set(MODEL_RATE_CANDIDATES_HZ)
    all_rates_tested = set(by_rate) == required
    ordered_payload = [by_rate[rate].payload() for rate in sorted(by_rate, reverse=True)]
    encoded = json.dumps(
        ordered_payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    evidence_sha256 = hashlib.sha256(encoded).hexdigest()
    selected: int | None = None
    if all_rates_tested:
        selected = next(
            (rate for rate in MODEL_RATE_CANDIDATES_HZ if by_rate[rate].accepted),
            None,
        )
    return ModelRateSelection(
        selected_rate_hz=selected,
        evidence_sha256=evidence_sha256,
        all_rates_tested=all_rates_tested,
    )
