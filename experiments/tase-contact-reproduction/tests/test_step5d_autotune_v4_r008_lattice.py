"""Offline tests for the r008 lattice go/no-go (no TP rotation)."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.lattice import (  # noqa: E402
    assert_r006_accepts,
    default_anchor,
    default_box,
    scrambled_sobol,
)
from step5d_autotune_v4_r008.controller_seam import (  # noqa: E402
    ActiveMotionEnvelopeV3,
    NORMAL_CAP_M_S,
)
from step5d_autotune_v4_r008.staircase import build_staircase, edge_half_anchor  # noqa: E402


def test_default_anchor_accepted_by_r006_candidate() -> None:
    assert_r006_accepts((default_anchor(),))


def test_sobol_points_accepted_by_r006_candidate() -> None:
    points = scrambled_sobol(default_box(), count=64, seed=3)
    assert len(points) >= 16
    assert_r006_accepts(points)


def test_staircase_scales_and_edge() -> None:
    levels = build_staircase()
    assert levels[0].scale == 1.0
    assert levels[-1].scale == 32.0
    half = edge_half_anchor(levels, 2)
    assert half.pd_ratio == pytest.approx(levels[2].point.pd_ratio * 0.5)


def test_envelope_caps_and_slope() -> None:
    env = ActiveMotionEnvelopeV3()
    u, reason = env.evaluate(commanded_normal_m_s=0.02, filtered_normal_n=5.0, dt_s=0.002)
    assert abs(u) <= NORMAL_CAP_M_S
    assert reason is None
