"""NO-v3 coplanarity candidate: legacy identity, Coulomb identifiability, gates."""
import json
from pathlib import Path

import numpy as np
import pytest

from contact_yield_math import so3_exp
from contact_yield_normal import NormalEstimator
from contact_yield_replay import replay_artifact
from contact_yield_runner import run_closed_loop
from test_contact_yield import library

ROOT = Path(__file__).resolve().parents[1]
PARAMS = json.loads((ROOT / "config/yield_normal_observer_v3.json").read_text())
V2 = json.loads((ROOT / "config/yield_normal_observer_v2.json").read_text())
TRUE = np.array([0.0, 0.0, -1.0])
DT = 0.002
LEGACY_DIAGNOSTIC = {
    "inward_normal_base",
    "contact_gate",
    "excitation_gate",
    "motion_update_applied",
    "force_bias_correction_applied",
    "signed_normal_load_n",
    "tangent_speed_m_s",
    "friction_bias",
}


def unit(value):
    vector = np.asarray(value, dtype=float)
    return vector / np.linalg.norm(vector)


def angle(left, right):
    return float(np.arccos(np.clip(unit(left) @ unit(right), -1.0, 1.0)))


def coulomb_force(normal, velocity, load_n=5.0, mu=0.4):
    inward = unit(normal)
    tangent = np.asarray(velocity, dtype=float) - inward * (inward @ velocity)
    return load_n * (-inward) - mu * load_n * unit(tangent)


def step(estimator, velocity, force, in_contact=True, dt_s=DT):
    return estimator.update(
        dt_s=dt_s,
        measured_linear_velocity_base_m_s=velocity,
        measured_force_base_n=force,
        in_contact=in_contact,
    )


def make(prior=None, **overrides):
    parameters = dict(PARAMS)
    parameters.update(overrides)
    if prior is None:
        return NormalEstimator(TRUE, **parameters)
    return NormalEstimator(TRUE, initial_inward_normal_base=unit(prior), **parameters)


def drive(estimator, samples):
    return [estimator.update(**sample) for sample in samples]


def test_frozen_v3_config_disables_force_bias_and_keeps_new_gains():
    assert PARAMS == {
        "motion_gain": 0.3,
        "force_correction_gain": 0.0,
        "motion_normalization_floor_m_s": 0.002,
        "motion_rate_cap_rad_s": 0.05,
        "coplanarity_gain_s_inv": 0.3,
        "coplanarity_cross_floor_n_m_s": 1e-9,
    }


def test_zero_coplanarity_gain_preserves_legacy_and_v2_identity_and_arithmetic():
    samples = (
        dict(dt_s=DT, measured_linear_velocity_base_m_s=[0.01, 0.0, 0.002],
             measured_force_base_n=[0.2, 0.1, 5.0], in_contact=True),
        dict(dt_s=DT, measured_linear_velocity_base_m_s=[0.0, 0.012, -0.001],
             measured_force_base_n=[0.0, 0.0, 6.0], in_contact=True),
        dict(dt_s=DT, measured_linear_velocity_base_m_s=[0.01, 0.0, 0.0],
             measured_force_base_n=[0.0, 0.0, 5.0], in_contact=False),
        dict(dt_s=0.001, measured_linear_velocity_base_m_s=[0.003, 0.004, 0.001],
             measured_force_base_n=[0.4, -0.2, 4.0], in_contact=True),
    )
    legacy = NormalEstimator(TRUE)
    silent = NormalEstimator(TRUE, coplanarity_gain_s_inv=0.0, coplanarity_cross_floor_n_m_s=1e-3)
    assert "coplanarity_gain_s_inv" not in legacy.parameters()
    assert "coplanarity_cross_floor_n_m_s" not in legacy.parameters()
    assert silent.parameters() == legacy.parameters()
    for expected, actual in zip(drive(legacy, samples), drive(silent, samples)):
        assert set(actual) == LEGACY_DIAGNOSTIC
        assert actual == expected
    np.testing.assert_array_equal(legacy.normal, silent.normal)

    prior = unit([0.15, 0.12, -1.0])
    v2 = NormalEstimator(prior, **V2)
    v2_silent = NormalEstimator(prior, **V2, coplanarity_gain_s_inv=0.0)
    assert v2.parameters() == v2_silent.parameters()
    assert "coplanarity_gain_s_inv" not in v2.parameters()
    for expected, actual in zip(drive(v2, samples), drive(v2_silent, samples)):
        assert set(actual) == LEGACY_DIAGNOSTIC
        assert actual == expected
    np.testing.assert_array_equal(v2.normal, v2_silent.normal)


def test_coulomb_coplanarity_is_valid_with_normal_velocity_but_blind_along_slide():
    velocity = np.array([0.008, 0.0, -0.006])
    force = coulomb_force(TRUE, velocity)
    honest = make(prior=TRUE, motion_gain=0.0)
    transverse = make(prior=unit([0.0, 0.2, -1.0]), motion_gain=0.0)
    along = make(prior=unit([0.2, 0.0, -1.0]), motion_gain=0.0)
    start_y = abs(transverse.normal[1])
    start_x = abs(along.normal[0])
    for _ in range(3000):
        honest_step = step(honest, velocity, force)
        transverse_step = step(transverse, velocity, force)
        along_step = step(along, velocity, force)
        assert honest_step["contact_gate"] and honest_step["excitation_gate"]
        assert abs(honest_step["coplanarity_residual"]) < 1e-12
        assert not honest_step["motion_update_applied"]
        assert transverse_step["coplanarity_update_applied"]
        assert along_step["coplanarity_update_applied"]
        assert abs(along_step["coplanarity_residual"]) < 1e-12
    assert angle(honest.normal, TRUE) < 1e-12
    assert abs(transverse.normal[1]) < 0.25 * start_y
    assert abs(along.normal[0]) > 0.98 * start_x
    # Motion still sees the along-slide tilt when the measured velocity is tangential.
    tangent = np.array([0.008, 0.0, 0.0])
    combined = make(prior=unit([0.2, 0.0, -1.0]))
    for _ in range(3000):
        result = step(combined, tangent, coulomb_force(TRUE, tangent))
        assert result["motion_update_applied"]
    assert abs(combined.normal[0]) < 0.25 * start_x


def test_transverse_applied_force_contaminates_coplanarity():
    velocity = np.array([0.008, 0.0, -0.006])
    force = coulomb_force(TRUE, velocity) + np.array([0.0, 2.0, 0.0])
    estimator = make(prior=TRUE, motion_gain=0.0)
    first = step(estimator, velocity, force)
    assert first["coplanarity_update_applied"]
    assert abs(first["coplanarity_residual"]) > 0.2
    for _ in range(1500):
        step(estimator, velocity, force)
    assert abs(estimator.normal[1]) > 0.05


def test_combined_rate_cap_and_existing_gates():
    velocity = np.array([0.01, 0.0, 0.0])
    force = coulomb_force(TRUE, velocity)
    prior = unit([0.35, 0.35, -1.0])
    capped = make(prior=prior, motion_gain=40.0, coplanarity_gain_s_inv=40.0)
    before = capped.normal.copy()
    result = step(capped, velocity, force)
    assert result["motion_update_applied"] and result["coplanarity_update_applied"]
    turned = angle(before, capped.normal)
    assert turned <= DT * PARAMS["motion_rate_cap_rad_s"] + 1e-12
    assert turned > 0.5 * DT * PARAMS["motion_rate_cap_rad_s"]

    gated = make(prior=prior, motion_gain=0.0)
    untouched = gated.snapshot()
    lost_contact = step(gated, velocity, force, in_contact=False)
    assert lost_contact["coplanarity_residual"] is not None
    assert not lost_contact["coplanarity_update_applied"]
    assert not lost_contact["motion_update_applied"]
    assert gated.snapshot() == untouched
    weak_load = step(gated, velocity, [0.0, 0.0, 0.2])
    assert not weak_load["contact_gate"]
    assert not weak_load["coplanarity_update_applied"]
    assert gated.snapshot() == untouched
    quiet = step(gated, [0.0005, 0.0, 0.0], force)
    assert quiet["contact_gate"] and not quiet["excitation_gate"]
    assert not quiet["coplanarity_update_applied"]
    assert gated.snapshot() == untouched


def test_degenerate_cross_product_skips_coplanarity_and_keeps_finite_unit_state():
    velocity = np.array([0.01, 0.0, 0.005])
    force = 1000.0 * velocity
    estimator = make(prior=TRUE, motion_gain=0.0)
    before = estimator.snapshot()
    result = step(estimator, velocity, force)
    assert result["contact_gate"] and result["excitation_gate"]
    assert result["coplanarity_cross_norm_n_m_s"] <= PARAMS["coplanarity_cross_floor_n_m_s"]
    assert result["coplanarity_residual"] is None
    assert not result["coplanarity_update_applied"]
    assert not result["motion_update_applied"]
    assert estimator.snapshot() == before

    tilted = make(prior=unit([0.2, 0.15, -1.0]))
    for _ in range(200):
        sample = step(tilted, [0.008, 0.0, -0.006], coulomb_force(TRUE, [0.008, 0.0, -0.006]))
        assert sample["coplanarity_residual"] is not None
        assert np.isfinite(sample["coplanarity_residual"])
        assert np.isfinite(sample["coplanarity_cross_norm_n_m_s"])
    assert abs(np.linalg.norm(tilted.normal) - 1.0) < 1e-12
    with pytest.raises(ValueError, match="finite"):
        step(tilted, [np.nan, 0.0, 0.0], [0.0, 0.0, 5.0])
    with pytest.raises(ValueError, match="unit"):
        tilted.restore({"inward_normal_base": [0.0, 0.0, -2.0], "approach_inward_base": TRUE.tolist()})


def test_invalid_coplanarity_gain_and_floor_are_rejected():
    with pytest.raises(ValueError, match="nonnegative"):
        NormalEstimator(TRUE, coplanarity_gain_s_inv=-0.1)
    with pytest.raises(ValueError, match="finite"):
        NormalEstimator(TRUE, coplanarity_gain_s_inv=float("inf"))
    with pytest.raises(ValueError, match="positive"):
        NormalEstimator(TRUE, coplanarity_cross_floor_n_m_s=0.0)
    with pytest.raises(ValueError, match="positive"):
        NormalEstimator(TRUE, coplanarity_cross_floor_n_m_s=-1e-9)
    with pytest.raises(ValueError, match="finite"):
        NormalEstimator(TRUE, coplanarity_cross_floor_n_m_s=float("nan"))
    with pytest.raises(ValueError, match="motion_normalization_floor"):
        NormalEstimator(TRUE, coplanarity_gain_s_inv=0.3)


def test_combined_rate_cap_does_not_bound_force_bias_branch():
    velocity = np.array([0.01, 0.0, 0.0])
    force = np.array([0.2, 0.0, 5.0])
    estimator = make(
        prior=TRUE,
        motion_gain=0.0,
        force_correction_gain=1.5,
        motion_rate_cap_rad_s=1e-6,
    )
    before = estimator.normal.copy()
    result = step(estimator, velocity, force)
    assert result["force_bias_correction_applied"]
    assert angle(before, estimator.normal) > 50.0 * DT * 1e-6


def test_active_parameters_replay_with_nonzero_prior(library):
    approach = np.array([0.0, 0.0, -1.0])
    axis = np.cross(approach, [1.0, 0.0, 0.0])
    prior = so3_exp(unit(axis) * np.deg2rad(10.0)) @ approach
    artifact = run_closed_loop(
        method="DSFC",
        duration_s=0.08,
        qp_library=library,
        estimator_parameters={**PARAMS, "initial_inward_normal_base": prior.tolist()},
    )
    assert not artifact["metrics"]["failed"]
    stored = artifact["identity_payload"]["estimator_parameters"]
    assert stored["coplanarity_gain_s_inv"] == 0.3
    assert stored["coplanarity_cross_floor_n_m_s"] == 1e-9
    assert stored["force_correction_gain"] == 0.0
    np.testing.assert_allclose(
        artifact["initial_controller_snapshot"]["normal_estimate"]["inward_normal_base"],
        prior,
    )
    assert replay_artifact(artifact)["passed"]
