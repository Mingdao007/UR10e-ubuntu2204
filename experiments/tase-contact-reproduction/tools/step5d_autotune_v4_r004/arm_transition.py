"""Fail-closed, edge-aligned ARM-to-CONTACT poll telemetry classifier."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


ARM_TRANSITION_TELEMETRY_SCHEMA = "step5d.autotune-v4/r004-arm-transition-classification-v3"
ARM_TRANSITION_TELEMETRY_VERSION = 3


class ArmTransitionTelemetryError(ValueError):
    pass


def _finite(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ArmTransitionTelemetryError(f"{name} is not finite") from exc
    if not math.isfinite(parsed):
        raise ArmTransitionTelemetryError(f"{name} is not finite")
    return parsed


def _optional_finite(value: Any, name: str) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _recv_mapping(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    recv = row.get("recv")
    if not isinstance(recv, Mapping):
        return None
    nested = recv.get("recv")
    return nested if isinstance(nested, Mapping) else recv


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _marker_deltas(markers: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    rows = [dict(row) for row in (markers or ()) if isinstance(row, Mapping)]
    result: list[dict[str, Any]] = []
    for previous, current in zip(rows, rows[1:]):
        previous_wall = _optional_finite(previous.get("monotonic_s"), "marker monotonic")
        current_wall = _optional_finite(current.get("monotonic_s"), "marker monotonic")
        previous_thread = _optional_int(previous.get("thread_time_ns"))
        current_thread = _optional_int(current.get("thread_time_ns"))
        if previous_wall is None or current_wall is None:
            continue
        row: dict[str, Any] = {
            "from": previous.get("name"),
            "to": current.get("name"),
            "wall_s": current_wall - previous_wall,
            "thread_cpu_s": None,
            "voluntary_context_switches": None,
            "involuntary_context_switches": None,
        }
        if previous_thread is not None and current_thread is not None:
            row["thread_cpu_s"] = max(0.0, (current_thread - previous_thread) / 1_000_000_000.0)
        previous_v = _optional_int(previous.get("voluntary_context_switches"))
        current_v = _optional_int(current.get("voluntary_context_switches"))
        previous_i = _optional_int(previous.get("involuntary_context_switches"))
        current_i = _optional_int(current.get("involuntary_context_switches"))
        if previous_v is not None and current_v is not None:
            row["voluntary_context_switches"] = max(0, current_v - previous_v)
        if previous_i is not None and current_i is not None:
            row["involuntary_context_switches"] = max(0, current_i - previous_i)
        result.append(row)
    return result


def _marker_span(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return one exact marker span without hiding malformed counters."""

    previous_wall = _optional_finite(previous.get("monotonic_s"), "marker monotonic")
    current_wall = _optional_finite(current.get("monotonic_s"), "marker monotonic")
    if previous_wall is None or current_wall is None or current_wall < previous_wall:
        return None
    previous_tid = _optional_int(previous.get("native_tid"))
    current_tid = _optional_int(current.get("native_tid"))
    if (
        previous_tid is not None
        and current_tid is not None
        and previous_tid != current_tid
    ):
        return None
    row: dict[str, Any] = {
        "from": previous.get("name"),
        "to": current.get("name"),
        "from_poll_count": _optional_int(previous.get("poll_count")),
        "to_poll_count": _optional_int(current.get("poll_count")),
        "wall_s": current_wall - previous_wall,
        "thread_cpu_s": None,
        "voluntary_context_switches": None,
        "involuntary_context_switches": None,
    }
    previous_thread = _optional_int(previous.get("thread_time_ns"))
    current_thread = _optional_int(current.get("thread_time_ns"))
    if previous_thread is not None and current_thread is not None:
        if current_thread < previous_thread:
            return None
        row["thread_cpu_s"] = (current_thread - previous_thread) / 1_000_000_000.0
    for field in ("voluntary_context_switches", "involuntary_context_switches"):
        previous_count = _optional_int(previous.get(field))
        current_count = _optional_int(current.get(field))
        if previous_count is not None and current_count is not None:
            if current_count < previous_count:
                return None
            row[field] = current_count - previous_count
    return row


def _marker_aligned_edge(
    fresh: Sequence[tuple[int, dict[str, Any], float | None, int | None]],
    markers: Sequence[Mapping[str, Any]],
    *,
    threshold: float,
) -> tuple[
    tuple[int, dict[str, Any], float, int, dict[str, Any], float],
    dict[str, Any],
] | None:
    """Bind the classifier to ARM echo -> first hot-loop poll exactly."""

    marker_rows = [dict(row) for row in markers if isinstance(row, Mapping)]
    starts = [row for row in marker_rows if row.get("name") == "arm_echo_observed"]
    ends = [row for row in marker_rows if row.get("name") == "first_hot_loop_poll"]
    if len(starts) != 1 or len(ends) != 1:
        return None
    start = starts[0]
    end = ends[0]
    span = _marker_span(start, end)
    if span is None:
        return None

    by_index = {item[0]: item for item in fresh}
    start_count = _optional_int(start.get("poll_count"))
    end_count = _optional_int(end.get("poll_count"))
    previous: tuple[int, dict[str, Any], float | None, int | None] | None = None
    current: tuple[int, dict[str, Any], float | None, int | None] | None = None
    if start_count is not None or end_count is not None:
        if (
            start_count is None
            or end_count is None
            or start_count <= 0
            or end_count <= start_count
        ):
            return None
        previous = by_index.get(start_count - 1)
        current = by_index.get(end_count - 1)
    else:
        # Historical v1 markers did not carry poll_count.  Their monotonic
        # timestamps can still bind a row only when both markers immediately
        # follow their respective polls in the same clock domain.
        start_wall = _optional_finite(start.get("monotonic_s"), "marker monotonic")
        end_wall = _optional_finite(end.get("monotonic_s"), "marker monotonic")
        if start_wall is None or end_wall is None:
            return None
        before_start = [
            item
            for item in fresh
            if (
                (poll_end := _optional_finite(item[1].get("poll_end_mono_s"), "poll end"))
                is not None
                and 0.0 <= start_wall - poll_end < threshold
            )
        ]
        if before_start:
            previous = before_start[-1]
            after_previous = [
                item
                for item in fresh
                if item[0] > previous[0]
                and (
                    (poll_end := _optional_finite(item[1].get("poll_end_mono_s"), "poll end"))
                    is not None
                    and 0.0 <= end_wall - poll_end < threshold
                )
            ]
            if after_previous:
                current = after_previous[-1]
    if previous is None or current is None or current[0] <= previous[0]:
        return None
    previous_timestamp = previous[2]
    current_timestamp = current[2]
    if previous_timestamp is None or current_timestamp is None:
        return None
    if (
        previous[3] is not None
        and current[3] is not None
        and current[3] < previous[3]
    ):
        return None
    if not math.isfinite(current_timestamp - previous_timestamp) or current_timestamp < previous_timestamp:
        return None
    return (
        (
            previous[0],
            previous[1],
            previous_timestamp,
            current[0],
            current[1],
            current_timestamp,
        ),
        span,
    )


def _insufficient(*, threshold: float, markers: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    return {
        "schema": ARM_TRANSITION_TELEMETRY_SCHEMA,
        "version": ARM_TRANSITION_TELEMETRY_VERSION,
        "status": "insufficient_poll_evidence",
        "hard_gap_s": None,
        "host_wall_gap_s": None,
        "max_between_poll_wall_s": None,
        "max_between_poll_thread_s": None,
        "thread_cpu_gap_s": None,
        "poll_duration_s": None,
        "poll_thread_duration_s": None,
        "drained_packet_count": None,
        "drained_timestamp_span_s": None,
        "edge": None,
        "edge_source": None,
        "marker_edge": None,
        "marker_deltas": _marker_deltas(markers),
        "classification": "insufficient_evidence",
        "admission_override_allowed": False,
        "hard_gate_preserved": False,
        "gap_threshold_s": threshold,
    }


def classify_arm_transition(
    polls: Sequence[Mapping[str, Any]],
    *,
    gap_threshold_s: float = 0.020,
    markers: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify the exact largest fresh-poll edge without changing admission.

    All optional evidence stays absent when the source did not record it.  A
    missing value therefore cannot masquerade as a zero-duration CPU interval
    or an empty RTDE drain.
    """

    threshold = _finite(gap_threshold_s, "gap threshold")
    if threshold <= 0.0:
        raise ArmTransitionTelemetryError("gap threshold must be positive")
    rows = [dict(row) for row in polls if isinstance(row, Mapping)]
    fresh: list[tuple[int, dict[str, Any], float | None, int | None]] = []
    for index, row in enumerate(rows):
        if row.get("fresh") is False:
            continue
        fresh.append(
            (
                index,
                row,
                _optional_finite(row.get("output_timestamp"), "output timestamp"),
                _optional_int(row.get("consumed_packet_sequence")),
            )
        )
    if len(fresh) < 2:
        return _insufficient(threshold=threshold, markers=markers)

    if any(item[2] is None for item in fresh):
        return _insufficient(threshold=threshold, markers=markers)
    packet_sequence_present = any(item[3] is not None for item in fresh)
    if packet_sequence_present and any(item[3] is None for item in fresh):
        return _insufficient(threshold=threshold, markers=markers)

    edge: tuple[int, dict[str, Any], float, int, dict[str, Any], float] | None = None
    edge_source = "largest_fresh_gap"
    marker_edge: dict[str, Any] | None = None
    if markers is not None:
        aligned = _marker_aligned_edge(fresh, markers, threshold=threshold)
        if aligned is None:
            return _insufficient(threshold=threshold, markers=markers)
        edge, marker_edge = aligned
        edge_source = "arm_echo_to_first_hot_loop_poll"
    else:
        best_gap = -1.0
        for previous, current in zip(fresh, fresh[1:]):
            previous_timestamp = previous[2]
            current_timestamp = current[2]
            if previous_timestamp is None or current_timestamp is None:
                return _insufficient(threshold=threshold, markers=markers)
            if packet_sequence_present:
                previous_sequence = previous[3]
                current_sequence = current[3]
                if (
                    previous_sequence is None
                    or current_sequence is None
                    or current_sequence < previous_sequence
                ):
                    return _insufficient(threshold=threshold, markers=markers)
            gap = current_timestamp - previous_timestamp
            if not math.isfinite(gap) or gap < 0.0:
                return _insufficient(threshold=threshold, markers=markers)
            if edge is None or gap > best_gap:
                edge = (
                    previous[0],
                    previous[1],
                    previous_timestamp,
                    current[0],
                    current[1],
                    current_timestamp,
                )
                best_gap = gap
    if edge is None:
        return _insufficient(threshold=threshold, markers=markers)

    previous_index, previous_row, previous_ts, current_index, current_row, current_ts = edge
    timestamp_gap = current_ts - previous_ts
    previous_end = _optional_finite(previous_row.get("poll_end_mono_s"), "previous poll end")
    current_start = _optional_finite(current_row.get("poll_start_mono_s"), "current poll start")
    current_end = _optional_finite(current_row.get("poll_end_mono_s"), "current poll end")
    previous_thread_end = _optional_int(previous_row.get("poll_thread_end_ns"))
    current_thread_start = _optional_int(current_row.get("poll_thread_start_ns"))
    current_thread_end = _optional_int(current_row.get("poll_thread_end_ns"))
    wall_gap = (
        max(0.0, current_start - previous_end)
        if previous_end is not None and current_start is not None
        else None
    )
    thread_gap = (
        max(0.0, (current_thread_start - previous_thread_end) / 1_000_000_000.0)
        if previous_thread_end is not None and current_thread_start is not None
        else None
    )
    poll_duration = None
    raw_poll_duration = _optional_int(current_row.get("poll_wall_duration_ns"))
    outer_poll = current_row.get("recv")
    outer_poll = outer_poll if isinstance(outer_poll, Mapping) else None
    if raw_poll_duration is None and outer_poll is not None:
        raw_poll_duration = _optional_int(outer_poll.get("poll_wall_duration_ns"))
    if raw_poll_duration is not None:
        poll_duration = raw_poll_duration / 1_000_000_000.0
    elif current_end is not None and current_start is not None:
        poll_duration = max(0.0, current_end - current_start)
    recv = _recv_mapping(current_row)
    drained = None if recv is None else _optional_int(recv.get("drained_packet_count"))
    if recv is not None:
        oldest = _optional_finite(recv.get("oldest_timestamp"), "oldest drained timestamp")
        latest = _optional_finite(recv.get("latest_timestamp"), "latest drained timestamp")
    else:
        oldest = latest = None
    drained_span = (
        max(0.0, latest - oldest)
        if oldest is not None and latest is not None
        else None
    )
    thread_duration = None
    raw_thread_duration = _optional_int(current_row.get("poll_thread_duration_ns"))
    if raw_thread_duration is None and outer_poll is not None:
        raw_thread_duration = _optional_int(outer_poll.get("poll_thread_duration_ns"))
    if raw_thread_duration is not None:
        thread_duration = raw_thread_duration / 1_000_000_000.0
    elif current_thread_start is not None and current_thread_end is not None:
        thread_duration = max(
            0.0,
            (current_thread_end - current_thread_start) / 1_000_000_000.0,
        )

    available_gaps = [timestamp_gap]
    if wall_gap is not None:
        available_gaps.append(wall_gap)
    host_gap = max(available_gaps)
    host_edge_complete = wall_gap is not None and thread_gap is not None
    marker_deltas = _marker_deltas(markers)
    if host_gap < threshold:
        classification = "within_threshold"
    elif (
        host_edge_complete
        and wall_gap >= threshold
        and thread_gap >= threshold * 0.25
    ):
        # Exact current-thread CPU time outranks the consequential socket
        # backlog: queued RTDE packets prove source continuity, not an off-CPU
        # host pause.
        classification = "host_on_cpu_boundary_work"
    elif host_edge_complete and wall_gap >= threshold and thread_gap < threshold * 0.25:
        # A poll edge without context-switch evidence cannot distinguish an
        # involuntary deschedule, a voluntary block, IRQ interference, or an
        # unobserved call.  Keep the result fail-closed until markers bind it.
        if int((marker_edge or {}).get("involuntary_context_switches") or 0) > 0:
            classification = "host_off_cpu_involuntary"
        elif int((marker_edge or {}).get("voluntary_context_switches") or 0) > 0:
            classification = "host_voluntary_block"
        elif drained is not None and drained > 0:
            classification = "queued_while_host_paused"
        else:
            classification = "insufficient_evidence"
    elif wall_gap is not None and wall_gap < threshold and poll_duration is not None and poll_duration >= threshold:
        classification = "controller_source_silence"
    elif drained is not None and drained > 0:
        classification = "queued_while_host_paused"
    else:
        classification = "insufficient_evidence"

    return {
        "schema": ARM_TRANSITION_TELEMETRY_SCHEMA,
        "version": ARM_TRANSITION_TELEMETRY_VERSION,
        "status": "classified",
        "hard_gap_s": timestamp_gap,
        "host_wall_gap_s": wall_gap,
        "max_between_poll_wall_s": wall_gap,
        "max_between_poll_thread_s": thread_gap,
        "thread_cpu_gap_s": thread_gap,
        "poll_duration_s": poll_duration,
        "poll_thread_duration_s": thread_duration,
        "drained_packet_count": drained,
        "drained_timestamp_span_s": drained_span,
        "edge": {
            "previous_index": previous_index,
            "current_index": current_index,
            "previous_output_timestamp": previous_ts,
            "current_output_timestamp": current_ts,
            "previous_consumed_packet_sequence": previous_row.get("consumed_packet_sequence"),
            "current_consumed_packet_sequence": current_row.get("consumed_packet_sequence"),
        },
        "edge_source": edge_source,
        "marker_edge": marker_edge,
        "marker_deltas": marker_deltas,
        "classification": classification,
        "admission_override_allowed": False,
        "hard_gate_preserved": host_gap < threshold,
        "gap_threshold_s": threshold,
    }


__all__ = [
    "ARM_TRANSITION_TELEMETRY_SCHEMA",
    "ARM_TRANSITION_TELEMETRY_VERSION",
    "ArmTransitionTelemetryError",
    "classify_arm_transition",
]
