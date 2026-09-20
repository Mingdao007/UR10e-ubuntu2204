from pathlib import Path
import numpy as np
import pytest
from contact_yield_path_guard import YieldPathGuard, YieldPathGuardError
from contact_yield_qp import YieldQp


def context(error=(0., 0., 0.), basis=None, previous=(0.,0.,0.)):
    basis = np.eye(3) if basis is None else basis
    return dict(basis=basis, error_base_m=basis@error,
                reference_velocity_base=np.zeros(3), previous_velocity_base=basis@previous,
                state_age_s=.001)


def test_hard_guard_precedes_soft_and_stale_rejects():
    with pytest.raises(YieldPathGuardError, match='hard ellipse'):
        YieldPathGuard(context((.025,0,0)))
    stale=context(); stale['state_age_s']=.020001
    with pytest.raises(YieldPathGuardError, match='stale'):
        YieldPathGuard(stale)


def test_interior_exact_identity_and_rotated_boundary_projection():
    nominal=np.array([.01,.002,.003,.04,.01,.02])
    np.testing.assert_array_equal(YieldPathGuard(context()).project(nominal),nominal)
    basis=np.array([[0.,-1.,0.],[1.,0.,0.],[0.,0.,1.]])
    guard=YieldPathGuard(context((.0219,0,0),basis,previous=(.002,0,0)))
    nominal[:3]=basis@np.array([.003,0,.001])
    result=guard.project(nominal)
    local=basis.T@result[:3]
    assert local[0] < .001
    assert local[2] == pytest.approx(.001)
    np.testing.assert_array_equal(result[3:],nominal[3:])
    guard.validate(result)
    with pytest.raises(YieldPathGuardError, match='violates'):
        guard.validate(nominal)


def test_native_joint_solution_carries_guard_and_no_post_qp_change():
    qp=YieldQp(Path('build/contact-qp/libcontact_qp.so'),deadline_s=None)
    result=qp.compose(np.eye(6),[.003,0,0,0,0,0],[-.05]*6,[.05]*6,
                      [0,0,1],continuous_scaling=True,
                      path_guard_context=context((.0219,0,0),previous=(.002,0,0)))
    assert result['path_guard']['intervention']
    assert result['qdot_rad_s'][0] < .001
    np.testing.assert_allclose(result['qdot_rad_s'],result['applied_twist_base'],atol=1e-8)
    assert result['path_guard']['soft_axes_m'] == pytest.approx([.022,.012])
