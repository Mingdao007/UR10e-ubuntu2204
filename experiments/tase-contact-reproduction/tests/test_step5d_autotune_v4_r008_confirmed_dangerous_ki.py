"""Confirmed-dangerous Ki≈0.00181 must be excluded from STAIRCASE/BO proposals.

Evidence (exact same lattice Ki, independent P/D):
1. far005 BO_TRIAL disp42 — P=0.003364, Ki=0.001810193359837562, D=158.39 → PATH ~61 N
2. limit50 STAIRCASE attempt10 (source=r006_staircase) — P=0.002828, same Ki, D=28
   → PATH ~0.218 s from ~6.5 N to ~61.2 N

``phase_margin_deg`` misses this pocket; exclusion is explicit in log2(Ki).
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

from step5d_autotune_v4_r006.contracts import I_ON_ANCHOR, STEP_OCTAVE  # noqa: E402
from step5d_autotune_v4_r008.design_binding import (  # noqa: E402
    anchor_from_document,
    load_domain_design,
)
from step5d_autotune_v4_r008.lattice import (  # noqa: E402
    CONFIRMED_DANGEROUS_KI,
    CONFIRMED_DANGEROUS_KI_RADIUS_LOG2,
    confirmed_dangerous_ki,
    default_box,
    scrambled_sobol,
    snap_to_quarter,
)
from step5d_autotune_v4_r008.staircase import (  # noqa: E402
    build_staircase_coupled_fixed_kf,
)

DOMAIN_PATH = ROOT / "config" / "step5d" / "autotune_v4_r008_domain.json"

# far005 live_20260805_120727 / ARCHIVED_far005 disp42
FAR005_DISP42 = {
    "force_p_gain": 0.003363585661078849,
    "force_i_gain": 0.001810193359837562,
    "force_damping": 158.39191898578665,
}

# limit50-pathring dispatch 000000000010 (STAIRCASE, not ANCHOR / not I_OFF)
LIMIT50_ATTEMPT10 = {
    "force_p_gain": 0.0028284271248,
    "force_i_gain": 0.001810193359837562,
    "force_damping": 28.0,
}


def _ki_at_i_step_delta(delta: int) -> float:
    center_step = int(
        round(math.log2(CONFIRMED_DANGEROUS_KI / I_ON_ANCHOR) / STEP_OCTAVE)
    )
    return float(I_ON_ANCHOR * (2.0 ** ((center_step + delta) * STEP_OCTAVE)))


def test_radius_is_one_quarter_octave() -> None:
    assert CONFIRMED_DANGEROUS_KI_RADIUS_LOG2 == pytest.approx(STEP_OCTAVE)
    assert CONFIRMED_DANGEROUS_KI_RADIUS_LOG2 == pytest.approx(0.25)


def test_exact_evidence_ki_is_banned() -> None:
    assert FAR005_DISP42["force_i_gain"] == LIMIT50_ATTEMPT10["force_i_gain"]
    assert FAR005_DISP42["force_i_gain"] == pytest.approx(CONFIRMED_DANGEROUS_KI)
    assert confirmed_dangerous_ki(force_i_gain=CONFIRMED_DANGEROUS_KI)
    assert confirmed_dangerous_ki(force_i_gain=FAR005_DISP42["force_i_gain"])
    assert confirmed_dangerous_ki(force_i_gain=LIMIT50_ATTEMPT10["force_i_gain"])


def test_adjacent_lattice_neighbors_banned_distant_admitted() -> None:
    # ±1 quarter-octave (one I lattice step) inside radius.
    assert confirmed_dangerous_ki(force_i_gain=_ki_at_i_step_delta(-1))
    assert confirmed_dangerous_ki(force_i_gain=_ki_at_i_step_delta(+1))
    # ±2 quarter-octaves (half octave) outside radius — still proposable.
    assert not confirmed_dangerous_ki(force_i_gain=_ki_at_i_step_delta(-2))
    assert not confirmed_dangerous_ki(force_i_gain=_ki_at_i_step_delta(+2))
    # Safe distant / I_OFF.
    assert not confirmed_dangerous_ki(force_i_gain=0.0)
    assert not confirmed_dangerous_ki(force_i_gain=I_ON_ANCHOR)
    assert not confirmed_dangerous_ki(force_i_gain=_ki_at_i_step_delta(-4))


def test_domain_coupled_staircase_level0_hits_banned_ki_higher_levels_clear() -> None:
    """Legacy coupled fixed-kf plan: document pocket at scale=1; ×2 leaves ±1 ban.

    Host resume must NOT use this builder (see P-up Ki-lock tests). Higher
    coupled scales clear the ±1 quarter-octave ban only because Ki doubles
    away from the pocket — that growth is still rejected for live trials.
    """

    domain = load_domain_design(DOMAIN_PATH)
    anchor = anchor_from_document(domain)
    levels = build_staircase_coupled_fixed_kf(anchor)
    snapped0 = levels[0].point.to_parameter_point().i_gain
    assert confirmed_dangerous_ki(force_i_gain=float(snapped0))
    for level in levels[1:]:
        ki = float(level.point.to_parameter_point().i_gain)
        assert not confirmed_dangerous_ki(force_i_gain=ki), (level.scale, ki)


def test_sobol_proposals_exclude_dangerous_ki() -> None:
    points = scrambled_sobol(default_box(), count=48, seed=11)
    for point in points:
        typed = point.to_parameter_point()
        assert not confirmed_dangerous_ki(force_i_gain=float(typed.i_gain))


def test_ask_choice_filter_excludes_dangerous_ki(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from step5d_autotune_v4_r008 import live_adapter as mod
    from step5d_autotune_v4_r008.lattice import point_from_physical

    domain = load_domain_design(DOMAIN_PATH)
    bad = point_from_physical(
        force_p_gain=FAR005_DISP42["force_p_gain"],
        force_damping=FAR005_DISP42["force_damping"],
        force_i_gain=FAR005_DISP42["force_i_gain"],
        normal_filter_tau_s=0.0875,
        orientation_ko=0.8,
        motion_kp=2.121320343559643,
    ).to_parameter_point()
    good_ki = _ki_at_i_step_delta(-4)
    good = point_from_physical(
        force_p_gain=float(domain["anchor"]["force_p_gain"]),
        force_damping=float(domain["anchor"]["force_damping"]),
        force_i_gain=good_ki,
        normal_filter_tau_s=float(domain["anchor"]["normal_filter_tau_s"]),
        orientation_ko=float(domain["anchor"]["orientation_ko"]),
        motion_kp=float(domain["anchor"]["motion_kp"]),
    ).to_parameter_point()
    # Snap good_ki onto lattice via the point path.
    good = point_from_physical(
        force_p_gain=good.p_gain,
        force_damping=good.d_gain,
        force_i_gain=snap_to_quarter(good_ki, I_ON_ANCHOR),
        normal_filter_tau_s=good.tau_s,
        orientation_ko=good.ko,
        motion_kp=good.kp,
    ).to_parameter_point()

    assert confirmed_dangerous_ki(force_i_gain=bad.i_gain)
    assert not confirmed_dangerous_ki(force_i_gain=good.i_gain)

    class _FakeClient:
        def update_artifact_binding(self, *_a, **_k) -> None:
            return None

        def ask(self, *, choices, pending, incumbent, q):  # noqa: ANN001
            assert all(not confirmed_dangerous_ki(force_i_gain=p.i_gain) for p in choices)
            assert any(abs(p.i_gain - good.i_gain) < 1e-15 for p in choices)
            return SimpleNamespace(point=choices[0], metadata={})

    opt = mod.R008ProductionOptimizer.__new__(mod.R008ProductionOptimizer)
    opt._box = mod.box_from_document(domain)
    opt._domain_stiffness_n_per_m = float(domain["greybox_stiffness_n_per_m"])
    opt._domain_rho = float(domain["greybox_rho"])
    opt._sobol_seed = 8
    opt._ask_count = 0
    opt._include_kf_off_fraction = 1.0
    opt.client = _FakeClient()
    opt.r006_ledger = type("L", (), {"sidecar_binding": lambda self: {}})()
    opt.last_ask_metadata = {}

    monkeypatch.setattr(mod, "propose_candidates", lambda *_a, **_k: (bad, good))
    monkeypatch.setattr(mod, "neighbors", lambda *_a, **_k: ())
    monkeypatch.setattr(mod, "live_acquisition_unstable", lambda **_k: False)
    monkeypatch.setattr(mod, "live_phase_margin_unstable", lambda **_k: False)
    monkeypatch.setattr(mod, "_point_from_candidate", lambda c: c)
    monkeypatch.setattr(mod, "_candidate_from_point", lambda p: p)

    ask = mod.R008ProductionOptimizer.ask(
        opt,
        observations=(),
        pending=(),
        incumbent=good,
        q=1,
    )
    assert not confirmed_dangerous_ki(force_i_gain=ask.candidate.i_gain)
