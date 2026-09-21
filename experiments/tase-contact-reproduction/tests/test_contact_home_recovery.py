"""Bounded return to approved Home without weakening the contact entry gate."""
import copy,json
import numpy as np
import pytest
from contact_yield_math import so3_exp,so3_log
from build_contact_home import build,BASENAME,recovery_geometry
from build_contact_recovery import build_recovery,RELIEF_PROGRAM
from run_contact_home import admit_sample
from test_contact_benchmark_triplet import receipt
from contact_yield_task_frame import FIGURE8_CONTACT_HOME_XYZ_M


def recovery():
    h=receipt();h['bounded_recovery']=True
    h['clearance_entry']=True
    h['home_pose'][:3]=list(FIGURE8_CONTACT_HOME_XYZ_M)
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


def test_recovery_reuses_historical_generated_motion(tmp_path):
    h=recovery();p=tmp_path/'home.json';p.write_text(json.dumps(h));out=tmp_path/'package'
    result=build(p,out);text=(out/f'{BASENAME}.script').read_text()
    assert text.startswith('# VERSION:')
    assert 'movel(rise_pose, a=0.060, v=0.040, r=0.0)' in text
    assert 'movel(transfer_pose, a=0.060, v=0.040, r=0.0)' in text
    assert 'movel(descent_pose, a=0.060, v=0.040, r=0.0)' in text
    assert result['numeric_sanity']['speed_m_s']==.04
    assert result['numeric_sanity']['segment_1_speed_m_s']==.04
    assert result['numeric_sanity']['segment_2_speed_m_s']==.04
    assert result['numeric_sanity']['speed_basis'].startswith('step5d_autotune_start_hover_r001')
    assert 'v=0.0005' not in text and 'v=0.002' not in text
    assert result['home_pose']==h['home_pose']
    assert 'home_angle > 0.020' in text
    assert 'BOUNDED_RECOVERY:' in text
    h['rtde']['actual_TCP_pose'][0]-=.004
    with pytest.raises(ValueError,match='3mm or 20mrad'):recovery_geometry(h)


def test_direct_clearance_home_retains_historical_transfer_speed(tmp_path):
    h=receipt();h['clearance_entry']=True
    h['home_pose'][:3]=list(FIGURE8_CONTACT_HOME_XYZ_M)
    h['rtde'].update(actual_TCP_pose=list(h['home_pose']),actual_qd=[0]*6,safety_status_bits=1)
    p=tmp_path/'home.json';p.write_text(json.dumps(h));out=tmp_path/'package'
    result=build(p,out);text=(out/f'{BASENAME}.script').read_text()
    assert 'movel(transfer_pose, a=0.135, v=0.090, r=0.0)' in text
    assert result['numeric_sanity']['segment_2_speed_m_s']==.09


def test_relief_reuses_historical_vertical_motion(tmp_path):
    h=recovery();p=tmp_path/'home.json';p.write_text(json.dumps(h));out=tmp_path/'recovery-package'
    build_recovery(p,out)
    text=(out/f'{RELIEF_PROGRAM}.script').read_text()
    binding=json.loads((out/f'{RELIEF_PROGRAM}.binding.json').read_text())
    assert 'movel(rise_pose, a=0.060, v=0.040, r=0.0)' in text
    assert 'v=0.0005' not in text and 'v=0.002' not in text
    assert binding['vertical_speed_m_s']==.04
    assert binding['vertical_acceleration_m_s2']==.06
    assert binding['speed_basis'].startswith('step5d_autotune_start_hover_r001')
    assert binding['staged_recovery'] is True
    assert 'angle > 0.020' in text


def test_withdrawal_only_allows_reversing_the_existing_vertical_search():
    h=receipt();h['bounded_withdrawal']=True
    h['rtde'].update(actual_TCP_pose=list(h['home_pose']),actual_qd=[0]*6,safety_status_bits=1)
    h['rtde']['actual_TCP_pose'][2]-=.013
    s=copy.deepcopy(h['rtde']);admit_sample(s,h,initial=True)
    s['actual_TCP_pose'][2]-=.0002
    with pytest.raises(ValueError,match='farther into'):admit_sample(s,h,initial=False)
    s=copy.deepcopy(h['rtde']);s['actual_TCP_pose'][0]+=.001
    with pytest.raises(ValueError,match='vertical approach'):admit_sample(s,h,initial=False)
