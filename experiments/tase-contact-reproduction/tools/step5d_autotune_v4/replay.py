"""Offline V4 objective replay with actual timestamps and exact 550-bin closure."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence

from .contracts import TARGET_FORCE_N
from .runtime import TimingGuard


@dataclass(frozen=True)
class ReplayRow:
    monotonic_s: float
    filtered_normal_n: float
    raw_normal_n: float
    force_norm_n: float
    torque_norm_nm: float
    sensor_fresh: bool


@dataclass(frozen=True)
class ReplayResult:
    complete_bins: int
    required_bins: int
    mae_n: float | None
    bias_n: float | None
    std_n: float | None
    p99_normal_n: float | None
    max_force_norm_n: float | None
    max_torque_norm_nm: float | None
    timing: dict[str, float | bool | str]
    eligible_shape: bool


def replay(rows: Sequence[ReplayRow]) -> ReplayResult:
    timing_guard = TimingGuard()
    finite_rows: list[ReplayRow] = []
    for row in rows:
        timing_guard.observe(row.monotonic_s)
        if (
            row.sensor_fresh
            and all(
                math.isfinite(value)
                for value in (
                    row.monotonic_s,
                    row.filtered_normal_n,
                    row.raw_normal_n,
                    row.force_norm_n,
                    row.torque_norm_nm,
                )
            )
        ):
            finite_rows.append(row)
    if not finite_rows:
        return ReplayResult(
            0,
            550,
            None,
            None,
            None,
            None,
            None,
            None,
            timing_guard.acceptance(),
            False,
        )
    origin = finite_rows[0].monotonic_s
    bins: dict[int, list[float]] = {}
    force_norms: list[float] = []
    torques: list[float] = []
    raw_normals: list[float] = []
    for row in finite_rows:
        relative = row.monotonic_s - origin
        if not 5.0 <= relative < 60.0:
            continue
        index = int((relative - 5.0) / 0.1)
        if 0 <= index < 550:
            bins.setdefault(index, []).append(row.filtered_normal_n)
            force_norms.append(row.force_norm_n)
            torques.append(row.torque_norm_nm)
            raw_normals.append(row.raw_normal_n)
    complete = len(bins)
    timing = timing_guard.acceptance()
    if complete != 550:
        return ReplayResult(
            complete,
            550,
            None,
            None,
            None,
            None,
            max(force_norms, default=None),
            max(torques, default=None),
            timing,
            False,
        )
    binned = [statistics.fmean(bins[index]) for index in range(550)]
    errors = [value - TARGET_FORCE_N for value in binned]
    sorted_raw = sorted(raw_normals)
    p99_index = min(len(sorted_raw) - 1, math.ceil(0.99 * len(sorted_raw)) - 1)
    return ReplayResult(
        complete,
        550,
        statistics.fmean(abs(value) for value in errors),
        statistics.fmean(errors),
        statistics.pstdev(binned),
        sorted_raw[p99_index],
        max(force_norms),
        max(torques),
        timing,
        bool(timing["passed"]),
    )


__all__ = ["ReplayResult", "ReplayRow", "replay"]
