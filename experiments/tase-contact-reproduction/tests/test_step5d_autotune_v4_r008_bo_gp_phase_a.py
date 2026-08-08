"""Phase A BO/GP unit tests: feature map, incumbent, Sobol I-off, train dedup."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r006.lattice import ANCHOR_POINT, IMode, ParameterPoint  # noqa: E402
from step5d_autotune_v4_r006.optimizer_worker import _features as r006_features  # noqa: E402
from step5d_autotune_v4_r008.lattice import (  # noqa: E402
    PIN_TAU_S,
    PIN_TAU_STEP,
    default_box,
    pin_parameter_point_tau,
    scrambled_sobol,
)
from step5d_autotune_v4_r008.live_adapter import R008HostLoop, R008ProductionOptimizer  # noqa: E402
from step5d_autotune_v4_r008.optimizer import (  # noqa: E402
    CANDIDATE_SOBOL,
    feature_map_r008,
    propose_candidates,
)
from step5d_autotune_v4_r008.optimizer_worker_batched import (  # noqa: E402
    FIXED_NOISE_N2,
    _features as batched_features,
)


def test_batched_worker_features_match_feature_map_r008() -> None:
    point = ParameterPoint(
        p_step=12,
        d_step=8,
        tau_step=0,
        i_mode=IMode.OFF,
        i_step=None,
        ko_step=2,
        kp_step=2,
    )
    assert batched_features(point) == feature_map_r008(point)
    # Must differ from the frozen r006 map (axis0 is log2(P/D), not -log2(P)).
    assert batched_features(point) != r006_features(point)
    assert batched_features(point)[0] == feature_map_r008(point)[0]


def test_fixed_noise_floor_is_2p5e_5() -> None:
    assert FIXED_NOISE_N2 == 2.5e-5


def test_scrambled_sobol_i_off_lock_is_native_i_off() -> None:
    points = scrambled_sobol(
        default_box(), count=24, seed=8, include_kf_off_fraction=1.0
    )
    assert len(points) == 24
    assert all(point.kf_off for point in points)
    typed = [point.to_parameter_point() for point in points]
    assert all(p.i_mode is IMode.OFF and p.i_gain == 0.0 for p in typed)


def test_scrambled_sobol_tau_pin_is_native_constant() -> None:
    points = scrambled_sobol(default_box(), count=24, seed=8, pin_tau=True)
    assert len(points) == 24
    typed = [point.to_parameter_point() for point in points]
    assert all(p.tau_s == pytest.approx(PIN_TAU_S) for p in typed)
    assert all(p.tau_step == PIN_TAU_STEP for p in typed)


def test_propose_candidates_respects_kf_off_fraction() -> None:
    locked = propose_candidates(
        default_box(), count=CANDIDATE_SOBOL, seed=8, include_kf_off_fraction=1.0
    )
    assert len(locked) == CANDIDATE_SOBOL
    assert all(p.i_mode is IMode.OFF and p.i_gain == 0.0 for p in locked)
    assert all(p.tau_s == pytest.approx(PIN_TAU_S) for p in locked)


def test_pin_parameter_point_tau_rewrites_neighbors() -> None:
    moved = ParameterPoint(tau_step=PIN_TAU_STEP + 2)
    pinned = pin_parameter_point_tau(moved)
    assert pinned.tau_step == PIN_TAU_STEP
    assert pinned.tau_s == pytest.approx(PIN_TAU_S)


def test_bo_incumbent_is_argmin_mae_not_pending_tail() -> None:
    loop = object.__new__(R008HostLoop)
    loop.epoch = 1
    elite = SimpleNamespace(i_off=True, force_i_gain=0.0, force_damping=188.0)
    tail = SimpleNamespace(i_off=True, force_i_gain=0.0, force_damping=16.6)
    cursor = SimpleNamespace(i_off=True, force_i_gain=0.0, force_damping=1.0)
    loop.cursor = cursor
    loop.ledger = SimpleNamespace(
        records=(
            SimpleNamespace(
                epoch=1, sealed=True, eligible=True, mae_n=0.88, candidate=elite
            ),
            SimpleNamespace(
                epoch=1, sealed=True, eligible=True, mae_n=1.25, candidate=tail
            ),
            # Other epoch must not win.
            SimpleNamespace(
                epoch=2,
                sealed=True,
                eligible=True,
                mae_n=0.10,
                candidate=SimpleNamespace(i_off=True, force_damping=999.0),
            ),
            # I-on must not win under I-off incumbent rule.
            SimpleNamespace(
                epoch=1,
                sealed=True,
                eligible=True,
                mae_n=0.50,
                candidate=SimpleNamespace(i_off=False, force_i_gain=0.01),
            ),
        )
    )
    assert loop._bo_incumbent_candidate() is elite


def test_bo_incumbent_falls_back_to_cursor_when_empty() -> None:
    loop = object.__new__(R008HostLoop)
    loop.epoch = 1
    cursor = SimpleNamespace(i_off=True, force_damping=1.0)
    loop.cursor = cursor
    loop.ledger = SimpleNamespace(records=())
    assert loop._bo_incumbent_candidate() is cursor


def test_ask_excludes_sealed_trainable_train_keys(monkeypatch) -> None:
    """Choice pool must drop sealed trainable keys (pending exclusion alone is insufficient)."""

    from step5d_autotune_v4_r006.lattice import neighbors

    trained = ANCHOR_POINT
    pending_point = ParameterPoint(p_step=1)
    sobol_extra = ParameterPoint(p_step=5, d_step=3)

    captured: dict[str, object] = {}

    class _FakeClient:
        def update_artifact_binding(self, binding) -> None:
            return None

        def ask(self, *, choices, pending, incumbent, q):
            captured["choices"] = tuple(choices)
            captured["pending"] = tuple(pending)
            return SimpleNamespace(point=choices[0], metadata={"ok": True})

    opt = object.__new__(R008ProductionOptimizer)
    opt._box = default_box()
    opt._domain_stiffness_n_per_m = 1.0e5
    opt._domain_rho = 1.0
    opt._sobol_seed = 8
    opt._ask_count = 0
    opt._include_kf_off_fraction = 1.0
    opt.client = _FakeClient()
    opt.r006_ledger = SimpleNamespace(sidecar_binding=lambda: {})
    opt.last_ask_metadata = {}

    monkeypatch.setattr(
        "step5d_autotune_v4_r008.live_adapter.propose_candidates",
        lambda *a, **k: (trained, pending_point, sobol_extra),
    )
    monkeypatch.setattr(
        "step5d_autotune_v4_r008.live_adapter.neighbors",
        lambda incumbent: (),
    )
    monkeypatch.setattr(
        "step5d_autotune_v4_r008.live_adapter.live_acquisition_unstable",
        lambda **kwargs: False,
    )
    monkeypatch.setattr(
        "step5d_autotune_v4_r008.live_adapter.confirmed_dangerous_ki",
        lambda **kwargs: False,
    )
    monkeypatch.setattr(
        "step5d_autotune_v4_r008.live_adapter.live_phase_margin_unstable",
        lambda **kwargs: False,
    )
    monkeypatch.setattr(
        "step5d_autotune_v4_r008.live_adapter._point_from_candidate",
        lambda candidate: candidate if isinstance(candidate, ParameterPoint) else candidate,
    )
    monkeypatch.setattr(
        "step5d_autotune_v4_r008.live_adapter._candidate_from_point",
        lambda point: point,
    )

    observations = (
        SimpleNamespace(sealed=True, eligible=True, candidate=trained),
        SimpleNamespace(sealed=True, eligible=False, candidate=ParameterPoint(p_step=9)),
    )
    opt.ask(
        observations=observations,
        pending=(pending_point,),
        incumbent=trained,
        q=1,
    )
    choices = captured["choices"]
    assert trained not in choices
    assert pending_point not in choices
    assert sobol_extra in choices


def test_host_sobol_fraction_locked_vs_unlocked(monkeypatch) -> None:
    loop = object.__new__(R008HostLoop)
    monkeypatch.setattr(loop, "_has_i_on_unlock_observation", lambda: False)
    assert loop._sobol_include_kf_off_fraction() == 1.0
    monkeypatch.setattr(loop, "_has_i_on_unlock_observation", lambda: True)
    assert loop._sobol_include_kf_off_fraction() == 0.15
