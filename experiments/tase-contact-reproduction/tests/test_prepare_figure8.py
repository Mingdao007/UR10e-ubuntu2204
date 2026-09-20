"""Admission capture rejects stale/moving/wrong-Home samples before baselining."""
from types import SimpleNamespace
import pytest
from prepare_figure8 import validate_sample


def sample():
    pose=(.46,.17,.033,2.03,2.39,0.)
    profile=SimpleNamespace(payload_kg=.413,cog_m=(.0011,.0031,.0163),controller_tcp_m_rad=(0,0,.0874,0,0,0))
    output=SimpleNamespace(safety_normal=True,runtime_state=1,received_monotonic_s=1.,
        tcp_pose_m_rad=pose,qd_rad_s=(0,)*6,tcp_speed_m_s_rad_s=(0,)*6,
        payload_kg=profile.payload_kg,payload_cog_m=profile.cog_m,tcp_offset_m_rad=profile.controller_tcp_m_rad)
    return output,SimpleNamespace(home_pose=pose),profile


def test_fresh_stationary_clearance_sample():
    output,contract,profile=sample()
    validate_sample(output,(0,)*6,1.,1.002,contract,profile)


@pytest.mark.parametrize('fault',['stale','moving','home','force','tool'])
def test_failed_capture_never_becomes_a_baseline(fault):
    output,contract,profile=sample()
    wrench=(0,)*6;received=1.
    if fault=='stale': received=.8
    if fault=='moving': output.qd_rad_s=(.01,)*6
    if fault=='home': output.tcp_pose_m_rad=(.48,*output.tcp_pose_m_rad[1:])
    if fault=='force': wrench=(21,0,0,0,0,0)
    if fault=='tool': output.payload_kg=2.
    with pytest.raises(ValueError):
        validate_sample(output,wrench,received,1.002,contract,profile)
