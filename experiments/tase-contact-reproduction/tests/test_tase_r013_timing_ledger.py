from __future__ import annotations

from types import SimpleNamespace

import pytest

from tase_r013_timing_ledger import (
    R013_PROTOCOL_ID,
    STAGES,
    TaseR013TimingLedger,
    TimingLedgerError,
    lifecycle_events_from_writer,
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


def test_recovery_outcome_accepts_governed_home_recovered_state() -> None:
    ledger = TaseR013TimingLedger(
        "recovered",
        source_success=False,
        recovery={
            "success": True,
            "state": "HOME_RECOVERED",
            "home": {"success": True},
        },
    )
    outcome = ledger.recovery_outcome()
    assert outcome["success"] is True
    assert outcome["home_verified"] is True
    assert outcome["source_attempt_stays_failed"] is True


def test_receipt_merge_orders_events_across_owners_without_inventing_time() -> None:
    ledger = ledger_from_receipts(
        "mixed",
        dispatch_receipt={
            "lifecycle_events": [
                {"stage": "PATH", "event": "start", "timestamp_s": 10.0},
                {"stage": "STOP", "event": "requested", "timestamp_s": 70.0},
            ]
        },
        supervisor_result={
            "lifecycle_events": [
                {"stage": "HOME_CHECK", "event": "verified", "timestamp_s": 1.0},
                {"stage": "STOP", "event": "requested", "timestamp_s": 70.1},
                {"stage": "UNLOAD_RELIEF", "event": "start", "timestamp_s": 71.0},
                {"stage": "HOME", "event": "verified", "timestamp_s": 75.0},
            ]
        },
    )
    assert [row["stage"] for row in ledger.events] == [
        "HOME_CHECK", "PATH", "STOP", "UNLOAD_RELIEF", "HOME"
    ]
    assert ledger.events[-1]["timestamp_s"] == pytest.approx(75.0)
    assert ledger.supplemental_events == [
        {"stage": "STOP", "event": "requested", "timestamp_s": 70.1}
    ]
    assert ledger.missing_events == []


def test_writer_reducer_accepts_serialized_integer_echo_keys() -> None:
    writer = SimpleNamespace(
        robot_observations=[
            {"integer_echoes": {"26": 20}, "received_monotonic_s": 1.0},
            {"integer_echoes": {"26": 21}, "received_monotonic_s": 2.0},
            {"integer_echoes": {"26": 25}, "received_monotonic_s": 3.0},
            {"integer_echoes": {"26": 40}, "received_monotonic_s": 4.0},
            {"integer_echoes": {"26": 78}, "received_monotonic_s": 5.0},
        ],
        admission_robot_observations=[],
        _path_command_started_mono_s=3.1,
        _r013_path_end_request_mono_s=3.9,
    )
    events = lifecycle_events_from_writer(
        writer, home_check_s=0.0, stop_s=4.1, home_verified=True
    )
    assert {row["stage"] for row in events} == set(STAGES)
    assert next(row for row in events if row["stage"] == "PATH")["timestamp_s"] == pytest.approx(3.1)


def test_writer_reducer_ignores_stale_prearm_clearance_state() -> None:
    writer = SimpleNamespace(
        robot_observations=[
            {"integer_echoes": {26: 78}, "received_monotonic_s": 0.5},
            {"integer_echoes": {26: 20}, "received_monotonic_s": 1.0},
            {"integer_echoes": {26: 21}, "received_monotonic_s": 2.0},
            {"integer_echoes": {26: 25}, "received_monotonic_s": 3.0},
            {"integer_echoes": {26: 40}, "received_monotonic_s": 64.0},
            {"integer_echoes": {26: 78}, "received_monotonic_s": 65.0},
        ],
        admission_robot_observations=[],
        _path_command_started_mono_s=3.1,
        _r013_path_end_request_mono_s=63.9,
    )
    events = lifecycle_events_from_writer(writer, home_check_s=0.0, home_verified=True)
    clearance = next(row for row in events if row["stage"] == "CLEARANCE")
    home = next(row for row in events if row["stage"] == "HOME")
    assert clearance["timestamp_s"] == pytest.approx(65.0)
    assert home["timestamp_s"] == pytest.approx(65.0)


def test_writer_reducer_preserves_readiness_hold_boundaries() -> None:
    writer = SimpleNamespace(
        robot_observations=[
            {"integer_echoes": {26: 20}, "received_monotonic_s": 1.0},
            {"integer_echoes": {26: 21}, "received_monotonic_s": 2.0},
            {"integer_echoes": {26: 25}, "received_monotonic_s": 13.0},
            {"integer_echoes": {26: 40}, "received_monotonic_s": 74.0},
            {"integer_echoes": {26: 78}, "received_monotonic_s": 75.0},
        ],
        admission_robot_observations=[],
        _qualification_control=SimpleNamespace(
            readiness_hold_start_monotonic_s=3.0,
            readiness_hold_end_monotonic_s=13.0,
        ),
        _path_command_started_mono_s=13.1,
        _r013_path_end_request_mono_s=73.1,
    )
    events = lifecycle_events_from_writer(writer, home_check_s=0.0, home_verified=True)
    readiness = [row for row in events if row["stage"] == "READINESS_HOLD"]
    assert readiness == [
        {"stage": "READINESS_HOLD", "event": "start", "timestamp_s": 3.0},
        {"stage": "READINESS_HOLD", "event": "end", "timestamp_s": 13.0},
    ]
    ledger = ledger_from_receipts("readiness", lifecycle_events=events)
    assert ledger.durations()["readiness_hold_s"] == pytest.approx(10.0)


def test_home_verified_boundary_and_protocol_identity_are_not_downgraded() -> None:
    writer = SimpleNamespace(
        robot_observations=[
            {"integer_echoes": {26: 20}, "received_monotonic_s": 1.0},
            {"integer_echoes": {26: 21}, "received_monotonic_s": 2.0},
            {"integer_echoes": {26: 25}, "received_monotonic_s": 13.0},
            {"integer_echoes": {26: 40}, "received_monotonic_s": 74.0},
            {"integer_echoes": {26: 78}, "received_monotonic_s": 75.0},
        ],
        admission_robot_observations=[],
        _qualification_control=SimpleNamespace(
            readiness_hold_start_monotonic_s=3.0,
            readiness_hold_end_monotonic_s=13.0,
        ),
        _path_command_started_mono_s=13.1,
        _r013_path_end_request_mono_s=73.1,
    )
    events = lifecycle_events_from_writer(writer, home_check_s=0.0, home_verified=True)
    ledger = ledger_from_receipts("home", lifecycle_events=events)
    assert ledger.durations()["home_to_home_s"] == pytest.approx(75.0)
    with pytest.raises(TimingLedgerError, match="protocol"):
        ledger_from_receipts(
            "mixed",
            dispatch_receipt={"protocol_id": "contact_yield_full_period_v1"},
        )
    with pytest.raises(TimingLedgerError, match="live_path"):
        ledger_from_receipts(
            "mixed-live-path",
            dispatch_receipt={
                "live_path": {
                    "kind": "full_period",
                    "protocol_id": "contact_yield_full_period_v1",
                }
            },
        )
    with pytest.raises(TimingLedgerError, match="live_path kind"):
        ledger_from_receipts(
            "legacy-full-kind-only",
            dispatch_receipt={
                "live_path": {
                    "kind": "full_period",
                    "path_duration_s": 62.83185307179586,
                }
            },
        )
    with pytest.raises(TimingLedgerError, match="live_path kind"):
        ledger_from_receipts(
            "conflicting-kind-with-id",
            dispatch_receipt={
                "live_path": {
                    "kind": "full_period",
                    "protocol_id": R013_PROTOCOL_ID,
                    "path_duration_s": 62.83185307179586,
                }
            },
        )
    with pytest.raises(TimingLedgerError, match="explicit R013 identity"):
        ledger_from_receipts(
            "duration-only",
            dispatch_receipt={"live_path": {"path_duration_s": 60.0}},
        )


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
