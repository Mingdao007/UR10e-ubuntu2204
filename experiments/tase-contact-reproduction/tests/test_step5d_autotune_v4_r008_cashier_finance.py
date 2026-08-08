"""Tests for cashier/finance roles, body reconcile, and canary stall gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from step5d_autotune_v4_r008.body_reconcile import (
    RECONCILE_JSONL_NAME,
    append_body_reconcile,
    append_reconcile_for_attempt,
    build_reconcile_row,
    summarize_observer_events,
)
from step5d_autotune_v4_r008.canary_body_gate import (
    StallGateState,
    evaluate_stall_gate,
)
from step5d_autotune_v4_r008.cashier_finance import (
    FORMAL_FINANCE_MAY_CLOSE_BOOKS,
    roles_document,
)
from step5d_autotune_v4_r008.state20_search_trace import STATE20_SEARCH_TRACE_SIDECAR_NAME


def test_roles_formal_finance_cannot_close_books() -> None:
    doc = roles_document()
    assert doc["roles"]["finance_body_observer"]["formal_may_close_books"] is False
    assert FORMAL_FINANCE_MAY_CLOSE_BOOKS is False


def test_reconcile_row_never_trainable(tmp_path: Path) -> None:
    row = build_reconcile_row(
        attempt_sequence=7,
        kind="BO_TRIAL",
        events=[
            {
                "event": "search_moving",
                "verdict": "moving_down",
                "host_claim_phase": "CONTACT_SEARCH",
                "detail": {"search_elapsed_s": 2.5},
            },
            {
                "event": "contacted_stalled_live",
                "verdict": "contacted_stalled",
                "host_claim_phase": "CONTACT_SEARCH",
                "packet_sequence": 99,
            },
        ],
    )
    assert row["trainable"] is False
    assert row["enters_gp_ledger"] is False
    assert row["search_looked_moving"] is True
    assert row["had_live_stall"] is True
    with pytest.raises(ValueError):
        append_body_reconcile(tmp_path, {**row, "trainable": True})


def test_append_reconcile_for_attempt_writes_sidecar(tmp_path: Path) -> None:
    obs = tmp_path / "r008-body-observer.jsonl"
    obs.write_text(
        json.dumps(
            {
                "event": "search_moving",
                "wall_time_s": 10.0,
                "verdict": "moving_down",
                "host_claim_phase": "CONTACT_SEARCH",
                "detail": {"search_elapsed_s": 1.0},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    row = append_reconcile_for_attempt(
        tmp_path,
        attempt_sequence=3,
        kind="SPACEFILL",
        wall_t0=9.0,
        wall_t1=11.0,
    )
    path = tmp_path / RECONCILE_JSONL_NAME
    assert path.is_file()
    saved = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert saved["attempt_sequence"] == 3
    assert saved["enters_gp_ledger"] is False
    assert row["search_moving_ticks"] == 1


def test_formal_stall_gate_never_aborts(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "host.log").write_text("CONTACT_SEARCH_START\n", encoding="utf-8")
    # Flat z + high force → contacted_stalled if packets advance
    path = run / STATE20_SEARCH_TRACE_SIDECAR_NAME
    rows = []
    for i in range(3):
        rows.append(
            {
                "schema": "step5d.autotune-v4/r008-state20-search-trace-v1",
                "packet_sequence": 100 + i,
                "wall_time_s": 1.0 + i,
                "monotonic_s": 1.0 + i,
                "tp_state": 20,
                "command_mode": 0,
                "force_norm_n": 6.0,
                "normal_load_n": 6.0,
                "filtered_normal_n": 6.0,
                "sensor_fresh": True,
                "tcp_pose_m_rad": [0.5, 0.1, 0.0195, 0.0, 0.0, 0.0],
                "tcp_speed_m_s_rad_s": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "stationary": True,
            }
        )
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    st = StallGateState()
    # Drive stall_since by evaluating twice with advancing time.
    d1 = evaluate_stall_gate(run, st, mode="formal", stall_abort_s=1.0, now=10.0)
    assert d1.abort is False
    # Add more packets
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({**rows[-1], "packet_sequence": 200, "wall_time_s": 20.0}, sort_keys=True)
            + "\n"
        )
    d2 = evaluate_stall_gate(run, st, mode="formal", stall_abort_s=1.0, now=20.0)
    assert d2.abort is False
    assert d2.mode == "formal"


def test_canary_stall_gate_aborts_after_threshold(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "host.log").write_text("CONTACT_SEARCH_START\n", encoding="utf-8")
    path = run / STATE20_SEARCH_TRACE_SIDECAR_NAME

    def _write(seq: int, t: float) -> None:
        row = {
            "schema": "step5d.autotune-v4/r008-state20-search-trace-v1",
            "packet_sequence": seq,
            "wall_time_s": t,
            "monotonic_s": t,
            "tp_state": 20,
            "command_mode": 0,
            "force_norm_n": 6.0,
            "normal_load_n": 6.0,
            "filtered_normal_n": 6.0,
            "sensor_fresh": True,
            "tcp_pose_m_rad": [0.5, 0.1, 0.0195, 0.0, 0.0, 0.0],
            "tcp_speed_m_s_rad_s": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "stationary": True,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    st = StallGateState()
    _write(1, 1.0)
    _write(2, 1.2)
    d0 = evaluate_stall_gate(run, st, mode="canary", stall_abort_s=2.0, now=10.0)
    assert d0.abort is False
    _write(3, 1.4)
    d1 = evaluate_stall_gate(run, st, mode="canary", stall_abort_s=2.0, now=11.0)
    assert d1.abort is False
    _write(4, 1.6)
    d2 = evaluate_stall_gate(run, st, mode="canary", stall_abort_s=2.0, now=13.5)
    assert d2.abort is True
    assert d2.reason is not None
    assert "stall" in d2.reason


def test_summarize_mismatch_counts() -> None:
    summary = summarize_observer_events(
        [
            {
                "event": "tick",
                "verdict": "contacted_stalled",
                "host_claim_phase": "CONTACT_SEARCH_START",
            }
        ]
    )
    assert summary["claim_vs_body_mismatch_ticks"] == 1
