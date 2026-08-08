"""Offline unit tests for STAGE25/PATH entry rate-limit ramp."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.path_entry_rate_limit import (  # noqa: E402
    DEFAULT_ENTRY_AMP_CAP_M_S,
    DEFAULT_FULL_AMP_CAP_M_S,
    DEFAULT_WINDOW_S,
    ENV_FLAG,
    HOOK_POINT,
    PathEntryRateLimitConfig,
    PathEntryRateLimitError,
    PathEntryRateLimitRamp,
    amp_ceiling_m_s,
    env_flag_enabled,
    estimate_spring_force_rise_n,
    reconstruct_pi_normal_speed_m_s,
)

LIMIT50_RUN = (
    ROOT
    / "runs/step5d_autotune_v4_r008/live_20260806_002340_b3_wave4_limit50_pathring"
)
ATTEMPT10_TRACE = LIMIT50_RUN / "r008-state25-path-trace.jsonl"


def test_defaults_match_design_window() -> None:
    assert DEFAULT_WINDOW_S == pytest.approx(0.150)
    assert 0.100 <= DEFAULT_WINDOW_S <= 0.200
    assert DEFAULT_ENTRY_AMP_CAP_M_S == pytest.approx(0.0005)
    # End-of-window ceiling stays well below B6 5 mm/s path cap.
    assert DEFAULT_FULL_AMP_CAP_M_S == pytest.approx(0.001)
    assert DEFAULT_FULL_AMP_CAP_M_S < 0.005


def test_config_default_disarmed_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    cfg = PathEntryRateLimitConfig()
    assert cfg.enabled is False
    assert cfg.is_armed() is False
    assert env_flag_enabled() is False


def test_env_flag_required_for_live_style_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_FLAG, "1")
    cfg = PathEntryRateLimitConfig(enabled=True, require_env_flag=True)
    assert cfg.is_armed() is True
    monkeypatch.setenv(ENV_FLAG, "0")
    assert cfg.is_armed() is False


def test_disabled_is_passthrough() -> None:
    ramp = PathEntryRateLimitRamp(config=PathEntryRateLimitConfig())
    # baseline then path — still passthrough when disabled
    ramp.apply(commanded_normal_m_s=0.0004, mode="baseline", dt_s=0.002)
    out = ramp.apply(commanded_normal_m_s=0.005, mode="path", dt_s=0.002)
    assert out.limited_normal_m_s == pytest.approx(0.005)
    assert out.active is False
    assert out.clipped is False
    assert out.reason == "disabled"


def test_amp_ceiling_ramp_endpoints() -> None:
    cfg = PathEntryRateLimitConfig.offline_enabled(ceiling_gamma=1.0)
    assert amp_ceiling_m_s(0.0, cfg) == pytest.approx(cfg.entry_amp_cap_m_s)
    assert amp_ceiling_m_s(cfg.window_s, cfg) == pytest.approx(cfg.full_amp_cap_m_s)
    mid = amp_ceiling_m_s(0.5 * cfg.window_s, cfg)
    assert mid == pytest.approx(
        0.5 * (cfg.entry_amp_cap_m_s + cfg.full_amp_cap_m_s)
    )
    # Default gamma=2 stays closer to entry at mid-window than linear.
    cfg2 = PathEntryRateLimitConfig.offline_enabled(ceiling_gamma=2.0)
    mid2 = amp_ceiling_m_s(0.5 * cfg2.window_s, cfg2)
    assert mid2 < mid


def test_entry_ramp_clips_saturated_path_command() -> None:
    ramp = PathEntryRateLimitRamp(config=PathEntryRateLimitConfig.offline_enabled())
    ramp.apply(commanded_normal_m_s=0.0005, mode="baseline", dt_s=0.002)
    # Saturated path command at entry (Ki pocket shape).
    out = ramp.apply(commanded_normal_m_s=0.005, mode="path", dt_s=0.002)
    assert out.active is True
    assert out.clipped is True
    assert abs(out.limited_normal_m_s) <= DEFAULT_ENTRY_AMP_CAP_M_S + 1e-12
    assert abs(out.limited_normal_m_s) <= out.amp_ceiling_m_s + 1e-12


def test_slew_limits_step_from_zero() -> None:
    cfg = PathEntryRateLimitConfig.offline_enabled(max_slew_m_s2=0.01)
    ramp = PathEntryRateLimitRamp(config=cfg)
    out = ramp.apply(commanded_normal_m_s=0.005, mode="path", dt_s=0.002)
    # From prev_u=0, |du|<=0.01*0.002=2e-5, also ceiling at entry=5e-4.
    assert abs(out.limited_normal_m_s) == pytest.approx(2e-5)


def test_window_expires_and_passthrough_returns() -> None:
    cfg = PathEntryRateLimitConfig.offline_enabled(window_s=0.100)
    ramp = PathEntryRateLimitRamp(config=cfg)
    t = 0.0
    last = None
    for _ in range(60):
        last = ramp.apply(
            commanded_normal_m_s=0.005,
            mode="path",
            dt_s=0.002,
            monotonic_s=t,
        )
        t += 0.002
    assert last is not None
    assert last.reason == "window_elapsed"
    assert last.active is False
    assert last.limited_normal_m_s == pytest.approx(0.005)


def test_command_mode_int_path_edge() -> None:
    ramp = PathEntryRateLimitRamp(config=PathEntryRateLimitConfig.offline_enabled())
    ramp.apply(commanded_normal_m_s=0.0005, mode=1, dt_s=0.002)  # baseline
    out = ramp.apply(commanded_normal_m_s=0.005, mode=2, dt_s=0.002)  # PATH
    assert out.active is True
    assert out.clipped is True


def test_reject_bad_window() -> None:
    with pytest.raises(PathEntryRateLimitError):
        PathEntryRateLimitConfig.offline_enabled(window_s=0.050)


def test_live_adapter_may_import_but_default_disarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase B: module may be imported; ramp stays OFF without env flag."""

    monkeypatch.delenv(ENV_FLAG, raising=False)
    live_adapter = (
        ROOT / "tools/step5d_autotune_v4_r008/live_adapter.py"
    ).read_text(encoding="utf-8")
    assert "path_entry_rate_limit" in live_adapter
    assert ENV_FLAG in live_adapter or "R008_PATH_ENTRY_RATE_LIMIT" in live_adapter
    assert "PathEntryRateLimit" in live_adapter
    assert "post_outer_loop" in HOOK_POINT

    qualification = (
        ROOT / "tools/step5d_autotune_v4_r004/qualification.py"
    ).read_text(encoding="utf-8")
    assert "PathEntryRateLimitRamp" in qualification
    assert "path_entry_rate_limit" in qualification

    cfg = PathEntryRateLimitConfig.from_environ()
    assert cfg.enabled is False
    assert cfg.is_armed() is False
    ramp = PathEntryRateLimitRamp(config=cfg)
    ramp.apply(commanded_normal_m_s=0.0005, mode="baseline", dt_s=0.002)
    out = ramp.apply(commanded_normal_m_s=0.005, mode="path", dt_s=0.002)
    assert out.active is False
    assert out.clipped is False
    assert out.limited_normal_m_s == pytest.approx(0.005)
    assert out.reason == "disabled"

def test_pi_reconstruction_attempt10_entry_saturated() -> None:
    # Overlay from dispatch 000000000010.json
    p = 0.0028284271248
    ki = 0.001810193359837562
    u = reconstruct_pi_normal_speed_m_s(
        force_p_gain=p,
        force_i_gain=ki,
        filtered_normal_n=6.371714195752705,
        force_integral_n_s=49.78353022106472,
        setpoint_n=5.0,
    )
    assert u > 0.08  # far above path/envelope caps → saturated pocket


@pytest.mark.skipif(not ATTEMPT10_TRACE.is_file(), reason="limit50 run missing")
def test_attempt10_spring_counterfactual_clips_peak() -> None:
    """Ki-pocket saturated command through the ramp keeps spring peak << 61N.

    attempt10 facts: 6.45N→61.24N in 0.2177s, Δz≈−0.59 mm.  Treat the
    outer loop as amplitude-saturated into the surface (matching PI
    reconstruction ≫ caps) and compare spring force with/without the entry
    ramp over the same 0–218 ms window.
    """

    rows = []
    with ATTEMPT10_TRACE.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("attempt_ordinal") == 10:
                rows.append(row)
    rows = rows[:109]
    assert len(rows) >= 50
    t0 = float(rows[0]["monotonic_s"])
    z0 = float(rows[0]["tcp_z_m"])
    f0 = float(rows[0]["force_norm_n"])
    f_peak = max(float(r["force_norm_n"]) for r in rows)
    assert f_peak > 60.0
    assert f0 < 7.0

    z_end = float(rows[108]["tcp_z_m"])
    dz = z_end - z0
    assert dz < 0.0
    k_eff = (f_peak - f0) / abs(dz)

    dts: list[float] = []
    for i in range(1, len(rows)):
        dt = float(rows[i]["monotonic_s"]) - float(rows[i - 1]["monotonic_s"])
        if dt > 0.0:
            dts.append(dt)
    assert abs(sum(dts) - 0.2177) < 0.01

    # Saturated into-surface Z velocity at the observed mean |vz|.
    v_sat = dz / sum(dts)
    assert v_sat < 0.0
    f_sat = estimate_spring_force_rise_n(
        velocities_m_s=[v_sat] * len(dts),
        dt_values_s=dts,
        k_eff_n_per_m=k_eff,
        f0_n=f0,
    )
    assert max(f_sat) > 50.0

    # Counterfactual: same saturated demand, PATH-entry ramp armed
    # (default 150 ms window, ease-in ceiling 0.5→1.0 mm/s).
    cfg = PathEntryRateLimitConfig.offline_enabled()
    ramp = PathEntryRateLimitRamp(config=cfg)
    ramp.apply(
        commanded_normal_m_s=0.0,
        mode="baseline",
        dt_s=0.002,
        monotonic_s=t0 - 0.002,
    )
    v_lim: list[float] = []
    mono = t0
    u_demand = -v_sat  # positive normal_speed ↔ negative Z
    n_active = 0
    for dt in dts:
        out = ramp.apply(
            commanded_normal_m_s=u_demand,
            mode="path",
            dt_s=dt,
            monotonic_s=mono,
        )
        mono += dt
        if out.active:
            n_active += 1
            assert abs(out.limited_normal_m_s) <= out.amp_ceiling_m_s + 1e-12
            assert abs(out.limited_normal_m_s) <= cfg.full_amp_cap_m_s + 1e-12
        v_lim.append(-out.limited_normal_m_s)

    assert n_active >= 60  # full default 150 ms window at ~2 ms ticks
    f_lim = estimate_spring_force_rise_n(
        velocities_m_s=v_lim,
        dt_values_s=dts,
        k_eff_n_per_m=k_eff,
        f0_n=f0,
    )
    peak_lim = max(f_lim)
    # Must materially clip the 61N-class spike (exact peak depends on k_eff).
    assert peak_lim < 35.0
    assert peak_lim < 0.55 * f_peak
    assert max(abs(v) for v in v_lim[:50]) < abs(v_sat)


def test_hook_point_string_documents_seam() -> None:
    assert "ActiveMotionEnvelopeV3" in HOOK_POINT
    assert "outer_loop" in HOOK_POINT
