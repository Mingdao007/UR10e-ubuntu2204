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
from typing import Any, Iterator, Mapping

from step5d_autotune_v4_r007.timing import (
    R007TimingDecision,
    R007TimingError,
    evaluate_r007_timing,
    r007_timing_scope,
)

LEGACY_MIN_RATE_HZ = 460.0
TP_RATIO_FAILURE = "tp_consumption_ratio_below_0p98"
INCOMPLETE_FAILURE = "timing_evidence_incomplete"
R008_TIMING_NOTE = "r008_tp_ratio_rescued_when_layers_ge_460hz"


def evaluate_r008_timing(evidence: Any) -> R007TimingDecision:
    """r007 decision with an additive TP-ratio rescue for healthy ~480 Hz echoes.

    Incomplete evidence (duration<=0 / zero distinct counts) fails closed as
    ``timing_evidence_incomplete`` instead of raising through ``.successful``
    while ``r008_timing_scope`` is active. That lets QUAL seal
    SAFE_NONTRAINABLE with ``timing_gate=False`` rather than crashing the host.
    """

    try:
        decision = evaluate_r007_timing(evidence)
    except R007TimingError as exc:
        return R007TimingDecision(
            eligible=False,
            failures=(INCOMPLETE_FAILURE,),
            cadence_failures=(),
            telemetry={
                "r008_incomplete_reason": str(exc),
                "duration_s": getattr(evidence, "duration_s", None),
            },
        )
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


def qualification_timing_metrics(evidence: Any) -> dict[str, Any]:
    """Bounded QUAL metrics: keep layered TimingEvidence visible on the ledger.

    Frozen r005 QUAL conversion only stored ``mature_evidence_sha256``, which
    hid why ``timing_gate`` failed on near-bump canaries.
    """

    metrics: dict[str, Any] = {
        "mature_evidence_sha256": getattr(evidence, "evidence_sha256", None),
    }
    raw_metrics = getattr(evidence, "metrics", None)
    if isinstance(raw_metrics, Mapping):
        # Packet-interval gate inside QualificationEvidenceCollector (not the
        # layered TimingEvidence gate mirrored on ObservationRecord.timing_gate).
        if "timing_gate_passed" in raw_metrics:
            metrics["qualification_packet_timing_gate"] = bool(
                raw_metrics["timing_gate_passed"]
            )
        if "baseline_samples" in raw_metrics:
            metrics["qualification_baseline_samples"] = raw_metrics["baseline_samples"]
        for key in (
            "saw_baseline",
            "saw_retract",
            "saw_latch",
            "setpoint_min_n",
            "setpoint_max_n",
        ):
            if key in raw_metrics:
                metrics[key] = raw_metrics[key]
    timing = getattr(evidence, "timing_evidence", None)
    if timing is not None:
        as_dict = getattr(timing, "as_dict", None)
        metrics["timing_evidence"] = as_dict() if callable(as_dict) else dict(timing)
        decision = evaluate_r008_timing(timing)
        metrics["r008_timing_decision"] = decision.as_dict()
    else:
        metrics["timing_evidence"] = None
        metrics["r008_timing_decision"] = {
            "eligible": False,
            "failures": [INCOMPLETE_FAILURE],
            "cadence_failures": [],
            "telemetry": {"r008_incomplete_reason": "timing_evidence_missing"},
        }
    return metrics


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
    "INCOMPLETE_FAILURE",
    "LEGACY_MIN_RATE_HZ",
    "TP_RATIO_FAILURE",
    "evaluate_r008_timing",
    "qualification_timing_metrics",
    "r008_timing_scope",
]
