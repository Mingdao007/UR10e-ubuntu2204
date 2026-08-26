"""Pure diagnostic receipt for the first five seconds of R013 PATH contact.

The receipt deliberately consumes observed State25 rows only.  It does not
interpolate missing samples, participate in optimizer scoring, or affect
physical admission.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


CONTACT_TRANSIENT_RECEIPT_SCHEMA = (
    "step5d.autotune-v4/r013-contact-transient-receipt-v1"
)
CONTACT_TRANSIENT_SIDECAR_SCHEMA = (
    "step5d.autotune-v4/r013-contact-transient-sidecar-v1"
)
CONTACT_TRANSIENT_SIDECAR_NAME = "r013-contact-transient-receipts.jsonl"
CONTACT_TRANSIENT_WINDOW_S = 5.0
CONTACT_TRANSIENT_BAND_HALF_WIDTH_N = 0.5
CONTACT_TRANSIENT_BAND_HOLD_S = 1.0
CONTACT_TRANSIENT_TARGET_FORCE_N = 5.0


class ContactTransientError(ValueError):
    """The State25 observations cannot form a trustworthy transient receipt."""


def _finite_float(value: Any, *, label: str, index: int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContactTransientError(f"row {index} {label} is not numeric") from exc
    if not math.isfinite(number):
        raise ContactTransientError(f"row {index} {label} is nonfinite")
    return number


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def contact_transient_receipt(
    rows: Sequence[Mapping[str, Any]],
    *,
    target_force_n: float = CONTACT_TRANSIENT_TARGET_FORCE_N,
) -> dict[str, Any]:
    """Compute the versioned ``[0, 5)`` contact-transient receipt.

    Rows must be strictly ordered by ``relative_path_time_s``.  For callers
    that provide only the authoritative ``path_time_s``, the first row is
    used as the local origin.  Force selection prefers
    ``filtered_normal_n`` per row and falls back to ``normal_load_n`` only
    when the filtered value is absent or ``None``.
    """

    if not rows:
        raise ContactTransientError("State25 rows are empty")
    target = _finite_float(target_force_n, label="target_force_n", index=-1)
    if any(not isinstance(row, Mapping) for row in rows):
        bad_index = next(index for index, row in enumerate(rows) if not isinstance(row, Mapping))
        raise ContactTransientError(f"row {bad_index} is not an object")
    observations: list[tuple[float, float]] = []
    relative_time_present = ["relative_path_time_s" in row for row in rows]
    if all(relative_time_present):
        time_field = "relative_path_time_s"
    elif not any(relative_time_present):
        time_field = "path_time_s"
    else:
        raise ContactTransientError("rows mix relative and authoritative path time")

    raw_times: list[float] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ContactTransientError(f"row {index} is not an object")
        if time_field not in row or row[time_field] is None:
            raise ContactTransientError(f"row {index} is missing {time_field}")
        raw_times.append(_finite_float(row[time_field], label=time_field, index=index))

        filtered = row.get("filtered_normal_n")
        if filtered is None:
            if "normal_load_n" not in row or row["normal_load_n"] is None:
                raise ContactTransientError(
                    f"row {index} is missing filtered_normal_n and normal_load_n"
                )
            force = _finite_float(row["normal_load_n"], label="normal_load_n", index=index)
        else:
            force = _finite_float(filtered, label="filtered_normal_n", index=index)
        observations.append((raw_times[-1], force))

    if time_field == "path_time_s":
        origin = observations[0][0]
        times = [time - origin for time, _ in observations]
    else:
        times = [time for time, _ in observations]
    if times[0] < 0.0:
        raise ContactTransientError("State25 path time starts before zero")
    for index, (previous, current) in enumerate(zip(times, times[1:]), 1):
        if current <= previous:
            raise ContactTransientError(
                f"State25 path time is unsorted at rows {index - 1}/{index}"
            )

    window = [
        (time, force)
        for time, (_, force) in zip(times, observations, strict=True)
        if 0.0 <= time < CONTACT_TRANSIENT_WINDOW_S
    ]
    if not window:
        raise ContactTransientError("State25 rows contain no samples in [0, 5) s")

    window_times = [time for time, _ in window]
    forces = [force for _, force in window]
    absolute_errors = [abs(force - target) for force in forces]
    gaps = [
        current - previous
        for previous, current in zip(window_times, window_times[1:])
    ]

    band_start: float | None = None
    first_band_hold: float | None = None
    had_band_entry = False
    rebound_count = 0
    previous_in_band = False
    for time, force in window:
        in_band = abs(force - target) <= CONTACT_TRANSIENT_BAND_HALF_WIDTH_N
        if in_band:
            if band_start is None:
                band_start = time
                had_band_entry = True
            if (
                first_band_hold is None
                and band_start is not None
                and time - band_start >= CONTACT_TRANSIENT_BAND_HOLD_S
            ):
                first_band_hold = band_start
        else:
            if had_band_entry and previous_in_band:
                rebound_count += 1
            band_start = None
        previous_in_band = in_band

    return {
        "schema": CONTACT_TRANSIENT_RECEIPT_SCHEMA,
        "target_force_n": target,
        "window_s": [0.0, CONTACT_TRANSIENT_WINDOW_S],
        "sample_count": len(window),
        "first_force_n": forces[0],
        "min_force_n": min(forces),
        "max_force_n": max(forces),
        "mae_to_target_n": sum(absolute_errors) / len(absolute_errors),
        "p95_abs_error_n": _nearest_rank(absolute_errors, 0.95),
        "max_abs_error_n": max(absolute_errors),
        "maximum_gap_s": max(gaps, default=0.0),
        "first_continuous_band_1s_s": first_band_hold,
        "rebound_count": rebound_count,
    }


__all__ = [
    "CONTACT_TRANSIENT_BAND_HALF_WIDTH_N",
    "CONTACT_TRANSIENT_BAND_HOLD_S",
    "CONTACT_TRANSIENT_RECEIPT_SCHEMA",
    "CONTACT_TRANSIENT_SIDECAR_NAME",
    "CONTACT_TRANSIENT_SIDECAR_SCHEMA",
    "CONTACT_TRANSIENT_TARGET_FORCE_N",
    "CONTACT_TRANSIENT_WINDOW_S",
    "ContactTransientError",
    "contact_transient_receipt",
]
