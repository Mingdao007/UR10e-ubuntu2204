from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from run_contact_home import admit_sample, validate_robot_sample


def fixture():
    pose=[.474,.17,.049,2.0331342431227566,2.3949884238824652,0.]
    sample={'actual_TCP_pose':pose,'actual_TCP_speed':[0.]*6,'actual_q':[0.]*6,'actual_qd':[0.]*6,
            'tcp_offset':[0,0,.0874,0,0,0],'payload':.413,'payload_cog':[.0011,.0031,.0163],'safety_status_bits':1}
    home={'rtde':{'actual_TCP_pose':pose},'home_pose':[.487834547,.129337053,.033,*pose[3:]]}
    return sample,home


def test_home_checks_tool_and_no_contact_floor():
    sample,home=fixture();admit_sample(sample,home,initial=True)
    sample['actual_TCP_pose']=[.488,.13,.031,*sample['actual_TCP_pose'][3:]]
    with pytest.raises(ValueError,match='floor'):admit_sample(sample,home,initial=False)
    sample,home=fixture();sample['payload_cog'][1]=.0013
    with pytest.raises(ValueError,match='tool'):admit_sample(sample,home,initial=True)


def test_home_stale_start_and_safety_stop_rejected():
    sample,home=fixture();sample['actual_TCP_speed'][0]=.001
    with pytest.raises(ValueError,match='stationary'):admit_sample(sample,home,initial=True)
    sample,home=fixture();sample['safety_status_bits']=4
    with pytest.raises(ValueError,match='NORMAL'):admit_sample(sample,home,initial=False)


def test_normal_with_3pe_input_is_not_a_stop():
    sample,home=fixture();sample['safety_status_bits']=2049
    admit_sample(sample,home,initial=True)
    for bit in range(1,11):
        sample['safety_status_bits']=2049 | (1 << bit)
        with pytest.raises(ValueError,match='NORMAL'):admit_sample(sample,home,initial=True)
    sample['safety_status_bits']=4097
    with pytest.raises(ValueError,match='NORMAL'):admit_sample(sample,home,initial=True)


def test_recovery_protective_allowlist_rejects_unknown_safety_bits():
    sample, _ = fixture()
    for bits in (4, 2052):
        sample['safety_status_bits'] = bits
        validate_robot_sample(sample, allow_protective=True)
    for bits in (0, -1, 5, 2053, '4', True):
        sample['safety_status_bits'] = bits
        with pytest.raises(ValueError):
            validate_robot_sample(sample, allow_protective=True)


def test_slow_figure8_transfer_timeout_uses_path_length_without_widening_geometry(monkeypatch):
    import run_contact_home
    # Keep this regression focused on the timeout geometry.  The live Home
    # profile is intentionally historical 90 mm/s; the older slow profile is
    # injected here so the path-length assertion remains meaningful.
    monkeypatch.setattr(run_contact_home, 'HOME_TRANSFER_SPEED_M_S', .002)
    home_motion_timeout_s = run_contact_home.home_motion_timeout_s
    sample,home=fixture()
    home['rtde']['actual_TCP_pose']=[.487834547,.129337053,.033,*sample['actual_TCP_pose'][3:]]
    home['home_pose']=[.4620551816,.1778825964,.033,*sample['actual_TCP_pose'][3:]]
    home['clearance_entry']=True
    assert 37 < home_motion_timeout_s(home) < 38
    home['home_pose'][0]=.1
    with pytest.raises(ValueError,match='80mm'):home_motion_timeout_s(home)
