"""Offline tests for live Tube CBF seam (default OFF)."""

from __future__ import annotations

import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(
    0,
    str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"),
)

from step5d_autotune_v4_r004.path_reference import (  # noqa: E402
    PATH_STAGE_ID,
    step5_path_reference,
)
from step5d_autotune_v4_r008.tube_cbf_live import (  # noqa: E402
    TubeCbfLiveConfig,
    TubeCbfLiveFilter,
    path_frame_uv,
)


def test_env_default_off() -> None:
    cfg = TubeCbfLiveConfig.from_environ({})
    assert cfg.mode == "off"
    assert cfg.armed is False


def test_non_path_mode_identity() -> None:
    filt = TubeCbfLiveFilter(config=TubeCbfLiveConfig(mode="active"))
    twist = (0.01, -0.02, 0.003, 0.0, 0.0, 0.0)
    out = filt.apply(
        twist,
        mode="baseline",
        actual_tcp_pose=(0.5, 0.1, 0.03, 0.0, 0.0, 0.0),
        path_time_s=1.0,
    )
    assert out.applied is False
    assert out.desired_twist == pytest.approx(twist)
    assert out.result is None


def test_shadow_does_not_change_twist_near_wall() -> None:
    filt = TubeCbfLiveFilter(config=TubeCbfLiveConfig(mode="shadow", a_m=0.030, b_m=0.030))
    # Place TCP 22 mm along +lateral from a near-origin reference at t=0.
    ref0 = step5_path_reference(PATH_STAGE_ID, (0.487795411149049, 0.12932679270060748), 0.0)
    desired = ref0["desired_xy"]
    # Offset ~22 mm in base -X ≈ path lateral
    pose = (desired[0] - 0.022, desired[1], 0.033, 0.0, 0.0, 0.0)
    twist = (0.05, 0.0, 0.0, 0.0, 0.0, 0.0)
    out = filt.apply(twist, mode="path", actual_tcp_pose=pose, path_time_s=0.0)
    assert out.result is not None
    assert out.applied is False
    assert out.desired_twist == pytest.approx(twist)


def test_active_can_fire_and_keep_vz() -> None:
    filt = TubeCbfLiveFilter(config=TubeCbfLiveConfig(mode="active", a_m=0.030, b_m=0.030))
    ref0 = step5_path_reference(PATH_STAGE_ID, (0.487795411149049, 0.12932679270060748), 0.0)
    desired = ref0["desired_xy"]
    pose = (desired[0] - 0.022, desired[1], 0.033, 0.0, 0.0, 0.0)
    u, v = path_frame_uv(pose[:2], 0.0)
    assert math.hypot(u, v) > 0.02
    twist = (0.05, 0.0, -0.007, 0.1, 0.2, 0.3)
    out = filt.apply(twist, mode="path", actual_tcp_pose=pose, path_time_s=0.0)
    assert out.result is not None
    assert out.result.engaged is True
    # Either fires or constraint already ok; vz/angular always preserved.
    assert out.desired_twist[2] == pytest.approx(-0.007)
    assert out.desired_twist[3:] == pytest.approx((0.1, 0.2, 0.3))


def test_path_frame_uv_zero_on_reference() -> None:
    ref = step5_path_reference(PATH_STAGE_ID, (0.5, 0.1), 5.0)
    u, v = path_frame_uv(ref["desired_xy"], 5.0)
    assert abs(u) < 1e-9
    assert abs(v) < 1e-9
