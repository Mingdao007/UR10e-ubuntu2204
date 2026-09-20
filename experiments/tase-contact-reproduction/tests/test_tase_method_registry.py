"""Offline registry integration for independently selectable TASE adapters."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_contact_qp import build  # noqa: E402
from contact_method_registry import RegistryError, default_registry  # noqa: E402
from contact_yield_kinematics import load_kinematics  # noqa: E402
from contact_yield_method_registry import (  # noqa: E402
    MethodUnavailableError,
    load_method_records,
    load_offline_method_records,
    resolve_method,
)


@pytest.fixture(scope="module")
def qp_library(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("tase-registry-qp"))


@pytest.fixture(scope="module")
def ur10e_kinematics():
    return load_kinematics(require_ur10e=True)


def observation(kinematics, q: np.ndarray, time_s: float) -> dict[str, object]:
    row = kinematics.pose_and_jacobian(q)
    rotation = np.asarray(row["rotation"], dtype=float)
    reaction_normal = -rotation[:, 2]
    force_base = 5.0 * reaction_normal
    return {
        "time_s": time_s,
        "state_age_s": 0.001,
        "position_m": row["position_m"],
        "rotation": rotation,
        "joint_position_rad": tuple(float(value) for value in q),
        "jacobian": row["jacobian"],
        "raw_force_base_n": tuple(float(value) for value in force_base),
        "raw_torque_base_nm": (0.0, 0.0, 0.0),
        "joint_velocity_lower": (-0.15,) * 6,
        "joint_velocity_upper": (0.15,) * 6,
        "linear_velocity_base_m_s": (0.0, 0.0, 0.0),
        "angular_velocity_base_rad_s": (0.0, 0.0, 0.0),
    }


def reference(observation_row: dict[str, object], *, force_n: float = 5.0) -> dict[str, object]:
    position = np.asarray(observation_row["position_m"], dtype=float)
    return {
        "position_m": tuple(float(value) for value in position + np.array((0.0002, -0.0001, 0.0))),
        "velocity_m_s": (0.0002, -0.0001, 0.0),
        "reference_force_n": force_n,
        "phase": "path",
        "path_time_s": 0.0,
    }


def test_live_registry_stays_unavailable_while_offline_discovery_is_explicit() -> None:
    live_records = load_method_records()
    assert live_records["TASE_RNN"].available is False
    assert live_records["TASE_QP"].available is False
    with pytest.raises(MethodUnavailableError):
        resolve_method("TASE_RNN")
    with pytest.raises(MethodUnavailableError):
        resolve_method("TASE_QP")

    offline = load_offline_method_records()
    assert set(offline) == {
        "TASE_RNN",
        "TASE_RNN_MATURE_MINUS",
        "TASE_QP",
        "TASE_IMPROVED",
    }
    assert all(record.offline_only and not record.live_eligible for record in offline.values())
    assert offline["TASE_RNN"].variant == "printed_eq23_plus"
    assert offline["TASE_RNN_MATURE_MINUS"].variant == "mature_minus"
    assert offline["TASE_QP"].variant == "matched_outer_qp"
    assert offline["TASE_IMPROVED"].variant == (
        "local-normal-gated-leaky-normal-priority-slack-qp"
    )


def test_default_registry_initializes_each_advertised_offline_name(qp_library: Path) -> None:
    registry = default_registry()
    expected = {
        "TASE_RNN": ("rnn", "printed_eq23_plus", "plus"),
        "TASE_RNN_MATURE_MINUS": ("rnn", "mature_minus", "minus"),
        "TASE_QP": ("qp", "matched_outer_qp", None),
    }
    for name, (solver, variant, sign) in expected.items():
        handle = registry.initialize(name, qp_library=qp_library)
        assert handle.spec.implementation == "TaseOfflineMethodAdapter"
        assert handle.spec.qualification == "software_only"
        assert handle.backend.solver_name == solver
        assert handle.backend.variant == variant
        assert handle.backend.live_eligible is False
        if sign is not None:
            assert handle.backend.config.lambda_update_sign == sign
    improved = registry.initialize("TASE_IMPROVED", qp_library=qp_library)
    assert improved.spec.implementation == "TaseImprovedOfflineMethodAdapter"
    assert improved.spec.qualification == "software_only"
    assert improved.backend.variant == "local-normal-gated-leaky-normal-priority-slack-qp"
    assert improved.backend.live_eligible is False


def test_rnn_sign_metadata_matches_the_selected_equation_variant(
    qp_library: Path, ur10e_kinematics
) -> None:
    q = np.asarray((0.2, -1.1, 1.0, -1.5, 0.7, 0.3), dtype=float)
    obs = observation(ur10e_kinematics, q, 0.0)
    ref = reference(obs)
    registry = default_registry()
    printed = registry.initialize("TASE_RNN", qp_library=qp_library)
    mature = registry.initialize("TASE_RNN_MATURE_MINUS", qp_library=qp_library)
    printed_result = printed.step(obs, ref, 0.002)
    mature_result = mature.step(obs, ref, 0.002)
    assert printed_result["diagnostics"]["solver"]["proj_input_form"] == "J.T @ lambda_state"
    assert printed_result["diagnostics"]["solver"]["lambda_update_form"].startswith("lambda_state +=")
    assert mature_result["diagnostics"]["solver"]["lambda_update_form"].startswith("lambda_state -=")


def test_independent_adapters_use_real_ur10e_j_and_replay_private_state(
    qp_library: Path, ur10e_kinematics
) -> None:
    q = np.asarray((0.2, -1.1, 1.0, -1.5, 0.7, 0.3), dtype=float)
    obs0 = observation(ur10e_kinematics, q, 0.0)
    obs1 = observation(ur10e_kinematics, q, 0.002)
    ref0 = reference(obs0)
    ref1 = reference(obs1, force_n=4.9)
    assert not np.allclose(np.asarray(obs0["jacobian"]), np.eye(6))

    registry = default_registry()
    rnn = registry.initialize("TASE_RNN", qp_library=qp_library)
    qp = registry.initialize("TASE_QP", qp_library=qp_library)
    rnn_initial = rnn.snapshot()
    qp_initial = qp.snapshot()

    first_rnn = rnn.step(obs0, ref0, 0.002)
    assert first_rnn["diagnostics"]["offline_only"] is True
    assert first_rnn["diagnostics"]["live_eligible"] is False
    assert rnn.snapshot() != rnn_initial
    assert qp.snapshot() == qp_initial

    first_qp = qp.step(obs0, ref0, 0.002)
    assert first_qp["diagnostics"]["offline_only"] is True
    assert qp.snapshot() != qp_initial
    rnn_after_first = rnn.snapshot()

    rnn_replay_state = rnn.snapshot()
    expected_rnn = rnn.step(obs1, ref1, 0.002)
    rnn.restore(rnn_replay_state)
    replayed_rnn = rnn.step(obs1, ref1, 0.002)
    assert expected_rnn == replayed_rnn
    assert rnn.snapshot() != rnn_after_first

    qp_replay_state = qp.snapshot()
    expected_qp = qp.step(obs1, ref1, 0.002)
    qp.restore(qp_replay_state)
    replayed_qp = qp.step(obs1, ref1, 0.002)
    assert expected_qp == replayed_qp

    rnn.stop()
    assert rnn.backend.stopped is True
    with pytest.raises(RegistryError, match="stopped"):
        rnn.step(obs1, ref1, 0.002)
