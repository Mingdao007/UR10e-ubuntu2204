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
        safety_mode=1,safety_normal=True,tcp_offset_m_rad=robot['tcp_offset'],payload_kg=robot['payload'],
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
