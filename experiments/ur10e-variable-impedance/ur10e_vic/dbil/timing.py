"""Validate raw paced timing and select a hash-bound DBIL model rate.

The benchmark producer is intentionally not trusted to declare its own result.
It emits a candidate containing every scheduled tick.  This module recomputes
the summaries from those raw ticks and only then emits a selection manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


RATE_CANDIDATES_HZ = (200, 100, 50)
PACED_TIMING_MODE = "independent_wall_clock_paced_trials"
RAW_PACED_TIMING_SCHEMA_VERSION = 2
SELECTION_MANIFEST_SCHEMA_VERSION = 2
RAW_CANDIDATE_STATUS = "unvalidated_candidate"
VALIDATED_SELECTION_STATUS = "validated_selection_manifest"
_SHA256_FIELDS = (
    "checkpoint_sha256",
    "stats_sha256",
    "observation_sha256",
    "harness_source_sha256",
)


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


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_sha256(value: Any, name: str) -> str:
    text = str(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


def _validate_bindings(
    payload: Mapping[str, Any], expected_bindings: Mapping[str, Any]
) -> dict[str, Any]:
    bindings = payload.get("artifact_bindings")
    if not isinstance(bindings, Mapping):
        raise ValueError("paced timing candidate is missing artifact bindings")
    validated: dict[str, Any] = {}
    for name in _SHA256_FIELDS:
        actual = _require_sha256(bindings.get(name), name)
        expected = _require_sha256(expected_bindings.get(name), f"expected {name}")
        if actual != expected:
            raise ValueError(f"paced timing binding mismatch: {name}")
        validated[name] = actual
    runtime = bindings.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ValueError("paced timing candidate is missing runtime binding")
    required_runtime = {
        "python_version",
        "platform",
        "torch_version",
        "device_type",
        "device_name",
    }
    if not required_runtime <= runtime.keys() or not all(
        str(runtime[name]).strip() for name in required_runtime
    ):
        raise ValueError("paced timing runtime binding is incomplete")
    expected_runtime = expected_bindings.get("runtime")
    if expected_runtime is not None and dict(runtime) != dict(expected_runtime):
        raise ValueError("paced timing runtime binding mismatch")
    validated["runtime"] = dict(runtime)
    return validated


def _recompute_raw_trial(
    raw: Mapping[str, Any], *, duration_per_rate_s: float
) -> tuple[TimingEvidence, dict[str, Any]]:
    rate = int(raw.get("rate_hz", 0))
    if rate not in RATE_CANDIDATES_HZ:
        raise ValueError("raw paced rate must be 200, 100, or 50 Hz")
    period = float(raw.get("period_s", math.nan))
    if not math.isfinite(period) or not math.isclose(
        period, 1.0 / rate, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError("raw paced rate period does not match its frequency")
    started = float(raw.get("trial_started_s", math.nan))
    ended = float(raw.get("trial_ended_s", math.nan))
    if not math.isfinite(started) or not math.isfinite(ended) or ended <= started:
        raise ValueError("raw paced trial bounds are invalid")
    actual_duration = ended - started
    if actual_duration < duration_per_rate_s or actual_duration - duration_per_rate_s > 0.25:
        raise ValueError("raw paced trial duration is inconsistent")
    ticks = raw.get("raw_ticks")
    if not isinstance(ticks, Sequence) or isinstance(ticks, (str, bytes)) or not ticks:
        raise ValueError("raw paced trial requires per-tick evidence")
    latencies: list[float] = []
    skipped = 0
    deadline_misses = 0
    nonfinite_outputs = 0
    previous_release: float | None = None
    for expected_index, tick in enumerate(ticks):
        if not isinstance(tick, Mapping) or int(tick.get("tick_index", -1)) != expected_index:
            raise ValueError("raw paced tick indices must be contiguous")
        release = float(tick.get("release_s", math.nan))
        if not math.isfinite(release):
            raise ValueError("raw paced release must be finite")
        expected_release = started + expected_index * period
        if not math.isclose(release, expected_release, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("raw paced release grid drifted")
        if previous_release is not None and release <= previous_release:
            raise ValueError("raw paced releases must be strictly increasing")
        previous_release = release
        tick_started = tick.get("start_s")
        tick_ended = tick.get("end_s")
        nonfinite = tick.get("nonfinite_output")
        if tick_started is None or tick_ended is None:
            if tick_started is not None or tick_ended is not None or nonfinite is not None:
                raise ValueError("skipped raw tick must have null start/end/nonfinite")
            skipped += 1
            deadline_misses += 1
            continue
        start_value = float(tick_started)
        end_value = float(tick_ended)
        if (
            not math.isfinite(start_value)
            or not math.isfinite(end_value)
            or start_value < release
            or end_value < start_value
        ):
            raise ValueError("executed raw tick bounds are invalid")
        if not isinstance(nonfinite, bool):
            raise ValueError("executed raw tick requires a boolean nonfinite flag")
        nonfinite_outputs += int(nonfinite)
        latencies.append(end_value - start_value)
        deadline_misses += int(end_value >= release + period)
    if not latencies:
        raise ValueError("raw paced trial did not execute any inference")
    scheduled = len(ticks)
    expected_ticks = rate * duration_per_rate_s
    tick_tolerance = max(2.0, 0.001 * expected_ticks)
    if abs(scheduled - expected_ticks) > tick_tolerance:
        raise ValueError("raw paced schedule does not cover the declared trial")
    latency_array = sorted(latencies)
    quantile_index = 0.99 * (len(latency_array) - 1)
    lower_index = int(math.floor(quantile_index))
    upper_index = int(math.ceil(quantile_index))
    fraction = quantile_index - lower_index
    p99 = latency_array[lower_index] * (1.0 - fraction) + latency_array[upper_index] * fraction
    evidence = TimingEvidence(rate, actual_duration, p99, deadline_misses)
    summary = {
        "rate_hz": rate,
        "duration_s": actual_duration,
        "period_s": period,
        "scheduled_ticks": scheduled,
        "executed_ticks": len(latencies),
        "skipped_releases": skipped,
        "deadline_misses": deadline_misses,
        "nonfinite_outputs": nonfinite_outputs,
        "latency_first_s": latencies[0],
        "latency_p50_s": float(
            latency_array[(len(latency_array) - 1) // 2]
            if len(latency_array) % 2
            else 0.5
            * (
                latency_array[len(latency_array) // 2 - 1]
                + latency_array[len(latency_array) // 2]
            )
        ),
        "latency_p99_s": p99,
        "latency_max_s": max(latencies),
        "accepted": evidence.accepted and nonfinite_outputs == 0,
    }
    return evidence, summary


def build_paced_timing_selection_manifest(
    candidate: Mapping[str, Any], *, expected_bindings: Mapping[str, Any]
) -> dict[str, Any]:
    """Independently recompute a raw candidate and emit a selection manifest."""

    if candidate.get("schema_version") != RAW_PACED_TIMING_SCHEMA_VERSION:
        raise ValueError("unsupported raw paced timing schema")
    if candidate.get("timing_mode") != PACED_TIMING_MODE:
        raise ValueError("raw timing provenance is not independent paced trials")
    if candidate.get("candidate_status") != RAW_CANDIDATE_STATUS:
        raise ValueError("paced timing producer status is not an unvalidated candidate")
    if candidate.get("selection_eligible") is not False or candidate.get("selected_rate_hz") is not None:
        raise ValueError("timing producer may not self-select a model rate")
    if candidate.get("shadow_only") is not True or candidate.get("active_enabled") is not False:
        raise ValueError("raw timing candidate must remain shadow-only and inactive")
    duration = float(candidate.get("duration_per_rate_s", math.nan))
    if not math.isfinite(duration) or duration < 60.0:
        raise ValueError("raw timing declares less than 60 seconds per rate")
    bindings = _validate_bindings(candidate, expected_bindings)
    raw_results = candidate.get("rate_results")
    if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
        raise ValueError("raw timing candidate requires rate results")
    recomputed: list[dict[str, Any]] = []
    evidence: list[TimingEvidence] = []
    seen: set[int] = set()
    nonfinite_total = 0
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            raise ValueError("raw timing rate result must be an object")
        item, summary = _recompute_raw_trial(raw, duration_per_rate_s=duration)
        if item.rate_hz in seen:
            raise ValueError("raw timing candidate contains a duplicate rate")
        seen.add(item.rate_hz)
        producer_summary = raw.get("producer_summary")
        if not isinstance(producer_summary, Mapping):
            raise ValueError("raw timing rate result lacks producer summary")
        for name, value in summary.items():
            produced = producer_summary.get(name)
            if isinstance(value, float):
                if not isinstance(produced, (int, float)) or not math.isclose(
                    float(produced), value, rel_tol=0.0, abs_tol=1e-12
                ):
                    raise ValueError(f"producer timing summary mismatch: {name}")
            elif produced != value:
                raise ValueError(f"producer timing summary mismatch: {name}")
        evidence.append(item)
        recomputed.append(summary)
        nonfinite_total += int(summary["nonfinite_outputs"])
    if seen != set(RATE_CANDIDATES_HZ):
        raise ValueError("raw timing candidate must contain 200, 100, and 50 Hz")
    selected = select_model_rate_hz(evidence) if nonfinite_total == 0 else None
    return {
        "schema_version": SELECTION_MANIFEST_SCHEMA_VERSION,
        "timing_mode": PACED_TIMING_MODE,
        "validation_status": VALIDATED_SELECTION_STATUS,
        "candidate_payload_sha256": _canonical_sha256(candidate),
        "validator_source_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "artifact_bindings": bindings,
        "duration_per_rate_s": duration,
        "rate_results": recomputed,
        "selected_rate_hz": selected,
        "selection_eligible": True,
        "shadow_only": True,
        "active_enabled": False,
    }


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
    if payload.get("schema_version") != SELECTION_MANIFEST_SCHEMA_VERSION:
        raise ValueError("legacy paced timing cannot select without raw validation")
    if payload.get("validation_status") != VALIDATED_SELECTION_STATUS:
        raise ValueError("paced timing v2 is not a validated selection manifest")
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
