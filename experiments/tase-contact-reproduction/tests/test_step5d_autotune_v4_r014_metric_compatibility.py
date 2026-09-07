"""Compare the R014 metric directly with the current R013 metric owner."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from step5d_autotune_v4_r013.metrics import (  # noqa: E402
    GapPreservingMetricAccumulatorV1,
    MetricFingerprintV1,
)
from step5d_autotune_v4_r014.metrics import force_bin_metric  # noqa: E402


@pytest.mark.parametrize("offset_s", [0.0, -0.5e-12, -2e-12, 2e-12])
def test_r014_primary_secondary_and_decimal_boundary_match_r013(offset_s):
    samples = [(index / 10.0 + 0.05, 5.0 + (index % 7 - 3) / 4.0)
               for index in range(600)]
    # Distinct adjacent-bin errors make a one-bin misassignment observable.
    samples.extend((boundary + offset_s, 11.0) for boundary in (5.1, 6.1, 43.3))
    samples.sort()
    legacy = GapPreservingMetricAccumulatorV1(MetricFingerprintV1.cycloid())
    for time_s, force_n in samples:
        legacy.add_sample(time_s=time_s, normal_load_n=force_n)
    expected = legacy.seal()
    actual = force_bin_metric(samples)
    assert actual.primary_mae_n == pytest.approx(expected.full_curve_mae_n, abs=1e-14)
    assert actual.secondary_mae_n == pytest.approx(expected.formal_mae_n, abs=1e-14)
    assert actual.bin_means_n == pytest.approx(
        [sum(values) / len(values) for _, values in sorted(legacy.bins.items())],
        abs=1e-14,
    )
