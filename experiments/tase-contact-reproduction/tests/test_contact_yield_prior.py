"""Estimator uncertainty must not rotate the physical contact experiment."""
import numpy as np
import pytest
from contact_yield_normal import NormalEstimator
from contact_yield_math import so3_exp
from contact_yield_runner import make_system, run_closed_loop
from contact_yield_replay import replay_artifact, differences
from test_contact_yield import library


def tilted(approach, angle_deg=10.):
    axis=np.cross(approach, [1.,0.,0.]);axis/=np.linalg.norm(axis)
    return so3_exp(axis*np.deg2rad(angle_deg)) @ approach


def test_estimator_initial_uncertainty_does_not_replace_approach():
    approach=np.array([0.,0.,-1.]);prior=tilted(approach)
    original=NormalEstimator(approach)
    uncertain=NormalEstimator(approach,initial_inward_normal_base=prior)
    np.testing.assert_array_equal(uncertain.approach, original.approach)
    np.testing.assert_allclose(uncertain.normal,prior,atol=1e-15)
    assert 'initial_inward_normal_base' not in original.parameters()
    assert uncertain.parameters()['initial_inward_normal_base']==prior.tolist()
    with pytest.raises(ValueError,match='unit'):
        NormalEstimator(approach,initial_inward_normal_base=[0,0,-2])
    # Full state restoration does not rewrite the declared initialization.
    uncertain.normal=approach.copy();state=uncertain.snapshot()
    fresh=NormalEstimator(approach,**uncertain.parameters());fresh.restore(state)
    assert fresh.snapshot()==state
    assert fresh.parameters()==uncertain.parameters()


def test_initial_prior_changes_no_plant_or_task_definition(library):
    args=dict(method='DSFC',material='stiff_low_mu',dt_s=.002,timeline='full_cycle',
              qp_library=library,plant_substeps=8)
    base,plant,origin=make_system(**args)
    alternative=None
    try:
        prior=tilted(base.estimator.approach)
        alternative,other_plant,other_origin=make_system(**args,
            estimator_parameters={'initial_inward_normal_base':prior.tolist()})
        assert base.identity!=alternative.identity
        assert not differences(plant.snapshot(),other_plant.snapshot())
        assert plant.identity_payload==other_plant.identity_payload
        np.testing.assert_array_equal(origin,other_origin)
        np.testing.assert_array_equal(base.approach_inward_base,alternative.approach_inward_base)
        for t in (0.,20.,40.,62.8):assert base.task.reference(t)==alternative.task.reference(t)
        first=base.snapshot();second=alternative.snapshot()
        assert first['normal_estimate']['approach_inward_base']==second['normal_estimate']['approach_inward_base']
        for key in first:
            if key not in ('identity','normal_estimate'):assert first[key]==second[key]
    finally:
        base.close()
        if alternative is not None:alternative.close()


def test_tilted_prior_full_state_forward_replay(library):
    base,_,_=make_system(method='DSFC',material='stiff_low_mu',dt_s=.002,
        timeline='diagnostic',qp_library=library)
    try:prior=tilted(base.estimator.approach)
    finally:base.close()
    run=run_closed_loop(method='DSFC',duration_s=.08,qp_library=library,
        estimator_parameters={'initial_inward_normal_base':prior.tolist(), 'force_correction_gain':0.})
    assert not run['metrics']['failed']
    assert replay_artifact(run)['passed']
    np.testing.assert_allclose(run['initial_controller_snapshot']['normal_estimate']['inward_normal_base'],prior)


def test_explicit_frozen_observer_preserves_prior_with_excitation_and_contact():
    approach=np.array([0.,0.,-1.]);prior=tilted(approach)
    estimator=NormalEstimator(approach,initial_inward_normal_base=prior,
        motion_gain=0.,force_correction_gain=0.)
    before=estimator.snapshot()
    for velocity in ([.01,0.,.004],[0.,.01,-.004]):
        result=estimator.update(dt_s=.002,measured_linear_velocity_base_m_s=velocity,
            measured_force_base_n=[.3,.1,5.],in_contact=True)
        assert result['contact_gate'] and result['excitation_gate']
        assert not result['motion_update_applied']
        assert not result['force_bias_correction_applied']
        assert estimator.snapshot()==before
    with pytest.raises(ValueError,match='nonnegative'):
        NormalEstimator(approach,motion_gain=-1.)
