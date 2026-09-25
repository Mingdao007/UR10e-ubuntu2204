from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from analyze_tase_r31_qd_frequency_response import (  # noqa: E402
    SpectrumAnalysisError,
    _welch_pair,
    split_contiguous_indices,
)


def test_split_contiguous_indices_drops_gaps_and_sequence_skips():
    seq = list(range(1024)) + list(range(1025, 2049))
    continuous_t = [index * .002 for index in range(2048)]
    assert split_contiguous_indices(seq, continuous_t, nominal_dt_s=.002) == [(0, 2048)]

    gapped_t = [index * .002 for index in range(1024)] + [
        (index + 1) * .002 for index in range(1024, 2048)
    ]
    spans = split_contiguous_indices(seq, gapped_t, nominal_dt_s=.002)
    assert spans == [(0, 1024), (1024, 2048)]
    short_spans = split_contiguous_indices([1, 2, 3], [0.0, .002, .004], nominal_dt_s=.002)
    assert short_spans == [(0, 3)]
    with pytest.raises(SpectrumAnalysisError, match="strictly increase"):
        split_contiguous_indices([2, 1], [0.0, .002], nominal_dt_s=.002)


def test_welch_pair_recovers_known_gain_phase_and_coherence():
    fs = 500.0
    n = 8192
    time_s = np.arange(n) / fs
    target = np.sin(2.0 * np.pi * 10.0 * time_s)
    phase_rad = -0.3
    actual = 0.5 * np.sin(2.0 * np.pi * 10.0 * time_s + phase_rad)
    target_6 = np.repeat(target[:, None], 6, axis=1)
    actual_6 = np.repeat(actual[:, None], 6, axis=1)

    result = _welch_pair(target_6, actual_6, [(0, n)], sample_rate_hz=fs)
    band = result["per_joint"][0]["bands"]["5_to_15"]

    assert band["gain_db_median"] == pytest.approx(20.0 * np.log10(0.5), abs=0.3)
    assert band["phase_deg_median"] == pytest.approx(np.rad2deg(phase_rad), abs=5.0)
    assert band["coherence_median"] > 0.99
