from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from audit_tase_r013_phase_error_repeatability import (  # noqa: E402
    PhaseAuditError,
    bin_filtered_force,
    phase_profile_cv,
)


def test_bins_filtered_force_on_half_open_formal_window():
    rows = []
    for index in range(550):
        time_s = 5.0 + (index + 0.5) * 0.1
        rows.append({"reference_phase": "path", "reference_time_s": time_s, "normal_force_filtered_n": 5.0 + index / 100.0})
    rows.extend((
        {"reference_phase": "path", "reference_time_s": 4.999, "normal_force_filtered_n": 100.0},
        {"reference_phase": "path", "reference_time_s": 60.0, "normal_force_filtered_n": -100.0},
    ))
    result = bin_filtered_force(rows)
    assert result.shape == (550,)
    assert result[0] == pytest.approx(5.0)
    assert result[-1] == pytest.approx(10.49)


def test_bin_coverage_failure_is_explicit():
    with pytest.raises(PhaseAuditError, match="coverage"):
        bin_filtered_force([])


def test_leave_one_attempt_out_phase_profile_beats_constant_for_repeatable_shape():
    phase = np.sin(np.linspace(0.0, 2.0 * np.pi, 100, endpoint=False))
    curves = np.asarray([1.0 + phase + offset for offset in (-0.1, 0.0, 0.1, 0.05)])
    result = phase_profile_cv(curves)
    cv = result["leave_one_attempt_out"]
    assert result["attempt_count"] == 4
    assert result["mean_pairwise_pearson_error_curve"] == pytest.approx(1.0)
    assert cv["mean_error_rmse_reduction_vs_constant_fraction"] > 0.85
