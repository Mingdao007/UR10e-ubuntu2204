from pathlib import Path
from types import SimpleNamespace
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from build_contact_qp import build
from contact_qp import QpError, QpSolverProfile
import step5d_autotune_v4_r004.calibrated_runtime as calibrated


@pytest.fixture(scope="module")
def profile(tmp_path_factory):
    # Generous deadline only for deterministic offline behavior tests.
    return QpSolverProfile(build(tmp_path_factory.mktemp("runtime-qp")), deadline_s=1.)


@pytest.fixture
def runtime(monkeypatch, profile):
    monkeypatch.setattr(calibrated, "observed_model_hashes", lambda _: {})
    monkeypatch.setattr(calibrated, "build_calibrated_model", lambda: SimpleNamespace(
        model=SimpleNamespace(lowerPositionLimit=np.full(6, -6.), upperPositionLimit=np.full(6, 6.))))
    monkeypatch.setattr(calibrated, "tcp_jacobian_base", lambda *args: np.eye(6))
    def forbidden(*args, **kwargs):
        raise AssertionError("QP route must not construct an RNN")
    monkeypatch.setattr(calibrated, "StrictTaseRnnSolver", forbidden)
    return calibrated.V4CalibratedRuntime(object(), object(), solver_profile=profile)


def command(runtime, mode="path", value=.01):
    return runtime.command(actual_q=[0]*6, actual_qd=[0]*6,
        actual_tcp_pose=[0]*6, desired_twist=[value]*6,
        actual_dt_s=.002, mode=mode, path_time_s=1.)


def test_actual_runtime_selects_native_qp_and_records_backend(runtime):
    result = command(runtime)
    np.testing.assert_allclose(result.qdot, .01, atol=1e-6)
    assert result.solver_status == 40.
    assert runtime.last_solver_diagnostics["backend"] == "osqp-codegen-c"
    assert runtime.last_solver_diagnostics["equality_residual"] < 1e-6


def test_runtime_snapshot_restores_all_qp_duals_and_stop_clears_command(runtime):
    command(runtime)
    state = runtime.dynamic_state_snapshot()
    assert len(state["solver_state"]["y"]) == 12
    assert command(runtime, mode="stop").qdot == (0.,)*6
    runtime.restore_dynamic_state(state)
    assert runtime.dynamic_state_snapshot() == state


def test_runtime_infeasible_command_does_not_fallback(runtime):
    with pytest.raises(QpError):
        command(runtime, value=.2)


def test_r013_wrapper_accepts_explicit_qp_profile(monkeypatch, profile):
    import step5d_autotune_v4_r013.live_runtime as r013
    monkeypatch.setattr(r013, "_ACTIVE_SOLVER_PROFILE", profile)
    class Base:
        def __init__(self, *, solver_profile, **kwargs):
            self.received = solver_profile
    wrapped = r013._make_runtime_class(Base)()
    assert wrapped.received is profile
    assert wrapped._r013_solver_profile.as_dict()["backend"] == "osqp-codegen-c"
