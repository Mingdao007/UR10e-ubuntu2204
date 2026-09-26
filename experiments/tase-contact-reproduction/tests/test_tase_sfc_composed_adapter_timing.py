import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from tase_sfc_composed_adapter import (  # noqa: E402
    MAX_FRESH_GAP_S,
    NATIVE_LAW_MAX_STEP_S,
    TaseSfcCompositionError,
    _elapsed_substep_count,
)


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (0.002, 1),
        (0.004, 1),
        (0.004001, 2),
        (0.012067986, 4),
        (MAX_FRESH_GAP_S, 5),
    ],
)
def test_recorded_interval_is_partitioned_with_native_law_step_limit(elapsed, expected):
    count = _elapsed_substep_count(elapsed)
    assert count == expected
    assert elapsed / count <= NATIVE_LAW_MAX_STEP_S


@pytest.mark.parametrize("elapsed", [0.0, -0.001, MAX_FRESH_GAP_S + 1e-9, float("nan")])
def test_recorded_interval_outside_live_freshness_bound_is_rejected(elapsed):
    with pytest.raises(TaseSfcCompositionError, match="observed dt_s"):
        _elapsed_substep_count(elapsed)
