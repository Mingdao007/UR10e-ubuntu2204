"""Strict, gap-preserving offline metric primitives for R013 paths."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping, Sequence

from .path_context import (
    FIGURE8_DURATION_S,
    FIGURE8_FORMAL_BIN_COUNT,
)


METRIC_FINGERPRINT_SCHEMA = "step5d.autotune-v4/r013-metric-fingerprint-v1"
METRIC_ACCUMULATOR_SCHEMA = "step5d.autotune-v4/r013-gap-preserving-metric-accumulator-v1"
METRIC_RESULT_SCHEMA = "step5d.autotune-v4/r013-metric-result-v1"
CYCLOID_METRIC = "force-mae-v2-sealed"
FIGURE8_METRIC = "figure8-force-mae-v1"
BIN_STATISTIC = "mean_abs(mean(normal_load_n_in_bin)-target_force_n)_over_bins"


class MetricError(ValueError):
    """A metric fingerprint, sample, or sealed result is invalid."""


class MetricGapError(MetricError):
    """The formal metric window contains one or more unobserved bins."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise MetricError(f"R013 {name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MetricError(f"R013 {name} must be numeric") from exc
    if not math.isfinite(result):
        raise MetricError(f"R013 {name} must be finite")
    return result


@dataclass(frozen=True)
class MetricFingerprintV1:
    metric_id: str
    target_force_n: float
    formal_start_s: float
    formal_end_s: float
    bin_width_s: float
    required_bin_count: int
    statistic: str = BIN_STATISTIC
    schema: str = METRIC_FINGERPRINT_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != METRIC_FINGERPRINT_SCHEMA or self.version != 1:
            raise MetricError("R013 metric fingerprint schema/version differs")
        if self.metric_id not in {CYCLOID_METRIC, FIGURE8_METRIC}:
            raise MetricError("R013 metric fingerprint id is unknown")
        target = _finite(self.target_force_n, "target_force_n")
        start = _finite(self.formal_start_s, "formal_start_s")
        end = _finite(self.formal_end_s, "formal_end_s")
        width = _finite(self.bin_width_s, "bin_width_s")
        if target != 5.0 or start != 5.0 or width != 0.1 or end <= start:
            raise MetricError("R013 metric fingerprint bounds differ")
        if type(self.required_bin_count) is not int or self.required_bin_count <= 0:
            raise MetricError("R013 metric required bin count is invalid")
        expected = int(round((end - start) / width))
        if expected != self.required_bin_count:
            raise MetricError("R013 metric bin count is not exact")
        expected_count = 550 if self.metric_id == CYCLOID_METRIC else FIGURE8_FORMAL_BIN_COUNT
        expected_end = 60.0 if self.metric_id == CYCLOID_METRIC else FIGURE8_DURATION_S
        if self.required_bin_count != expected_count or end != expected_end:
            raise MetricError("R013 metric fingerprint contract differs")
        if self.statistic != BIN_STATISTIC:
            raise MetricError("R013 metric statistic differs")
        object.__setattr__(self, "target_force_n", target)
        object.__setattr__(self, "formal_start_s", start)
        object.__setattr__(self, "formal_end_s", end)
        object.__setattr__(self, "bin_width_s", width)

    @classmethod
    def cycloid(cls) -> "MetricFingerprintV1":
        return cls(CYCLOID_METRIC, 5.0, 5.0, 60.0, 0.1, 550)

    @classmethod
    def figure8(cls) -> "MetricFingerprintV1":
        return cls(FIGURE8_METRIC, 5.0, 5.0, FIGURE8_DURATION_S, 0.1, FIGURE8_FORMAL_BIN_COUNT)

    @classmethod
    def from_metric_id(cls, metric_id: str) -> "MetricFingerprintV1":
        if metric_id == CYCLOID_METRIC:
            return cls.cycloid()
        if metric_id == FIGURE8_METRIC:
            return cls.figure8()
        raise MetricError("R013 metric fingerprint id is unknown")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "metric_id": self.metric_id,
            "target_force_n": self.target_force_n,
            "formal_start_s": self.formal_start_s,
            "formal_end_s": self.formal_end_s,
            "bin_width_s": self.bin_width_s,
            "required_bin_count": self.required_bin_count,
            "statistic": self.statistic,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MetricFingerprintV1":
        required = {
            "schema", "version", "metric_id", "target_force_n", "formal_start_s",
            "formal_end_s", "bin_width_s", "required_bin_count", "statistic",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise MetricError("R013 metric fingerprint fields differ")
        return cls(**dict(value))


@dataclass(frozen=True)
class MetricResultV1:
    metric_fingerprint: MetricFingerprintV1
    sealed: bool
    exact: bool
    formal_mae_n: float
    full_curve_mae_n: float | None
    transient_mae_n: float | None
    observed_formal_bin_count: int
    observed_full_bin_count: int
    sample_count: int
    observed_transient_bin_count: int = 0
    required_formal_bin_count: int = 0
    required_full_bin_count: int = 0
    required_transient_bin_count: int = 0
    formal_coverage: float = 0.0
    full_curve_coverage: float = 0.0
    transient_coverage: float = 0.0
    schema: str = METRIC_RESULT_SCHEMA
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "metric_fingerprint": self.metric_fingerprint.as_dict(),
            "sealed": self.sealed,
            "exact": self.exact,
            "formal_mae_n": self.formal_mae_n,
            "full_curve_mae_n": self.full_curve_mae_n,
            "transient_mae_n": self.transient_mae_n,
            "observed_formal_bin_count": self.observed_formal_bin_count,
            "observed_full_bin_count": self.observed_full_bin_count,
            "sample_count": self.sample_count,
            "observed_transient_bin_count": self.observed_transient_bin_count,
            "required_formal_bin_count": self.required_formal_bin_count,
            "required_full_bin_count": self.required_full_bin_count,
            "required_transient_bin_count": self.required_transient_bin_count,
            "formal_coverage": self.formal_coverage,
            "full_curve_coverage": self.full_curve_coverage,
            "transient_coverage": self.transient_coverage,
        }


class GapPreservingMetricAccumulatorV1:
    """Accumulates raw samples without interpolation or gap filling."""

    def __init__(self, metric_fingerprint: MetricFingerprintV1) -> None:
        if not isinstance(metric_fingerprint, MetricFingerprintV1):
            raise MetricError("R013 accumulator requires a typed metric fingerprint")
        self.metric_fingerprint = metric_fingerprint
        self._bins: dict[int, list[float]] = {}
        self._last_time_s: float | None = None
        self._sample_count = 0

    @property
    def schema(self) -> str:
        return METRIC_ACCUMULATOR_SCHEMA

    @property
    def bins(self) -> Mapping[int, tuple[float, ...]]:
        return {index: tuple(values) for index, values in self._bins.items()}

    def add_sample(self, *, time_s: float, normal_load_n: float) -> None:
        time_value = _finite(time_s, "sample time_s")
        load_value = _finite(normal_load_n, "normal_load_n")
        if time_value < 0.0 or time_value >= self.metric_fingerprint.formal_end_s:
            raise MetricError("R013 metric sample is outside the path interval")
        if self._last_time_s is not None and time_value < self._last_time_s:
            raise MetricError("R013 metric sample time is non-monotonic")
        self._last_time_s = time_value
        index = int(math.floor((time_value + 1e-12) / self.metric_fingerprint.bin_width_s))
        self._bins.setdefault(index, []).append(load_value)
        self._sample_count += 1

    def add_samples(self, samples: Iterable[Mapping[str, Any]]) -> None:
        for sample in samples:
            if not isinstance(sample, Mapping):
                raise MetricError("R013 metric sample row is invalid")
            self.add_sample(time_s=sample["time_s"], normal_load_n=sample["normal_load_n"])

    def _bin_error(self, index: int) -> float:
        values = self._bins.get(index)
        if not values:
            raise MetricGapError(f"R013 metric formal bin {index} is missing")
        mean_load = math.fsum(values) / len(values)
        return abs(mean_load - self.metric_fingerprint.target_force_n)

    def _mae_for_indices(self, indices: Sequence[int]) -> float | None:
        if not indices:
            return None
        if any(index not in self._bins for index in indices):
            return None
        errors = [self._bin_error(index) for index in indices]
        return math.fsum(errors) / len(errors)

    def seal(self) -> MetricResultV1:
        fingerprint = self.metric_fingerprint
        formal_start = int(round(fingerprint.formal_start_s / fingerprint.bin_width_s))
        formal_indices = tuple(range(formal_start, formal_start + fingerprint.required_bin_count))
        missing = tuple(index for index in formal_indices if index not in self._bins)
        if missing:
            raise MetricGapError(
                f"R013 {fingerprint.metric_id} cannot seal with formal gaps: {missing[:8]}"
            )
        full_end = int(round(fingerprint.formal_end_s / fingerprint.bin_width_s))
        full_indices = tuple(range(full_end))
        transient_indices = tuple(range(formal_start))
        formal_mae = math.fsum(self._bin_error(index) for index in formal_indices) / len(formal_indices)
        observed_formal = sum(index in self._bins for index in formal_indices)
        observed_full = sum(index in self._bins for index in full_indices)
        observed_transient = sum(index in self._bins for index in transient_indices)
        return MetricResultV1(
            metric_fingerprint=fingerprint,
            sealed=True,
            exact=True,
            formal_mae_n=formal_mae,
            full_curve_mae_n=self._mae_for_indices(full_indices),
            transient_mae_n=self._mae_for_indices(transient_indices),
            observed_formal_bin_count=observed_formal,
            observed_full_bin_count=observed_full,
            sample_count=self._sample_count,
            observed_transient_bin_count=observed_transient,
            required_formal_bin_count=len(formal_indices),
            required_full_bin_count=len(full_indices),
            required_transient_bin_count=len(transient_indices),
            formal_coverage=observed_formal / len(formal_indices),
            full_curve_coverage=observed_full / len(full_indices),
            transient_coverage=(
                observed_transient / len(transient_indices)
                if transient_indices else 1.0
            ),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": METRIC_ACCUMULATOR_SCHEMA,
            "version": 1,
            "metric_fingerprint": self.metric_fingerprint.as_dict(),
            "sample_count": self._sample_count,
            "observed_bins": sorted(self._bins),
            "last_time_s": self._last_time_s,
        }


__all__ = [
    "BIN_STATISTIC", "CYCLOID_METRIC", "FIGURE8_METRIC", "GapPreservingMetricAccumulatorV1",
    "MetricError", "MetricFingerprintV1", "MetricGapError", "MetricResultV1",
]
