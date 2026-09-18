from pathlib import Path
import sys
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from contact_benchmark_outer import ContactOuterLoop
R=np.diag([1.,-1.,-1.])

def make(law=None):
    return ContactOuterLoop(law_step=law or (lambda f,dt:f*.0001),anchor_m=[0,0,0],
        task_basis=np.eye(3),target_rotation=R,raw_force_limit_n=20.,raw_torque_limit_nm=2.)

def tick(o, force=(0,0,0), **kwargs):
    return o.step(time_s=0.,dt_s=.002,position_m=[0,0,0],rotation=R,
        raw_force_base_n=force,raw_torque_base_nm=[0,0,0],**kwargs)

def test_force_sign_and_same_reference_no_raw_posture():
    assert tick(make())['twist_base'][2] < 0
    assert tick(make(),(0,0,10))['twist_base'][2] > 0
    assert tick(make(),(0,0,5))['twist_base'][2] == 0
    assert tick(make(),(4,0,5))['twist_base'][3:] == (0,0,0)

def test_raw_guard_cannot_be_canceled_by_injection():
    o=make(lambda f,dt: (_ for _ in ()).throw(AssertionError('law must not run')))
    with pytest.raises(ValueError,match='raw sensor guard'):
        tick(o,(0,0,21),injection_task_n=[0,0,-21])
    assert o.snapshot()['initialized'] is False

def test_orientation_error_closes_in_base_frame():
    import pinocchio as pin
    o=make(); perturb=np.array([.02,-.03,.01]); observed=pin.exp3(perturb)@R
    out=o.step(time_s=0.,dt_s=.002,position_m=[0,0,0],rotation=observed,
               raw_force_base_n=[0,0,5],raw_torque_base_nm=[0,0,0])
    assert np.dot(out['twist_base'][3:],perturb)<0

def test_hard_guard_precedes_law_and_clock_no_reset():
    o=make();tick(o,(0,0,5))
    with pytest.raises(ValueError,match='clock'):tick(o,(0,0,5))
    with pytest.raises(ValueError,match='hard PATH'):
        o.step(time_s=.002,dt_s=.002,position_m=[.026,0,0],rotation=R,
               raw_force_base_n=[0,0,5],raw_torque_base_nm=[0,0,0])

def test_frame_semantics_rejects_tool_pointing_away():
    with pytest.raises(ValueError,match='approach'):
        ContactOuterLoop(law_step=lambda f,dt:f,anchor_m=[0,0,0],task_basis=np.eye(3),
                         target_rotation=np.eye(3),raw_force_limit_n=20,raw_torque_limit_nm=2)


def test_baseline_path_preserves_filter_and_uses_independent_clocks():
    calls=[]
    o=make(lambda f,dt:(calls.append((f.copy(),dt)) or f*.0001))
    common=dict(position_m=[0,0,0],rotation=R,raw_torque_base_nm=[0,0,0])
    a=o.step(time_s=70.,dt_s=.0015,phase='baseline',force_reference_n=1.,raw_force_base_n=[0,0,1],**common)
    assert a['twist_base']==(0.,)*6
    state=o.snapshot()
    b=o.step(time_s=70.003,dt_s=.003,phase='path',path_time_s=0.,raw_force_base_n=[0,0,5],**common)
    assert b['filtered_force_base_n'][2]==pytest.approx(1+4*(-np.expm1(-.003/.02)))
    assert b['reference_force_n']==5 and calls[-1][1]==.003
    o.restore(state)
    c=o.step(time_s=70.003,dt_s=.003,phase='path',path_time_s=0.,raw_force_base_n=[0,0,5],**common)
    assert b==c
    with pytest.raises(ValueError,match='phase transition'):
        o.step(time_s=70.005,dt_s=.002,phase='baseline',raw_force_base_n=[0,0,5],**common)


def test_actual_dt_and_reference_limits_reject_before_law():
    common=dict(time_s=0.,position_m=[0,0,0],rotation=R,raw_force_base_n=[0,0,5],raw_torque_base_nm=[0,0,0])
    o=make(lambda f,dt:(_ for _ in ()).throw(AssertionError('law should not run')))
    for dt in (0.,.004001,float('nan')):
        with pytest.raises(ValueError,match='interval'):o.step(dt_s=dt,**common)
    with pytest.raises(ValueError,match='PATH requires'):o.step(dt_s=.002,force_reference_n=1.,**common)
    with pytest.raises(ValueError,match='raw sensor guard'):tick(o,(0,0,20))


def test_path_projection_cannot_exceed_shared_velocity_cap():
    o=make(lambda f,dt:np.array([.2,.2,0.]))
    # Inside the hard ellipse but in the CBF engagement region.
    out=o.step(time_s=0.,dt_s=.003,position_m=[.018,0,0],rotation=R,
               raw_force_base_n=[0,0,5],raw_torque_base_nm=[0,0,0])
    assert np.linalg.norm(out['capped_task_velocity_m_s'][:2])<=.01+1e-12


def test_contact_age_policy_allows_held_and_keeps_geometry_guard_separate():
    common=dict(position_m=[0,0,0],rotation=R,raw_force_base_n=[0,0,5],
                raw_torque_base_nm=[0,0,0],phase='baseline')
    held=make().step(time_s=0.,dt_s=.002,state_age_s=.05,**common)
    assert held['age_band']=='held'
    assert held['observation_age_s']==pytest.approx(.05)

    # The 80 ms freshness stop is distinct from the existing 1 mm geometric
    # uncertainty budget, which rejects this faster-age case first.
    with pytest.raises(ValueError,match='latency uncertainty'):
        make().step(time_s=0.,dt_s=.002,state_age_s=.07,**common)
    with pytest.raises(ValueError,match='stale observation'):
        make().step(time_s=0.,dt_s=.002,state_age_s=.08,**common)
