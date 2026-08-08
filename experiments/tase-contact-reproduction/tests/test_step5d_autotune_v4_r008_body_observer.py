"""Offline tests for r008 body observer (no robot)."""

from __future__ import annotations

import json
from pathlib import Path

from step5d_autotune_v4_r008.body_observer import (
    BodyObserverState,
    ObserverEventKind,
    observe_once,
    search_duration_report,
)
from step5d_autotune_v4_r008.robot_body_truth import BodyVerdict
from step5d_autotune_v4_r008.state20_search_trace import STATE20_SEARCH_TRACE_SIDECAR_NAME


def _write_state20(run_dir: Path, rows: list[dict]) -> None:
    path = run_dir / STATE20_SEARCH_TRACE_SIDECAR_NAME
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _row(
    *,
    seq: int,
    z: float,
    t: float,
    tp: int = 20,
    force: float = 0.2,
    claim_phase_via_host: bool = True,
) -> dict:
    return {
        "schema": "step5d.autotune-v4/r008-state20-search-trace-v1",
        "packet_sequence": seq,
        "wall_time_s": t,
        "monotonic_s": t,
        "tp_state": tp,
        "command_mode": 0,
        "force_norm_n": force,
        "normal_load_n": force,
        "filtered_normal_n": force,
        "sensor_fresh": True,
        "tcp_pose_m_rad": [0.5, 0.1, z, 0.0, 0.0, 0.0],
        "tcp_speed_m_s_rad_s": [0.0, 0.0, -0.002 if tp == 20 and force < 1 else 0.0, 0.0, 0.0, 0.0],
        "stationary": force >= 1.0,
    }


def test_search_moving_and_frozen_packet_no_double_stall(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    # Fake host claim via host.log
    (run / "host.log").write_text("CONTACT_SEARCH_START stuff\n", encoding="utf-8")
    _write_state20(
        run,
        [
            _row(seq=1, z=0.030, t=1.0, force=0.2),
            _row(seq=2, z=0.027, t=1.2, force=0.3),
        ],
    )
    st = BodyObserverState()
    ev1 = observe_once(run, st, host_live=True, dashboard_playing=True, now=10.0)
    kinds = {e["event"] for e in ev1}
    assert ObserverEventKind.SEARCH_MOVING.value in kinds

    # Same packets (no new state20) → no contacted_stalled_live spam
    ev2 = observe_once(run, st, host_live=True, dashboard_playing=True, now=11.0)
    assert not any(e["event"] == ObserverEventKind.CONTACTED_STALLED_LIVE.value for e in ev2)


def test_host_dead_robot_playing(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _write_state20(run, [_row(seq=5, z=0.02, t=1.0, force=6.0)])
    st = BodyObserverState()
    ev = observe_once(run, st, host_live=False, dashboard_playing=True, now=20.0)
    assert any(e["event"] == ObserverEventKind.HOST_DEAD_ROBOT_PLAYING.value for e in ev)


def test_search_report_reads_phase_timings(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    pt = run / "r008-phase-timings.jsonl"
    pt.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v4/r008-phase-timings-v1",
                "attempt_sequence": 1,
                "durations_s": {"contact_search_s": 18.9},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rep = search_duration_report(run)
    assert rep["recent_mean_s"] == 18.9
    assert rep["planned_search_s"] == 8.89
