"""r008 QUAL timing_gate diagnostics: incomplete fail-closed + ledger metrics."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r004.timing import TimingEvidence, TimingEvidenceCollector  # noqa: E402
from step5d_autotune_v4_r008.timing import (  # noqa: E402
    INCOMPLETE_FAILURE,
    evaluate_r008_timing,
    qualification_timing_metrics,
    r008_timing_scope,
)


def test_incomplete_timing_fails_closed_under_r008_scope() -> None:
    empty = TimingEvidenceCollector().finalize()
    assert empty.duration_s == 0.0
    assert empty.successful is False  # r004 path

    decision = evaluate_r008_timing(empty)
    assert decision.eligible is False
    assert INCOMPLETE_FAILURE in decision.failures

    with r008_timing_scope():
        assert empty.successful is False
        assert empty.passed is False


def test_qualification_timing_metrics_surfaces_layered_decision() -> None:
    collector = TimingEvidenceCollector()
    for i in range(100):
        collector.observe_layered_sample(
            10.0 + i * 0.002,
            source_sequences={"writer": i, "rtde": i, "kunwei": i, "tp": i},
            source_ages_s={
                "writer": 0.001,
                "rtde": 0.001,
                "kunwei": 0.001,
                "tp": 0.001,
            },
        )
    timing = collector.finalize()
    evidence = SimpleNamespace(
        evidence_sha256="a" * 64,
        metrics={
            "timing_gate_passed": True,
            "baseline_samples": 50,
            "saw_baseline": True,
            "saw_retract": True,
            "saw_latch": True,
        },
        timing_evidence=timing,
    )
    with r008_timing_scope():
        metrics = qualification_timing_metrics(evidence)
    assert metrics["mature_evidence_sha256"] == "a" * 64
    assert metrics["qualification_packet_timing_gate"] is True
    assert metrics["qualification_baseline_samples"] == 50
    assert isinstance(metrics["timing_evidence"], dict)
    assert metrics["timing_evidence"]["duration_s"] == pytest.approx(timing.duration_s)
    assert metrics["r008_timing_decision"]["eligible"] is True
    assert metrics["r008_timing_decision"]["failures"] == []


def test_qualification_timing_metrics_records_incomplete_without_raise() -> None:
    evidence = SimpleNamespace(
        evidence_sha256="b" * 64,
        metrics={"timing_gate_passed": True, "baseline_samples": 2},
        timing_evidence=TimingEvidenceCollector().finalize(),
    )
    with r008_timing_scope():
        metrics = qualification_timing_metrics(evidence)
    assert metrics["qualification_packet_timing_gate"] is True
    assert metrics["r008_timing_decision"]["eligible"] is False
    assert INCOMPLETE_FAILURE in metrics["r008_timing_decision"]["failures"]


def test_healthy_short_baseline_still_passes_r008() -> None:
    """Near-bump QUAL baselines can be short; cadence ratios still decide."""

    collector = TimingEvidenceCollector()
    for i in range(25):  # ~50 ms @ 500 Hz
        collector.observe_layered_sample(
            1.0 + i * 0.002,
            source_sequences={"writer": i, "rtde": i, "kunwei": i, "tp": i},
            source_ages_s={
                "writer": 0.001,
                "rtde": 0.001,
                "kunwei": 0.001,
                "tp": 0.001,
            },
        )
    evidence = collector.finalize()
    with r008_timing_scope():
        assert evidence.successful is True
        assert evaluate_r008_timing(evidence).eligible is True


def test_kunwei_tcp_batch_hold_does_not_drive_feedback_age_p99() -> None:
    """integ5 QUAL failure mode: kunwei inter-batch age ~48 ms, TP ~8 ms.

    Distinct kunwei rate stays healthy while TCP coalescing sawtooths
    source_ages['kunwei']; feedback_age must track loop lag (tp/writer/rtde),
    not that transport hold.  FEEDBACK_AGE_P99_MAX_S stays 10 ms.
    """

    collector = TimingEvidenceCollector()
    for i in range(200):
        kunwei_age = (i % 25) * 0.002  # 0..48 ms sawtooth
        collector.observe_layered_sample(
            1.0 + i * 0.002,
            source_sequences={
                "writer": i,
                "rtde": i,
                "kunwei": i // 25,
                "tp": i,
            },
            source_ages_s={
                "writer": 0.0005,
                "rtde": 0.0005,
                "kunwei": kunwei_age,
                "tp": 0.008,
            },
        )
    evidence = collector.finalize(duration_s=0.4)
    assert evidence.feedback_age_p99_s == pytest.approx(0.008, abs=1e-6)
    assert evidence.feedback_age_p99_s <= 0.01
    with r008_timing_scope():
        decision = evaluate_r008_timing(evidence)
        assert "feedback_age_p99_over_10ms" not in decision.failures


def test_r008_live_writer_adapter_exports() -> None:
    from step5d_autotune_v4_r008.live_adapter import R008LiveWriterAdapter

    assert issubclass(R008LiveWriterAdapter, object)
