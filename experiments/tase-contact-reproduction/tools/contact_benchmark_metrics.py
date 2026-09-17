"""Truthful time-weighted metrics for the six-controller contact benchmark.

The benchmark metric is independent of the historical fixed-width/bin metrics.
Each sample carries its own force reference and both measured and reference
Cartesian positions.  A sample is held only until the next sample when that
interval is no longer than ``max_gap_s``.  The hold is a bookkeeping rule for
the sampled signal; it does not interpolate a missing interval.  Leading and
trailing intervals, and gaps larger than ``max_gap_s``, remain uncovered.

The interval is half-open, ``[start_s, end_s)``.  A sample at ``end_s`` can
close the preceding interval but contributes no duration of its own.  A
complete result therefore needs an in-range sample at the start and a sample
at (or just beyond) the end with no large gaps between them.  An interrupted
result is censored even when its elapsed coverage happens to be complete, and
never exposes an optimizer objective.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any


METRIC_SCHEMA = "ur10e.contact-benchmark-metrics-v1"
METRIC_VERSION = 1
_SAMPLE_FIELDS = {
    "time_s",
    "force_n",
    "reference_force_n",
    "position_m",
    "reference_position_m",
}


class ContactMetricError(ValueError):
    """An input or metric state cannot be admitted to the benchmark metric."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ContactMetricError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContactMetricError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ContactMetricError(f"{name} must be a finite number")
    return number


def _vector3(value: Any, name: str) -> tuple[float, float, float]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or len(value) != 3
    ):
        raise ContactMetricError(f"{name} must contain exactly 3 values")
    return tuple(
        _finite(item, f"{name}[{index}]") for index, item in enumerate(value)
    )  # type: ignore[return-value]


def _force_error(force_n: float, reference_force_n: float) -> float:
    error = force_n - reference_force_n
    if not math.isfinite(error):
        raise ContactMetricError("force error must be finite")
    return error


def _path_error(
    position_m: tuple[float, float, float],
    reference_position_m: tuple[float, float, float],
) -> float:
    deltas = tuple(
        actual - reference
        for actual, reference in zip(position_m, reference_position_m)
    )
    squared = math.fsum(delta * delta for delta in deltas)
    if not math.isfinite(squared):
        raise ContactMetricError("3D path error must be finite")
    error = math.sqrt(squared)
    if not math.isfinite(error):
        raise ContactMetricError("3D path error must be finite")
    return error


@dataclass(frozen=True)
class ContactBenchmarkConfig:
    """Explicit metric interval and the largest admitted sample-to-sample gap."""

    start_s: float
    end_s: float
    max_gap_s: float

    def __post_init__(self) -> None:
        start = _finite(self.start_s, "start_s")
        end = _finite(self.end_s, "end_s")
        max_gap = _finite(self.max_gap_s, "max_gap_s")
        if end <= start:
            raise ContactMetricError("end_s must be greater than start_s")
        if max_gap <= 0.0:
            raise ContactMetricError("max_gap_s must be positive")
        object.__setattr__(self, "start_s", start)
        object.__setattr__(self, "end_s", end)
        object.__setattr__(self, "max_gap_s", max_gap)

    @property
    def elapsed_s(self) -> float:
        return self.end_s - self.start_s

    def as_dict(self) -> dict[str, float]:
        return {
            "start_s": self.start_s,
            "end_s": self.end_s,
            "max_gap_s": self.max_gap_s,
            "elapsed_s": self.elapsed_s,
        }


@dataclass(frozen=True)
class ContactSample:
    """One aligned force/path observation.

    ``force_n`` and ``reference_force_n`` are scalar normal/contact force
    values in N.  The path fields are measured and reference Cartesian points
    in metres.  References are intentionally per-sample so a changing force
    profile is represented without a hidden global target.
    """

    time_s: float
    force_n: float
    reference_force_n: float
    position_m: tuple[float, float, float]
    reference_position_m: tuple[float, float, float]

    def __post_init__(self) -> None:
        time_s = _finite(self.time_s, "time_s")
        force_n = _finite(self.force_n, "force_n")
        reference_force_n = _finite(self.reference_force_n, "reference_force_n")
        position_m = _vector3(self.position_m, "position_m")
        reference_position_m = _vector3(
            self.reference_position_m, "reference_position_m"
        )
        _force_error(force_n, reference_force_n)
        _path_error(position_m, reference_position_m)
        object.__setattr__(self, "time_s", time_s)
        object.__setattr__(self, "force_n", force_n)
        object.__setattr__(self, "reference_force_n", reference_force_n)
        object.__setattr__(self, "position_m", position_m)
        object.__setattr__(self, "reference_position_m", reference_position_m)

    @property
    def force_error_n(self) -> float:
        return self.force_n - self.reference_force_n

    @property
    def path_error_m(self) -> float:
        return _path_error(self.position_m, self.reference_position_m)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContactSample":
        if not isinstance(value, Mapping) or set(value) != _SAMPLE_FIELDS:
            raise ContactMetricError(
                "contact sample fields must be exactly "
                "time_s, force_n, reference_force_n, position_m, reference_position_m"
            )
        return cls(
            time_s=value["time_s"],
            force_n=value["force_n"],
            reference_force_n=value["reference_force_n"],
            position_m=value["position_m"],
            reference_position_m=value["reference_position_m"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "time_s": self.time_s,
            "force_n": self.force_n,
            "reference_force_n": self.reference_force_n,
            "position_m": list(self.position_m),
            "reference_position_m": list(self.reference_position_m),
        }


def _coerce_sample(
    value: ContactSample | Mapping[str, Any] | Sequence[Any],
) -> ContactSample:
    if isinstance(value, ContactSample):
        return value
    if isinstance(value, Mapping):
        return ContactSample.from_mapping(value)
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) == 5
    ):
        return ContactSample(*value)
    raise ContactMetricError("contact sample must be ContactSample, mapping, or 5-tuple")


@dataclass(frozen=True)
class ContactMetrics:
    """Computed metrics plus explicit coverage and admission state.

    ``missing_segment_count`` counts uncovered time segments, not an invented
    number of rows.  No nominal sample rate is assumed, so the exact missing
    evidence is represented by ``missing_intervals_s`` and
    ``missing_duration_s``.  MAE, RMSE, and path RMS are normalized over
    covered duration.  The peak fields report the largest observed point in
    the target interval, including an observed terminal endpoint, without
    filling any gap.
    """

    interval_start_s: float
    interval_end_s: float
    max_gap_s: float
    elapsed_duration_s: float
    covered_duration_s: float
    missing_duration_s: float
    coverage_fraction: float
    sample_count: int
    in_interval_sample_count: int
    covered_sample_count: int
    missing_segment_count: int
    large_gap_count: int
    missing_intervals_s: tuple[tuple[float, float], ...]
    force_mae_n: float | None
    force_rmse_n: float | None
    peak_force_error_n: float | None
    path_rms_m: float | None
    path_peak_m: float | None
    interrupted: bool
    coverage_complete: bool
    complete: bool
    censored: bool
    objective_eligible: bool
    schema: str = METRIC_SCHEMA
    version: int = METRIC_VERSION

    @property
    def status(self) -> str:
        return "complete" if self.complete else "censored"

    @property
    def objective(self) -> float | None:
        """The force RMSE objective, exposed only for complete trials."""

        if not self.objective_eligible:
            return None
        return self.force_rmse_n

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "interval_start_s": self.interval_start_s,
            "interval_end_s": self.interval_end_s,
            "max_gap_s": self.max_gap_s,
            "elapsed_duration_s": self.elapsed_duration_s,
            "covered_duration_s": self.covered_duration_s,
            "missing_duration_s": self.missing_duration_s,
            "coverage_fraction": self.coverage_fraction,
            "sample_count": self.sample_count,
            "in_interval_sample_count": self.in_interval_sample_count,
            "covered_sample_count": self.covered_sample_count,
            "missing_segment_count": self.missing_segment_count,
            "large_gap_count": self.large_gap_count,
            "missing_intervals_s": [list(interval) for interval in self.missing_intervals_s],
            "force_mae_n": self.force_mae_n,
            "force_rmse_n": self.force_rmse_n,
            "peak_force_error_n": self.peak_force_error_n,
            "path_rms_m": self.path_rms_m,
            "path_peak_m": self.path_peak_m,
            "interrupted": self.interrupted,
            "coverage_complete": self.coverage_complete,
            "complete": self.complete,
            "censored": self.censored,
            "objective_eligible": self.objective_eligible,
            "objective": self.objective,
            "status": self.status,
        }


def _merge_intervals(
    intervals: Iterable[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return ()
    merged: list[list[float]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        current = merged[-1]
        if start <= current[1]:
            current[1] = max(current[1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


class ContactMetricsAccumulator:
    """Streaming accumulator for the contact benchmark metric.

    ``add_sample`` accepts observations in timestamp order.  It computes a
    sample's contribution only when the next timestamp arrives, so a final
    sample before ``end_s`` is intentionally left as missing terminal
    coverage.  Call ``finalize(interrupted=True)`` for an out-of-band stop;
    that result remains censored and has no objective.
    """

    def __init__(self, config: ContactBenchmarkConfig) -> None:
        if not isinstance(config, ContactBenchmarkConfig):
            raise ContactMetricError("config must be ContactBenchmarkConfig")
        self.config = config
        self._last_sample: ContactSample | None = None
        self._has_in_interval_sample = False
        self._in_interval_sample_count = 0
        self._last_in_interval_time_s: float | None = None
        self._sample_count = 0
        self._covered_sample_count = 0
        self._covered_duration_s = 0.0
        self._force_abs_integral = 0.0
        self._force_sq_integral = 0.0
        self._path_sq_integral = 0.0
        self._peak_force_error_n: float | None = None
        self._path_peak_m: float | None = None
        self._missing_intervals: list[tuple[float, float]] = []
        self._large_gap_count = 0
        self._finalized_result: ContactMetrics | None = None
        self._finalized_interrupted: bool | None = None

    @staticmethod
    def _add_finite(total: float, term: float, name: str) -> float:
        if not math.isfinite(term):
            raise ContactMetricError(f"{name} contribution is not finite")
        result = total + term
        if not math.isfinite(result):
            raise ContactMetricError(f"{name} accumulator is not finite")
        return result

    def _record_covered_segment(
        self,
        sample: ContactSample,
        start_s: float,
        end_s: float,
    ) -> None:
        duration_s = end_s - start_s
        if duration_s <= 0.0:
            return
        force_error_n = abs(sample.force_error_n)
        path_error_m = sample.path_error_m
        self._covered_duration_s = self._add_finite(
            self._covered_duration_s, duration_s, "covered duration"
        )
        self._force_abs_integral = self._add_finite(
            self._force_abs_integral,
            force_error_n * duration_s,
            "force MAE",
        )
        self._force_sq_integral = self._add_finite(
            self._force_sq_integral,
            force_error_n * force_error_n * duration_s,
            "force RMSE",
        )
        self._path_sq_integral = self._add_finite(
            self._path_sq_integral,
            path_error_m * path_error_m * duration_s,
            "path RMS",
        )
        self._covered_sample_count += 1
        if self._peak_force_error_n is None or force_error_n > self._peak_force_error_n:
            self._peak_force_error_n = force_error_n
        if self._path_peak_m is None or path_error_m > self._path_peak_m:
            self._path_peak_m = path_error_m

    def _record_pair(self, previous: ContactSample, current: ContactSample) -> None:
        start_s = self.config.start_s
        end_s = self.config.end_s
        if not start_s <= previous.time_s < end_s:
            return
        delta_s = current.time_s - previous.time_s
        if delta_s <= 0.0:
            return
        segment_end_s = min(current.time_s, end_s)
        if segment_end_s <= previous.time_s:
            return
        if delta_s <= self.config.max_gap_s or math.isclose(
            delta_s, self.config.max_gap_s, rel_tol=0.0, abs_tol=1e-12
        ):
            self._record_covered_segment(previous, previous.time_s, segment_end_s)
        else:
            self._missing_intervals.append((previous.time_s, segment_end_s))
            self._large_gap_count += 1

    def add_sample(
        self,
        sample: ContactSample | Mapping[str, Any] | Sequence[Any],
    ) -> "ContactMetricsAccumulator":
        if self._finalized_result is not None:
            raise ContactMetricError("cannot add a sample after finalize")
        parsed = _coerce_sample(sample)
        if (
            self._last_sample is not None
            and parsed.time_s < self._last_sample.time_s
        ):
            raise ContactMetricError("sample timestamps must be monotonically increasing")
        if self._last_sample is not None:
            self._record_pair(self._last_sample, parsed)
        if self.config.start_s <= parsed.time_s <= self.config.end_s:
            force_error_n = abs(parsed.force_error_n)
            path_error_m = parsed.path_error_m
            if (
                self._peak_force_error_n is None
                or force_error_n > self._peak_force_error_n
            ):
                self._peak_force_error_n = force_error_n
            if self._path_peak_m is None or path_error_m > self._path_peak_m:
                self._path_peak_m = path_error_m
        if self.config.start_s <= parsed.time_s < self.config.end_s:
            if not self._has_in_interval_sample:
                self._has_in_interval_sample = True
                if parsed.time_s > self.config.start_s:
                    self._missing_intervals.append(
                        (self.config.start_s, parsed.time_s)
                    )
            self._in_interval_sample_count += 1
            self._last_in_interval_time_s = parsed.time_s
        self._last_sample = parsed
        self._sample_count += 1
        return self

    def add_samples(
        self,
        samples: Iterable[ContactSample | Mapping[str, Any] | Sequence[Any]],
    ) -> "ContactMetricsAccumulator":
        for sample in samples:
            self.add_sample(sample)
        return self

    def finalize(self, *, interrupted: bool = False) -> ContactMetrics:
        if not isinstance(interrupted, bool):
            raise ContactMetricError("interrupted must be a bool")
        if self._finalized_result is not None:
            if interrupted != self._finalized_interrupted:
                raise ContactMetricError("finalize interruption state differs")
            return self._finalized_result

        if not self._has_in_interval_sample:
            self._missing_intervals.append(
                (self.config.start_s, self.config.end_s)
            )
        elif self._last_sample is not None and self._last_sample.time_s < self.config.end_s:
            # There was no sample at or after the terminal endpoint.  The
            # interval after the last observed point is therefore unknown.
            assert self._last_in_interval_time_s is not None
            self._missing_intervals.append(
                (self._last_in_interval_time_s, self.config.end_s)
            )

        missing_intervals = _merge_intervals(self._missing_intervals)
        elapsed_s = self.config.elapsed_s
        missing_duration_s = math.fsum(
            end_s - start_s for start_s, end_s in missing_intervals
        )
        if not math.isfinite(missing_duration_s):
            raise ContactMetricError("missing duration is not finite")
        missing_duration_s = min(max(missing_duration_s, 0.0), elapsed_s)
        covered_duration_s = elapsed_s - missing_duration_s
        coverage_fraction = covered_duration_s / elapsed_s
        if not math.isfinite(coverage_fraction):
            raise ContactMetricError("coverage fraction is not finite")
        coverage_complete = not missing_intervals
        if coverage_complete:
            # Avoid exposing a round-off-sized uncovered tail when the
            # explicit interval partition has no missing segment.
            covered_duration_s = elapsed_s
            missing_duration_s = 0.0
            coverage_fraction = 1.0

        has_metric_duration = covered_duration_s > 0.0
        force_mae_n: float | None
        force_rmse_n: float | None
        path_rms_m: float | None
        if has_metric_duration:
            force_mae_n = self._force_abs_integral / covered_duration_s
            force_rmse_n = math.sqrt(self._force_sq_integral / covered_duration_s)
            path_rms_m = math.sqrt(self._path_sq_integral / covered_duration_s)
            for value, name in (
                (force_mae_n, "force MAE"),
                (force_rmse_n, "force RMSE"),
                (path_rms_m, "path RMS"),
            ):
                if not math.isfinite(value):
                    raise ContactMetricError(f"{name} is not finite")
        else:
            force_mae_n = None
            force_rmse_n = None
            path_rms_m = None

        complete = coverage_complete and not interrupted
        result = ContactMetrics(
            interval_start_s=self.config.start_s,
            interval_end_s=self.config.end_s,
            max_gap_s=self.config.max_gap_s,
            elapsed_duration_s=elapsed_s,
            covered_duration_s=covered_duration_s,
            missing_duration_s=missing_duration_s,
            coverage_fraction=coverage_fraction,
            sample_count=self._sample_count,
            in_interval_sample_count=self._in_interval_sample_count,
            covered_sample_count=self._covered_sample_count,
            missing_segment_count=len(missing_intervals),
            large_gap_count=self._large_gap_count,
            missing_intervals_s=missing_intervals,
            force_mae_n=force_mae_n,
            force_rmse_n=force_rmse_n,
            peak_force_error_n=self._peak_force_error_n,
            path_rms_m=path_rms_m,
            path_peak_m=self._path_peak_m,
            interrupted=interrupted,
            coverage_complete=coverage_complete,
            complete=complete,
            censored=not complete,
            objective_eligible=complete,
        )
        self._finalized_result = result
        self._finalized_interrupted = interrupted
        return result


def compute_contact_metrics(
    samples: Iterable[ContactSample | Mapping[str, Any] | Sequence[Any]],
    config: ContactBenchmarkConfig,
    *,
    interrupted: bool = False,
) -> ContactMetrics:
    """Compute offline metrics through the same path as streaming updates."""

    return ContactMetricsAccumulator(config).add_samples(samples).finalize(
        interrupted=interrupted
    )


# Descriptive aliases keep call sites readable without creating a second metric
# implementation or changing any historical metric module.
ContactBenchmarkMetricAccumulator = ContactMetricsAccumulator
ContactBenchmarkMetricConfig = ContactBenchmarkConfig
ContactBenchmarkMetricResult = ContactMetrics


__all__ = [
    "METRIC_SCHEMA",
    "METRIC_VERSION",
    "ContactBenchmarkConfig",
    "ContactBenchmarkMetricAccumulator",
    "ContactBenchmarkMetricConfig",
    "ContactBenchmarkMetricResult",
    "ContactMetricError",
    "ContactMetrics",
    "ContactMetricsAccumulator",
    "ContactSample",
    "compute_contact_metrics",
]
