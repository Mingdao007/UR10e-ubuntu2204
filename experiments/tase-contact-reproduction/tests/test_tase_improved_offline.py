"""Numerical sanity checks for the offline-only TASE-improved variant."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_paper_outer_loop import contact_gated_leaky_step  # noqa: E402
from tase_improved_offline import (  # noqa: E402
    TaseImprovedOfflineMethodAdapter,
    project_local_normal_force,
    solve_normal_priority_qp,
)


def _unit(value: np.ndarray) -> np.ndarray:
    return value / np.linalg.norm(value)


@pytest.mark.parametrize(
    "normal",
    (
        np.array((0.0, 0.0, 1.0)),  # flat proxy
        _unit(np.array((0.25, -0.18, 0.95))),  # tilted proxy
        _unit(np.array((0.002 * 0.7, -0.003 * -0.4, 1.0))),  # low-curvature proxy
    ),
)
def test_local_normal_projection_is_model_free_for_unknown_surface_proxies(normal: np.ndarray) -> None:
    tangent = _unit(np.cross(normal, np.array((1.0, 0.0, 0.0))))
    force = 4.0 * normal + 0.7 * tangent
    projection = project_local_normal_force(force, normal)

    np.testing.assert_allclose(projection.normal_force_n, 4.0, atol=1e-12)
    np.testing.assert_allclose(projection.tangential_force_n, 0.7, atol=1e-12)
    np.testing.assert_allclose(
        np.dot(np.asarray(projection.tangential_force_base_n), normal), 0.0, atol=1e-12
    )
    np.testing.assert_allclose(np.linalg.norm(projection.normal_base), 1.0, atol=1e-12)


def _integral_kwargs(**overrides: float | bool | str) -> dict[str, object]:
    values: dict[str, object] = {
        "force_error_n": 0.1,
        "normal_load_n": 1.0,
        "contact_gate_force_n": 0.5,
        "integral_state_n_s": 0.8,
        "dt_s": 0.1,
        "leak_tau_s": 0.5,
        "force_p_gain": 0.1,
        "force_i_gain": 0.1,
        "force_damping": 0.0,
        "normal_velocity_m_s": 0.0,
        "normal_velocity_limit_m_s": 1.0,
        "state_limit_n_s": 1.0,
        "authority_error_n": 1.0,
        "integral_enabled": True,
        "reset_reason": "",
    }
    values.update(overrides)
    return values


def test_i_off_raw_clipped_and_gated_leaky_integrals_are_distinct() -> None:
    i_off = contact_gated_leaky_step(
        **_integral_kwargs(integral_enabled=False, reset_reason="i_off")
    )
    raw_clipped = float(np.clip(0.8 + 0.1 * 0.1, -1.0, 1.0))
    gated_leaky = contact_gated_leaky_step(**_integral_kwargs())
    ungated_leaky = contact_gated_leaky_step(
        **_integral_kwargs(normal_load_n=0.1)
    )

    assert i_off.integral_state_n_s == pytest.approx(0.0)
    assert raw_clipped == pytest.approx(0.81)
    assert gated_leaky.integral_state_n_s == pytest.approx(
        0.8 * np.exp(-0.1 / 0.5) + 0.1 * 0.1
    )
    assert gated_leaky.integral_state_n_s < raw_clipped
    assert gated_leaky.contact_gated is True
    assert ungated_leaky.contact_gated is False
    assert ungated_leaky.integral_state_n_s == pytest.approx(0.8 * np.exp(-0.1 / 0.5))


def test_gated_leaky_integral_anti_windup_freezes_same_sign_innovation() -> None:
    result = contact_gated_leaky_step(
        **_integral_kwargs(
            force_error_n=10.0,
            integral_state_n_s=0.8,
            force_p_gain=0.5,
            force_i_gain=0.5,
            normal_velocity_limit_m_s=0.01,
        )
    )

    assert result.velocity_saturated is True
    assert result.conditional_frozen is True
    assert result.integral_state_n_s < 0.8
    assert result.integral_saturated is True


def test_normal_priority_qp_spends_infeasibility_on_tangent_task() -> None:
    result = solve_normal_priority_qp(
        jacobian=np.eye(6),
        xdot_c=(0.1, 0.0, -0.004, 0.0, 0.0, 0.0),
        reaction_normal_base=(0.0, 0.0, 1.0),
        omega_minus=(-0.01,) * 6,
        omega_plus=(0.01,) * 6,
    )

    assert result.exact_feasible is False
    assert result.normal_residual < 1e-8
    assert result.tangential_residual_norm > 0.08
    assert result.bound_violation == pytest.approx(0.0)
    assert result.residual_norm > 0.08


def _adapter_inputs(local_normal=(0.0, 0.0, 1.0)) -> tuple[dict[str, object], dict[str, object]]:
    measured: dict[str, object] = {
        "tcp_pose_base": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "tcp_velocity_base": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "wrench_tcp": (0.2, 0.0, 1.0, 0.0, 0.0, 0.0),
        "local_normal_base": local_normal,
        "jacobian_base": np.eye(6),
        "constraints": {
            "joint_velocity_lower": (-0.15,) * 6,
            "joint_velocity_upper": (0.15,) * 6,
        },
    }
    target: dict[str, object] = {
        "x_pd_base": (0.0, 0.0, 0.0),
        "xdot_pd_base": (0.0, 0.0, 0.0),
        "force_target_n": 1.0,
    }
    return measured, target


def test_improved_adapter_keeps_first_dt_and_atomic_restore() -> None:
    adapter = TaseImprovedOfflineMethodAdapter()
    measured, target = _adapter_inputs()
    first = adapter.step(measured, target, 0.001)
    assert first.diagnostics["first_sample"] is True
    assert first.diagnostics["actual_dt_s"] == pytest.approx(0.001)
    assert first.diagnostics["offline_only"] is True
    assert first.diagnostics["live_eligible"] is False
    assert "qp_slack" in first.diagnostics
    assert "qp_residual_norm" in first.diagnostics

    checkpoint = adapter.snapshot()
    replay_a = adapter.step(measured, target, 0.003)
    adapter.restore(checkpoint)
    replay_b = adapter.step(measured, target, 0.003)
    assert replay_a == replay_b

    before = adapter.snapshot()
    invalid_target = dict(target, force_target_n=float("nan"))
    with pytest.raises(ValueError):
        adapter.step(measured, invalid_target, 0.002)
    assert adapter.snapshot() == before
