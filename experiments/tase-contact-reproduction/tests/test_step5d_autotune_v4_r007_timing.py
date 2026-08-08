from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r004.timing import TimingEvidence  # noqa: E402
from step5d_autotune_v4_r006.motion_profile import ACTIVE_MOTION_ENVELOPE_V2  # noqa: E402
from step5d_autotune_v4_r007.timing import (  # noqa: E402
    evaluate_r007_timing,
    r007_timing_eligible,
    r007_timing_scope,
)


def _seq9() -> TimingEvidence:
    return TimingEvidence.from_counts(
        duration_s=60.0,
        writer_publishes=27583,
        rtde_frames=27583,
        kunwei_frames=59996,
        tp_consumed_echoes=27583,
        feedback_age_p99_s=0.007491,
        max_fresh_gap_s=0.006676,
    )


def test_seq9_passes_without_average_rate_veto_and_retains_telemetry() -> None:
    evidence = _seq9()

    # The old r004/r006 predicate rejects this 459.7167 Hz average.  r007
    # retains it as telemetry and evaluates the distinct/freshness gates.
    assert evidence.passed is False
    decision = evaluate_r007_timing(evidence)
    assert decision.eligible is True
    assert decision.failures == ()
    assert decision.telemetry["average_rate_hz"] == pytest.approx(459.7167, abs=1e-4)
    assert decision.telemetry["nominal_hz"] == 500.0
    assert decision.telemetry["average_rate_is_eligibility_gate"] is False
    assert r007_timing_eligible(evidence)


def test_endpoint_count_difference_is_allowed_when_v3_ratios_remain_at_least_98_percent() -> None:
    evidence = replace(_seq9(), distinct_rtde_frames=27_582)
    decision = evaluate_r007_timing(evidence)

    assert decision.eligible is True
    assert decision.failures == ()
    assert decision.telemetry["fresh_feedback_ratio"] >= 0.98


def test_real_v3_cadence_primitive_and_exact_dataclass_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    import step5d_autotune_v4_r007.timing as timing_module
    from step5d_runtime_codec import CadenceEvidence as V3CadenceEvidence

    seen: list[object] = []
    real_cadence_eligible = timing_module.cadence_eligible

    def observe(cadence: object):
        seen.append(cadence)
        return real_cadence_eligible(cadence)

    monkeypatch.setattr(timing_module, "cadence_eligible", observe)
    decision = evaluate_r007_timing(_seq9())

    assert decision.eligible is True
    assert len(seen) == 1
    assert type(seen[0]) is V3CadenceEvidence
    assert set(seen[0].__dataclass_fields__) == {
        "profile_id",
        "sent_packets",
        "consumed_packets",
        "fresh_feedback_packets",
        "row_gap_over_20ms_count",
    }
    assert seen[0].profile_id == ACTIVE_MOTION_ENVELOPE_V2.parent_execution_profile_id


@pytest.mark.parametrize(
    ("change", "reason"),
    (
        ({"max_fresh_gap_s": 0.030}, "max_fresh_gap_not_below_20ms"),
        ({"feedback_age_p99_s": 0.011}, "feedback_age_p99_over_10ms"),
        ({"distinct_tp_consumed_packet_echoes": 27000}, "tp_consumption_ratio_below_0p98"),
        ({"distinct_rtde_frames": 27000}, "fresh_feedback_ratio_below_0p98"),
        ({"max_fresh_gap_s": 0.080}, "max_fresh_gap_not_below_20ms"),
    ),
)
def test_r007_fails_closed_for_real_timing_regressions(change: dict[str, float | int], reason: str) -> None:
    decision = evaluate_r007_timing(replace(_seq9(), **change))
    assert decision.eligible is False
    assert reason in decision.failures


def test_exact_descriptors_restore_after_exception() -> None:
    original_passed = TimingEvidence.__dict__["passed"]
    original_successful = TimingEvidence.__dict__["successful"]
    evidence = _seq9()

    with pytest.raises(RuntimeError, match="scoped failure"):
        with r007_timing_scope():
            assert TimingEvidence.__dict__["passed"] is not original_passed
            assert TimingEvidence.__dict__["successful"] is not original_successful
            assert evidence.passed is True
            assert evidence.successful is True
            raise RuntimeError("scoped failure")

    assert TimingEvidence.__dict__["passed"] is original_passed
    assert TimingEvidence.__dict__["successful"] is original_successful
    assert evidence.passed is False
    assert evidence.successful is False


def test_r006_outside_scope_isolation_and_nested_restore() -> None:
    from step5d_autotune_v4_r006.fake_rtde import TimingRegression

    original_passed = TimingEvidence.__dict__["passed"]
    original_successful = TimingEvidence.__dict__["successful"]
    legacy = TimingRegression(
        duration_s=60.0,
        writer_count=27583,
        rtde_count=27583,
        tp_echo_count=27583,
        writer_rate_hz=459.7167,
        rtde_rate_hz=459.7167,
        echo_backlog=0,
    )
    assert legacy.passes is False

    with r007_timing_scope():
        assert _seq9().passed is True
        with r007_timing_scope():
            assert _seq9().successful is True
        assert TimingEvidence.__dict__["passed"] is not original_passed
        assert TimingEvidence.__dict__["successful"] is not original_successful
        assert legacy.passes is False

    assert TimingEvidence.__dict__["passed"] is original_passed
    assert TimingEvidence.__dict__["successful"] is original_successful
    assert legacy.passes is False
