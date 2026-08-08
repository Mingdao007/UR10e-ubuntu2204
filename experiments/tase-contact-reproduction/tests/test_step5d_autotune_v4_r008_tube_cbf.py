"""Offline unit tests for R008 analytic Tube CBF-QP."""

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

from step5d_autotune_v4_r008.tube_cbf import (  # noqa: E402
    PathEllipse,
    TubeCbfQp,
    filter_twist_xy,
    microbench_filter_twist_xy,
)


R_M = 0.030
ALPHA = 2.0
ENGAGE = 0.67


@pytest.fixture
def circle_qp() -> TubeCbfQp:
    return TubeCbfQp.from_semi_axes(R_M, R_M)


def test_deep_inside_is_identity_not_fired(circle_qp: TubeCbfQp) -> None:
    v_nom = (0.01, -0.02, 0.003)
    result = circle_qp.filter_twist_xy(
        v_nom, (0.0, 0.0), alpha=ALPHA, engage_rho=ENGAGE
    )
    assert result.rho == pytest.approx(0.0)
    assert result.h > 0.0
    assert result.engaged is False
    assert result.fired is False
    assert result.infeasible is False
    assert result.v_xyz == pytest.approx(v_nom)
    assert result.v_safe_xyz == pytest.approx(v_nom)


def test_near_wall_outward_velocity_fires_and_reduces(circle_qp: TubeCbfQp) -> None:
    # rho = 0.022/0.030 ≈ 0.733 > engage_rho; outward +x velocity.
    u = 0.022
    v_nom = (0.05, 0.0, 0.0)
    result = circle_qp.filter_twist_xy(
        v_nom, (u, 0.0), alpha=ALPHA, engage_rho=ENGAGE
    )
    assert result.engaged is True
    assert result.fired is True
    assert result.infeasible is False
    assert result.v_xyz[0] < v_nom[0]
    assert result.v_xyz[0] == pytest.approx(ALPHA * (R_M - u))  # circle SDF projection
    # CBF residual must be non-negative after correction.
    gx, gy = result.grad_h_xy
    residual = gx * result.v_xyz[0] + gy * result.v_xyz[1] + ALPHA * result.h
    assert residual == pytest.approx(0.0, abs=1e-12)


def test_vz_unchanged(circle_qp: TubeCbfQp) -> None:
    vz = -0.007
    result = circle_qp.filter_twist_xy(
        (0.05, 0.01, vz), (0.022, 0.0), alpha=ALPHA, engage_rho=ENGAGE
    )
    assert result.fired is True
    assert result.v_xyz[2] == pytest.approx(vz)


def test_a_equals_b_circle_isotropic() -> None:
    ellipse = PathEllipse(a_m=R_M, b_m=R_M)
    # Same radial offset on u and on v must match for a=b.
    phi_u = ellipse.phi(0.015, 0.0)
    phi_v = ellipse.phi(0.0, 0.015)
    assert phi_u == pytest.approx(phi_v)
    assert phi_u == pytest.approx(0.015 - R_M)
    qp = TubeCbfQp.from_ellipse(ellipse)
    ru = qp.filter_twist_xy((0.04, 0.0, 0.0), (0.022, 0.0), alpha=ALPHA, engage_rho=ENGAGE)
    rv = qp.filter_twist_xy((0.0, 0.04, 0.0), (0.0, 0.022), alpha=ALPHA, engage_rho=ENGAGE)
    assert ru.fired and rv.fired
    assert ru.v_xyz[0] == pytest.approx(rv.v_xyz[1])
    assert ru.v_xyz[1] == pytest.approx(0.0)
    assert rv.v_xyz[0] == pytest.approx(0.0)


def test_outside_marked_infeasible_no_invented_safe_v(circle_qp: TubeCbfQp) -> None:
    v_nom = (0.01, 0.0, 0.002)
    result = circle_qp.filter_twist_xy(
        v_nom, (0.040, 0.0), alpha=ALPHA, engage_rho=ENGAGE
    )
    assert result.h < 0.0
    assert result.phi > 0.0
    assert result.infeasible is True
    assert result.fired is False
    assert result.v_safe_xyz is None
    # Nominal is echoed for telemetry only; not claimed safe.
    assert result.v_xyz == pytest.approx(v_nom)


def test_module_filter_accepts_external_phi_grad() -> None:
    a = 0.035
    b = 0.030

    def phi_fn(u: float, v: float) -> float:
        return PathEllipse(a, b).phi(u, v)

    def grad_fn(u: float, v: float) -> tuple[float, float]:
        return PathEllipse(a, b).grad_phi(u, v)

    def rho_fn(u: float, v: float) -> float:
        return PathEllipse(a, b).rho(u, v)

    result = filter_twist_xy(
        (0.0, 0.05, 0.001),
        (0.0, 0.024),
        phi_fn=phi_fn,
        grad_fn=grad_fn,
        rho_fn=rho_fn,
        alpha=ALPHA,
        engage_rho=ENGAGE,
    )
    assert result.fired is True
    assert result.v_xyz[1] < 0.05
    assert result.v_xyz[2] == pytest.approx(0.001)
    assert result.infeasible is False


def test_external_phi_grad_without_rho_fn_raises() -> None:
    ellipse = PathEllipse(R_M, R_M)

    with pytest.raises(ValueError, match="rho_fn is required"):
        TubeCbfQp.from_callables(ellipse.phi, ellipse.grad_phi)

    with pytest.raises(ValueError, match="rho_fn is required"):
        filter_twist_xy(
            (0.05, 0.0, 0.0),
            (0.022, 0.0),
            phi_fn=ellipse.phi,
            grad_fn=ellipse.grad_phi,
            alpha=ALPHA,
            engage_rho=ENGAGE,
        )

    # Bare construction without rho_fn must fail closed at filter time.
    bare = TubeCbfQp(phi_fn=ellipse.phi, grad_fn=ellipse.grad_phi, rho_fn=None)
    with pytest.raises(ValueError, match="rho_fn is required"):
        bare.filter_twist_xy((0.05, 0.0, 0.0), (0.022, 0.0), alpha=ALPHA, engage_rho=ENGAGE)


def test_anisotropic_ellipse_near_short_axis() -> None:
    qp = TubeCbfQp.from_semi_axes(0.040, 0.030)
    # Near short (v) wall: rho = 0.024/0.030 = 0.8 > engage.
    result = qp.filter_twist_xy(
        (0.0, 0.05, 0.0), (0.0, 0.024), alpha=ALPHA, engage_rho=ENGAGE
    )
    assert result.engaged is True
    assert result.fired is True
    assert result.v_xyz[1] < 0.05


def test_inward_velocity_near_wall_not_fired(circle_qp: TubeCbfQp) -> None:
    result = circle_qp.filter_twist_xy(
        (-0.05, 0.0, 0.0), (0.022, 0.0), alpha=ALPHA, engage_rho=ENGAGE
    )
    assert result.engaged is True
    assert result.fired is False
    assert result.infeasible is False
    assert result.v_xyz[0] == pytest.approx(-0.05)


def test_microbench_p95_much_less_than_1ms() -> None:
    stats = microbench_filter_twist_xy(n=1000, a_m=R_M, b_m=R_M)
    # 100 Hz budget is 10 ms; analytic projection should be ≪ 1 ms p95.
    assert stats["p95_s"] < 1e-3
    assert stats["p99_s"] < 2e-3
