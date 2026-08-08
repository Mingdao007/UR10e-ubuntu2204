"""Offline 500 Hz absolute-deadline and independent rate-gate evidence."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping


NOMINAL_HZ = 500.0
MIN_RATE_HZ = 460.0
PERIOD_S = 1.0 / NOMINAL_HZ


class TimingError(RuntimeError):
    """A deadline, timestamp, or rate-gate invariant is invalid."""


@dataclass
class AbsoluteDeadlinePacer:
    """Pace against one absolute timeline; never sleep period-after-period."""

    frequency_hz: float = NOMINAL_HZ
    start_monotonic: float | None = None
    _index: int = 0
    missed_deadlines: int = 0

    def __post_init__(self) -> None:
        if not math.isclose(self.frequency_hz, NOMINAL_HZ, rel_tol=0.0, abs_tol=1e-12):
            raise TimingError("r005 pacing frequency is fixed at 500 Hz")
        if self.start_monotonic is None:
            self.start_monotonic = time.monotonic()
        if not math.isfinite(float(self.start_monotonic)):
            raise TimingError("pacer start must be finite")

    @property
    def next_index(self) -> int:
        return self._index

    def deadline(self, index: int | None = None) -> float:
        selected = self._index if index is None else index
        if isinstance(selected, bool) or not isinstance(selected, int) or selected < 0:
            raise TimingError("pacer index must be non-negative")
        return float(self.start_monotonic) + selected * PERIOD_S

    def tick(
        self,
        *,
        now: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> float:
        deadline = self.deadline()
        current = time.monotonic() if now is None else float(now)
        if not math.isfinite(current):
            raise TimingError("pacer clock must be finite")
        remaining = deadline - current
        if remaining > 0.0:
            if now is not None:
                # A simulated clock is never advanced by a real sleep.
                pass
            else:
                sleep(remaining)
        else:
            self.missed_deadlines += int(remaining < -PERIOD_S * 0.5)
        self._index += 1
        return deadline


@dataclass
class RateGate:
    name: str
    minimum_hz: float = MIN_RATE_HZ
    timestamps: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name or "\n" in self.name:
            raise TimingError("rate gate name must be one line")
        if self.minimum_hz < MIN_RATE_HZ:
            raise TimingError("all r005 rate gates must be at least 460 Hz")

    def record(self, timestamp_s: float) -> None:
        timestamp = float(timestamp_s)
        if not math.isfinite(timestamp):
            raise TimingError(f"{self.name} timestamp is not finite")
        if self.timestamps and timestamp <= self.timestamps[-1]:
            raise TimingError(f"{self.name} timestamps must increase")
        self.timestamps.append(timestamp)

    @property
    def observed_hz(self) -> float | None:
        if len(self.timestamps) < 2:
            return None
        duration = self.timestamps[-1] - self.timestamps[0]
        return None if duration <= 0.0 else (len(self.timestamps) - 1) / duration

    @property
    def passes(self) -> bool:
        rate = self.observed_hz
        return rate is not None and rate >= self.minimum_hz


@dataclass
class RateGates:
    """Distinct writer, RTDE, Kunwei, and TP gates; no aggregate substitution."""

    gates: dict[str, RateGate] = field(
        default_factory=lambda: {
            name: RateGate(name)
            for name in ("writer", "rtde", "kunwei", "tp")
        }
    )

    def record(self, name: str, timestamp_s: float) -> None:
        if name not in self.gates:
            raise TimingError(f"unknown independent rate gate {name!r}")
        self.gates[name].record(timestamp_s)

    def evidence(self) -> "TimingEvidence":
        rates = {name: gate.observed_hz for name, gate in self.gates.items()}
        return TimingEvidence(rates=rates, nominal_hz=NOMINAL_HZ)


@dataclass(frozen=True)
class TimingEvidence:
    rates: Mapping[str, float | None]
    nominal_hz: float = NOMINAL_HZ

    def __post_init__(self) -> None:
        expected = {"writer", "rtde", "kunwei", "tp"}
        if set(self.rates) != expected:
            raise TimingError("timing evidence must contain four distinct rate gates")
        if not math.isclose(self.nominal_hz, NOMINAL_HZ, rel_tol=0.0, abs_tol=1e-12):
            raise TimingError("timing nominal frequency must be 500 Hz")
        for name, rate in self.rates.items():
            if rate is not None and (not math.isfinite(float(rate)) or rate < 0.0):
                raise TimingError(f"{name} rate is invalid")

    @property
    def passes(self) -> bool:
        return all(
            rate is not None and float(rate) >= MIN_RATE_HZ
            for rate in self.rates.values()
        )


__all__ = [
    "AbsoluteDeadlinePacer",
    "MIN_RATE_HZ",
    "NOMINAL_HZ",
    "PERIOD_S",
    "RateGate",
    "RateGates",
    "TimingEvidence",
    "TimingError",
]
