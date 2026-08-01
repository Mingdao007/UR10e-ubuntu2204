"""Live-attempt path and timing evidence primitives for r004."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Mapping

from .contracts import TARGET_FORCE_N


class EvidenceError(RuntimeError):
    """Evidence is incomplete, nonfinite, or outside acceptance bounds."""


@dataclass(frozen=True)
class PathSample:
    observed_at_s: float
    filtered_normal_n: float
    force_norm_n: float
    torque_norm_nm: float
    sensor_fresh: bool
    state: int
    safety_normal: bool

    def __post_init__(self) -> None:
        values = (
            self.observed_at_s,
            self.filtered_normal_n,
            self.force_norm_n,
            self.torque_norm_nm,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise EvidenceError("path sample contains a nonfinite value")
        if not isinstance(self.sensor_fresh, bool) or not isinstance(self.safety_normal, bool):
            raise EvidenceError("path sample flags are not typed")
        if self.force_norm_n < 0.0 or self.torque_norm_nm < 0.0:
            raise EvidenceError("path sample norms are negative")


@dataclass(frozen=True)
class AttemptEvidence:
    complete_bins: int
    effective_rate_hz: float
    p99_packet_interval_s: float
    max_packet_interval_s: float
    mae_n: float
    objective: float
    safety_gate_passed: bool
    contact_gate_passed: bool
    return_gate_passed: bool
    home_proof: Mapping[str, Any]
    path_samples: int
    path_bin_ids: tuple[int, ...]
    evidence_sha256: str
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.complete_bins != 550 or len(set(self.path_bin_ids)) != 550:
            raise EvidenceError("r004 path coverage is not exactly 550 bins")
        for role, value in (
            ("effective rate", self.effective_rate_hz),
            ("p99 packet interval", self.p99_packet_interval_s),
            ("max packet interval", self.max_packet_interval_s),
            ("MAE", self.mae_n),
            ("objective", self.objective),
        ):
            if not math.isfinite(float(value)) or float(value) < 0.0:
                raise EvidenceError(f"{role} is invalid")
        if not all(isinstance(value, bool) for value in (self.safety_gate_passed, self.contact_gate_passed, self.return_gate_passed)):
            raise EvidenceError("attempt gate values are not typed")
        if not isinstance(self.path_samples, int) or self.path_samples < 550:
            raise EvidenceError("path sample count is invalid")
        if len(self.evidence_sha256) != 64 or any(char not in "0123456789abcdef" for char in self.evidence_sha256):
            raise EvidenceError("attempt evidence SHA is invalid")


class PathEvidenceCollector:
    """Use actual host-observed RTDE/Kunwei samples; never synthesize bins."""

    BIN_WIDTH_S = 0.1
    REQUIRED_BINS = 550

    def __init__(self) -> None:
        self.path_started_at_s: float | None = None
        self._bins: dict[int, list[float]] = {}
        self._timestamps: list[float] = []
        self._samples: list[PathSample] = []
        self._states: set[int] = set()
        self._safety_all = True

    def observe(self, sample: PathSample) -> None:
        self._states.add(sample.state)
        self._safety_all = self._safety_all and sample.safety_normal
        if sample.state != 25:
            return
        if self.path_started_at_s is None:
            self.path_started_at_s = sample.observed_at_s
        relative = sample.observed_at_s - self.path_started_at_s
        if relative < 0.0:
            raise EvidenceError("path timestamp regressed")
        index = int(relative / self.BIN_WIDTH_S)
        if 0 <= index < self.REQUIRED_BINS:
            self._bins.setdefault(index, []).append(sample.filtered_normal_n)
            self._timestamps.append(sample.observed_at_s)
            self._samples.append(sample)

    @property
    def safety_gate_passed(self) -> bool:
        """Public aggregate used by the live boundary; no private collection access."""

        return self._safety_all

    @property
    def observed_states(self) -> frozenset[int]:
        return frozenset(self._states)

    @staticmethod
    def _p99(intervals: list[float]) -> float:
        if not intervals:
            return float("inf")
        ordered = sorted(intervals)
        index = min(len(ordered) - 1, max(0, math.ceil(0.99 * len(ordered)) - 1))
        return ordered[index]

    def finalize(
        self,
        *,
        return_gate_passed: bool,
        home_proof: Mapping[str, Any],
        contact_gate_passed: bool | None = None,
        mae_n: float | None = None,
        objective: float | None = None,
    ) -> AttemptEvidence:
        if self.path_started_at_s is None or len(self._bins) != self.REQUIRED_BINS:
            raise EvidenceError(f"r004 path has {len(self._bins)} of 550 bins")
        if len(self._timestamps) < 2:
            raise EvidenceError("r004 path has insufficient timing samples")
        intervals = [right - left for left, right in zip(self._timestamps, self._timestamps[1:])]
        if any(not math.isfinite(value) or value <= 0.0 for value in intervals):
            raise EvidenceError("r004 path timing is not strictly increasing")
        means = [statistics.fmean(self._bins[index]) for index in range(self.REQUIRED_BINS)]
        errors = [value - TARGET_FORCE_N for value in means]
        calculated_mae = statistics.fmean(abs(value) for value in errors)
        start = self._timestamps[0]
        duration = self._timestamps[-1] - start
        rate = len(self._timestamps) / duration if duration > 0.0 else 0.0
        complete_contact = contact_gate_passed if contact_gate_passed is not None else {20, 21, 25}.issubset(self._states)
        evidence_material = {
            "bins": {str(index): self._bins[index] for index in range(self.REQUIRED_BINS)},
            "timestamps": self._timestamps,
            "states": sorted(self._states),
            "home_proof": dict(home_proof),
        }
        evidence_sha = hashlib.sha256(
            json.dumps(evidence_material, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        return AttemptEvidence(
            complete_bins=self.REQUIRED_BINS,
            effective_rate_hz=rate,
            p99_packet_interval_s=self._p99(intervals),
            max_packet_interval_s=max(intervals),
            mae_n=calculated_mae if mae_n is None else float(mae_n),
            objective=calculated_mae if objective is None else float(objective),
            safety_gate_passed=self._safety_all,
            contact_gate_passed=bool(complete_contact),
            return_gate_passed=bool(return_gate_passed),
            home_proof=dict(home_proof),
            path_samples=len(self._timestamps),
            path_bin_ids=tuple(range(self.REQUIRED_BINS)),
            evidence_sha256=evidence_sha,
            metrics={"state_codes": sorted(self._states), "path_duration_s": duration},
        )


__all__ = ["AttemptEvidence", "EvidenceError", "PathEvidenceCollector", "PathSample"]
