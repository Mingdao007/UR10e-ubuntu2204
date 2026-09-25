from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from replay_tase_qp_attempt_fixed_inputs import (  # noqa: E402
    ReplayError,
    apply_captured_slew,
)


def test_captured_slew_reconstructs_same_direction_scale() -> None:
    applied, scale = apply_captured_slew(
        [0.4, -0.2, 0, 0, 0, 0],
        [0.0] * 6,
        0.1,
    )
    assert scale == pytest.approx(0.25)
    assert np.allclose(applied, [0.1, -0.05, 0, 0, 0, 0], atol=1e-12)


def test_captured_slew_leaves_already_bounded_delta_unchanged() -> None:
    expected = [0.01, -0.02, 0, 0, 0, 0]
    applied, scale = apply_captured_slew(expected, [0.0] * 6, 0.02)
    assert scale == pytest.approx(1.0)
    assert np.allclose(applied, expected, atol=1e-12)


def test_captured_slew_rejects_invalid_limit() -> None:
    with pytest.raises(ReplayError, match="slew limit"):
        apply_captured_slew([0.0] * 6, [0.0] * 6, -0.1)
