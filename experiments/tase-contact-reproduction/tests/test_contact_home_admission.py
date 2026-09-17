from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from run_contact_home import admit_sample


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
