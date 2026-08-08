"""P-up / Ki-lock staircase: scale>1 forces I_OFF so Ki does not ride with P."""

from __future__ import annotations

from step5d_autotune_v4_r008.lattice import confirmed_dangerous_ki
from step5d_autotune_v4_r008.staircase import build_staircase


def test_scale_gt_1_defaults_to_i_off_and_raises_p() -> None:
    levels = build_staircase()
    base = levels[0]
    assert base.scale == 1.0
    assert base.point.kf_off is False
    up = next(level for level in levels if level.scale == 2.0)
    assert up.point.kf_off is True
    assert up.point.force_i_gain == 0.0
    assert up.point.force_p_gain > base.point.force_p_gain
    assert not confirmed_dangerous_ki(force_i_gain=float(up.point.force_i_gain))


def test_coupled_mode_still_available_for_ablation() -> None:
    levels = build_staircase(i_off_above_unity_scale=False)
    up = next(level for level in levels if level.scale == 2.0)
    assert up.point.kf_off is False
    assert up.point.force_i_gain > 0.0


def test_unity_keeps_anchor_integral_mode() -> None:
    levels = build_staircase()
    assert levels[0].point.kf_off is False
    assert levels[0].point.force_i_gain > 0.0
