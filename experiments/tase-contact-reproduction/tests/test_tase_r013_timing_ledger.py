from __future__ import annotations

import pytest

from tase_r013_timing_ledger import (
    STAGES,
    TaseR013TimingLedger,
    TimingLedgerError,
    ledger_from_receipts,
    summarize_timing_ledgers,
)


def _events(offset: float = 0.0):
    return [
        {"stage": stage, "timestamp_s": offset + index * 10.0, "event": "start"}
        for index, stage in enumerate(STAGES)
    ]


def test_lifecycle_ledger_records_home_to_home_and_path_duration() -> None:
    ledger = TaseR013TimingLedger.from_events("a", _events()[:7])
    ledger.mark("PATH", 60.5, event="end")
    for index, stage in enumerate(STAGES[7:], start=7):
        ledger.mark(stage, index * 10.0)
    ledger.mark("HOME", 110.0, event="verified")
    durations = ledger.durations()
    assert durations["home_to_home_s"] == pytest.approx(110.0)
    assert durations["formal_path_s"] == pytest.approx(60.5 - 60.0)
    assert ledger.as_dict()["protocol_id"] == "figure8_window60_r013_compat_v1"


def test_regression_is_rejected() -> None:
    ledger = TaseR013TimingLedger("a")
    ledger.mark("HOME_CHECK", 1.0)
    with pytest.raises(TimingLedgerError):
        ledger.mark("CONTACT_SEARCH", 0.5)
    ledger.mark("CONTACT_SEARCH", 2.0)
    with pytest.raises(TimingLedgerError):
        ledger.mark("HOME_CHECK", 3.0)


def test_recovery_merge_preserves_failed_attempt_and_records_home_outcome() -> None:
    ledger = ledger_from_receipts(
        "failed",
        lifecycle_events=_events(),
        dispatch_receipt={
            "success": False,
            "automatic_home_recovery": {
                "success": True,
                "state": "HOME",
                "home_verified": True,
            },
        },
    )
    outcome = ledger.recovery_outcome()
    assert outcome["success"] is True
    assert outcome["home_verified"] is True
    assert outcome["source_attempt_stays_failed"] is True


def test_summary_reports_median_p90_and_failure_stages() -> None:
    first = TaseR013TimingLedger.from_events("a", _events())
    second = TaseR013TimingLedger.from_events(
        "b", _events(10.0), failure_stage="PATH", failure_condition="timeout"
    )
    first.mark("HOME", 110.0, event="verified")
    second.mark("HOME", 120.0, event="verified")
    summary = summarize_timing_ledgers([first, second])
    assert summary["attempt_count"] == 2
    assert summary["failure_stage_counts"] == {"PATH": 1}
    assert summary["home_to_home"]["count"] == 2
