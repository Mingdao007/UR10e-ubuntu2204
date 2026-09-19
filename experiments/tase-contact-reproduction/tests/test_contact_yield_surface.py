"""Vary evaluator-owned curvature without changing the control task or leaking truth."""
import numpy as np
import pytest
from contact_yield_runner import make_system,run_closed_loop
from contact_yield_replay import replay_artifact
from contact_yield_simulator import OBSERVATION_KEYS
from test_contact_yield import library


def test_curvature_changes_plant_only_and_preserves_start_contact(library):
    args=dict(method='DSFC',material='stiff_low_mu',dt_s=.002,timeline='full_cycle',qp_library=library)
    ctrl,plant,origin=make_system(**args)
    changed=None
    try:
        changed,other,new_origin=make_system(**args,surface_parameters={'kappa_xx':6.,'kappa_yy':8.})
        assert ctrl.identity==changed.identity
        assert ctrl.snapshot()==changed.snapshot()
        assert plant.identity!=other.identity
        np.testing.assert_array_equal(origin,new_origin)
        assert plant.surface.gap_m(origin)==other.surface.gap_m(origin)
        np.testing.assert_array_equal(plant.surface.true_outward_normal(origin),other.surface.true_outward_normal(origin))
        offset=origin+np.array([.04,.01,0.])
        assert abs(plant.surface.height(*offset[:2])-other.surface.height(*offset[:2]))>.004
        assert np.arccos(plant.surface.true_outward_normal(offset)@other.surface.true_outward_normal(offset))>.15
        for model in (plant,other):
            packed=model.observe(path_time_s=0.,scenario='nominal',reference_velocity_m_s=[.004,.002,0.])
            assert set(packed['observation'])==OBSERVATION_KEYS
            assert 'true_inward_normal_base' in packed['evaluator']
    finally:
        ctrl.close()
        if changed is not None:changed.close()


@pytest.mark.parametrize('parameters',[{'origin_m':[0,0,0]},{'kappa_xx':float('nan')},{'kappa_xx':True}])
def test_surface_configuration_rejects_ambiguous_or_nonfinite_input(library,parameters):
    with pytest.raises(ValueError):
        make_system(method='DSFC',material='stiff_low_mu',dt_s=.002,timeline='diagnostic',
                    qp_library=library,surface_parameters=parameters)


def test_nondefault_surface_full_state_replay(library):
    parameters={'kappa_xx':6.,'kappa_yy':8.}
    result=run_closed_loop(method='DSFC',duration_s=.08,qp_library=library,surface_parameters=parameters)
    assert not result['metrics']['failed']
    assert result['surface_parameters']==parameters
    assert result['plant_identity_payload']['surface']['kappa_xx']==6.
    assert replay_artifact(result)['passed']
