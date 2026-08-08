"""P-up + Ki-lock staircase replaces coupled fixed-kf after Ki-pocket failure.

User decision 2026-08-06: root issue is Ki (not Kp). Reject coupled scales that
keep kf fixed (×2 grows Ki with P). Raise P/bandwidth with Ki locked safe;
default policy is I_OFF.
"""

from __future__ import annotations

from pathlib import Path
import math
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(REPO / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r008.design_binding import (  # noqa: E402
    anchor_from_document,
    load_domain_design,
)
from step5d_autotune_v4_r008.lattice import (  # noqa: E402
    CONFIRMED_DANGEROUS_KI,
    confirmed_dangerous_ki,
)
from step5d_autotune_v4_r008.staircase import (  # noqa: E402
    STAIRCASE_KI_LOCK_POLICY,
    STAIRCASE_USE_P_UP_KI_LOCK,
    build_staircase,
    build_staircase_coupled_fixed_kf,
    build_staircase_p_up_ki_lock,
    held_safe_ki_gain,
)

DOMAIN_PATH = ROOT / "config" / "step5d" / "autotune_v4_r008_domain.json"


def test_defaults_prefer_p_up_i_off() -> None:
    assert STAIRCASE_USE_P_UP_KI_LOCK is True
    assert STAIRCASE_KI_LOCK_POLICY == "i_off"


def test_coupled_scale2_doubles_ki_away_from_but_grown_from_pocket() -> None:
    """Document rejected policy: fixed kf => Ki scales with P; x2 is 1.0 octave above pocket."""

    domain = load_domain_design(DOMAIN_PATH)
    anchor = anchor_from_document(domain)
    levels = build_staircase_coupled_fixed_kf(anchor)
    ki0 = float(levels[0].point.to_parameter_point().i_gain)
    ki2 = float(levels[1].point.to_parameter_point().i_gain)
    p0 = float(levels[0].point.to_parameter_point().p_gain)
    p2 = float(levels[1].point.to_parameter_point().p_gain)
    assert confirmed_dangerous_ki(force_i_gain=ki0)
    assert ki0 == pytest.approx(CONFIRMED_DANGEROUS_KI)
    assert p2 == pytest.approx(2.0 * p0)
    assert ki2 == pytest.approx(2.0 * ki0)
    # Outside ±1 quarter-octave ban, but still the coupled growth we reject.
    assert not confirmed_dangerous_ki(force_i_gain=ki2)
    assert math.log2(ki2 / CONFIRMED_DANGEROUS_KI) == pytest.approx(1.0)


def test_p_up_i_off_levels_raise_p_and_lock_ki() -> None:
    domain = load_domain_design(DOMAIN_PATH)
    anchor = anchor_from_document(domain)
    base_p = float(anchor.to_parameter_point().p_gain)
    levels = build_staircase_p_up_ki_lock(anchor, ki_policy="i_off")
    assert len(levels) == 6
    prev_p = 0.0
    for level in levels:
        typed = level.point.to_parameter_point()
        assert typed.i_gain == pytest.approx(0.0)
        assert not confirmed_dangerous_ki(force_i_gain=float(typed.i_gain))
        assert float(typed.p_gain) > prev_p
        prev_p = float(typed.p_gain)
    assert float(levels[0].point.to_parameter_point().p_gain) == pytest.approx(base_p, rel=1e-3)
    assert float(levels[1].point.to_parameter_point().p_gain) > base_p


def test_build_staircase_default_is_p_up_not_coupled() -> None:
    domain = load_domain_design(DOMAIN_PATH)
    anchor = anchor_from_document(domain)
    default_levels = build_staircase(anchor)
    p_up = build_staircase_p_up_ki_lock(anchor)
    coupled = build_staircase_coupled_fixed_kf(anchor)
    assert [lv.point.key() for lv in default_levels] == [lv.point.key() for lv in p_up]
    assert default_levels[0].point.to_parameter_point().i_gain == pytest.approx(0.0)
    assert coupled[0].point.to_parameter_point().i_gain != pytest.approx(0.0)
    assert confirmed_dangerous_ki(
        force_i_gain=float(coupled[0].point.to_parameter_point().i_gain)
    )


def test_held_safe_policy_outside_ban_and_raises_p() -> None:
    domain = load_domain_design(DOMAIN_PATH)
    anchor = anchor_from_document(domain)
    safe = held_safe_ki_gain()
    assert not confirmed_dangerous_ki(force_i_gain=safe)
    levels = build_staircase_p_up_ki_lock(anchor, ki_policy="held_safe")
    base_p = float(levels[0].point.to_parameter_point().p_gain)
    for level in levels:
        typed = level.point.to_parameter_point()
        assert not confirmed_dangerous_ki(force_i_gain=float(typed.i_gain))
        assert float(typed.i_gain) == pytest.approx(safe, rel=1e-9)
    assert float(levels[1].point.to_parameter_point().p_gain) > base_p


def test_host_import_uses_p_up_flag() -> None:
    from step5d_autotune_v4_r008 import live_adapter as mod

    assert mod.STAIRCASE_USE_P_UP_KI_LOCK is True


def test_align_staircase_cursor_resets_after_pocket_enqueue() -> None:
    from types import SimpleNamespace

    from step5d_autotune_v4_r008.live_adapter import R008HostLoop

    domain = load_domain_design(DOMAIN_PATH)
    anchor = anchor_from_document(domain)
    plan = build_staircase_p_up_ki_lock(anchor)
    loop = object.__new__(R008HostLoop)
    loop.events = []
    loop._STAIRCASE = R008HostLoop._STAIRCASE
    loop._staircase = plan
    loop._staircase_scheduled = 2
    loop._domain_rebind_path_value = None
    loop._load_domain_rebind = lambda _p: None  # type: ignore[method-assign]
    loop._rows_for = lambda _kind: ()  # type: ignore[method-assign]
    loop.queue = SimpleNamespace(
        _metadata={
            "a": SimpleNamespace(
                kind="STAIRCASE",
                candidate=SimpleNamespace(i_gain=CONFIRMED_DANGEROUS_KI),
            ),
            "b": SimpleNamespace(
                kind="STAIRCASE",
                candidate=SimpleNamespace(i_gain=2.0 * CONFIRMED_DANGEROUS_KI),
            ),
        }
    )
    R008HostLoop._align_staircase_cursor_to_p_up_ki_lock(loop)
    assert loop._staircase_scheduled == 0
    assert any(
        e.startswith("R008_STAIRCASE_P_UP_KI_LOCK_RESET:") for e in loop.events
    )
