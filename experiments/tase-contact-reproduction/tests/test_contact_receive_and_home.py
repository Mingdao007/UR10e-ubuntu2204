"""Receive-clock and rotation representation regressions; no device access."""
import math
from types import SimpleNamespace
import pytest
from step5d_autotune_v4_r004 import transport
from step5d_autotune_v4_r004.home_profile import load_fixed_home_profile, verify_fixed_home


def test_equivalent_rotvec_and_small_real_error():
    profile = load_fixed_home_profile()
    angle = math.sqrt(sum(v*v for v in profile.pose[3:]))
    axis = tuple(v/angle for v in profile.pose[3:])
    pose = (*profile.pose[:3], *(v*(angle-2*math.pi) for v in axis))
    assert verify_fixed_home(profile, pose, [0]*6).orientation_error_rad < 1e-12
    pose = (*profile.pose[:3], *(v*(angle-2*math.pi+.02) for v in axis))
    assert verify_fixed_home(profile, pose, [0]*6).orientation_error_rad == pytest.approx(.02)


def test_receive_clock_survives_duplicate_and_empty_poll(monkeypatch):
    raw = {'timestamp':10., 'payload':1., 'payload_cog':[0.]*3,
           'tcp_offset':[0.]*6, 'actual_TCP_speed':[0.]*6,
           'actual_TCP_pose':[0.]*6, 'actual_q':[0.]*6, 'actual_qd':[0.]*6,
           'safety_mode':1,'robot_mode':7,'runtime_state':2,
           'output_double_register_24':0.}
    raw.update({f'output_int_register_{i}':0 for i in transport.OUTPUT_INTEGER_FIELDS})
    frames=iter([raw,raw,None,{**raw,'timestamp':10.002}])
    times=iter([100.,101.,102.,103.])
    monkeypatch.setattr(transport.time,'monotonic',lambda:next(times))
    monkeypatch.setattr(transport.time,'time',lambda:1700000000.)
    rtde=transport.LiveR004RTDETransport('offline-only')
    rtde.client=SimpleNamespace(recv_latest_sample=lambda *args:next(frames))
    first=rtde.poll_output()
    assert first.received_monotonic_s == 100.
    assert first.observed_at_s == 1700000000.
    assert rtde.poll_output() is None
    assert rtde.poll_output() is None
    assert rtde.latest is first and first.received_monotonic_s == 100.
    assert rtde.poll_output().received_monotonic_s == 103.
