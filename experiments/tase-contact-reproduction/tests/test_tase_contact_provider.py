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


def test_resident_home_load_changes_runtime_and_resets_integral(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'config/tase_figure8_integral_0p1.json'
    first = json.loads(source.read_text(encoding='utf-8'))
    second = json.loads(source.read_text(encoding='utf-8'))
    second['candidate_id'] = 'resident-second'
    second['Md_scalar'] = 10.0
    second['Bd_scalar'] = 700.0
    second['frozen']['force_integral_limit_n_s'] = 0.5
    first_path = tmp_path / 'first.json'
    second_path = tmp_path / 'second.json'
    first_path.write_text(json.dumps(first), encoding='utf-8')
    second_path.write_text(json.dumps(second), encoding='utf-8')
    p = TaseContactProvider(
        contract=current_model_binding(), candidate=V4Candidate(),
        motion_profile=native_motion_profile(),
        home_pose=load_identity_contract().home_pose,
        solver_profile=LEGACY_R1, protocol_id=R013_COMPAT60_PROTOCOL_ID,
    )
    try:
        seed = p.snapshot()
        first_binding = p.apply_outer_parameters_at_home(first_path)
        assert first_binding['Md_scalar'] == p.runtime.outer_loop_config.Md_scalar
        assert first_binding['Bd_scalar'] == p.runtime.outer_loop_config.Bd_scalar
        assert p.runtime.force_integral_limit_n_s == 0.1
        for index in range(5):
            p.runtime.desired_twist(
                actual_tcp_pose=load_identity_contract().home_pose,
                actual_tcp_speed=(0.0,) * 6,
                force_tcp_n=(0.0, 0.0, -4.0),
                filtered_normal_n=4.0, internal_setpoint_n=5.0,
                actual_dt_s=0.002, mode='path', path_time_s=index * 0.002,
            )
        assert p.snapshot()['runtime']['outer_state']['force_integral_n_s'] > 0.0
        p.restore(seed)
        second_binding = p.apply_outer_parameters_at_home(second_path)
        assert second_binding['candidate_id'] == 'resident-second'
        assert p.runtime.outer_loop_config.Md_scalar == 10.0
        assert p.runtime.outer_loop_config.Bd_scalar == 700.0
        assert p.runtime.force_integral_limit_n_s == 0.5
        assert p.snapshot()['runtime']['outer_state']['force_integral_n_s'] == 0.0
        invalid = dict(second, unexpected=True)
        bad_path = tmp_path / 'bad.json'
        bad_path.write_text(json.dumps(invalid), encoding='utf-8')
        with pytest.raises(ValueError, match='unknown fields'):
            p.apply_outer_parameters_at_home(bad_path)
        assert p.runtime.outer_loop_config.Md_scalar == 10.0
        assert p.parameter_binding == second_binding
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


def test_integral_off_parameter_is_applied_and_remains_exact_zero(provider, tmp_path):
    payload = {
        'schema': 'tase.outer-parameters-v1',
        'candidate_id': 'screen-E-00',
        'stage': 'screening',
        'index': 0,
        'Md_scalar': 12.0,
        'Bd_scalar': 550.0,
        'frozen': {
            'kp': 4.0,
            'ko': 5.0,
            'kf': 1.0,
            'force_target_n': 5.0,
            'force_integral_limit_n_s': 1.0,
            'force_integral_policy': 'integral-off-v1',
            'force_integral_authority_error_n': 0.5,
            'force_sign_convention': 'step5_step6_positive_normal_load',
        },
    }
    path = tmp_path / 'screen-E-00.json'
    path.write_text(json.dumps(payload), encoding='utf-8')

    binding = provider.apply_outer_parameters_at_home(path)
    assert binding['force_integral_policy'] == 'integral-off-v1'
    assert provider.runtime.actual_runtime_config()['force_integral_policy'] == 'integral-off-v1'
    assert provider.runtime.dynamic_state_snapshot()['outer_state']['force_integral_n_s'] == 0.0
    for index in range(20):
        provider.runtime.desired_twist(
            actual_tcp_pose=load_identity_contract().home_pose,
            actual_tcp_speed=(0.0,) * 6,
            force_tcp_n=(0.0, 0.0, -2.0),
            filtered_normal_n=2.0,
            internal_setpoint_n=5.0,
            actual_dt_s=0.002,
            mode='path',
            path_time_s=index * 0.002,
        )
        assert provider.runtime.dynamic_state_snapshot()['outer_state']['force_integral_n_s'] == 0.0


def test_conditional_policy_uses_published_taskspace_saturation_for_next_tick(provider):
    runtime = provider.runtime
    runtime.outer_loop_config = replace(
        TASE_LIVE_OUTER_CONFIG,
        force_integral_policy='conditional-double-clamp-v1',
    )
    runtime.force_integral_policy = 'conditional-double-clamp-v1'
    runtime.reset_outer_loop_state()

    def command(force, at_s):
        return runtime.desired_twist(
            actual_tcp_pose=load_identity_contract().home_pose,
            actual_tcp_speed=(0.0,) * 6,
            force_tcp_n=(0.0, 0.0, -force),
            filtered_normal_n=force,
            internal_setpoint_n=5.0,
            actual_dt_s=0.002,
            mode='path',
            path_time_s=at_s,
        )

    command(1.0, 0.0)
    integral_after_first = runtime._outer_state.force_integral_n_s
    feedback = runtime.record_published_outer_output(
        final_qdot=(0.0,) * 6,
        jacobian_6x6=np.eye(6),
        explicit_realization_limit=True,
        packet_sequence=10,
        published_at_s=1.0,
    )
    assert feedback is not None
    assert feedback['task_space_saturated'] is False
    assert feedback['realization_saturated'] is True
    assert feedback['realization_saturation_residual_m_s'] > 0.0
    assert feedback['same_direction_freeze_sign'] == 1, feedback

    command(1.0, 0.002)
    assert runtime._pending_outer_feedback['previous_output_freeze_applied'] is True
    assert runtime._outer_state.force_integral_n_s == pytest.approx(integral_after_first)
    second_twist = runtime._pending_outer_feedback['task_space_command_twist']
    runtime.record_published_outer_output(
        final_qdot=second_twist,
        jacobian_6x6=np.eye(6),
        explicit_realization_limit=False,
        packet_sequence=11,
        published_at_s=1.002,
    )

    command(6.0, 0.004)
    assert runtime._pending_outer_feedback['previous_output_freeze_applied'] is False
    assert runtime._outer_state.force_integral_n_s < integral_after_first


def test_conditional_policy_publishes_inner_velocity_limit_feedback(provider):
    runtime = provider.runtime
    runtime.outer_loop_config = replace(
        TASE_LIVE_OUTER_CONFIG,
        force_integral_policy='conditional-double-clamp-v1',
    )
    runtime.force_integral_policy = 'conditional-double-clamp-v1'
    runtime.reset_outer_loop_state()
    home_pose = load_identity_contract().home_pose
    requested_command = runtime.desired_twist(
        actual_tcp_pose=home_pose,
        actual_tcp_speed=(0.0,) * 6,
        force_tcp_n=(0.0, 0.0, 0.0),
        filtered_normal_n=0.0,
        internal_setpoint_n=5.0,
        actual_dt_s=0.01,
        mode='path',
        path_time_s=0.0,
    )
    pending = runtime._pending_outer_feedback
    assert pending['explicit_velocity_limit_active'] is True
    force_axis = np.asarray(pending['force_normal_base'])
    requested_normal = float(np.asarray(pending['requested_twist'][:3]) @ force_axis)
    task_normal = float(
        np.asarray(pending['task_space_command_twist'][:3]) @ force_axis
    )
    assert abs(requested_normal - task_normal) > 1e-12

    feedback = runtime.record_published_outer_output(
        final_qdot=requested_command,
        jacobian_6x6=np.eye(6),
        explicit_realization_limit=False,
        packet_sequence=12,
        published_at_s=1.0,
    )
    assert feedback['explicit_velocity_limit_active'] is True
    assert feedback['task_space_saturated'] is True
    assert feedback['same_direction_freeze_sign'] == 1

    integral_after_limited_output = runtime._outer_state.force_integral_n_s
    runtime.desired_twist(
        actual_tcp_pose=home_pose,
        actual_tcp_speed=(0.0,) * 6,
        force_tcp_n=(0.0, 0.0, -6.0),
        filtered_normal_n=6.0,
        internal_setpoint_n=5.0,
        actual_dt_s=0.002,
        mode='path',
        path_time_s=0.002,
    )
    assert runtime._pending_outer_feedback['previous_output_freeze_applied'] is False
    assert runtime._outer_state.force_integral_n_s < integral_after_limited_output


def test_conditional_policy_does_not_freeze_on_plain_rnn_tracking_residual(provider):
    runtime = provider.runtime
    runtime.outer_loop_config = replace(
        TASE_LIVE_OUTER_CONFIG,
        force_integral_policy='conditional-double-clamp-v1',
    )
    runtime.force_integral_policy = 'conditional-double-clamp-v1'
    runtime.reset_outer_loop_state()
    kwargs = dict(
        actual_tcp_pose=load_identity_contract().home_pose,
        actual_tcp_speed=(0.0,) * 6,
        force_tcp_n=(0.0, 0.0, -1.0),
        filtered_normal_n=1.0,
        internal_setpoint_n=5.0,
        actual_dt_s=0.002,
        mode='path',
    )
    runtime.desired_twist(**kwargs, path_time_s=0.0)
    before = runtime._outer_state.force_integral_n_s
    normal = np.asarray(runtime._pending_outer_feedback['force_normal_base'])
    request = tuple(float(value) for value in np.r_[normal * 0.0001, np.zeros(3)])
    runtime._pending_outer_feedback['requested_twist'] = request
    runtime._pending_outer_feedback['task_space_command_twist'] = request
    feedback = runtime.record_published_outer_output(
        final_qdot=(0.0,) * 6,
        jacobian_6x6=np.eye(6),
        explicit_realization_limit=False,
        packet_sequence=10,
        published_at_s=1.0,
    )
    assert feedback['realization_saturation_residual_m_s'] > 0.0
    assert feedback['realization_saturated'] is False
    assert feedback['same_direction_freeze_sign'] == 0
    runtime.desired_twist(**kwargs, path_time_s=0.002)
    assert runtime._pending_outer_feedback['previous_output_freeze_applied'] is False
    assert runtime._outer_state.force_integral_n_s > before


def test_published_feedback_uses_actual_native_joint_cap(provider):
    assert provider.runtime.motion_profile.qdot_cap_rad_s == pytest.approx(0.05)
    assert provider.solver_profile.qdot_limit_rad_s == pytest.approx(0.15)
    provider.runtime._pending_outer_feedback = {
        'schema': 'step5d-outer-output-feedback-v1',
        'policy': 'conditional-double-clamp-v1',
        'force_error_n': 1.0,
        'force_normal_base': (1.0, 0.0, 0.0),
        'requested_twist': (0.06, 0.0, 0.0, 0.0, 0.0, 0.0),
        'task_space_command_twist': (0.06, 0.0, 0.0, 0.0, 0.0, 0.0),
    }
    provider.last_result = {
        'qdot_rad_s': (0.05, 0.0, 0.0, 0.0, 0.0, 0.0),
        'jacobian_6x6': np.eye(6).tolist(),
        'actual_q_rad': CANONICAL_HOME_Q,
        'host_slew_scale': 1.0,
        'gate_projection_applied': False,
    }
    provider.confirm_published_packet(
        (0.05, 0.0, 0.0, 0.0, 0.0, 0.0),
        packet_sequence=21,
        published_at_s=1.0,
    )
    feedback = provider.last_result['published_output_feedback']
    assert feedback['explicit_realization_limit'] is True
    assert feedback['realization_saturated'] is True
    assert feedback['same_direction_freeze_sign'] == 1


def test_rejected_path_calculation_restores_output_feedback_state(provider, monkeypatch):
    runtime = provider.runtime
    runtime.outer_loop_config = replace(
        TASE_LIVE_OUTER_CONFIG,
        force_integral_policy='conditional-double-clamp-v1',
    )
    runtime.force_integral_policy = 'conditional-double-clamp-v1'
    runtime._last_published_outer_feedback = {
        'schema': 'step5d-outer-output-feedback-v1',
        'same_direction_freeze_sign': 1,
    }
    runtime._pending_outer_feedback = {
        'schema': 'step5d-outer-output-feedback-v1',
        'explicit_velocity_limit_active': True,
    }
    before = provider.snapshot()

    def reject_command(**_kwargs):
        raise ValueError('synthetic solver calculation rejection')

    monkeypatch.setattr(runtime, 'command', reject_command)
    output, sensor = tick(provider, .002, force=1.0)
    with pytest.raises(ValueError, match='synthetic solver calculation rejection'):
        provider.command(
            output=output,
            sensor=sensor,
            monotonic_s=.002,
            actual_dt_s=.002,
            mode='path',
            path_time_s=1.0,
            internal_setpoint_n=5.0,
        )
    assert provider.snapshot() == before


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


def test_bounded_path_diagnostics_capture_solver_and_limit_activity(monkeypatch):
    provider = object.__new__(TaseContactProvider)
    provider.reset_command_diagnostics()
    def command(**_kwargs):
        provider.last_result = {
            'phase': 'path', 'host_slew_scale': .5,
            'gate_projection_applied': True, 'force_rise_guard': True,
            'solver': {'backend': 'numpy', 'solve_wall_s': .001,
                       'active_bounds_mask': (True, False, False, False, False, False)},
        }
        return 'command'
    monkeypatch.setattr(provider, 'command', command)
    assert provider.execution_command() == 'command'
    stats = provider.command_diagnostics
    assert stats['path_calls'] == stats['solver_timed_calls'] == 1
    assert stats['solver_active_bound_calls'] == 1
    assert stats['host_slew_limited_calls'] == 1
    assert stats['gate_projection_calls'] == 0
    assert stats['solver_backend_counts']['numpy'] == 1
    provider.reset_command_diagnostics()
    assert provider.command_diagnostics['calls'] == 0


def test_real_path_step_propagates_existing_rnn_solver_time(provider):
    for now, path_time in ((.002, 0.0), (.004, 1.002)):
        output, sensor = tick(provider, now, force=5.)
        provider.execution_command(
            output=output, sensor=sensor, monotonic_s=now,
            actual_dt_s=.002, mode='path', path_time_s=path_time,
            internal_setpoint_n=5.,
        )
    solver = provider.last_result['solver']
    assert solver['backend'] == 'numpy'
    assert solver['solve_wall_s'] > 0
    assert provider.command_diagnostics['path_calls'] == 1
    assert provider.command_diagnostics['solver_timed_calls'] == 1


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


@pytest.mark.parametrize('send_fails', [False, True])
def test_native_writer_commits_output_feedback_only_after_successful_send(
    monkeypatch, send_fails
):
    from types import SimpleNamespace

    from contact_yield_live_writer import NativeYieldLiveWriter, R006LiveWriter
    from step5d_autotune_v4_r004.wire import CommandMode

    calls = []
    provider = SimpleNamespace(
        confirm_published_packet=lambda qdot, **kwargs: calls.append((qdot, kwargs))
    )
    writer = object.__new__(NativeYieldLiveWriter)
    writer.__dict__.update({
        '_qualification_control': SimpleNamespace(contact_command_provider=provider),
        '_last_output': None,
        '_stopped': False,
        '_host_path_publish_count': 0,
        '_host_formal_path_publish_count': 0,
        '_path_timing_stats': None,
        '_last_writer_publish_mono_s': None,
        '_r013_path_early_end_controller': None,
        'live_path_request': SimpleNamespace(
            kind='r013_compat_60', path_duration_s=60.0
        ),
        '_service_mode': False,
        '_mono_clock': lambda: 123.5,
        'command_observations': [],
    })
    packet_qdot = (0.01, 0.02, 0.03, 0.04, 0.05, 0.049)
    proposed_qdot = (0.011, 0.021, 0.031, 0.041, 0.048, 0.049)

    def send_success(self, _sensor, **kwargs):
        self._last_writer_publish_mono_s = 123.5
        doubles = (0.0,) * 13 + packet_qdot + (0.0,) * 5
        packet = SimpleNamespace(sequence=17, double_values=doubles)
        self._after_transport_send_success(
            packet,
            command_mode=kwargs['command_mode'],
            reference_phase=kwargs['reference_phase'],
            reference_time_s=kwargs['reference_time_s'],
            published_at_s=123.5,
        )
        return packet

    def send_failure(self, _sensor, **_kwargs):
        raise OSError('synthetic packet send failure')

    monkeypatch.setattr(
        R006LiveWriter,
        '_send_packet',
        send_failure if send_fails else send_success,
    )
    send = lambda: writer._send_packet(
        None,
        command_mode=CommandMode.PATH,
        proposed_qdot=proposed_qdot,
        reference_phase='path',
        reference_time_s=0.0,
    )
    if send_fails:
        with pytest.raises(OSError, match='synthetic packet send failure'):
            send()
        assert calls == []
        assert writer._host_formal_path_publish_count == 0
    else:
        packet = send()
        assert packet.sequence == 17
        assert writer._host_path_publish_count == 1
        assert writer._host_formal_path_publish_count == 1
        assert calls == [(
            packet_qdot,
            {'packet_sequence': 17, 'published_at_s': 123.5,
             'reference_phase': 'path', 'reference_time_s': 0.0},
        )]


@pytest.mark.parametrize('send_fails', [False, True])
def test_tase_replay_trace_commits_only_after_successful_path_send(
    provider, monkeypatch, send_fails
):
    from types import SimpleNamespace

    from contact_yield_live_writer import NativeYieldLiveWriter, R006LiveWriter
    from step5d_autotune_v4_r004.wire import CommandMode

    provider.reset_replay_evidence(1)
    provider.bind_command_history((0.0,) * 6, 15.0)
    output, sensor = tick(provider, .002, force=1.0)
    command = provider.execution_command(
        output=output, sensor=sensor, monotonic_s=.002, actual_dt_s=.002,
        mode='path', path_time_s=1.25, internal_setpoint_n=1.0,
    )
    packet_qdot = tuple(command.qdot)
    assert provider.replay_evidence.count == 0
    assert provider.replay_evidence.pending_ready is True

    writer = object.__new__(NativeYieldLiveWriter)
    writer.__dict__.update({
        '_qualification_control': SimpleNamespace(contact_command_provider=provider),
        '_last_output': None,
        '_stopped': False,
        '_host_path_publish_count': 0,
        '_host_formal_path_publish_count': 0,
        '_path_timing_stats': None,
        '_last_writer_publish_mono_s': None,
        '_r013_path_early_end_controller': None,
        'live_path_request': SimpleNamespace(
            kind='r013_compat_60', path_duration_s=60.0
        ),
        '_service_mode': False,
        '_mono_clock': lambda: 123.5,
        'command_observations': [],
    })

    def send_success(self, _sensor, **kwargs):
        self._last_writer_publish_mono_s = 123.5
        doubles = (0.0,) * 13 + packet_qdot + (0.0,) * 5
        packet = SimpleNamespace(sequence=17, double_values=doubles)
        self._after_transport_send_success(
            packet,
            command_mode=kwargs['command_mode'],
            reference_phase=kwargs['reference_phase'],
            reference_time_s=kwargs['reference_time_s'],
            published_at_s=123.5,
        )
        return packet

    def send_failure(self, _sensor, **_kwargs):
        raise OSError('synthetic packet send failure')

    monkeypatch.setattr(
        R006LiveWriter, '_send_packet',
        send_failure if send_fails else send_success,
    )
    send = lambda: writer._send_packet(
        None,
        command_mode=CommandMode.PATH,
        proposed_qdot=packet_qdot,
        reference_phase='path',
        reference_time_s=.25,
    )
    if send_fails:
        with pytest.raises(OSError, match='synthetic packet send failure'):
            send()
        assert provider.replay_evidence.count == 0
        assert writer._host_formal_path_publish_count == 0
    else:
        packet = send()
        assert packet.sequence == 17
        assert provider.replay_evidence.count == 1
        assert writer._host_path_publish_count == 1
        assert writer._host_formal_path_publish_count == 1
        row = next(provider.replay_evidence.iter_rows())
        assert row['packet_sequence'] == 17
        assert row['reference_phase'] == 'path'
        assert row['reference_time_s'] == pytest.approx(.25)
        assert row['host_monotonic_s'] == pytest.approx(.002)
        assert row['actual_dt_s'] == pytest.approx(.002)
        assert row['published_packet_qdot_rad_s'] == pytest.approx(packet_qdot)
        assert row['solver_qdot_lower_rad_s'] == pytest.approx(
            provider.runtime.last_solver_qdot_lower
        )
        assert row['solver_qdot_upper_rad_s'] == pytest.approx(
            provider.runtime.last_solver_qdot_upper
        )
        np.testing.assert_allclose(
            row['jacobian_6x6'], provider.last_result['jacobian_6x6']
        )
        assert row['previous_published_qdot_rad_s'] == pytest.approx((0.0,) * 6)
        assert row['host_slew_scale'] == pytest.approx(
            provider.last_result['host_slew_scale']
        )
        slew_delta = row['host_slew_delta_limit_rad_s']
        np.testing.assert_allclose(
            row['slew_adjusted_qdot_lower_rad_s'],
            np.maximum(provider.runtime.last_solver_qdot_lower, -slew_delta),
        )
        np.testing.assert_allclose(
            row['slew_adjusted_qdot_upper_rad_s'],
            np.minimum(provider.runtime.last_solver_qdot_upper, slew_delta),
        )
        assert row['requested_outer_twist_m_s_rad_s'] == pytest.approx(
            provider.last_result['outer_output_feedback_pending'][
                'task_space_command_twist'
            ]
        )
        assert row['solver_elapsed_s'] >= 0.0
        assert row['provider_elapsed_s'] > 0.0
        assert row['transition_events'][0]['event_type'] == 'path_origin'


def test_replay_trace_fails_closed_when_history_fails_after_transport_success(
    provider, monkeypatch
):
    from types import SimpleNamespace

    import step5d_autotune_v4_r004_live_writer as base_writer_module
    from contact_yield_live_writer import NativeYieldLiveWriter
    from step5d_autotune_v4_r004.wire import CommandMode

    provider.reset_replay_evidence(9)
    provider.bind_command_history((0.0,) * 6, 15.0)
    output, sensor = tick(provider, .002, force=1.0)
    command = provider.execution_command(
        output=output, sensor=sensor, monotonic_s=.002, actual_dt_s=.002,
        mode='path', path_time_s=1.25, internal_setpoint_n=1.0,
    )
    packet_qdot = tuple(command.qdot)
    sent = []

    class FakeTransport:
        def send_packet(self, doubles, integers):
            sent.append((doubles, integers))

    class FailingHistory:
        def record(self, *args, **kwargs):
            raise RuntimeError('synthetic post-send history failure')

    packet = SimpleNamespace(
        sequence=17,
        double_values=(0.0,) * 13 + packet_qdot + (0.0,) * 5,
        integer_values=(0,) * 10,
    )
    monkeypatch.setattr(
        base_writer_module, 'build_wire_packet', lambda *args, **kwargs: packet
    )
    writer = object.__new__(NativeYieldLiveWriter)
    writer.__dict__.update({
        '_qualification_control': SimpleNamespace(contact_command_provider=provider),
        '_controller_transport': FakeTransport(),
        '_kunwei_transport': object(),
        'contract': object(),
        'candidate': object(),
        '_packet_sequence': 17,
        '_session_input': lambda _mode: object(),
        '_hot_path_mark': lambda *args, **kwargs: None,
        '_packet_history': FailingHistory(),
        '_stopped': False,
        '_host_path_publish_count': 0,
        '_host_formal_path_publish_count': 0,
        '_path_timing_stats': None,
        '_last_writer_publish_mono_s': None,
        '_r013_path_early_end_controller': None,
        '_service_mode': False,
        '_mono_clock': lambda: 123.5,
        'command_observations': [],
    })
    with pytest.raises(RuntimeError, match='synthetic post-send history failure'):
        writer._send_packet(
            sensor,
            command_mode=CommandMode.PATH,
            proposed_qdot=packet_qdot,
            reference_phase='path',
            reference_time_s=.25,
        )

    assert len(sent) == 1
    assert writer._host_path_publish_count == 1
    assert writer._host_formal_path_publish_count == 1
    assert provider.replay_evidence.count == 0
    provider.replay_evidence.set_expected_count(
        writer._host_formal_path_publish_count
    )
    status = provider.replay_evidence.validate_published_packets([])
    assert status['complete'] is False
    assert 'published_tick_count_mismatch' in status['failure_reasons']


def test_tase_replay_trace_captures_force_preempt_state_after_warm_start(provider):
    provider.reset_replay_evidence(4)
    provider.bind_command_history((0.0,) * 6, 15.0)
    output, sensor = tick(provider, .002, force=8.0)
    sensor = replace(sensor, normal_load_n=8.0, force_norm_n=8.0)
    command = provider.execution_command(
        output=output, sensor=sensor, monotonic_s=.002, actual_dt_s=.002,
        mode='path', path_time_s=1.0, internal_setpoint_n=1.0,
    )
    assert provider.last_result['force_preempt_warm_start'] is True
    provider.confirm_published_packet(
        command.qdot, packet_sequence=28, published_at_s=.004,
        reference_phase='path', reference_time_s=0.0,
    )
    events = next(provider.replay_evidence.iter_rows())['transition_events']
    assert [event['event_type'] for event in events][:2] == [
        'path_origin', 'force_preempt_warm_start',
    ]
    assert events[1]['event_identity'] == 1
    assert len(events[1]['lambda_state']) == 6
    assert len(events[1]['theta_dot_state']) == 6


def test_tase_replay_trace_attempt_reset_discards_prior_sequences(provider):
    capture = provider.replay_evidence
    capture.reset_attempt(2)
    capture.begin_command()
    capture.stage_sample(
        host_monotonic_s=1.0, actual_dt_s=.002,
        desired_twist=(0.0,) * 6, jacobian=np.eye(6),
        solver_lower=(-.15,) * 6, solver_upper=(.15,) * 6,
        previous_qdot=(0.0,) * 6, host_slew_scale=1.0,
        host_slew_delta_limit=.03, packet_qdot=(0.0,) * 6,
        solver_elapsed_s=.0001,
    )
    capture.set_provider_elapsed(.0002)
    capture.commit_published(
        packet_sequence=81, published_at_s=1.001,
        reference_phase='path', reference_time_s=4.0,
        packet_qdot=(0.0,) * 6,
    )
    assert capture.count == 1

    capture.reset_attempt(3)
    assert capture.attempt_sequence == 3
    assert capture.count == capture.transition_count == 0
    assert capture.expected_count is None
    assert capture._last_packet_sequence == -1
    assert list(capture.iter_rows()) == []


def test_tase_replay_trace_indexes_complete_metric_window_and_excludes_sixty():
    from tase_contact_provider import _TaseReplayEvidenceBuffer

    capture = _TaseReplayEvidenceBuffer(capacity=30_000, path_duration_s=60.0)
    capture.reset_attempt(1)
    zero = (0.0,) * 6
    identity = np.eye(6)
    lower, upper = (-.15,) * 6, (.15,) * 6
    for index in range(30_000):
        reference_time = index * .002
        capture.begin_command()
        capture.stage_sample(
            host_monotonic_s=100.0 + reference_time,
            actual_dt_s=.002,
            desired_twist=zero,
            jacobian=identity,
            solver_lower=lower,
            solver_upper=upper,
            previous_qdot=zero,
            host_slew_scale=1.0,
            host_slew_delta_limit=.03,
            packet_qdot=zero,
            solver_elapsed_s=.0001,
        )
        capture.set_provider_elapsed(.0002)
        capture.commit_published(
            packet_sequence=index,
            published_at_s=100.001 + reference_time,
            reference_phase='path',
            reference_time_s=reference_time,
            packet_qdot=zero,
        )
    assert capture.count == 30_000
    metric_times = capture.samples[:capture.count, 0]
    metric_window = metric_times[(metric_times >= 5.0) & (metric_times < 60.0)]
    assert len(metric_window) == 27_500
    assert metric_window[0] == pytest.approx(5.0)
    assert metric_window[-1] == pytest.approx(59.998)
    capture.begin_command()
    capture.stage_sample(
        host_monotonic_s=160.0, actual_dt_s=.002,
        desired_twist=zero, jacobian=identity,
        solver_lower=lower, solver_upper=upper,
        previous_qdot=zero, host_slew_scale=1.0,
        host_slew_delta_limit=.03, packet_qdot=zero,
        solver_elapsed_s=.0001,
    )
    capture.set_provider_elapsed(.0002)
    capture.commit_published(
        packet_sequence=30_000, published_at_s=160.001,
        reference_phase='path', reference_time_s=60.0,
        packet_qdot=zero,
    )
    assert capture.count == 30_000


def test_tase_replay_trace_overflow_is_explicit_evidence_failure():
    from tase_contact_provider import _TaseReplayEvidenceBuffer

    capture = _TaseReplayEvidenceBuffer(capacity=1, path_duration_s=60.0)
    capture.reset_attempt(1)
    zero = (0.0,) * 6
    identity = np.eye(6)
    lower, upper = (-.15,) * 6, (.15,) * 6
    for sequence in (1, 2):
        capture.begin_command()
        capture.stage_sample(
            host_monotonic_s=float(sequence), actual_dt_s=.002,
            desired_twist=zero, jacobian=identity,
            solver_lower=lower, solver_upper=upper,
            previous_qdot=zero, host_slew_scale=1.0,
            host_slew_delta_limit=.03, packet_qdot=zero,
            solver_elapsed_s=.0001,
        )
        capture.set_provider_elapsed(.0002)
        capture.commit_published(
            packet_sequence=sequence, published_at_s=float(sequence),
            reference_phase='path', reference_time_s=(sequence - 1) * .002,
            packet_qdot=zero,
        )
    capture.set_expected_count(2)
    status = capture.validate_published_packets([
        (1.0, {'sequence': 1}), (2.0, {'sequence': 2}),
    ])
    assert capture.count == 1
    assert capture.dropped_count == 1
    assert status['complete'] is False
    assert 'tick_capacity_overflow' in status['failure_reasons']


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


def test_live_solver_profile_is_qp_only_for_tase_qp(tmp_path):
    from contact_qp import QpSolverProfile
    from contact_yield_live_writer import (
        YieldLiveWriterError,
        _select_tase_solver_profile,
        _validate_tase_qp_starting_profile,
    )
    from types import SimpleNamespace

    library = tmp_path / 'libcontact_qp.so'
    library.write_bytes(b'profile identity fixture')
    qp = _select_tase_solver_profile(family='tase_qp', library=library)
    rnn = _select_tase_solver_profile(family='tase_mature', library=library)
    assert isinstance(qp, QpSolverProfile)
    assert qp.as_dict()['backend'] == 'osqp-codegen-c'
    assert qp.qdot_limit_rad_s == pytest.approx(.05)
    assert qp.deadline_s == pytest.approx(.001)
    assert rnn is LEGACY_R1
    with pytest.raises(YieldLiveWriterError, match='unsupported live TASE family'):
        _select_tase_solver_profile(family='tase_rnn', library=library)
    expected_parameters = SimpleNamespace(
        Md_scalar=8.592659656558919,
        Bd_scalar=772.1473715259434,
        kp=4.0,
        ko=5.0,
        kf=1.0,
        force_target_n=5.0,
        force_integral_limit_n_s=.5,
        force_integral_policy='legacy-clamp-v1',
        force_integral_authority_error_n=.5,
    )
    _validate_tase_qp_starting_profile(
        parameter_file=library,
        outer_config=expected_parameters,
        parameter_binding={'candidate_id': 'bo-10', 'stage': 'bo'},
    )
    with pytest.raises(YieldLiveWriterError, match='explicit sealed bo-10'):
        _validate_tase_qp_starting_profile(
            parameter_file=None,
            outer_config=expected_parameters,
            parameter_binding={'candidate_id': 'bo-10', 'stage': 'bo'},
        )


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


def test_r013_packet_reference_matches_bounded_evaluated_reference():
    p = TaseContactProvider(contract=current_model_binding(), candidate=V4Candidate(),
        motion_profile=native_motion_profile(), home_pose=load_identity_contract().home_pose,
        solver_profile=LEGACY_R1, protocol_id=R013_COMPAT60_PROTOCOL_ID)
    try:
        o, s = tick(p, .002, force=5.)
        reference = p.formal_reference(60.004)
        o.tcp_pose_m_rad = (*reference['position_m'], *load_identity_contract().home_pose[3:])
        p.command(output=o, sensor=s, monotonic_s=.002, actual_dt_s=.002,
                  mode='path', path_time_s=61.008, internal_setpoint_n=5.)
        assert p.last_result['formal_time_s'] == 60.004
        assert p.last_result['host_formal_time_s'] == pytest.approx(60.008)
        p.formal_reference(p.last_result['formal_time_s'])
        for invalid in (float('nan'), float('inf'), -1., 60.005):
            with pytest.raises(ValueError, match='bounded protocol seam'):
                p.formal_reference(invalid)
    finally:
        p.close()


def test_provider_restore_rolls_back_freshness_without_history_copy(provider):
    p = provider
    seed = p.snapshot()
    o, s = tick(p, .002)
    p.command(output=o, sensor=s, monotonic_s=.002, actual_dt_s=.002,
              mode='baseline', internal_setpoint_n=1.)
    assert p.freshness.as_dict()['observation_count'] == 1
    p.restore(seed)
    assert p.freshness.as_dict()['observation_count'] == 0
    assert p.snapshot() == seed
