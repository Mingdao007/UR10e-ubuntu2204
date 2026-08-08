"""Bounded r007 timing eligibility over the existing r004 evidence type.

The r004 ``TimingEvidence`` object remains the telemetry authority.  Its
legacy ``passed``/``successful`` properties include the old per-layer average
rate veto; r007 deliberately does not call those properties.  Instead it
reuses the V3 ``cadence_eligible`` predicate for the 98% consumption/freshness
ratios and adds the independent freshness/stale limits required by the r007
activation.

This module has no controller, network, package, or live-writer side effects.
The optional context manager is an explicitly scoped, process-local adapter for
callers that still read the r004 properties.  It restores the exact original
descriptors on every exit path.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import math
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from step5d_runtime_codec import CadenceEvidence, cadence_eligible
from step5d_autotune_v4_r006.motion_profile import ACTIVE_MOTION_ENVELOPE_V2


R007_TIMING_SCHEMA = "step5d.autotune-v4/r007-timing-eligibility-v1"
R007_TIMING_VERSION = "r007-timing-eligibility-v1"
NOMINAL_HZ = 500.0
FEEDBACK_P99_MAX_S = 0.010
MAX_FRESH_GAP_S = 0.020
ACTIVE_STALE_STOP_S = 0.080
MIN_RATIO = 0.98

LOGGER = logging.getLogger(__name__)


class R007TimingError(ValueError):
    """r007 timing evidence is untyped, incomplete, or non-finite."""


def _number(value: Any, role: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R007TimingError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        raise R007TimingError(f"{role} must be finite and {'positive' if positive else 'non-negative'}")
    if result < 0.0:
        raise R007TimingError(f"{role} must be non-negative")
    return result


def _count(value: Any, role: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise R007TimingError(f"{role} must be an integer")
    if value < (1 if positive else 0):
        requirement = "positive" if positive else "non-negative"
        raise R007TimingError(f"{role} must be {requirement}")
    return value


def _r004_timing_type() -> type[Any]:
    from step5d_autotune_v4_r004.timing import TimingEvidence

    return TimingEvidence


@dataclass(frozen=True)
class R007TimingDecision:
    """Versioned, bounded decision plus retained telemetry and failure reasons."""

    eligible: bool
    failures: tuple[str, ...]
    cadence_failures: tuple[str, ...]
    telemetry: Mapping[str, Any]
    schema: str = R007_TIMING_SCHEMA
    version: str = R007_TIMING_VERSION

    def __post_init__(self) -> None:
        if self.schema != R007_TIMING_SCHEMA or self.version != R007_TIMING_VERSION:
            raise R007TimingError("r007 timing decision schema/version differs")
        if not isinstance(self.eligible, bool):
            raise R007TimingError("r007 timing decision is not boolean")
        if any(not isinstance(item, str) or not item for item in (*self.failures, *self.cadence_failures)):
            raise R007TimingError("r007 timing failure reasons are not typed")
        if not isinstance(self.telemetry, Mapping):
            raise R007TimingError("r007 timing telemetry is not a mapping")
        object.__setattr__(self, "telemetry", MappingProxyType(dict(self.telemetry)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "eligible": self.eligible,
            "failures": list(self.failures),
            "cadence_failures": list(self.cadence_failures),
            "telemetry": dict(self.telemetry),
        }

    def log(self, logger: logging.Logger = LOGGER) -> None:
        """Emit one bounded JSON event; raw frame/sample arrays are never logged."""

        logger.info(
            "r007_timing_decision %s",
            json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False),
        )


def evaluate_r007_timing(evidence: Any) -> R007TimingDecision:
    """Evaluate r007 timing without consulting r004's average-rate veto.

    ``TimingEvidence`` counters are aggregate delivery counts from the r004
    collector.  They feed V3's consumption/fresh-feedback ratios only; exact
    sequence/echo identity remains the live transport's responsibility. Kunwei
    remains a separate positive fresh-frame stream.
    ``layer_rates_hz`` and the derived average rate are retained as telemetry,
    never as an eligibility condition.
    """

    timing_type = _r004_timing_type()
    if not isinstance(evidence, timing_type):
        raise R007TimingError("r007 predicate requires r004 TimingEvidence")

    duration_s = _number(evidence.duration_s, "duration_s", positive=True)
    nominal_hz = _number(evidence.nominal_rate_hz, "nominal_rate_hz", positive=True)
    feedback_p99_s = _number(evidence.feedback_age_p99_s, "feedback_age_p99_s")
    max_gap_s = _number(evidence.max_fresh_gap_s, "max_fresh_gap_s")
    writer = _count(evidence.successful_writer_publishes, "writer distinct count", positive=True)
    rtde = _count(evidence.distinct_rtde_frames, "RTDE distinct count", positive=True)
    kunwei = _count(evidence.distinct_kunwei_frames, "Kunwei distinct count", positive=True)
    tp = _count(evidence.distinct_tp_consumed_packet_echoes, "TP distinct count", positive=True)

    cadence, cadence_failures = cadence_eligible(
        CadenceEvidence(
            profile_id=ACTIVE_MOTION_ENVELOPE_V2.parent_execution_profile_id,
            sent_packets=writer,
            consumed_packets=tp,
            fresh_feedback_packets=rtde,
            row_gap_over_20ms_count=int(max_gap_s >= MAX_FRESH_GAP_S),
        )
    )

    failures: list[str] = list(cadence_failures)
    if nominal_hz != NOMINAL_HZ:
        failures.append("nominal_rate_is_not_500_hz")
    if tp / writer < MIN_RATIO:
        failures.append("tp_consumption_ratio_below_0p98")
    if rtde / writer < MIN_RATIO:
        failures.append("fresh_feedback_ratio_below_0p98")
    if feedback_p99_s > FEEDBACK_P99_MAX_S:
        failures.append("feedback_age_p99_over_10ms")
    if max_gap_s >= MAX_FRESH_GAP_S:
        failures.append("max_fresh_gap_not_below_20ms")
    if max_gap_s >= ACTIVE_STALE_STOP_S:
        failures.append("active_stale_stop_boundary_reached")

    layer_rates = dict(evidence.layer_rates_hz)
    telemetry = {
        "schema": R007_TIMING_SCHEMA,
        "version": R007_TIMING_VERSION,
        "duration_s": duration_s,
        "nominal_hz": nominal_hz,
        "average_rate_hz": writer / duration_s,
        "layer_rates_hz": layer_rates,
        "writer_distinct": writer,
        "rtde_distinct": rtde,
        "kunwei_distinct": kunwei,
        "tp_distinct": tp,
        "tp_consumption_ratio": tp / writer,
        "fresh_feedback_ratio": rtde / writer,
        "feedback_age_p99_s": feedback_p99_s,
        "max_fresh_gap_s": max_gap_s,
        "feedback_age_p99_max_s": FEEDBACK_P99_MAX_S,
        "max_fresh_gap_limit_s": MAX_FRESH_GAP_S,
        "active_stale_stop_s": ACTIVE_STALE_STOP_S,
        "average_rate_is_eligibility_gate": False,
        "cadence_eligible": cadence,
    }
    decision = R007TimingDecision(
        eligible=not failures,
        failures=tuple(dict.fromkeys(failures)),
        cadence_failures=tuple(cadence_failures),
        telemetry=telemetry,
    )
    decision.log()
    return decision


def r007_timing_eligible(evidence: Any) -> bool:
    """Boolean convenience predicate for explicit r007 optimizer/retest seams."""

    return evaluate_r007_timing(evidence).eligible


@contextmanager
def r007_timing_scope() -> Iterator[None]:
    """Temporarily route r004 ``passed``/``successful`` through r007.

    The patch is process-local and opt-in.  The original descriptor objects are
    captured from ``__dict__`` and restored by identity, including when the
    scoped body raises or a nested scope is used.
    """

    timing_type = _r004_timing_type()
    original_passed = timing_type.__dict__["passed"]
    original_successful = timing_type.__dict__["successful"]

    def _passed(instance: Any) -> bool:
        return evaluate_r007_timing(instance).eligible

    def _successful(instance: Any) -> bool:
        return bool(instance.passed)

    patched_passed = property(_passed, doc="r007 scoped timing eligibility")
    patched_successful = property(_successful, doc="r007 scoped timing eligibility")
    try:
        setattr(timing_type, "passed", patched_passed)
        setattr(timing_type, "successful", patched_successful)
    except BaseException:
        setattr(timing_type, "passed", original_passed)
        setattr(timing_type, "successful", original_successful)
        raise
    try:
        yield
    finally:
        setattr(timing_type, "passed", original_passed)
        setattr(timing_type, "successful", original_successful)


scoped_r007_timing_patch = r007_timing_scope


__all__ = [
    "ACTIVE_STALE_STOP_S",
    "FEEDBACK_P99_MAX_S",
    "MAX_FRESH_GAP_S",
    "MIN_RATIO",
    "NOMINAL_HZ",
    "R007_TIMING_SCHEMA",
    "R007_TIMING_VERSION",
    "R007TimingDecision",
    "R007TimingError",
    "evaluate_r007_timing",
    "r007_timing_eligible",
    "r007_timing_scope",
    "scoped_r007_timing_patch",
]
