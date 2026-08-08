"""Offline Tube+CBF shadow gates (nominal fire≈0, soft-before-hard)."""

from __future__ import annotations

from pathlib import Path

import pytest

from step5d_autotune_v4_r008.tube_cbf_shadow import (
    hard_breach,
    run_shadow,
    shadow_injection_ladder,
    shadow_nominal_from_path_xy,
    soft_before_hard_ok,
)

ROOT = Path(__file__).resolve().parents[1]
PATH_XY = (
    ROOT
    / "runs/step5d_autotune_v4_r008/live_20260806_002340_b3_wave4_limit50_pathring"
    / "r008-path-xy.jsonl"
)


@pytest.mark.skipif(not PATH_XY.is_file(), reason="limit50 path-xy sidecar missing")
def test_nominal_path_xy_fire_rate_near_zero() -> None:
    report = shadow_nominal_from_path_xy(PATH_XY)
    assert report.n >= 8
    assert report.fire_rate == pytest.approx(0.0)
    assert report.hard_breach_count == 0
    assert report.infeasible_count == 0


def test_injection_soft_fires_before_hard_breach() -> None:
    report = shadow_injection_ladder(
        # engage_rho=0.67 on a=35mm → engage from ~23.5 mm
        offsets_m=(0.024, 0.026, 0.028, 0.030, 0.031),
        soft_a_m=0.035,
        soft_b_m=0.035,
        hard_r_m=0.030,
        engage_rho=0.67,
        alpha=2.0,
        outward_speed_m_s=0.05,
    )
    assert report.n == 5
    assert soft_before_hard_ok(report)
    # 24–28 mm: soft fires; not hard yet
    pre = [s for s in report.samples if s.u_m < 0.030]
    assert any(s.soft.fired for s in pre)
    assert all(not s.hard_breach for s in pre)
    assert all(not s.hard_while_soft_feasible for s in pre)
    # >=30 mm: hard circle while still inside soft 35 mm ellipse
    post = [s for s in report.samples if s.u_m >= 0.030]
    assert post and all(s.hard_breach for s in post)
    assert all(s.hard_while_soft_feasible for s in post)
    assert report.hard_while_soft_feasible_count == len(post)


def test_hard_breach_circle_threshold() -> None:
    assert not hard_breach(0.029, 0.0, hard_r_m=0.030)
    assert hard_breach(0.030, 0.0, hard_r_m=0.030)
    assert hard_breach(0.0, 0.031, hard_r_m=0.030)


def test_synthetic_deep_inside_idle() -> None:
    report = run_shadow(
        [("center", 0.0, 0.0), ("1mm", 0.001, 0.0)],
        soft_a_m=0.030,
        soft_b_m=0.030,
    )
    assert report.fire_count == 0
    assert report.infeasible_count == 0
