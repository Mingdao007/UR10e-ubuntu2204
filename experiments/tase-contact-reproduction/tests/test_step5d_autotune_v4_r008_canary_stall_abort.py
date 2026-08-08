"""Offline tests for canary stall abort policy (no robot)."""

from __future__ import annotations

import json
from pathlib import Path

from step5d_autotune_v4_r008.body_observer import (
    CANARY_STALL_ABORT_ARTIFACT,
    CanaryStallAbortState,
    ObserverEventKind,
    canary_stall_abort_tick,
    write_canary_stall_abort_artifact,
)
from step5d_autotune_v4_r008.state20_search_trace import STATE20_SEARCH_TRACE_SIDECAR_NAME


def _write_state20(run_dir: Path, rows: list[dict]) -> None:
    path = run_dir / STATE20_SEARCH_TRACE_SIDECAR_NAME
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _stall_row(*, seq: int, z: float, t: float) -> dict:
    return {
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
        "tcp_pose_m_rad": [0.5, 0.1, z, 0.0, 0.0, 0.0],
        "tcp_speed_m_s_rad_s": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "stationary": True,
    }


def _seed_search_run(run: Path, *, seq: int, z: float, t: float) -> None:
    (run / "host.log").write_text("CONTACT_SEARCH_START\n", encoding="utf-8")
    _write_state20(run, [_stall_row(seq=seq, z=z, t=t)])


def test_formal_mode_never_returns_abort(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _seed_search_run(run, seq=1, z=0.02, t=1.0)
    st = CanaryStallAbortState()
    t0 = 100.0
    _events, abort = canary_stall_abort_tick(
        run, st, canary_mode=False, sustained_s=8.0, now=t0
    )
    assert abort is None
    _write_state20(
        run,
        [
            _stall_row(seq=1, z=0.02, t=1.0),
            _stall_row(seq=2, z=0.02, t=1.1),
        ],
    )
    _events, abort = canary_stall_abort_tick(
        run, st, canary_mode=False, sustained_s=8.0, now=t0 + 20.0
    )
    assert abort is None


def test_canary_aborts_after_sustained_stall(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _seed_search_run(run, seq=1, z=0.02, t=1.0)
    st = CanaryStallAbortState()
    t0 = 200.0
    _events, abort = canary_stall_abort_tick(
        run, st, canary_mode=True, sustained_s=8.0, now=t0
    )
    assert abort is None
    _write_state20(
        run,
        [
            _stall_row(seq=1, z=0.02, t=1.0),
            _stall_row(seq=2, z=0.01999, t=1.1),
        ],
    )
    events, abort = canary_stall_abort_tick(
        run, st, canary_mode=True, sustained_s=8.0, now=t0 + 1.0
    )
    assert any(e["event"] == ObserverEventKind.CONTACTED_STALLED_LIVE.value for e in events)
    assert abort is None
    _events, abort = canary_stall_abort_tick(
        run, st, canary_mode=True, sustained_s=8.0, now=t0 + 9.0
    )
    assert abort is not None
    assert abort["reason"] == "contacted_stalled_sustained"
    assert abort["sustained_s"] >= 8.0
    assert abort["canary_mode"] is True


def test_stall_clears_before_threshold_no_abort(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _seed_search_run(run, seq=1, z=0.02, t=1.0)
    st = CanaryStallAbortState()
    t0 = 300.0
    _write_state20(
        run,
        [
            _stall_row(seq=1, z=0.02, t=1.0),
            _stall_row(seq=2, z=0.01999, t=1.1),
        ],
    )
    canary_stall_abort_tick(run, st, canary_mode=True, sustained_s=8.0, now=t0 + 1.0)
    _write_state20(
        run,
        [
            _stall_row(seq=1, z=0.02, t=1.0),
            _stall_row(seq=2, z=0.01999, t=1.1),
            {
                **_stall_row(seq=3, z=0.015, t=1.2),
                "force_norm_n": 0.2,
                "normal_load_n": 0.2,
                "filtered_normal_n": 0.2,
                "stationary": False,
                "tcp_speed_m_s_rad_s": [0.0, 0.0, -0.003, 0.0, 0.0, 0.0],
            },
        ],
    )
    _events, abort = canary_stall_abort_tick(
        run, st, canary_mode=True, sustained_s=8.0, now=t0 + 20.0
    )
    assert abort is None


def test_frozen_packet_does_not_start_stall_timer(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "host.log").write_text("CONTACT_SEARCH_START\n", encoding="utf-8")
    _write_state20(run, [_stall_row(seq=5, z=0.02, t=1.0)])
    st = CanaryStallAbortState()
    canary_stall_abort_tick(run, st, canary_mode=True, sustained_s=8.0, now=50.0)
    _events, abort = canary_stall_abort_tick(
        run, st, canary_mode=True, sustained_s=8.0, now=100.0
    )
    assert st.stall_wall_t0 is None
    assert abort is None


def test_write_abort_artifact(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    doc = {"reason": "contacted_stalled_sustained", "sustained_s": 8.1}
    path = write_canary_stall_abort_artifact(run, doc)
    assert path.name == CANARY_STALL_ABORT_ARTIFACT
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["reason"] == "contacted_stalled_sustained"


def test_gate_policy_formal_never_aborts(tmp_path: Path) -> None:
    from step5d_autotune_v4_r008.canary_body_gate import (
        StallGateState,
        canary_stall_abort_policy,
    )

    run = tmp_path / "run"
    run.mkdir()
    _seed_search_run(run, seq=1, z=0.02, t=1.0)
    st = StallGateState()
    t0 = 400.0
    _write_state20(
        run,
        [
            _stall_row(seq=1, z=0.02, t=1.0),
            _stall_row(seq=2, z=0.01999, t=1.1),
        ],
    )
    canary_stall_abort_policy(run, st, mode="formal", stall_abort_s=8.0, now=t0 + 1.0)
    decision = canary_stall_abort_policy(
        run, st, mode="formal", stall_abort_s=8.0, now=t0 + 20.0
    )
    assert decision.abort is False
    assert decision.mode == "formal"
