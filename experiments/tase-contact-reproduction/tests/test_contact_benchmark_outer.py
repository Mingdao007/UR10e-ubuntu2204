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
