"""Focused offline contract tests for TASE normal/SFC tangent fusion."""

from pathlib import Path
import sys

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from tase_sfc_fusion import (  # noqa: E402
    CLAMP_AUTHORITY_LIMIT_N,
    CLAMP_POLICY_ID,
    CLAMP_STATE_LIMIT_N_S,
    COMPOSITION_ID,
    ConditionalDoubleClamp,
    FinalBoundedJointVelocityQP,
    FusionError,
    RESET_BOUNDARIES,
    TaseSfcFusion,
    TransportedTangentBasis,
    normal_tangent_projectors,
)


def test_projectors_are_orthogonal_and_basis_stays_continuous() -> None:
    projectors = normal_tangent_projectors((1.0, 2.0, 3.0))
    np.testing.assert_allclose(projectors.Pn @ projectors.Pn, projectors.Pn, atol=1e-12)
    np.testing.assert_allclose(projectors.Pt @ projectors.Pt, projectors.Pt, atol=1e-12)
    np.testing.assert_allclose(projectors.Pn @ projectors.Pt, np.zeros((3, 3)), atol=1e-12)
    np.testing.assert_allclose(projectors.Pt @ projectors.normal, np.zeros(3), atol=1e-12)

    basis = TransportedTangentBasis((0.0, 0.0, 1.0))
    previous = basis.basis
    for normal in ((0.02, 0.0, 0.9998), (0.04, 0.01, 0.9991), (0.04, 0.03, 0.9985)):
        current = basis.update(normal)
        unit = np.asarray(normal, dtype=float)
        unit /= np.linalg.norm(unit)
        np.testing.assert_allclose(current.T @ current, np.eye(2), atol=1e-10)
        np.testing.assert_allclose(current.T @ unit, np.zeros(2), atol=1e-10)
        # Transport must not arbitrarily flip either tangent vector between
        # adjacent online normal estimates.
        assert np.all(np.diag(previous.T @ current) > 0.99)
        previous = current


def test_fusion_zeroes_tase_tangent_and_keeps_sfc_outside_path_disabled() -> None:
    fusion = TaseSfcFusion((0.0, 0.0, 1.0))
    tase = np.array((0.3, -0.4, 0.2, 0.1, -0.2, 0.3))
    sfc = np.array((0.6, 0.7, 0.8, 4.0, 5.0, 6.0))

    outside = fusion.fuse(tase, sfc, (0.0, 0.0, 1.0), phase="qualification")
    assert outside.sfc_enabled is False
    np.testing.assert_allclose(outside.twist[:3], (0.0, 0.0, 0.2), atol=1e-12)
    assert outside.diagnostics["tase_tangent_zeroed"] is True

    inside = fusion.fuse(tase, sfc, (0.0, 0.0, 1.0), phase="PATH")
    assert inside.sfc_enabled is True
    np.testing.assert_allclose(inside.twist[:3], (0.6, 0.7, 0.2), atol=1e-12)
    np.testing.assert_allclose(inside.twist[3:], tase[3:], atol=1e-12)
    # The SFC tangential wrench/output cannot alter the TASE normal component.
    altered_sfc = fusion.fuse(tase, (60.0, -70.0, 800.0, 40.0, 50.0, 60.0), (0.0, 0.0, 1.0), phase="PATH")
    assert altered_sfc.twist[2] == pytest.approx(inside.twist[2])

    fusion.update_sfc_state((1.0, -2.0))
    fusion.set_phase("unload")
    assert fusion.sfc_enabled is False
    np.testing.assert_allclose(fusion.sfc_state, np.zeros(2))
    assert "mode_exit" in fusion.reset_events


def test_conditional_double_clamp_freezes_unwinds_and_reports_resets() -> None:
    clamp = ConditionalDoubleClamp(kp=4.0, ki=1.0)
    assert CLAMP_POLICY_ID == "conditional-double-clamp-v1"
    assert CLAMP_STATE_LIMIT_N_S == 1.0
    assert CLAMP_AUTHORITY_LIMIT_N == 0.5
    assert clamp.effective_limit() == pytest.approx(1.0)
    assert clamp.effective_limit(0.1) == pytest.approx(0.4)

    first = clamp.update(1.0, 0.2)
    assert first.state_n_s == pytest.approx(0.2)
    frozen = clamp.update(1.0, 0.2, saturation_sources={"qp": True})
    assert frozen.frozen is True
    assert frozen.freeze_reason == "qp_same_direction"
    assert frozen.downstream_saturation_source == "qp"
    assert frozen.state_n_s == pytest.approx(first.state_n_s)

    # An opposite-sign error is allowed to unwind even when a downstream
    # saturation is present in the opposite direction.
    unwound = clamp.update(-1.0, 0.1, saturation_sources={"slew": 1.0})
    assert unwound.unwind is True
    assert unwound.state_n_s < first.state_n_s
    assert clamp.receipt_diagnostics()["unwind_events"] == 1

    for boundary in RESET_BOUNDARIES:
        clamp.update(0.4, 0.01, reset_boundary=boundary)
        assert clamp.state_n_s == pytest.approx(0.0)
    receipt = clamp.receipt_diagnostics()
    assert receipt["state_limit_n_s"] == pytest.approx(1.0)
    assert receipt["authority_limit_n"] == pytest.approx(0.5)
    assert receipt["effective_limit_n_s"] == pytest.approx(1.0)
    assert receipt["no_back_calculation"] is True

    with pytest.raises(FusionError, match="clamp error must be finite"):
        clamp.update(float("nan"), 0.01)
    assert clamp.state_n_s == pytest.approx(0.0)
    assert clamp.receipt_diagnostics()["reset_events"][-1] == "invalid_state"


def test_final_qp_has_normal_priority_and_enforces_qdot_and_slew_bounds() -> None:
    qp = FinalBoundedJointVelocityQP()
    result = qp.realize(
        np.eye(6),
        normal=(0.0, 0.0, 1.0),
        normal_twist=(0.0, 0.0, 0.04, 0.0, 0.0, 0.0),
        tangent_twist=(0.8, 0.0, 0.0, 0.0, 0.0, 0.0),
        previous_qdot=np.zeros(6),
        qdot_lower=-0.2 * np.ones(6),
        qdot_upper=0.2 * np.ones(6),
        slew_limit=0.04,
    )
    qdot = np.asarray(result.qdot_rad_s)
    assert qdot[2] == pytest.approx(0.04, abs=1e-8)
    assert qdot[0] == pytest.approx(0.04, abs=1e-8)
    assert np.all(qdot <= 0.2 + 1e-12)
    assert np.all(qdot >= -0.2 - 1e-12)
    assert np.all(np.abs(qdot) <= 0.04 + 1e-12)
    assert result.normal_preserved is True
    assert result.tangent_degraded is True
    assert result.diagnostics["single_realization_interface"] is True
    assert result.diagnostics["normal_priority"] is True


def test_fusion_and_clamp_snapshot_replay_is_deterministic() -> None:
    fusion = TaseSfcFusion((0.0, 0.0, 1.0))
    clamp = ConditionalDoubleClamp()
    normals = ((0.0, 0.0, 1.0), (0.01, 0.0, 0.9999), (0.02, 0.01, 0.9997))
    snapshots = []
    outputs = []
    for index, normal in enumerate(normals):
        phase = "PATH" if index else "qualification"
        outputs.append(fusion.fuse((0.0, 0.0, 0.05, 0.0, 0.0, 0.0), (0.02, 0.01, 0.2, 0, 0, 0), normal, phase=phase))
        clamp.update(0.2 - 0.1 * index, 0.01)
        snapshots.append((fusion.snapshot(), clamp.snapshot()))

    fusion.restore(snapshots[1][0])
    clamp.restore(snapshots[1][1])
    replay_fusion = fusion.fuse((0.0, 0.0, 0.05, 0.0, 0.0, 0.0), (0.02, 0.01, 0.2, 0, 0, 0), normals[2], phase="PATH")
    replay_clamp = clamp.update(0.0, 0.01)
    assert replay_fusion == fusion.fuse((0.0, 0.0, 0.05, 0.0, 0.0, 0.0), (0.02, 0.01, 0.2, 0, 0, 0), normals[2], phase="PATH")
    assert replay_clamp.state_n_s == pytest.approx(clamp.state_n_s)
    assert replay_clamp.diagnostics == clamp._last.diagnostics  # noqa: SLF001
    assert COMPOSITION_ID == "TASE_RNN_MATURE+SFC_TANGENTIAL"


def test_normal_jump_freezes_sfc_until_explicit_mode_reset() -> None:
    fusion = TaseSfcFusion((0.0, 0.0, 1.0))
    fusion.fuse(
        (0.0, 0.0, 0.05, 0.0, 0.0, 0.0),
        (0.01, 0.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 1.0),
        phase="PATH",
    )
    jumped = fusion.fuse(
        (0.0, 0.0, 0.05, 0.0, 0.0, 0.0),
        (0.01, 0.0, 0.0, 0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        phase="PATH",
    )
    assert jumped.sfc_enabled is False
    assert jumped.diagnostics["normal_jump"] is True
    assert "normal_jump" in fusion.reset_events
    np.testing.assert_allclose(fusion.sfc_state, np.zeros(2))

    # A mode transition is the explicit reset boundary that permits a fresh
    # tangent basis/control state to be admitted.
    fusion.set_phase("qualification")
    resumed = fusion.fuse(
        (0.0, 0.0, 0.05, 0.0, 0.0, 0.0),
        (0.01, 0.0, 0.0, 0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        phase="PATH",
    )
    assert resumed.sfc_enabled is True


def test_invalid_fusion_frame_resets_sfc_state() -> None:
    fusion = TaseSfcFusion((0.0, 0.0, 1.0))
    fusion.fuse((0.0, 0.0, 0.05, 0.0, 0.0, 0.0), (0.01, 0.0, 0.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0), phase="PATH")
    with pytest.raises(FusionError, match="finite"):
        fusion.fuse((0.0, 0.0, float("nan"), 0.0, 0.0, 0.0), (0.0,) * 6, (0.0, 0.0, 1.0), phase="PATH")
    assert fusion.sfc_enabled is False
    assert "invalid_state" in fusion.reset_events
    np.testing.assert_allclose(fusion.sfc_state, np.zeros(2))


def test_final_qp_fails_closed_when_normal_is_unrealizable() -> None:
    qp = FinalBoundedJointVelocityQP()
    with pytest.raises(FusionError, match="preserve the TASE normal"):
        qp.realize(
            np.zeros((6, 1)),
            normal=(0.0, 0.0, 1.0),
            normal_twist=(0.0, 0.0, 0.04, 0.0, 0.0, 0.0),
            tangent_twist=(0.0,) * 6,
            previous_qdot=np.zeros(1),
            qdot_lower=np.zeros(1),
            qdot_upper=np.zeros(1),
            slew_limit=0.04,
        )
