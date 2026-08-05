"""Track A1: durable host phase-timing marks and duration splits."""

from __future__ import annotations

from pathlib import Path

from step5d_autotune_v4_r008.live_adapter import R008HostLoop
from step5d_autotune_v4_r008.phase_timings import (
    PHASE_TIMINGS_SCHEMA,
    append_phase_timings_sidecar,
    build_phase_timings_s,
    compute_phase_durations_s,
    marks_from_events,
    parse_phase_event,
    wrap_path_sample_sink_for_stage25,
)


def test_parse_and_marks_from_events() -> None:
    events = [
        "HOME:t+1.000:dt=1.000",
        "ARM:t+5.000:dt=2.000",
        "CONTACT_SEARCH_START:t+5.001:dt=0.001",
        "STAGE25_START:t+40.000:dt=34.999",
        "SEARCH_DONE:t+40.000:dt=0.000",
        "RUN_60S:t+100.000:dt=60.000",
        "SAFE_RETURN:t+101.000:dt=1.000",
        "SEAL:t+102.500:dt=1.500",
        "RESUME_COLD_READ_VERIFIED",
    ]
    marks = marks_from_events(events)
    assert marks["HOME"] == 1.0
    assert marks["CONTACT_SEARCH_START"] == 5.001
    assert marks["STAGE25_START"] == 40.0
    assert "RESUME_COLD_READ_VERIFIED" not in marks
    assert parse_phase_event("not-a-mark") is None


def test_compute_phase_durations_splits_search_and_path() -> None:
    marks = {
        "HOME": 10.0,
        "DISPATCH": 12.0,
        "ARM": 15.0,
        "CONTACT_SEARCH_START": 15.01,
        "STAGE25_START": 55.0,
        "RUN_60S": 120.0,
        "SAFE_RETURN": 121.0,
        "SEAL": 123.0,
    }
    durations = compute_phase_durations_s(marks)
    assert durations["home_s"] == 10.0
    assert durations["contact_search_s"] == 55.0 - 15.01
    assert durations["path_60_s"] == 120.0 - 55.0
    assert durations["safe_return_s"] == 1.0
    assert durations["seal_s"] == 2.0


def test_build_phase_timings_payload_and_sidecar(tmp_path: Path) -> None:
    marks = {
        "HOME": 1.0,
        "ARM": 2.0,
        "CONTACT_SEARCH_START": 2.0,
        "STAGE25_START": 30.0,
        "RUN_60S": 90.0,
        "SAFE_RETURN": 91.0,
        "SEAL": 92.0,
    }
    payload = build_phase_timings_s(marks, attempt_sequence=7, kind="BO_TRIAL")
    assert payload["schema"] == PHASE_TIMINGS_SCHEMA
    assert payload["attempt_sequence"] == 7
    assert payload["durations_s"]["contact_search_s"] == 28.0
    path = append_phase_timings_sidecar(tmp_path, payload)
    assert path.name == "r008-phase-timings.jsonl"
    assert path.read_text(encoding="utf-8").strip()


def test_wrap_path_sample_sink_notifies_every_path_sample() -> None:
    seen: list[int] = []
    calls: list[str] = []

    class _Sample:
        def __init__(self, state: int | None, path_time_s: float | None) -> None:
            self.state = state
            self.path_time_s = path_time_s

    def sink(sample: _Sample) -> None:
        seen.append(int(sample.state or -1))

    wrapped = wrap_path_sample_sink_for_stage25(
        sink, on_stage25_start=lambda: calls.append("stage25")
    )
    wrapped(_Sample(20, None))
    wrapped(_Sample(25, 0.0))
    wrapped(_Sample(25, 0.1))
    # HostLoop.notify_stage25_start debounces per attempt; wrapper must not.
    assert calls == ["stage25", "stage25"]
    assert seen == [20, 25, 25]


def test_host_loop_phase_marks_include_search_split() -> None:
    loop = object.__new__(R008HostLoop)
    loop.events = []
    loop._phase_origin_s = None
    loop._phase_last_s = None
    loop._phase_marks = {}
    loop._stage25_marked = False

    loop._begin_phase_clock()
    loop._phase("HOME")
    loop._phase("DISPATCH")
    loop._phase("ARM")
    assert "CONTACT_SEARCH_START" in loop._phase_marks
    loop.notify_stage25_start()
    loop.notify_stage25_start()  # idempotent
    assert "STAGE25_START" in loop._phase_marks
    assert "SEARCH_DONE" in loop._phase_marks
    loop._phase("RUN_60S")
    loop._phase("SAFE_RETURN")
    loop._phase("SEAL")
    durations = compute_phase_durations_s(loop._phase_marks)
    assert durations["contact_search_s"] is not None
    assert durations["path_60_s"] is not None
    assert durations["seal_s"] is not None
