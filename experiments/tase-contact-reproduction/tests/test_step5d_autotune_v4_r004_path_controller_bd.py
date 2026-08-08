"""Bd / force_damping must change path_controller normal qdot (no stub)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4.contracts import V4Candidate  # noqa: E402
from step5d_autotune_v4_r004.path_controller import V4PathController  # noqa: E402
from step5d_autotune_v4_r008.greybox.reconstruct import normal_velocity  # noqa: E402
import numpy as np  # noqa: E402


def _settle_normal_vz(*, damping: float, steps: int = 80) -> float:
    cand = replace(
        V4Candidate(),
        force_i_gain=0.0,
        force_damping=float(damping),
    )
    ctrl = V4PathController(cand)
    tick = None
    for _ in range(steps):
        tick = ctrl.step(
            actual_dt_s=0.002,
            raw_normal_n=4.0,
            setpoint_n=5.0,
            mode="baseline",
        )
    assert tick is not None
    return float(tick.proposed_qdot[2])


def test_higher_damping_reduces_settled_normal_speed() -> None:
    low = _settle_normal_vz(damping=19.7989899)
    high = _settle_normal_vz(damping=39.5979797)
    # Into-surface is −Z; |vz| must shrink as D rises (v_ss ≈ P·e/D).
    assert low < 0.0 and high < 0.0
    assert abs(high) < abs(low) * 0.75


def test_path_controller_matches_greybox_normal_velocity_kernel() -> None:
    cand = replace(V4Candidate(), force_i_gain=0.0, force_damping=27.977)
    ctrl = V4PathController(cand)
    dt = 0.002
    filtered = np.full(40, 4.0, dtype=float)
    dts = np.full(40, dt, dtype=float)
    u_ref, _ = normal_velocity(
        filtered,
        dts,
        force_p_gain=float(cand.force_p_gain),
        force_damping=float(cand.force_damping),
        kf=0.0,
        target_force_n=5.0,
    )
    for index in range(40):
        tick = ctrl.step(
            actual_dt_s=dt,
            raw_normal_n=4.0,
            setpoint_n=5.0,
            mode="baseline",
        )
        # proposed_qdot[2] = −normal_speed; greybox u is into-surface +.
        assert tick.proposed_qdot[2] == pytest.approx(-float(u_ref[index]), abs=1e-12)


def test_no_bd_stub_literals_remain() -> None:
    src = (
        ROOT / "tools/step5d_autotune_v4_r004/path_controller.py"
    ).read_text(encoding="utf-8")
    assert "(terms[\"Bd\"] * terms[\"P\"]) * 0.0" not in src
    assert "* 1e-6" not in src
    shared = (ROOT / "tools/step5d_autotune_v4/path_controller.py").read_text(
        encoding="utf-8"
    )
    assert "(terms[\"Bd\"] * terms[\"P\"]) * 0.0" not in shared
    assert "* 1e-6" not in shared