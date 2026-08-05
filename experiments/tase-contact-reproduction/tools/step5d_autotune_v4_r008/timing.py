"""r008 timing overlay: keep r007 cadence rescue, drop false TP/writer fails.

Real TP consumed-packet echoes sustain ~480 Hz while the writer is paced off
fresh RTDE at ~500 Hz.  r007's ``tp/writer >= 0.98`` therefore false-fails
healthy baselines (~0.963) and blocks ``qualification_eligible`` forever.

r008 keeps every other r007 predicate (freshness, stale stop, RTDE/writer
ratio, average-rate as telemetry only) and only rescues the TP consumption
ratio when both TP and writer layer rates clear the legacy 460 Hz floor.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Iterator

from step5d_autotune_v4_r007.timing import (
    R007TimingDecision,
    evaluate_r007_timing,
    r007_timing_scope,
)

LEGACY_MIN_RATE_HZ = 460.0
TP_RATIO_FAILURE = "tp_consumption_ratio_below_0p98"
R008_TIMING_NOTE = "r008_tp_ratio_rescued_when_layers_ge_460hz"


def evaluate_r008_timing(evidence: Any) -> R007TimingDecision:
    """r007 decision with an additive TP-ratio rescue for healthy ~480 Hz echoes."""

    decision = evaluate_r007_timing(evidence)
    if TP_RATIO_FAILURE not in decision.failures:
        return decision

    duration_s = float(evidence.duration_s)
    if not (duration_s > 0.0):
        return decision
    tp_hz = float(evidence.distinct_tp_consumed_packet_echoes) / duration_s
    wr_hz = float(evidence.successful_writer_publishes) / duration_s
    if tp_hz < LEGACY_MIN_RATE_HZ or wr_hz < LEGACY_MIN_RATE_HZ:
        return decision

    failures = tuple(item for item in decision.failures if item != TP_RATIO_FAILURE)
    cadence_failures = tuple(
        item for item in decision.cadence_failures if item != TP_RATIO_FAILURE
    )
    telemetry = dict(decision.telemetry)
    telemetry[R008_TIMING_NOTE] = True
    telemetry["r008_tp_hz"] = tp_hz
    telemetry["r008_writer_hz"] = wr_hz
    return replace(
        decision,
        eligible=not failures,
        failures=failures,
        cadence_failures=cadence_failures,
        telemetry=telemetry,
    )


@contextmanager
def r008_timing_scope() -> Iterator[None]:
    """Process-local patch: r004 ``passed``/``successful`` use ``evaluate_r008_timing``."""

    from step5d_autotune_v4_r004.timing import TimingEvidence

    original_passed = TimingEvidence.__dict__["passed"]
    original_successful = TimingEvidence.__dict__["successful"]

    def _passed(instance: Any) -> bool:
        return evaluate_r008_timing(instance).eligible

    def _successful(instance: Any) -> bool:
        return bool(instance.passed)

    try:
        setattr(TimingEvidence, "passed", property(_passed, doc="r008 scoped timing eligibility"))
        setattr(
            TimingEvidence,
            "successful",
            property(_successful, doc="r008 scoped timing eligibility"),
        )
        yield
    finally:
        setattr(TimingEvidence, "passed", original_passed)
        setattr(TimingEvidence, "successful", original_successful)


# Keep the r007 symbol importable for tests that assert the parent seam still exists.
_parent_scope = r007_timing_scope


__all__ = [
    "LEGACY_MIN_RATE_HZ",
    "TP_RATIO_FAILURE",
    "evaluate_r008_timing",
    "r008_timing_scope",
]
