"""Native admission rejects missing observations and uncorrected overloads."""
import json
import math
import pytest
from contact_yield_live_writer import (
    NativeYieldLiveWriter, YieldLiveWriterError, load_run_dir_receipts,
)
from contact_yield_live_contract import contact_home_binding, YieldLiveContractError
from step5d_autotune_v4_r006.live_adapter import R006LiveWriter, R006LiveAdapterError
from test_contact_yield_live import _prepare, PRESERVED


def load(tmp_path, contract):
    return load_run_dir_receipts(tmp_path, contract=contract, route_id="r006-yield-live",
                                attempt_id="r006-test", now_s=100.)


@pytest.mark.parametrize("raw", [(21.,0.,0.,0.,0.,0.), (0.,0.,0.,0.,2.01,0.),
                                 (float('nan'),0.,0.,0.,0.,0.)])
def test_raw_overload_precedes_baseline_compensation(monkeypatch, raw):
    writer = object.__new__(NativeYieldLiveWriter)
    writer.software_baseline_n = raw  # A subtract-first check would see zeros.
    def forbidden(*args, **kwargs):
        pytest.fail("overload reached parent compensation")
    monkeypatch.setattr(R006LiveWriter, "_sensor_packet", forbidden)
    with pytest.raises(YieldLiveWriterError):
        writer._sensor_packet(raw=raw, observed_at_s=1.)


def test_baseline_is_actual_file_digest_not_placeholder(tmp_path):
    contract = _prepare(tmp_path)
    prerequisites, _ = load(tmp_path, contract)
    import hashlib
    assert prerequisites.input_baseline_ledger_sha256 == hashlib.sha256(
        (tmp_path / 'software_baseline_receipt.json').read_bytes()).hexdigest()
    assert prerequisites.software_baseline_n == (0.,) * 6
    (tmp_path / 'baseline_capture.json').write_text('changed')
    with pytest.raises(YieldLiveWriterError, match="digest"):
        load(tmp_path, contract)


@pytest.mark.parametrize('field,value', [('stationary',False), ('final_pose',None),
                                        ('observed_at_s',101.), ('observed_at_s',float('nan'))])
def test_home_observation_cannot_be_replaced_by_desired_pose(tmp_path, field, value):
    contract = _prepare(tmp_path)
    p = tmp_path/'home_start_receipt.json'
    doc = json.loads(p.read_text()); doc[field] = value; p.write_text(json.dumps(doc))
    with pytest.raises((YieldLiveWriterError, YieldLiveContractError, R006LiveAdapterError)):
        load(tmp_path, contract)


def test_actual_equivalent_home_pose_is_preserved(tmp_path):
    contract = _prepare(tmp_path)
    pose = list(contract.home_pose)
    pose[3:] = [-x for x in pose[3:]]
    binding = contact_home_binding(contract=contract, final_pose=pose,
        final_q=PRESERVED['home_q'], observed_at_s=90., receipt_sha256='a'*64)
    assert binding.entry_receipt.final_pose == tuple(pose)
    pose[3] += .1
    with pytest.raises((YieldLiveContractError,R006LiveAdapterError),match='orientation'):
        contact_home_binding(contract=contract, final_pose=pose,
            final_q=PRESERVED['home_q'], observed_at_s=90., receipt_sha256='a'*64)


@pytest.mark.parametrize('fresh,state,stationary,confirmed', [
    (True,90,True,True), (False,90,True,False),
    (True,25,True,False), (True,90,False,False)])
def test_stop_needs_fresh_stationary_controller_observation(fresh,state,stationary,confirmed):
    from types import SimpleNamespace as NS
    from contact_yield_live_writer import stop_and_confirm
    clock = NS(now=1.)
    output = NS(timestamp=2., received_monotonic_s=1. if fresh else .1,
        stationary=stationary, safety_normal=True,
        integer_echoes={26:state,28:4,32:606006,33:18,34:618001},
        qd_rad_s=(0.,)*6, tcp_speed_m_s_rad_s=(0.,)*6)
    writer = NS(_mono_clock=lambda:clock.now, _last_output=NS(timestamp=1.),
        stop=lambda reason:None, _opened=True,
        _controller_transport=NS(poll_output=lambda **kw:output),
        _sleep=lambda dt:setattr(clock,'now',clock.now+dt))
    assert stop_and_confirm(writer,timeout_s=.01)['stopped'] is confirmed


def test_failed_stop_is_not_reported_as_success():
    from types import SimpleNamespace as NS
    from contact_yield_live import stop_owner
    def fail(*args, **kwargs):
        raise RuntimeError('send failed')
    writer = NS(_mono_clock=lambda:1., _last_output=None, stop=fail)
    holder = [NS(writer=writer,close=lambda:None)]
    result = stop_owner(holder)
    assert result['stopped'] is False
    assert result['errors'] == ['send failed']


def test_slew_interval_between_old_discrete_scales_is_found():
    import numpy as np
    from contact_yield_qp import continuous_feasible_scale, SCALE_SEQUENCE
    normal = np.array([0.,0.,.001,0.,0.,0.])
    progress = np.array([.01,0.,0.,0.,0.,0.])
    lower, upper = np.full(6,-.05), np.full(6,.05)
    lower[0], upper[0] = .0079, .0081
    assert not any(lower[0] <= scale*.01 <= upper[0] for scale in SCALE_SEQUENCE)
    scale = continuous_feasible_scale(np.eye(6),normal,progress,lower,upper)
    assert scale == pytest.approx(.81)
    command = normal + scale*progress
    assert np.all(command >= lower) and np.all(command <= upper)
    assert command[2] == normal[2]


def test_live_motion_profile_matches_native_tp_and_existing_task_caps():
    from contact_yield_live_writer import native_motion_profile
    profile = native_motion_profile()
    assert profile.qdot_cap_rad_s == .05
    assert profile.tp_acceleration_rad_s2 == 5.
    assert profile.tangential_cap_m_s == .01
    assert profile.normal_linear_cap_m_s == .003
    assert profile.angular_cap_rad_s == .05
    assert profile.execution_profile.qdot_cap_rad_s == .05


from test_contact_benchmark_runtime import lib  # native solver fixture, no devices


def test_live_slew_fallback_reports_sacrificed_normal_tracking(lib):
    import numpy as np
    from contact_yield_qp import YieldQp, YieldQpError
    qp = YieldQp(lib)
    twist = np.array([.01,0.,.003,0.,0.,0.])
    lower, upper = np.full(6,-.0005), np.full(6,.0005)
    # Original normal-preserving policy is genuinely infeasible here.
    with pytest.raises(YieldQpError):
        qp.compose(np.eye(6),twist,lower,upper,(0.,0.,1.))
    result = qp.compose(np.eye(6),twist,lower,upper,(0.,0.,1.),continuous_scaling=True)
    assert result['normal_unloading_preserved'] is False
    assert result['normal_task_scale'] == pytest.approx(1/6)
    assert result['task_scale'] == 0.
    assert result['qdot_rad_s'][2] == pytest.approx(.0005,abs=1e-7)
    assert max(abs(x) for x in result['qdot_rad_s']) <= .0005+1e-7
    assert result['feasibility_force_guarantee'] is False
