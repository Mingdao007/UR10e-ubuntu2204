"""Pure timing reduction for a TASE campaign attempt receipt."""
from __future__ import annotations

from typing import Any, Mapping


_STAGES = (
    "HOME_CHECK",
    "CONTACT_SEARCH",
    "CONTACT_LATCH",
    "QUALIFICATION",
    "READINESS_HOLD",
    "ENTRY",
    "PATH",
    "STOP",
    "UNLOAD_RELIEF",
    "CLEARANCE",
    "HOME",
)


def reduce_attempt_timing(
    lifecycle_events: list[Mapping[str, Any]],
    *,
    attempt_started_at: float | None = None,
    attempt_finished_at: float | None = None,
) -> dict[str, Any]:
    """Return stage timestamps and elapsed durations without touching devices."""
    timestamps: dict[str, float] = {}
    for event in lifecycle_events:
        stage = str(event.get("stage", ""))
        if stage not in _STAGES:
            continue
        value = float(event["timestamp_s"])
        if stage not in timestamps:
            timestamps[stage] = value
    durations: dict[str, float] = {}
    ordered = [stage for stage in _STAGES if stage in timestamps]
    for left, right in zip(ordered, ordered[1:]):
        durations[f"{left}_to_{right}"] = timestamps[right] - timestamps[left]
    result: dict[str, Any] = {
        "schema": "tase.attempt-timing-v1",
        "timestamps_s": timestamps,
        "durations_s": durations,
        "complete_lifecycle": all(stage in timestamps for stage in _STAGES),
    }
    if attempt_started_at is not None:
        result["attempt_started_at"] = float(attempt_started_at)
    if attempt_finished_at is not None:
        result["attempt_finished_at"] = float(attempt_finished_at)
        if attempt_started_at is not None:
            result["wall_duration_s"] = float(attempt_finished_at) - float(attempt_started_at)
    return result
