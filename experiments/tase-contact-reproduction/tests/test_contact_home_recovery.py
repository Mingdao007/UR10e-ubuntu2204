"""Bounded return to approved Home without weakening the contact entry gate."""
import copy,json
import numpy as np
import pytest
from contact_yield_math import so3_exp,so3_log
from build_contact_home import build,BASENAME,recovery_geometry
from run_contact_home import admit_sample
from test_contact_benchmark_triplet import receipt


def recovery():
    h=receipt();h['bounded_recovery']=True
    target=np.array(h['home_pose'])
    start=target.copy();start[:3]+=[-.001,-.0005,-.0008]
    start[3:]=so3_log(so3_exp(np.array([.012,0,0]))@so3_exp(target[3:]))
    h['rtde'].update(actual_TCP_pose=start.tolist(),actual_qd=[0]*6,safety_status_bits=1)
    return h


def test_planned_small_rotation_is_allowed_only_on_the_clearance_path():
    h=recovery();s=copy.deepcopy(h['rtde'])
    admit_sample(s,h,initial=True)
    start,target,turn=recovery_geometry(h)
    s['actual_TCP_pose']=np.r_[target[:3],so3_log(so3_exp(.5*turn)@so3_exp(start[3:]))].tolist()
    admit_sample(s,h,initial=False)
    s['actual_TCP_pose'][2]=start[2]
    with pytest.raises(ValueError,match='before vertical clearance'):admit_sample(s,h,initial=False)


def test_off_path_attitude_and_changed_start_are_rejected():
    h=recovery();s=copy.deepcopy(h['rtde']);s['actual_TCP_pose'][0]+=.001
    with pytest.raises(ValueError,match='initial observation changed'):admit_sample(s,h,initial=True)
    s=copy.deepcopy(h['rtde']);s['actual_TCP_pose'][:3]=h['home_pose'][:3]
    s['actual_TCP_pose'][3:]=so3_log(so3_exp(np.array([0,.006,0]))@so3_exp(np.array(h['home_pose'][3:]))).tolist()
    with pytest.raises(ValueError,match='attitude corridor'):admit_sample(s,h,initial=False)


def test_recovery_limits_and_slower_generated_motion(tmp_path):
    h=recovery();p=tmp_path/'home.json';p.write_text(json.dumps(h));out=tmp_path/'package'
    result=build(p,out);text=(out/f'{BASENAME}.script').read_text()
    assert text.startswith('# VERSION:')
    assert 'movel(rise_pose, a=0.010, v=0.002, r=0.0)' in text
    assert result['numeric_sanity']['speed_m_s']==.002
    assert result['home_pose']==h['home_pose']
    h['rtde']['actual_TCP_pose'][0]-=.004
    with pytest.raises(ValueError,match='3mm or 20mrad'):recovery_geometry(h)


def test_withdrawal_only_allows_reversing_the_existing_vertical_search():
    h=receipt();h['bounded_withdrawal']=True
    h['rtde'].update(actual_TCP_pose=list(h['home_pose']),actual_qd=[0]*6,safety_status_bits=1)
    h['rtde']['actual_TCP_pose'][2]-=.013
    s=copy.deepcopy(h['rtde']);admit_sample(s,h,initial=True)
    s['actual_TCP_pose'][2]-=.0002
    with pytest.raises(ValueError,match='farther into'):admit_sample(s,h,initial=False)
    s=copy.deepcopy(h['rtde']);s['actual_TCP_pose'][0]+=.001
    with pytest.raises(ValueError,match='vertical approach'):admit_sample(s,h,initial=False)
