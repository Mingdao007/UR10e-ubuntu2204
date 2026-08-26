"""Comparable force-bin metrics for R014 exact observations."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

from .common import R014Error, finite, sha256_value


BIN_WIDTH_S = 0.1
DURATION_S = 60.0
TARGET_N = 5.0
PRIMARY_BINS = 600
SECONDARY_START_BIN = 50
SECONDARY_BINS = 550


METRIC_CONTRACT = {
    "schema": "step5d.autotuner-r014/force-bin-metric-v1",
    "bin_width_s": BIN_WIDTH_S,
    "interval": "[0,60)",
    "primary": "mean(abs(mean(force_bin)-5N)); 600 complete bins",
    "secondary": "same over [5,60); 550 complete bins",
}
METRIC_FINGERPRINT = sha256_value(METRIC_CONTRACT)


@dataclass(frozen=True)
class ForceMetric:
    primary_mae_n: float
    secondary_mae_n: float
    bin_means_n: tuple[float, ...]
    metric_fingerprint: str = METRIC_FINGERPRINT

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary_mae_n": self.primary_mae_n,
            "secondary_mae_n": self.secondary_mae_n,
            "complete_bins": len(self.bin_means_n),
            "metric_fingerprint": self.metric_fingerprint,
        }


def force_bin_metric(rows: Iterable[tuple[float, float]]) -> ForceMetric:
    sums = [0.0] * PRIMARY_BINS
    counts = [0] * PRIMARY_BINS
    for raw_time, raw_force in rows:
        time_s = finite(raw_time, "time_s")
        force_n = finite(raw_force, "force_n")
        if not 0.0 <= time_s < DURATION_S:
            continue
        index = min(PRIMARY_BINS - 1, int(math.floor(time_s / BIN_WIDTH_S)))
        sums[index] += force_n
        counts[index] += 1
    missing = [index for index, count in enumerate(counts) if count == 0]
    if missing:
        raise R014Error(
            f"exact force metric requires 600 complete bins; missing={len(missing)} first={missing[0]}"
        )
    means = tuple(total / count for total, count in zip(sums, counts))
    errors = tuple(abs(value - TARGET_N) for value in means)
    return ForceMetric(
        primary_mae_n=sum(errors) / PRIMARY_BINS,
        secondary_mae_n=sum(errors[SECONDARY_START_BIN:]) / SECONDARY_BINS,
        bin_means_n=means,
    )
