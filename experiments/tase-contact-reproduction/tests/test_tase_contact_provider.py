"""Real mature RNN/provider checks using synthetic observations, no devices."""
from dataclasses import replace
import json
from pathlib import Path
import numpy as np
import pytest
from tase_contact_provider import (
    TASE_LIVE_OUTER_CONFIG,
    TASE_PAPER_OUTER_CONFIG,
    TASE_FORCE_PREEMPT_REARM_N,
    TASE_FORCE_PREEMPT_THRESHOLD_N,
    TASE_FORCE_RISE_INWARD_CAP_M_S,
    TaseContactProvider,
    current_model_binding,
    load_tase_outer_config,
)
from tase_figure8_protocol import PROTOCOL_ID as R013_COMPAT60_PROTOCOL_ID
from step5d_autotune_v4.contracts import V4Candidate
from step5d_autotune_v4_r014.solver_profile import LEGACY_R1
from contact_yield_live_writer import native_motion_profile, _prewarm_observation
from contact_yield_live_contract import load_identity_contract


# A calibrated IK solution at the canonical Figure-eight Home.  The prior
# synthetic all-zero joint vector placed the RNN at a UR10e singular
# configuration and made the path warm-start assertion depend on an
# impossible bench state.
CANONICAL_HOME_Q = (
    0.7452071915341404,
    -1.8180657673475784,
    -2.5627204525097316,
    -0.3123084932568952,
    1.527618967738308,
    5.459402166676702,
)


@pytest.fixture
def provider():
    p = TaseContactProvider(contract=current_model_binding(), candidate=V4Candidate(),
        motion_profile=native_motion_profile(), home_pose=load_identity_contract().home_pose,
        solver_profile=LEGACY_R1)
    yield p
    p.close()


def tick(p, now, *, elapsed=None, force=1.):
    o,s = _prewarm_observation(
        pose=load_identity_contract().home_pose,
        q=CANONICAL_HOME_Q,
        monotonic_s=now,
    )
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
    assert config is TASE_LIVE_OUTER_CONFIG
    assert (config.kp, config.ko, config.kf, config.Md_scalar, config.Bd_scalar) == (
        4.0, 5.0, 1.0, 12.0, 550.0
    )
    assert config.orientation_gain_scale == 1.0
    assert TASE_PAPER_OUTER_CONFIG.orientation_gain_scale == 1.0
    assert provider.parameter_binding['force_integral_limit_n_s'] == pytest.approx(1.0)
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


@pytest.mark.parametrize('integral_limit', [0.1, 1.0])
@pytest.mark.parametrize('force_error', [-1.0, 1.0])
def test_parameter_file_binds_explicit_low_windup_integral_limit(
    tmp_path, integral_limit, force_error
):
    payload = {
        'schema': 'tase.outer-parameters-v1',
        'candidate_id': 'manual-integral-0p1',
        'stage': 'diagnostic',
        'index': 0,
        'Md_scalar': 12.0,
        'Bd_scalar': 550.0,
        'protocol_id': R013_COMPAT60_PROTOCOL_ID,
        'duration_token': 'r013_60',
        'frozen': {
            'kp': 4.0,
            'ko': 5.0,
            'kf': 1.0,
            'force_target_n': 5.0,
            'force_integral_limit_n_s': integral_limit,
            'force_integral_policy': 'legacy-clamp-v1',
            'force_integral_authority_error_n': 0.5,
            'force_sign_convention': 'step5_step6_positive_normal_load',
        },
    }
    path = tmp_path / 'integral-0p1.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    config, binding = load_tase_outer_config(path)
    assert config.force_integral_limit_n_s == pytest.approx(integral_limit)
    assert binding['force_integral_limit_n_s'] == pytest.approx(integral_limit)
    p = TaseContactProvider(
        contract=current_model_binding(), candidate=V4Candidate(),
        motion_profile=native_motion_profile(),
        home_pose=load_identity_contract().home_pose,
        solver_profile=LEGACY_R1, outer_loop_config=config,
        parameter_binding=binding, protocol_id=R013_COMPAT60_PROTOCOL_ID,
    )
    try:
        # Exercise the real runtime replacement and integrator, not just the
        # parameter-file binding: a constructor default previously overwrote
        # 0.1 with 1.0 on every live update.
        for index in range(600):
            load = 5.0 - force_error
            p.runtime.desired_twist(
                actual_tcp_pose=load_identity_contract().home_pose,
                actual_tcp_speed=(0.0,) * 6,
                force_tcp_n=(0.0, 0.0, -load),
                filtered_normal_n=load, internal_setpoint_n=5.0,
                actual_dt_s=0.002, mode='path', path_time_s=index * 0.002,
            )
            state = p.snapshot()['runtime']['outer_state']['force_integral_n_s']
            assert abs(state) <= integral_limit + 1e-12
        assert state == pytest.approx(force_error * integral_limit)
        assert p.runtime.force_integral_limit_n_s == pytest.approx(integral_limit)
        assert p.parameter_binding['force_integral_limit_n_s'] == pytest.approx(integral_limit)
    finally:
        p.close()


def test_parameter_file_rejects_unapproved_integral_limit(tmp_path):
    payload = {
        'schema': 'tase.outer-parameters-v1',
        'candidate_id': 'manual-integral-0p2',
        'stage': 'diagnostic',
        'index': 0,
        'Md_scalar': 12.0,
        'Bd_scalar': 550.0,
        'protocol_id': R013_COMPAT60_PROTOCOL_ID,
        'duration_token': 'r013_60',
        'frozen': {
            'kp': 4.0,
            'ko': 5.0,
            'kf': 1.0,
            'force_target_n': 5.0,
            'force_integral_limit_n_s': 0.2,
            'force_integral_policy': 'legacy-clamp-v1',
            'force_integral_authority_error_n': 0.5,
            'force_sign_convention': 'step5_step6_positive_normal_load',
        },
    }
    path = tmp_path / 'integral-0p2.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='0.1, 0.5, or 1.0'):
        load_tase_outer_config(path)


def test_parameter_file_binds_conditional_double_clamp_policy(tmp_path):
    payload = {
        'schema': 'tase.outer-parameters-v1',
        'candidate_id': 'screen-D-00',
        'stage': 'screening',
        'index': 0,
        'Md_scalar': 9.565272137974492,
        'Bd_scalar': 693.6559295653944,
        'protocol_id': R013_COMPAT60_PROTOCOL_ID,
        'duration_token': 'r013_60',
        'frozen': {
            'kp': 4.0,
            'ko': 5.0,
            'kf': 1.0,
            'force_target_n': 5.0,
            'force_integral_limit_n_s': 1.0,
            'force_integral_policy': 'conditional-double-clamp-v1',
            'force_integral_authority_error_n': 0.5,
            'force_sign_convention': 'step5_step6_positive_normal_load',
        },
    }
    path = tmp_path / 'screen-D-00.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    config, binding = load_tase_outer_config(path)
    assert config.force_integral_policy == 'conditional-double-clamp-v1'
    assert binding['force_integral_policy'] == 'conditional-double-clamp-v1'
    assert config.force_integral_authority_error_n == pytest.approx(0.5)


def test_live_path_keeps_confirmed_home_orientation_velocity_zero(provider):
    o, s = tick(provider, .002)
    provider.command(
        output=o,
        sensor=s,
        monotonic_s=.002,
        actual_dt_s=.002,
        mode='path',
        path_time_s=0.0,
        internal_setpoint_n=1.0,
    )
    assert provider.last_result['outer_loop_binding']['live_orientation_policy'] == {
        'orientation_gain_scale': 1.0,
        'orientation_target_policy': 'fixed_approved_home_rotvec',
        'target': 'confirmed Figure-eight Home orientation',
        'paper_comparison_orientation_gain_scale': 1.0,
        'reason': 'avoid world +Z posture step at PATH admission',
    }
    predicted = provider.last_result['predicted_twist_m_s_rad_s']
    assert predicted[3:] == pytest.approx((0.0, 0.0, 0.0), abs=1e-12)


def test_r013_compat_accepts_bounded_returning_handshake_but_caps_reference_seam():
    contract = load_identity_contract()
    provider = TaseContactProvider(
        contract=current_model_binding(),
        candidate=V4Candidate(),
        motion_profile=native_motion_profile(),
        home_pose=contract.home_pose,
        solver_profile=LEGACY_R1,
        protocol_id=R013_COMPAT60_PROTOCOL_ID,
    )
    try:
        # The host may consume a bounded RETURNING tick after the 4 ms task
        # seam; the task reference itself must remain capped at that seam.
        ref = provider.reference(
            'step5d_strict_rnn_autotune_v1',
            contract.home_pose[:2],
            60.04,
        )
        seam_ref = provider.reference(
            'step5d_strict_rnn_autotune_v1',
            contract.home_pose[:2],
            60.004,
        )
        assert ref['desired_xy'] == pytest.approx(seam_ref['desired_xy'])
        with pytest.raises(ValueError, match='clock outside selected protocol'):
            provider.reference(
                'step5d_strict_rnn_autotune_v1',
                contract.home_pose[:2],
                60.081,
            )
    finally:
        provider.close()


def test_live_tase_uses_raw_normal_rise_envelope_without_replacing_evidence_filter(provider):
    o, s = tick(provider, .002, force=8.)
    # The writer's canonical readiness filter can still lag at 1 N while the
    # measured load has already risen.  The controller must see the
    # conservative live envelope, while the evidence field remains the
    # canonical filtered value.
    s = replace(s, filtered_normal_n=1., normal_load_n=8., force_norm_n=8.)
    provider.command(
        output=o,
        sensor=s,
        monotonic_s=.002,
        actual_dt_s=.002,
        mode='baseline',
        internal_setpoint_n=1.,
    )
    assert provider.last_result['filtered_normal_n'] == pytest.approx(1.)
    assert provider.last_result['control_normal_n'] == pytest.approx(8.)
    assert provider.last_result['outer_loop_binding']['live_force_measurement_envelope']['schema'] == (
        'tase-live-force-rise-envelope-v1'
    )


def test_live_tase_force_norm_remains_guard_only(provider):
    o, s = tick(provider, .002, force=8.)
    s = replace(s, filtered_normal_n=1., normal_load_n=8., force_norm_n=19.)
    provider.command(
        output=o,
        sensor=s,
        monotonic_s=.002,
        actual_dt_s=.002,
        mode='baseline',
        internal_setpoint_n=1.,
    )
    assert provider.last_result['filtered_normal_n'] == pytest.approx(1.)
    assert provider.last_result['measured_normal_n'] == pytest.approx(8.)
    assert provider.last_result['measured_force_norm_n'] == pytest.approx(19.)
    assert provider.last_result['control_normal_n'] == pytest.approx(8.)


def test_live_tase_raw_guard_uses_native_wrench_before_baseline_subtraction(provider):
    o, s = tick(provider, .002)
    corrected = ( -3.8078142267, -1.6947470046, -19.8677218756, 0.0460577241, -0.1704001463, 0.0224596705 )
    native = (3.3743647897, 2.5893761495, -17.7580403816, 0.2859546295, -0.4205270766, 0.1061911237)
    s = replace(
        s,
        wrench=corrected,
        raw_wrench=native,
        normal_load_n=-corrected[2],
        force_norm_n=float(np.linalg.norm(corrected[:3])),
        torque_norm_nm=float(np.linalg.norm(corrected[3:])),
        filtered_normal_n=-corrected[2],
    )
    # The recorded failure crossed 20 N only after baseline subtraction.  The
    # native sample remains below the unchanged 20 N / 2 Nm hard envelope.
    assert np.linalg.norm(native[:3]) < 20.0
    assert np.linalg.norm(corrected[:3]) > 20.0
    provider._observe(o, s, .002, .002)
    with pytest.raises(ValueError, match='raw sensor'):
        provider._observe(o, replace(s, raw_wrench=None), .002, .002)


def test_live_tase_baseline_uses_fixed_normal_primitive(provider, monkeypatch):
    o, s = tick(provider, .002)
    s = replace(s, normal_load_n=0.0, force_norm_n=0.0, filtered_normal_n=0.0)
    def forbidden_rnn_command(**kwargs):
        raise AssertionError('baseline contact acquisition must not call the RNN command path')

    monkeypatch.setattr(provider.runtime, 'command', forbidden_rnn_command)
    command = provider.command(
        output=o,
        sensor=s,
        monotonic_s=.002,
        actual_dt_s=.002,
        mode='baseline',
        internal_setpoint_n=1.,
    )
    realized = np.asarray(command.jacobian_6x6, dtype=float) @ np.asarray(command.qdot)
    assert command.qdot != (0.0,) * 6
    assert np.linalg.norm(realized[:2]) <= 2e-6
    assert np.linalg.norm(realized[3:]) <= 2e-6
    assert realized[2] < 0.0
    assert abs(float(realized[2])) <= 0.0005 + 1e-12
    assert provider.last_result['baseline_primitive_speed_m_s'] == pytest.approx(
        0.0001767766953, abs=1e-12
    )
    assert provider.last_result['baseline_normal_projection_applied'] is False
    assert provider.last_result['force_preempt_warm_start'] is False


def test_live_tase_force_preempt_warm_starts_rnn_once(provider):
    assert TASE_FORCE_PREEMPT_THRESHOLD_N == pytest.approx(5.75)
    assert TASE_FORCE_PREEMPT_REARM_N == pytest.approx(5.0)
    assert TASE_FORCE_PREEMPT_THRESHOLD_N > TASE_FORCE_PREEMPT_REARM_N
    o, s = tick(provider, .002, force=8.)
    s = replace(s, normal_load_n=8., force_norm_n=8.)
    provider.command(
        output=o,
        sensor=s,
        monotonic_s=.002,
        actual_dt_s=.002,
        mode='path',
        path_time_s=0.0,
        internal_setpoint_n=1.,
    )
    assert provider.last_result['force_preempt_warm_start'] is True
    assert provider.last_result['force_preempt_warm_started'] is True
    o, s = tick(provider, .004, force=8.)
    s = replace(s, normal_load_n=8., force_norm_n=8.)
    provider.command(
        output=o,
        sensor=s,
        monotonic_s=.004,
        actual_dt_s=.002,
        mode='path',
        path_time_s=0.0,
        internal_setpoint_n=1.,
    )
    assert provider.last_result['force_preempt_warm_start'] is False
    assert provider.last_result['force_preempt_warm_started'] is True


def test_live_tase_baseline_uses_measured_envelope_for_bounded_outward_speed(provider):
    o, s = tick(provider, .002, force=8.)
    s = replace(s, normal_load_n=8., force_norm_n=8., filtered_normal_n=1.)
    provider.command(
        output=o,
        sensor=s,
        monotonic_s=.002,
        actual_dt_s=.002,
        mode='baseline',
        internal_setpoint_n=1.,
    )
    assert provider.last_result['baseline_normal_unload_boost'] is False
    assert provider.last_result['baseline_primitive_speed_m_s'] == pytest.approx(-.00025)
    assert provider.last_result['predicted_approach_normal_velocity_m_s'] == pytest.approx(-.00025)


def test_live_tase_force_preempt_rearms_after_low_force_episode(provider):
    def run(now, force):
        o, s = tick(provider, now, force=force)
        s = replace(s, normal_load_n=force, force_norm_n=force)
        provider.command(
            output=o,
            sensor=s,
            monotonic_s=now,
            actual_dt_s=.002,
            mode='path',
            path_time_s=0.0,
            internal_setpoint_n=1.,
        )
        return provider.last_result

    first = run(.002, 8.)
    assert first['force_preempt_warm_start'] is True
    assert first['force_preempt_episode'] == 1
    held = run(.004, 8.)
    assert held['force_preempt_warm_start'] is False
    assert held['force_preempt_episode'] == 1
    rearmed = run(.006, 4.)
    assert rearmed['force_preempt_armed'] is True
    second = run(.008, 8.)
    assert second['force_preempt_warm_start'] is True
    assert second['force_preempt_episode'] == 2
    assert second['predicted_approach_normal_velocity_m_s'] == pytest.approx(
        -0.002, abs=2e-3
    )


def test_tase_projection_binds_final_qdot_and_preserves_provider_qdot(provider):
    provider.last_result = {
        'qdot_rad_s': (0.1, 0.2, 0.3, 0.4, 0.5, 0.6),
    }
    provider.record_final_qdot((0.01, 0.02, 0.03, 0.04, 0.05, 0.06))
    assert provider.last_result['provider_qdot_rad_s'] == (
        0.1, 0.2, 0.3, 0.4, 0.5, 0.6
    )
    assert provider.last_result['qdot_rad_s'] == (
        0.01, 0.02, 0.03, 0.04, 0.05, 0.06
    )


def test_live_tase_force_rise_guard_keeps_fixed_baseline_primitive_bounded(provider):
    def run(now, force, filtered=None, setpoint=1.0):
        o, s = tick(provider, now, force=force)
        s = replace(
            s,
            normal_load_n=force,
            force_norm_n=force,
            filtered_normal_n=force if filtered is None else filtered,
        )
        provider.command(
            output=o,
            sensor=s,
            monotonic_s=now,
            actual_dt_s=.002,
            mode='baseline',
            internal_setpoint_n=setpoint,
        )

    run(.002, 3.0)
    run(.004, 4.8, filtered=1.0, setpoint=5.0)
    assert provider.last_result['force_rise_guard'] is True
    assert provider.last_result['force_preempt_warm_start'] is False
    assert provider.last_result['force_preempt_direction_retry'] is False
    assert provider.last_result['force_rise_inward_cap_applied'] is False
    assert abs(provider.last_result['baseline_primitive_speed_m_s']) <= 0.00025 + 1e-12
    assert abs(provider.last_result['predicted_approach_normal_velocity_m_s']) <= 0.00025 + 1e-12


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
