"""Select the fastest model rate backed by a 60-second timing record."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping


RATE_CANDIDATES_HZ = (200, 100, 50)
PACED_TIMING_SCHEMA_VERSION = 1
PACED_TIMING_MODE = "independent_wall_clock_paced_trials"


@dataclass(frozen=True)
class TimingEvidence:
    rate_hz: int
    duration_s: float
    p99_latency_s: float
    deadline_misses: int

    def __post_init__(self) -> None:
        if self.rate_hz not in RATE_CANDIDATES_HZ:
            raise ValueError("rate_hz must be 200, 100, or 50")
        if not math.isfinite(self.duration_s) or self.duration_s < 60.0:
            raise ValueError("timing evidence must cover at least 60 seconds")
        if not math.isfinite(self.p99_latency_s) or self.p99_latency_s < 0.0:
            raise ValueError("p99 latency must be finite and non-negative")
        if self.deadline_misses < 0:
            raise ValueError("deadline_misses must be non-negative")

    @property
    def accepted(self) -> bool:
        return (
            self.deadline_misses == 0
            and self.p99_latency_s <= 0.8 * (1.0 / self.rate_hz)
        )


def select_model_rate_hz(evidence: Iterable[TimingEvidence]) -> int | None:
    """Low-level selector; callers must establish provenance separately."""

    by_rate = {item.rate_hz: item for item in evidence}
    for rate in RATE_CANDIDATES_HZ:
        item = by_rate.get(rate)
        if item is not None and item.accepted:
            return rate
    return None


@dataclass(frozen=True)
class PacedTimingBundle:
    evidence: tuple[TimingEvidence, ...]
    nonfinite_outputs: int
    provenance: str = PACED_TIMING_MODE

    @property
    def selected_rate_hz(self) -> int | None:
        if self.nonfinite_outputs != 0:
            return None
        return select_model_rate_hz(self.evidence)


def parse_independent_paced_timing(
    payload: Mapping[str, Any] | Any,
) -> PacedTimingBundle:
    """Validate an independently paced 200/100/50 Hz timing bundle.

    A generic list of four-field latency records is intentionally insufficient:
    it contains no proof that each rate was paced as its own 60-second trial.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("select-rate requires a paced timing bundle object")
    if payload.get("schema_version") != PACED_TIMING_SCHEMA_VERSION:
        raise ValueError("unsupported paced timing schema")
    if payload.get("timing_mode") != PACED_TIMING_MODE:
        raise ValueError("timing provenance is not independent paced trials")
    if payload.get("active_enabled") is not False:
        raise ValueError("paced timing bundle must retain active_enabled=false")
    if payload.get("selection_eligible") is not True:
        raise ValueError("paced timing bundle is not explicitly selection-eligible")
    duration_per_rate = payload.get("duration_per_rate_s")
    if (
        not isinstance(duration_per_rate, (int, float))
        or not math.isfinite(float(duration_per_rate))
        or float(duration_per_rate) < 60.0
    ):
        raise ValueError("paced timing declares less than 60 seconds per rate")
    raw_results = payload.get("rate_results")
    if not isinstance(raw_results, list) or len(raw_results) != len(
        RATE_CANDIDATES_HZ
    ):
        raise ValueError("paced timing requires exactly 200/100/50 Hz results")
    evidence: list[TimingEvidence] = []
    seen: set[int] = set()
    nonfinite_total = 0
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            raise ValueError("paced rate result must be an object")
        required = {
            "rate_hz",
            "duration_s",
            "period_s",
            "scheduled_ticks",
            "executed_ticks",
            "skipped_releases",
            "deadline_misses",
            "nonfinite_outputs",
            "latency_p99_s",
            "accepted",
        }
        if not required <= raw.keys():
            raise ValueError("paced rate result is missing provenance fields")
        rate = int(raw["rate_hz"])
        if rate not in RATE_CANDIDATES_HZ:
            raise ValueError("paced rate must be 200, 100, or 50 Hz")
        if rate in seen:
            raise ValueError("paced timing contains a duplicate rate")
        seen.add(rate)
        period = float(raw["period_s"])
        if not math.isfinite(period) or not math.isclose(
            period, 1.0 / rate, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("paced rate period does not match its frequency")
        actual_duration = float(raw["duration_s"])
        if (
            not math.isfinite(actual_duration)
            or abs(actual_duration - float(duration_per_rate)) > 0.1
        ):
            raise ValueError("paced rate duration is inconsistent with the declared trial")
        scheduled = int(raw["scheduled_ticks"])
        executed = int(raw["executed_ticks"])
        skipped = int(raw["skipped_releases"])
        if scheduled <= 0 or executed <= 0 or skipped < 0 or executed + skipped != scheduled:
            raise ValueError("paced schedule accounting is inconsistent")
        expected_ticks = rate * float(duration_per_rate)
        tick_tolerance = max(2.0, 0.001 * expected_ticks)
        if abs(scheduled - expected_ticks) > tick_tolerance:
            raise ValueError("paced schedule does not cover the declared 60-second trial")
        nonfinite = int(raw["nonfinite_outputs"])
        if nonfinite < 0:
            raise ValueError("nonfinite output count must be non-negative")
        item = TimingEvidence(
            rate_hz=rate,
            duration_s=actual_duration,
            p99_latency_s=float(raw["latency_p99_s"]),
            deadline_misses=int(raw["deadline_misses"]),
        )
        expected_accepted = item.accepted and nonfinite == 0
        if raw["accepted"] is not expected_accepted:
            raise ValueError("paced accepted flag disagrees with timing evidence")
        evidence.append(item)
        nonfinite_total += nonfinite
    if seen != set(RATE_CANDIDATES_HZ):
        raise ValueError("paced timing must contain 200, 100, and 50 Hz")
    bundle = PacedTimingBundle(tuple(evidence), nonfinite_total)
    if payload.get("selected_rate_hz") != bundle.selected_rate_hz:
        raise ValueError("paced selected rate disagrees with recomputed result")
    # Timing selection never authorizes DBIL active control.
    if payload.get("shadow_only") is not True:
        raise ValueError("paced timing evidence must remain shadow-only")
    return bundle
