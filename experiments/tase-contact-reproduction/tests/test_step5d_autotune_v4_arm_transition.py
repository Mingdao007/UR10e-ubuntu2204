from __future__ import annotations

import pytest

from step5d_autotune_v4_r004.arm_transition import classify_arm_transition


def _poll(
    ts: float,
    *,
    wall: float,
    thread_ns: int,
    drained: int,
    sequence: int | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "poll_start_mono_s": 1.0,
        "poll_end_mono_s": 1.0 + wall,
        "poll_thread_start_ns": 10_000_000,
        "poll_thread_end_ns": 10_000_000 + thread_ns,
        "output_timestamp": ts,
        "recv": {
            "poll_thread_duration_ns": thread_ns,
            "recv": {"drained_packet_count": drained},
        },
    }
    if sequence is not None:
        row["consumed_packet_sequence"] = sequence
    return row


def test_arm_transition_queued_drain_is_diagnostic_and_not_an_override() -> None:
    result = classify_arm_transition(
        [
            _poll(10.0, wall=0.001, thread_ns=1_000_000, drained=1),
            {
                **_poll(10.044, wall=0.001, thread_ns=3_000_000, drained=22),
                "poll_start_mono_s": 1.045,
                "poll_thread_start_ns": 12_000_000,
                "poll_thread_end_ns": 15_000_000,
            },
        ]
    )
    assert result["classification"] == "queued_while_host_paused"
    assert result["hard_gap_s"] == pytest.approx(0.044)
    assert result["edge"]["previous_consumed_packet_sequence"] is None
    assert result["edge"]["current_consumed_packet_sequence"] is None
    assert result["admission_override_allowed"] is False


def test_arm_transition_source_silence_stays_a_hard_failure() -> None:
    result = classify_arm_transition(
        [
            _poll(10.0, wall=0.001, thread_ns=0, drained=0),
            {
                **_poll(10.044, wall=0.001, thread_ns=0, drained=0),
                "poll_start_mono_s": 1.045,
                "poll_thread_start_ns": 55_000_000,
                "poll_thread_end_ns": 55_000_000,
            },
        ]
    )
    assert result["classification"] == "host_on_cpu_boundary_work"
    assert result["hard_gate_preserved"] is False


def test_arm_transition_missing_thread_or_recv_evidence_is_not_zero() -> None:
    result = classify_arm_transition(
        [
            {"output_timestamp": 10.0, "fresh": True, "poll_start_mono_s": 1.0, "poll_end_mono_s": 1.001},
            {"output_timestamp": 10.044, "fresh": True, "poll_start_mono_s": 1.045, "poll_end_mono_s": 1.046},
        ]
    )
    assert result["classification"] == "insufficient_evidence"
    assert result["drained_packet_count"] is None
    assert result["max_between_poll_thread_s"] is None
    assert result["poll_thread_duration_s"] is None
    assert result["hard_gate_preserved"] is False


def test_arm_transition_drained_count_is_bound_to_max_gap_edge() -> None:
    rows = [
        {**_poll(10.000, wall=0.001, thread_ns=1_000_000, drained=99, sequence=10), "poll_start_mono_s": 1.0},
        {**_poll(10.004, wall=0.001, thread_ns=1_000_000, drained=0, sequence=11), "poll_start_mono_s": 1.002},
        {**_poll(10.044, wall=0.001, thread_ns=1_000_000, drained=3, sequence=12), "poll_start_mono_s": 1.043},
    ]
    rows[1]["poll_thread_start_ns"] = 11_000_000
    rows[1]["poll_thread_end_ns"] = 12_000_000
    rows[2]["poll_thread_start_ns"] = 13_000_000
    rows[2]["poll_thread_end_ns"] = 14_000_000
    result = classify_arm_transition(rows)
    assert result["hard_gap_s"] == pytest.approx(0.040)
    assert result["drained_packet_count"] == 3
    assert result["edge"]["previous_index"] == 1
    assert result["edge"]["current_index"] == 2
    assert result["edge"]["previous_consumed_packet_sequence"] == 11
    assert result["edge"]["current_consumed_packet_sequence"] == 12
    assert result["classification"] == "queued_while_host_paused"


def test_arm_transition_reads_outer_current_poll_durations() -> None:
    current = _poll(10.044, wall=0.001, thread_ns=2_000_000, drained=2, sequence=11)
    current["recv"] = {
        "poll_wall_duration_ns": 7_000_000,
        "poll_thread_duration_ns": 4_000_000,
        "recv": {
            "drained_packet_count": 2,
            "oldest_timestamp": 10.040,
            "latest_timestamp": 10.044,
        },
    }
    result = classify_arm_transition(
        [_poll(10.0, wall=0.001, thread_ns=1_000_000, drained=1, sequence=10), current]
    )
    assert result["poll_duration_s"] == pytest.approx(0.007)
    assert result["poll_thread_duration_s"] == pytest.approx(0.004)
    assert result["drained_packet_count"] == 2
    assert result["drained_timestamp_span_s"] == pytest.approx(0.004)


@pytest.mark.parametrize(
    ("expected", "thread_gap_s", "markers"),
    [
        ("host_on_cpu_boundary_work", 0.010, None),
        (
            "host_off_cpu_involuntary",
            0.001,
            (
                {
                    "name": "arm_echo_observed",
                    "poll_count": 1,
                    "monotonic_s": 0.0,
                    "thread_time_ns": 0,
                    "voluntary_context_switches": 0,
                    "involuntary_context_switches": 0,
                },
                {
                    "name": "first_hot_loop_poll",
                    "poll_count": 2,
                    "monotonic_s": 0.040,
                    "thread_time_ns": 1_000_000,
                    "voluntary_context_switches": 0,
                    "involuntary_context_switches": 1,
                },
            ),
        ),
        (
            "host_voluntary_block",
            0.001,
            (
                {
                    "name": "arm_echo_observed",
                    "poll_count": 1,
                    "monotonic_s": 0.0,
                    "thread_time_ns": 0,
                    "voluntary_context_switches": 0,
                    "involuntary_context_switches": 0,
                },
                {
                    "name": "first_hot_loop_poll",
                    "poll_count": 2,
                    "monotonic_s": 0.040,
                    "thread_time_ns": 1_000_000,
                    "voluntary_context_switches": 1,
                    "involuntary_context_switches": 0,
                },
            ),
        ),
    ],
)
def test_arm_transition_host_boundary_classifications(
    expected: str,
    thread_gap_s: float,
    markers: tuple[dict[str, object], ...] | None,
) -> None:
    previous = _poll(10.0, wall=0.001, thread_ns=1_000_000, drained=0, sequence=10)
    current = _poll(10.044, wall=0.001, thread_ns=1_000_000, drained=0, sequence=11)
    current["poll_start_mono_s"] = 1.001 + 0.040
    current["poll_thread_start_ns"] = 11_000_000 + int(thread_gap_s * 1_000_000_000)
    current["poll_thread_end_ns"] = current["poll_thread_start_ns"] + 1_000_000
    result = classify_arm_transition([previous, current], markers=markers)
    assert result["classification"] == expected


def test_arm_transition_markers_select_the_exact_edge_and_missing_markers_fail_closed() -> None:
    rows = [
        {
            **_poll(10.000, wall=0.001, thread_ns=1_000_000, drained=1, sequence=10),
            "poll_start_mono_s": 1.000,
            "poll_end_mono_s": 1.001,
        },
        {
            **_poll(10.040, wall=0.001, thread_ns=1_000_000, drained=20, sequence=11),
            "poll_start_mono_s": 1.041,
            "poll_end_mono_s": 1.042,
            "poll_thread_start_ns": 50_000_000,
            "poll_thread_end_ns": 51_000_000,
        },
        {
            "fresh": False,
            "output_timestamp": None,
            "consumed_packet_sequence": None,
            "poll_start_mono_s": 1.050,
            "poll_end_mono_s": 1.051,
            "poll_thread_start_ns": 51_100_000,
            "poll_thread_end_ns": 51_200_000,
            "recv": {},
        },
        {
            **_poll(10.070, wall=0.001, thread_ns=1_000_000, drained=0, sequence=12),
            "poll_start_mono_s": 1.072,
            "poll_end_mono_s": 1.073,
            "poll_thread_start_ns": 52_000_000,
            "poll_thread_end_ns": 53_000_000,
        },
    ]
    markers = (
        {
            "name": "arm_echo_observed",
            "poll_count": 2,
            "monotonic_s": 1.0421,
            "thread_time_ns": 51_100_000,
            "voluntary_context_switches": 0,
            "involuntary_context_switches": 0,
        },
        {
            "name": "first_hot_loop_poll",
            "poll_count": 4,
            "monotonic_s": 1.0731,
            "thread_time_ns": 52_100_000,
            "voluntary_context_switches": 0,
            "involuntary_context_switches": 1,
        },
    )

    result = classify_arm_transition(rows, markers=markers)
    assert result["edge_source"] == "arm_echo_to_first_hot_loop_poll"
    assert result["edge"]["previous_index"] == 1
    assert result["edge"]["current_index"] == 3
    assert result["hard_gap_s"] == pytest.approx(0.030)
    assert result["drained_packet_count"] == 0
    assert result["classification"] == "host_off_cpu_involuntary"

    missing = classify_arm_transition(rows, markers=())
    assert missing["classification"] == "insufficient_evidence"
    assert missing["edge"] is None


def test_arm_transition_packet_sequence_regression_is_insufficient() -> None:
    result = classify_arm_transition(
        [
            _poll(10.0, wall=0.001, thread_ns=1_000_000, drained=0, sequence=12),
            _poll(10.044, wall=0.001, thread_ns=1_000_000, drained=1, sequence=11),
        ]
    )
    assert result["classification"] == "insufficient_evidence"
    assert result["edge"] is None
    assert result["hard_gap_s"] is None


def test_arm_transition_source_silence_requires_poll_duration_evidence() -> None:
    result = classify_arm_transition(
        [
            _poll(10.0, wall=0.001, thread_ns=1_000_000, drained=0),
            {
                **_poll(10.044, wall=0.045, thread_ns=2_000_000, drained=0),
                "poll_start_mono_s": 1.001,
                "poll_end_mono_s": 1.046,
                "poll_thread_start_ns": 11_000_000,
                "poll_thread_end_ns": 13_000_000,
            },
        ]
    )
    assert result["classification"] == "controller_source_silence"
