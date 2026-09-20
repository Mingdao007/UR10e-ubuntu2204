"""Instance-local path/Home binding for the mature TASE outer loop."""
from types import SimpleNamespace
import math
import numpy as np
import pytest
import step5d_autotune_v4_r004.calibrated_runtime as module
from contact_yield_protocol import PERIOD_S
from step6_figure8_autotune_v1.v5_live_runtime import make_v5_runtime_path_reference


def runtime_at(pose):
    runtime = object.__new__(module.V4CalibratedRuntime)
    runtime.path_reference = make_v5_runtime_path_reference(pose)
    runtime.target_rotvec = np.asarray(pose[3:])
    runtime._feedforward_enabled = False
    return runtime


def test_current_home_attitude_does_not_restore_legacy_attitude():
    pose = (.4620551816, .1778825964, .033, 2.03313, 2.39499, 0.)
    runtime = runtime_at(pose)
    xy, angle = runtime.path_errors(actual_tcp_pose=pose, path_time_s=0., motion_kp=2.)
    np.testing.assert_allclose(xy, 0., atol=1e-12)
    np.testing.assert_allclose(angle, 0., atol=1e-12)


def test_full_period_closes_and_independent_anchor_does_not_leak():
    first = (.4620551816, .1778825964, .033, 2.03313, 2.39499, 0.)
    second = (.40, .10, .033, 0., math.pi, 0.)
    a, b = runtime_at(first), runtime_at(second)
    for runtime, pose in ((a, first), (b, second)):
        xy, angle = runtime.path_errors(actual_tcp_pose=pose, path_time_s=PERIOD_S, motion_kp=2.)
        np.testing.assert_allclose(xy, 0., atol=1e-12)
        np.testing.assert_allclose(angle, 0., atol=1e-12)
    xy, _ = a.path_errors(actual_tcp_pose=second, path_time_s=0., motion_kp=2.)
    np.testing.assert_allclose(xy, np.asarray(first[:2])-np.asarray(second[:2]))


def test_outer_loop_uses_same_bound_figure8_reference(monkeypatch):
    pose = (.4620551816, .1778825964, .033, 2.03313, 2.39499, 0.)
    runtime = runtime_at(pose)
    runtime._feedforward_enabled = True
    runtime.internal_setpoint_bounds_n = (1., 5.)
    runtime.candidate = SimpleNamespace(motion_kp=2., orientation_ko=1.)
    runtime.force_integral_limit_n_s = 1.
    runtime.force_integral_policy = module.LEGACY_FORCE_INTEGRAL_POLICY
    runtime.force_integral_authority_error_n = .5
    runtime.force_normal_velocity_limit_m_s = .003
    runtime._outer_state = module.Step5dOuterLoopState()
    runtime.motion_profile = None
    captured = []
    monkeypatch.setattr(module, 'derive_force_terms', lambda _: {'kf':1.,'Md':1.,'Bd':1.})
    def outer(config, state, inputs, **kwargs):
        captured.append(inputs)
        return SimpleNamespace(next_state=state, xdot_c=(0.,)*6)
    monkeypatch.setattr(module, 'compute_step5d_outer_loop', outer)
    runtime.desired_twist(actual_tcp_pose=pose, actual_tcp_speed=(0.,)*6,
        force_tcp_n=(0.,0.,-5.), filtered_normal_n=5., internal_setpoint_n=5.,
        actual_dt_s=.002, mode='path', path_time_s=15.)
    reference = runtime.path_reference(module.PATH_STAGE_ID, pose[:2], 15.)
    np.testing.assert_allclose(captured[0].x_pd_base[:2], reference['desired_xy'])
    np.testing.assert_allclose(captured[0].xdot_pd_base[:2], reference['desired_velocity_xy'])
