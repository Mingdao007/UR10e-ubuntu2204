from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from build_contact_qp import build
from contact_qp import NativeContactQp, QpError


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    return build(tmp_path_factory.mktemp("contact-qp"))


def test_native_dense_changing_jacobian_matches_direct_solve(library):
    solver = NativeContactQp(library, deadline_s=None)
    rng = np.random.default_rng(17)
    for _ in range(40):
        J = np.eye(6) + 0.15*rng.normal(size=(6, 6))
        expected = rng.uniform(-0.04, 0.04, 6)
        result = solver.solve(J, J @ expected, [-0.05]*6, [0.05]*6)
        np.testing.assert_allclose(result.qdot, np.linalg.solve(J, J @ expected), atol=1e-6)


def test_rank_deficient_feasible_problem_minimum_norm(library):
    solver = NativeContactQp(library, deadline_s=None)
    J = np.diag([1., 1., 1., 0., 0., 0.])
    result = solver.solve(J, [0.01, -0.02, 0.03, 0, 0, 0], [-0.05]*6, [0.05]*6)
    np.testing.assert_allclose(result.qdot, [0.01, -0.02, 0.03, 0, 0, 0], atol=1e-6)


def test_infeasible_and_invalid_never_update_warm_state(library):
    solver = NativeContactQp(library, deadline_s=None)
    before = solver.snapshot()
    with pytest.raises(QpError):
        solver.solve(np.eye(6), [0.2]*6, [-0.05]*6, [0.05]*6)
    assert solver.snapshot() == before
    with pytest.raises(QpError):
        solver.solve(np.eye(6), [float("nan")]*6, [-0.05]*6, [0.05]*6)
    with pytest.raises(QpError):
        solver.solve(np.eye(6), [0]*6, [0.1]*6, [0.05]*6)


def test_instances_and_full_state_restore(library):
    a = NativeContactQp(library, deadline_s=None)
    b = NativeContactQp(library, deadline_s=None)
    a.solve(np.eye(6), [0.01]*6, [-0.05]*6, [0.05]*6)
    snapshot = a.snapshot()
    b.solve(np.eye(6), [-0.02]*6, [-0.05]*6, [0.05]*6)
    assert a.snapshot() == snapshot
    a.reset()
    a.restore(snapshot)
    assert a.snapshot() == snapshot
    np.testing.assert_allclose(a.solve(np.eye(6), [0.01]*6, [-0.05]*6, [0.05]*6).qdot, 0.01, atol=1e-6)


def test_expired_deadline_rejects_even_solved_command(library):
    solver = NativeContactQp(library, deadline_s=1e-12)
    with pytest.raises(QpError, match="deadline"):
        solver.solve(np.eye(6), [0.01]*6, [-0.05]*6, [0.05]*6)
    assert solver.snapshot() == {"x": [0.]*6, "y": [0.]*12}
