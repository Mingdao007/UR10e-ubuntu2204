"""Real mature RNN/provider checks using synthetic observations, no devices."""
from dataclasses import replace
import json
from pathlib import Path
import numpy as np
import pytest
from tase_contact_provider import (
    TASE_PAPER_OUTER_CONFIG,
    TaseContactProvider,
    current_model_binding,
)
from step5d_autotune_v4.contracts import V4Candidate
from step5d_autotune_v4_r014.solver_profile import LEGACY_R1
from contact_yield_live_writer import native_motion_profile, _prewarm_observation
from contact_yield_live_contract import load_identity_contract


@pytest.fixture
def provider():
    p = TaseContactProvider(contract=current_model_binding(), candidate=V4Candidate(),
        motion_profile=native_motion_profile(), home_pose=load_identity_contract().home_pose,
        solver_profile=LEGACY_R1)
    yield p
    p.close()


def tick(p, now, *, elapsed=None, force=1.):
    o,s = _prewarm_observation(pose=load_identity_contract().home_pose, q=(0.,)*6, monotonic_s=now)
    o.stationary = True
    s = replace(s, wrench=(0.,0.,-force,0.,0.,0.), filtered_normal_n=force)
    return o,s


def test_real_rnn_full_state_replay_and_json_snapshot(provider):
    p=provider
    for index in range(3):
        now=.002*(index+1);o,s=tick(p,now)
        p.command(output=o,sensor=s,monotonic_s=now,actual_dt_s=.002,
                  mode='baseline',internal_setpoint_n=1.)
    checkpoint=p.snapshot()
    json.dumps(checkpoint,allow_nan=False)
    o,s=tick(p,.008)
    kwargs=dict(output=o,sensor=s,monotonic_s=.008,actual_dt_s=.002,
        mode='path',path_time_s=0.,internal_setpoint_n=1.)
    first=p.command(**kwargs)
    p.restore(checkpoint)
    second=p.command(**kwargs)
    np.testing.assert_array_equal(first.qdot,second.qdot)
    assert p.last_result['phase']=='entry'
    assert p.solver_profile.profile_id=='legacy-r1'


def test_live_tase_binds_paper_outer_parameters(provider):
    config = provider.runtime.outer_loop_config
    assert config is TASE_PAPER_OUTER_CONFIG
    assert (config.kp, config.ko, config.kf, config.Md_scalar, config.Bd_scalar) == (
        4.0, 5.0, 1.0, 12.0, 550.0
    )
    # The R006 candidate has a different derived force mapping; checking the
    # runtime binding prevents candidate tuning from silently replacing the
    # TASE Eq. 16/17 anchors.
    assert provider.runtime.candidate.motion_kp != config.kp
    o, s = tick(provider, .002)
    provider.command(
        output=o,
        sensor=s,
        monotonic_s=.002,
        actual_dt_s=.002,
        mode='baseline',
        internal_setpoint_n=1.,
    )
    assert provider.last_result['outer_loop_binding']['equations'] == ['Eq16', 'Eq17']


def test_stale_observation_does_not_advance_control_state(provider):
    before=provider.snapshot();o,s=tick(provider,.1)
    s=replace(s,observed_at_s=.001)
    with pytest.raises(ValueError):
        provider.command(output=o,sensor=s,monotonic_s=.1,actual_dt_s=.002,
            mode='baseline',internal_setpoint_n=1.)
    assert provider.snapshot()==before


def test_late_stationary_hold_freezes_rnn_and_requires_stationarity(provider):
    o,s=tick(provider,.002)
    provider.command(output=o,sensor=s,monotonic_s=.002,actual_dt_s=.002,
        mode='baseline',internal_setpoint_n=1.)
    before=provider.runtime.dynamic_state_snapshot()
    o,s=tick(provider,.014)
    provider.hold_pre_path_late_cycle(output=o,sensor=s,monotonic_s=.014,
        actual_dt_s=.012,reason='late')
    assert provider.runtime.dynamic_state_snapshot()==before
    o.stationary=False
    with pytest.raises(ValueError,match='stationary'):
        provider.hold_pre_path_late_cycle(output=o,sensor=s,monotonic_s=.026,
            actual_dt_s=.012,reason='late')


def test_mature_owner_factory_restores_real_solver_before_endpoints(tmp_path):
    from test_contact_yield_live import _prepare
    from contact_yield_live_writer import load_run_dir_receipts, build_native_yield_owner
    contract = _prepare(tmp_path)
    prereq, home = load_run_dir_receipts(tmp_path, contract=contract,
        route_id='r006-yield-live', attempt_id='r006-tase', now_s=100.)
    writer, runtime, p, request = build_native_yield_owner(
        method='TASE_RNN_MATURE', duration='full', command='pilot',
        prerequisites=prereq, home_binding=home, authority_root=tmp_path/'authority',
        route_id='r006-yield-live', attempt_id='r006-tase',
        controller_transport=object(), kunwei_transport=object(), wall_clock=lambda:100.)
    try:
        assert isinstance(p, TaseContactProvider)
        assert runtime is p
        assert p.runtime.solver is not None
        assert p.last_result is None
        assert request is not None
        from step5d_autotune_v4_r006.live_adapter import _R006NativeCanonicalQualificationControl, R006Candidate
        control = _R006NativeCanonicalQualificationControl(
            candidate=R006Candidate(), attempt_id='r006-tase',
            release_contract=writer.writer.contract, path_requested=True,
            canonical_runtime_only=True, motion_profile=native_motion_profile(),
            contact_command_provider=p)
        assert control._runtime is p
    finally:
        runtime.close()


@pytest.mark.parametrize('dt', [.006, .012, .019])
def test_mature_actual_dt_uses_existing_twenty_ms_timing_bound(provider, dt):
    o,s=tick(provider,.002)
    provider.command(output=o,sensor=s,monotonic_s=.002,actual_dt_s=.002,
                     mode='baseline',internal_setpoint_n=1.)
    o,s=tick(provider,.002+dt)
    provider.lifecycle_observer.step(actual_dt_s=dt,raw_normal_n=1.,setpoint_n=1.,mode='path')
    result=provider.command(output=o,sensor=s,monotonic_s=.002+dt,actual_dt_s=dt,
                            mode='path',path_time_s=.01,internal_setpoint_n=1.)
    assert np.isfinite(result.qdot).all()
    assert provider.last_result['actual_dt_s']==dt


def test_mature_twenty_ms_is_rejected_even_on_first_tick(provider):
    o,s=tick(provider,.02)
    before=provider.snapshot()
    with pytest.raises(ValueError,match='timing bound'):
        provider.command(output=o,sensor=s,monotonic_s=.02,actual_dt_s=.02,
                         mode='path',path_time_s=0.,internal_setpoint_n=1.)
    assert provider.snapshot()==before


def test_native_readiness_default_still_rejects_above_four_ms():
    from contact_benchmark_provider import ContactReadinessObserver
    with pytest.raises(ValueError,match='interval'):
        ContactReadinessObserver(.1).step(actual_dt_s=.006,raw_normal_n=1.,setpoint_n=1.,mode='baseline')
