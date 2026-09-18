"""Acceptance tests for the independent six-controller contact metric."""

from __future__ import annotations

from pathlib import Path
import math
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from contact_benchmark_metrics import (  # noqa: E402
    ContactBenchmarkConfig,
    ContactMetricError,
    ContactMetricsAccumulator,
    ContactSample,
    compute_contact_metrics,
)


CONFIG = ContactBenchmarkConfig(start_s=0.0, end_s=1.0, max_gap_s=0.6)


def sample(
    time_s: float,
    force_n: float,
    reference_force_n: float = 0.0,
    *,
    position_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
    reference_position_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> ContactSample:
    return ContactSample(
        time_s=time_s,
        force_n=force_n,
        reference_force_n=reference_force_n,
        position_m=position_m,
        reference_position_m=reference_position_m,
    )


def test_pointwise_metric_does_not_cancel_plus_minus_errors_like_legacy_bin_mae() -> None:
    # A historical bin mean would see (+10 + -10) / 2 == 0.  The pointwise
    # metric evaluates both observations over their own elapsed intervals.
    result = compute_contact_metrics(
        [sample(0.0, 10.0), sample(0.5, -10.0), sample(1.0, 0.0)],
        CONFIG,
    )

    assert result.force_mae_n == pytest.approx(10.0)
    assert result.force_rmse_n == pytest.approx(10.0)
    assert result.peak_force_error_n == pytest.approx(10.0)
    assert result.complete is True
    assert result.objective_eligible is True


def test_irregular_dt_is_time_weighted_and_streaming_matches_offline() -> None:
    config = ContactBenchmarkConfig(start_s=0.0, end_s=1.0, max_gap_s=1.0)
    rows = [
        sample(0.0, 0.0),
        sample(0.1, 10.0),
        sample(0.9, 0.0),
        sample(1.0, 0.0),
    ]
    offline = compute_contact_metrics(rows, config)
    streaming = ContactMetricsAccumulator(config).add_samples(rows).finalize()

    # Durations are .1, .8, .1, so MAE=8 and RMSE=sqrt(80); an unweighted
    # three-point calculation would incorrectly produce 10/3 and sqrt(200/3).
    assert offline.force_mae_n == pytest.approx(8.0)
    assert offline.force_rmse_n == pytest.approx(math.sqrt(80.0))
    assert offline.as_dict() == streaming.as_dict()


def test_large_gap_is_missing_without_interpolation_or_endpoint_hiding() -> None:
    config = ContactBenchmarkConfig(start_s=0.0, end_s=1.0, max_gap_s=0.2)
    result = compute_contact_metrics(
        [
            sample(0.0, 1.0),
            sample(0.1, 1.0),
            sample(0.7, 100.0),
            sample(0.8, 1.0),
            sample(1.0, 1.0),
        ],
        config,
    )

    assert result.covered_duration_s == pytest.approx(0.4)
    assert result.missing_duration_s == pytest.approx(0.6)
    assert result.coverage_fraction == pytest.approx(0.4)
    assert result.missing_intervals_s == ((0.1, 0.7),)
    assert result.large_gap_count == 1
    assert result.complete is False
    # The large interval is absent; the 100 N sample is counted only for its
    # own short, explicitly observed hold after the gap.
    assert result.peak_force_error_n == pytest.approx(100.0)


def test_missing_terminal_coverage_is_censored_and_reported() -> None:
    result = compute_contact_metrics(
        [sample(0.0, 2.0), sample(0.5, 2.0), sample(0.8, 20.0)],
        CONFIG,
    )

    assert result.covered_duration_s == pytest.approx(0.8)
    assert result.missing_duration_s == pytest.approx(0.2)
    assert result.missing_intervals_s == ((0.8, 1.0),)
    assert result.missing_segment_count == 1
    assert result.coverage_complete is False
    assert result.complete is False
    assert result.censored is True
    assert result.objective is None
    assert result.peak_force_error_n == pytest.approx(20.0)


def test_observed_terminal_endpoint_is_reported_as_a_peak_without_duration_weight() -> None:
    result = compute_contact_metrics(
        [sample(0.0, 0.0), sample(1.0, 10.0)],
        ContactBenchmarkConfig(start_s=0.0, end_s=1.0, max_gap_s=1.0),
    )

    assert result.complete is True
    assert result.force_mae_n == pytest.approx(0.0)
    assert result.peak_force_error_n == pytest.approx(10.0)


def test_interrupted_trial_never_admits_objective_even_with_full_coverage() -> None:
    result = compute_contact_metrics(
        [sample(0.0, 2.0), sample(0.5, 2.0), sample(1.0, 2.0)],
        CONFIG,
        interrupted=True,
    )

    assert result.coverage_complete is True
    assert result.complete is False
    assert result.censored is True
    assert result.interrupted is True
    assert result.objective_eligible is False
    assert result.objective is None


def test_constant_and_variable_per_sample_reference_force_are_honored() -> None:
    constant = compute_contact_metrics(
        [sample(0.0, 5.0, 5.0), sample(0.5, 5.0, 5.0), sample(1.0, 5.0, 5.0)],
        CONFIG,
    )
    variable = compute_contact_metrics(
        [
            sample(0.0, 5.0, 5.0),
            sample(1.0 / 3.0, 7.0, 5.0),
            sample(2.0 / 3.0, 5.0, 6.0),
            sample(1.0, 5.0, 5.0),
        ],
        CONFIG,
    )

    assert constant.force_mae_n == pytest.approx(0.0)
    assert constant.force_rmse_n == pytest.approx(0.0)
    assert constant.peak_force_error_n == pytest.approx(0.0)
    assert variable.force_mae_n == pytest.approx(1.0)
    assert variable.force_rmse_n == pytest.approx(math.sqrt(5.0 / 3.0))
    assert variable.peak_force_error_n == pytest.approx(2.0)


def test_path_metrics_are_time_weighted_3d_norms() -> None:
    result = compute_contact_metrics(
        [
            sample(0.0, 0.0, position_m=(0.0, 0.0, 0.0)),
            sample(0.25, 0.0, position_m=(0.3, 0.0, 0.0)),
            sample(0.75, 0.0, position_m=(0.0, 0.4, 0.0)),
            sample(1.0, 0.0, position_m=(0.0, 0.0, 0.0)),
        ],
        CONFIG,
    )

    assert result.path_peak_m == pytest.approx(0.4)
    assert result.path_rms_m == pytest.approx(math.sqrt(0.085))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"time_s": math.nan},
        {"force_n": math.inf},
        {"reference_force_n": math.nan},
        {"position_m": (0.0, math.inf, 0.0)},
        {"reference_position_m": (0.0, math.nan, 0.0)},
    ],
)
def test_nonfinite_sample_values_fail_closed(kwargs: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "time_s": 0.0,
        "force_n": 0.0,
        "reference_force_n": 0.0,
        "position_m": (0.0, 0.0, 0.0),
        "reference_position_m": (0.0, 0.0, 0.0),
    }
    fields.update(kwargs)
    with pytest.raises(ContactMetricError, match="finite"):
        ContactSample(**fields)  # type: ignore[arg-type]


def test_nonmonotonic_timestamp_and_invalid_interval_fail_closed() -> None:
    accumulator = ContactMetricsAccumulator(CONFIG)
    accumulator.add_sample(sample(0.5, 0.0))
    with pytest.raises(ContactMetricError, match="monotonically"):
        accumulator.add_sample(sample(0.4, 0.0))

    with pytest.raises(ContactMetricError, match="end_s"):
        ContactBenchmarkConfig(start_s=1.0, end_s=1.0, max_gap_s=0.1)
    with pytest.raises(ContactMetricError, match="max_gap_s"):
        ContactBenchmarkConfig(start_s=0.0, end_s=1.0, max_gap_s=0.0)


def test_start_endpoint_is_required_for_complete_coverage() -> None:
    result = compute_contact_metrics(
        [sample(0.1, 0.0), sample(0.5, 0.0), sample(1.0, 0.0)],
        CONFIG,
    )

    assert result.covered_duration_s == pytest.approx(0.9)
    assert result.missing_intervals_s == ((0.0, 0.1),)
    assert result.complete is False
    assert result.as_dict()["interval_start_s"] == 0.0
    assert result.as_dict()["interval_end_s"] == 1.0


def test_mapping_and_tuple_inputs_use_the_same_validated_api() -> None:
    mapping = sample(0.0, 1.0).as_dict()
    rows = [mapping, (0.5, 1.0, 0.0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)), sample(1.0, 1.0)]
    result = compute_contact_metrics(rows, CONFIG)
    assert result.complete is True
    assert result.sample_count == 3


def test_runtime_stale_or_geometric_reject_censors_metric_objective() -> None:
    freshness = {
        "stale_stop_count": 1,
        "geometric_latency_reject_count": 0,
    }
    result = compute_contact_metrics(
        [sample(0.0, 0.0), sample(0.5, 0.0), sample(1.0, 0.0)],
        CONFIG,
        freshness=freshness,
    )
    assert result.coverage_complete is True
    assert result.complete is False
    assert result.objective_eligible is False
    assert result.stale_stop_count == 1
    assert result.as_dict()["stale_stop_count"] == 1
