from pathlib import Path
from types import SimpleNamespace
import sys
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from contact_laws import ContactLaw
from contact_benchmark_provider import ContactCommandProvider
from step5d_autotune_v4_r004.wire import SensorPacket
from test_contact_benchmark_runtime import lib,fixture


def inputs(robot):
    output=SimpleNamespace(observed_at_s=robot['observed_at_s'],timestamp=robot['timestamp'],
        safety_mode=1,safety_normal=True,tcp_speed_m_s_rad_s=[0.]*6,tcp_offset_m_rad=robot['tcp_offset'],payload_kg=robot['payload'],
        payload_cog_m=robot['payload_cog'],q_rad=robot['actual_q'],qd_rad_s=robot['actual_qd'],
        tcp_pose_m_rad=robot['actual_TCP_pose'])
    sensor=SensorPacket(normal_load_n=5.,force_norm_n=5.,heartbeat=1.,sensor_fresh=True,
        stop_request=False,eoat_get_ack=True,torque_norm_nm=0.,wrench=(0.,0.,-5.,0.,0.,0.),
        filtered_normal_n=5.,observed_at_s=100.)
    return output,sensor


def test_provider_maps_measured_packet_to_qp_without_historical_outer(lib):
    with ContactLaw.from_config('LAC') as law:
        runtime,robot=fixture(law,lib);output,sensor=inputs(robot)
        provider=ContactCommandProvider(runtime=runtime,model_hashes={'calibration':runtime.model.calibration_hash})
        command=provider.command(output=output,sensor=sensor,monotonic_s=100.,actual_dt_s=.002,
            mode='path',path_time_s=0.,internal_setpoint_n=5.)
        assert command.qdot==provider.last_result['qdot_rad_s']
        assert provider.last_result['filtered_normal_n']==pytest.approx(5.)
        assert provider.solver_profile.as_dict()['backend']=='osqp-codegen-c'
        xy,omega=provider.path_errors(actual_tcp_pose=output.tcp_pose_m_rad,path_time_s=0.,motion_kp=4.)
        np.testing.assert_allclose(xy,[0,0],atol=1e-12)
        np.testing.assert_allclose(omega,[0,0,0],atol=1e-12)


def test_provider_rejects_missing_sensor_timestamp_and_unbound_pause(lib):
    from dataclasses import replace
    with ContactLaw.from_config('MSFC') as law:
        runtime,robot=fixture(law,lib);output,sensor=inputs(robot)
        provider=ContactCommandProvider(runtime=runtime,model_hashes={})
        args=dict(output=output,sensor=sensor,monotonic_s=100.,actual_dt_s=.002,
                  mode='baseline',path_time_s=0.,internal_setpoint_n=5.)
        with pytest.raises(ValueError,match='timestamped'):provider.command(**{**args,'sensor':replace(sensor,observed_at_s=None)})
        provider.command(**args)
        before=runtime.snapshot()
        with pytest.raises(ValueError,match='discontinuity'):provider.command(**{**args,'monotonic_s':100.01})
        assert before==runtime.snapshot()


def test_stationary_seam_carries_complete_msfc_state_without_clock_gap(lib):
    from dataclasses import replace
    with ContactLaw.from_config('MSFC') as law:
        runtime,robot=fixture(law,lib);output,sensor=inputs(robot)
        provider=ContactCommandProvider(runtime=runtime,model_hashes={})
        provider.command(output=output,sensor=sensor,monotonic_s=100.,actual_dt_s=.002,
                         mode='baseline',path_time_s=0.,internal_setpoint_n=1.)
        kernel_before=runtime.kernel.snapshot()
        for i in range(1,6):
            now=100.+i*.002;output.observed_at_s=now;output.timestamp+=.002
            provider.pause(output=output,sensor=replace(sensor,observed_at_s=now),monotonic_s=now,
                           actual_dt_s=.002,reason='r013_tp_stationary_seam_pending')
            assert runtime.kernel.snapshot()==kernel_before
        assert runtime.paused_s==pytest.approx(.01)
        now=100.012;output.observed_at_s=now;output.timestamp+=.002
        provider.command(output=output,sensor=replace(sensor,observed_at_s=now),monotonic_s=now,
                         actual_dt_s=.002,mode='path',path_time_s=0.,internal_setpoint_n=5.)
        with pytest.raises(ValueError,match='active PATH'):
            provider.pause(output=output,sensor=replace(sensor,observed_at_s=100.014),monotonic_s=100.014,
                           actual_dt_s=.002,reason='unexpected')


def test_readiness_observer_uses_common_tau_once_without_pid_state():
    import math
    from contact_benchmark_provider import ContactReadinessObserver
    observer=ContactReadinessObserver(.02)
    observer.step(actual_dt_s=.002,raw_normal_n=1.,setpoint_n=1.,mode='baseline')
    result=observer.step(actual_dt_s=.004,raw_normal_n=5.,setpoint_n=5.,mode='path')
    assert result.filtered_normal_n==pytest.approx(1.+4.*(1.-math.exp(-.004/.02)))
    assert result.role=='readiness_observation_only'
    before=observer.last_log
    with pytest.raises(ValueError):
        observer.step(actual_dt_s=.01,raw_normal_n=5.,setpoint_n=5.,mode='path')
    assert observer.last_log==before
    assert not hasattr(observer,'integral_error_n_s')
