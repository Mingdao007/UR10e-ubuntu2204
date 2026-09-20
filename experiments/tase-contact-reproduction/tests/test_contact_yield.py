"""Mechanism and evidence regressions; calibrated offline model, no device I/O."""
import copy
import numpy as np
import pytest
from contact_yield_controller import YieldControllerError
from contact_yield_runner import make_system,run_closed_loop,serial
from contact_yield_replay import replay_artifact,refinement_error
from contact_yield_metrics import compare_pair
from contact_yield_laws import YieldLaw
from contact_yield_simulator import YieldSimulatorError
from contact_yield_qp import _kind
from contact_qp import QpError
from build_contact_qp import build

@pytest.fixture(scope='module')
def library(tmp_path_factory):return build(tmp_path_factory.mktemp('yield-qp'))

def system(method,library):return make_system(method=method,material='stiff_low_mu',dt_s=.002,
                                               timeline='diagnostic',qp_library=library)

def reference(origin,t=0.):return {'phase':'path','path_time_s':t,'force_n':5.,'position_m':origin,'velocity_m_s':(0,0,0)}

def test_baseline_does_not_admit_shear_as_tangent_speed(library):
    controller, plant, origin = system("SFC", library)
    try:
        obs = dict(plant.observe()["observation"])
        obs["raw_force_base_n"] = (4.0, 0.0, 5.0)
        shared = {"force_n": 5.0, "position_m": tuple(origin), "velocity_m_s": (0.0, 0.0, 0.0)}
        before = controller.snapshot()
        baseline = controller.step(obs, {"phase": "baseline", "path_time_s": None, **shared}, 0.002)
        controller.restore(before)
        path = controller.step(obs, {"phase": "path", "path_time_s": 0.0, **shared}, 0.002)
        assert abs(path["twist_base"][0]) > 1e-4
        assert abs(baseline["twist_base"][0]) < 0.2 * abs(path["twist_base"][0])
    finally:
        controller.close()


def test_normal_force_error_reaches_every_native_law_and_no_feedforward_damping(library):
    speeds=[]
    for method in ('SFC','DSFC','MSFC'):
        controller,plant,origin=system(method,library)
        try:
            obs=plant.observe()['observation'];obs['raw_force_base_n']=(0,0,6.)
            before=controller.snapshot();a=controller.step(obs,reference(origin),.002)
            assert np.linalg.norm(a['law_normal_velocity_m_s'])>0
            assert np.linalg.norm(controller.law.state)>0
            controller.restore(before)
            ref=reference(origin);ref['velocity_m_s']=(.002,0,0)
            b=controller.step(obs,ref,.002)
            np.testing.assert_allclose(a['law_state'],b['law_state'],rtol=0,atol=1e-14)
            speeds.append(tuple(a['law_normal_velocity_m_s']))
        finally:controller.close()
    assert len(set(speeds))==3


def test_atomic_restore_and_geometry_latency_are_independent(library):
    c,p,o=system('MSFC',library)
    try:
        before=c.snapshot();bad=copy.deepcopy(before);bad['offset_base_m'][0]=float('nan')
        with pytest.raises(YieldControllerError):c.restore(bad)
        assert c.snapshot()==before
        obs=p.observe()['observation'];obs['true_normal']=(0,0,1)
        with pytest.raises(YieldControllerError,match='truth'):c.step(obs,reference(o),.002)
        obs.pop('true_normal');obs['state_age_s']=.075
        with pytest.raises(YieldControllerError,match='geometric latency'):c.step(obs,reference(o),.002)
        assert c.snapshot()==before
        state=p.snapshot();bad=copy.deepcopy(state);bad['servo_integral'][0]=float('nan')
        with pytest.raises(YieldSimulatorError):p.restore(bad)
        assert p.snapshot()==state
    finally:c.close()

@pytest.mark.parametrize('method',['SFC','SFC_RADIAL','DSFC','MSFC'])
def test_closed_contact_fullstate_forward_replay_and_warm_memory(method,library):
    r=run_closed_loop(method=method,scenario='sustained_release_oblique',duration_s=.6,
                     preparation='warm' if method=='MSFC' else 'cold',qp_library=library)
    assert not r['metrics']['failed'],r['metrics']['failure_message']
    assert r['kinematics_kind']=='ur10e_calibrated_pinocchio'
    assert len(r['rows'])==300
    assert r['metrics']['contact_loss_duration_s']==0
    report=replay_artifact(r)
    assert report['passed'],report['mismatches'][:1]
    if method=='MSFC':
        assert r['preparation_protocol']['warm_duration_s']==2.
        snap=r['formal_initial_snapshot']['controller']['law22']['values']
        assert any(abs(x)>1e-8 for x in snap[7:19])


def test_radial_ablation_matches_native_axis_restriction():
    with YieldLaw('SFC') as native,YieldLaw('SFC_RADIAL') as radial:
        for i in range(100):
            force=(1. if i<50 else 0.,0.,0.)
            np.testing.assert_allclose(native.step(force,.002),radial.step(force,.002),rtol=0,atol=1e-14)


def test_qp_numerical_failures_are_not_relabelled_infeasible():
    assert _kind(QpError('OSQP did not solve accurately: status=3'))=='infeasible'
    for message in ('OSQP did not solve accurately: status=7','residual too large','QP deadline exceeded','nonfinite solution'):
        assert _kind(QpError(message))!='infeasible'


def test_metrics_peak_is_contact_load_and_recovery_is_post_release(library):
    nominal=run_closed_loop(method='DSFC',duration_s=.6,qp_library=library,record_fullstate=False)
    disturbed=copy.deepcopy(nominal);disturbed['scenario']='sustained_release_tangent'
    for row in disturbed['rows']:
        if .22<=row['time_s']<.46:row['position_m'][0]+=.004
    pair=compare_pair(nominal,disturbed)
    assert pair['release_s']==pytest.approx(.42)
    assert pair['recovery_s']==pytest.approx(.04)
    assert pair['yield_peak_m']==pytest.approx(.004)
    assert nominal['metrics']['force_peak_n']==max(x['true_normal_load_n'] for x in nominal['rows'])
    fine=run_closed_loop(method='DSFC',duration_s=.6,dt_s=.001,qp_library=library,record_fullstate=False)
    result=refinement_error(nominal['rows'],fine['rows'],coarse_dt_s=.002,fine_dt_s=.001)
    assert result['n_compared']==300
    assert result['position_max_m']<.001


def test_changing_contact_direction_moves_orientation_without_reset(library):
    c,p,o=system('MSFC',library)
    try:
        obs=p.observe()['observation']
        c.step(obs,reference(o),.002)
        initial=c.snapshot()
        obs['time_s']=.002
        obs['raw_force_base_n']=(5*np.sin(.2),0.,5*np.cos(.2))
        result=c.step(obs,reference(o,.002),.002)
        assert np.linalg.norm(result['twist_base'][3:])>0
        assert result['inward_normal_base']!=tuple(initial['normal_estimate']['inward_normal_base'])
        assert not result['memory_reset']
        retained=c.snapshot();obs['time_s']=.004
        with pytest.raises(YieldControllerError,match='freeze'):
            c.step(obs,{**reference(o),'phase':'baseline','path_time_s':None},.002)
        assert c.snapshot()==retained
    finally:c.close()


def test_refined_plant_identity_replays_full_state_and_rejects_different_grid(library):
    r=run_closed_loop(method='MSFC',scenario='sustained_release_tangent',duration_s=.6,
                     qp_library=library,plant_substeps=8)
    assert not r['metrics']['failed'],r['metrics']['failure_message']
    assert r['identity_payload']['settings']['compliance_stiffness_n_per_m']==0
    assert r['plant_identity_payload']['integration_substeps']==8
    assert replay_artifact(r)['passed']
    c,p,_=make_system(method='MSFC',material='stiff_low_mu',dt_s=.002,
                     timeline='diagnostic',qp_library=library,plant_substeps=4)
    try:
        with pytest.raises(YieldSimulatorError,match='identity'):
            p.restore(r['initial_simulator_snapshot'])
    finally:c.close()
