"""BO ask must apply the same phase-margin veto as STAIRCASE.

far005 dispatch 42 and limit50 STAIRCASE attempt10 both hard-stopped at
hard_abs_normal_60n with Ki≈0.00181. Unsaturated PI linearization alone
reported PM≈54°; the rail-saturated I linearization (PATH entered with
|I|≈I_lim) brings PM below PM_MIN_DEG=25 so the live veto catches them.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(REPO / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r008.design import (  # noqa: E402
    PM_MIN_DEG,
    live_phase_margin_unstable,
    phase_margin_deg,
)
from step5d_autotune_v4_r008.design_binding import load_domain_design  # noqa: E402
from step5d_autotune_v4_r008.lattice import point_from_physical  # noqa: E402

DOMAIN_PATH = ROOT / "config" / "step5d" / "autotune_v4_r008_domain.json"

# far005 live_20260805_120727 request 000000000042 (hard_abs_normal_60n).
DISP42 = {
    "force_p_gain": 0.003363585661078849,
    "force_i_gain": 0.001810193359837562,
    "force_damping": 158.39191898578665,
    "normal_filter_tau_s": 0.0875,
}

# limit50-pathring live_20260806_002340 STAIRCASE attempt10 (hard_abs_normal_60n).
LIMIT50_ATTEMPT10 = {
    "force_p_gain": 0.0028284271248,
    "force_i_gain": 0.001810193359837562,
    "force_damping": 28.0,
    "normal_filter_tau_s": 0.14715687266940003,
}

# far005 request 000000000019 — sealed SPACEFILL with unsaturated PM < 25°.
PM_FAIL_019 = {
    "force_p_gain": 0.004756828460101381,
    "force_i_gain": 0.000905096679918781,
    "force_damping": 16.648899610038093,
    "normal_filter_tau_s": 0.20811124512547616,
}

# Live ANCHOR validation gains (I_OFF) — must remain PM-stable.
QUAL_LIKE_IOFF = {
    "force_p_gain": 0.0003535533906,
    "force_i_gain": 0.0,
    "force_damping": 28.0,
    "normal_filter_tau_s": 0.35,
}


def _plant() -> tuple[float, float]:
    domain = load_domain_design(DOMAIN_PATH)
    return float(domain["greybox_stiffness_n_per_m"]), float(domain["greybox_rho"])


def _pm(point: dict[str, float], stiff: float, rho: float) -> float:
    p_gain = float(point["force_p_gain"])
    damping = float(point["force_damping"])
    i_gain = float(point["force_i_gain"])
    tau = float(point["normal_filter_tau_s"])
    pd = p_gain / damping
    kf = 0.0 if i_gain <= 0.0 else i_gain / p_gain
    return phase_margin_deg(
        stiffness=stiff,
        pd_ratio=pd,
        damping=damping,
        tau_eff_s=tau * rho,
        kf=kf,
    )


def test_domain_artifact_loads() -> None:
    assert DOMAIN_PATH.is_file()
    stiff, rho = _plant()
    assert stiff > 0.0 and rho > 0.0


def test_ioff_qual_like_passes_phase_margin_veto() -> None:
    stiff, rho = _plant()
    assert _pm(QUAL_LIKE_IOFF, stiff, rho) > PM_MIN_DEG
    assert not live_phase_margin_unstable(
        stiffness_n_per_m=stiff,
        rho=rho,
        **QUAL_LIKE_IOFF,
    )


def test_domain_ion_anchor_is_rail_sat_unstable() -> None:
    """Domain I_ON staircase anchor == limit50 attempt10; sat-PM must veto it."""

    domain = load_domain_design(DOMAIN_PATH)
    stiff, rho = _plant()
    anchor = domain["anchor"]
    assert live_phase_margin_unstable(
        force_p_gain=float(anchor["force_p_gain"]),
        force_damping=float(anchor["force_damping"]),
        force_i_gain=float(anchor["force_i_gain"]),
        normal_filter_tau_s=float(anchor["normal_filter_tau_s"]),
        stiffness_n_per_m=stiff,
        rho=rho,
    )


def test_far005_disp42_crash_pm_below_threshold() -> None:
    stiff, rho = _plant()
    pm = _pm(DISP42, stiff, rho)
    assert pm < PM_MIN_DEG
    assert live_phase_margin_unstable(
        stiffness_n_per_m=stiff,
        rho=rho,
        **DISP42,
    )


def test_limit50_attempt10_crash_pm_below_threshold() -> None:
    stiff, rho = _plant()
    pm = _pm(LIMIT50_ATTEMPT10, stiff, rho)
    assert pm < PM_MIN_DEG
    assert live_phase_margin_unstable(
        stiffness_n_per_m=stiff,
        rho=rho,
        **LIMIT50_ATTEMPT10,
    )


def test_far005_pm_fail_request_is_vetoed() -> None:
    stiff, rho = _plant()
    pm = _pm(PM_FAIL_019, stiff, rho)
    # Measured offline on domain greybox: unsaturated already ~-3.8°.
    assert pm < PM_MIN_DEG
    assert live_phase_margin_unstable(
        stiffness_n_per_m=stiff,
        rho=rho,
        **PM_FAIL_019,
    )


def test_invalid_gains_are_unstable() -> None:
    stiff, rho = _plant()
    assert live_phase_margin_unstable(
        force_p_gain=0.0,
        force_damping=28.0,
        force_i_gain=0.0,
        normal_filter_tau_s=0.15,
        stiffness_n_per_m=stiff,
        rho=rho,
    )


def test_ask_choice_filter_excludes_pm_unstable(monkeypatch: pytest.MonkeyPatch) -> None:
    """R008ProductionOptimizer.ask must drop PM-unstable points before GP."""

    from types import SimpleNamespace

    from step5d_autotune_v4_r006.lattice import ParameterPoint
    from step5d_autotune_v4_r008 import live_adapter as mod

    domain = load_domain_design(DOMAIN_PATH)
    stiff = float(domain["greybox_stiffness_n_per_m"])
    rho = float(domain["greybox_rho"])

    def _unstable(point: ParameterPoint) -> bool:
        return live_phase_margin_unstable(
            force_p_gain=point.p_gain,
            force_damping=point.d_gain,
            force_i_gain=point.i_gain,
            normal_filter_tau_s=point.tau_s,
            stiffness_n_per_m=stiff,
            rho=rho,
        )

    bad = point_from_physical(**PM_FAIL_019, orientation_ko=0.2, motion_kp=1.5).to_parameter_point()
    good = point_from_physical(
        **QUAL_LIKE_IOFF,
        orientation_ko=float(domain["anchor"]["orientation_ko"]),
        motion_kp=float(domain["anchor"]["motion_kp"]),
    ).to_parameter_point()
    assert _unstable(bad) is True
    assert _unstable(good) is False

    class _FakeClient:
        def update_artifact_binding(self, *_a, **_k) -> None:
            return None

        def ask(self, *, choices, pending, incumbent, q):  # noqa: ANN001
            assert all(not _unstable(p) for p in choices)
            assert any(
                abs(p.p_gain - good.p_gain) < 1e-12 and abs(p.d_gain - good.d_gain) < 1e-12
                for p in choices
            )
            return SimpleNamespace(point=choices[0], metadata={})

    opt = mod.R008ProductionOptimizer.__new__(mod.R008ProductionOptimizer)
    opt._box = mod.box_from_document(domain)
    opt._domain_stiffness_n_per_m = stiff
    opt._domain_rho = rho
    opt._sobol_seed = 8
    opt._ask_count = 0
    opt.client = _FakeClient()
    opt.r006_ledger = type("L", (), {"sidecar_binding": lambda self: {}})()
    opt.last_ask_metadata = {}

    monkeypatch.setattr(mod, "propose_candidates", lambda *_a, **_k: (bad, good))
    monkeypatch.setattr(mod, "neighbors", lambda *_a, **_k: ())
    monkeypatch.setattr(mod, "live_acquisition_unstable", lambda **_k: False)
    monkeypatch.setattr(mod, "_point_from_candidate", lambda c: c)
    monkeypatch.setattr(mod, "_candidate_from_point", lambda p: p)

    ask = mod.R008ProductionOptimizer.ask(
        opt,
        observations=(),
        pending=(),
        incumbent=good,
        q=1,
    )
    assert not _unstable(ask.candidate)
