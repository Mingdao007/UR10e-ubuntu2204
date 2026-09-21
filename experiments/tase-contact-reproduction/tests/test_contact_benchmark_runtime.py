from pathlib import Path
import json,sys
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from contact_laws import ContactLaw
from build_contact_qp import build
from contact_benchmark_runtime import ContactRuntime, validate_measured_observation
from contact_benchmark_kernel import KernelDeadlineError
from step5c_calibrated_kinematics_audit import rotvec_to_matrix

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module')
def lib(tmp_path_factory):return build(tmp_path_factory.mktemp('runtime-qp'))

def fixture(law,lib):
    receipt=json.loads((ROOT/'report/contact-six-qp-20260917/preserved-home.json').read_text())
    robot=dict(receipt['rtde']);robot.update(actual_q=receipt['home_q'],actual_qd=[0.]*6,
        actual_TCP_pose=receipt['home_pose'],observed_at_s=100.,timestamp=1234.,safety_status_bits=2049)
    runtime=ContactRuntime(law=law,qp_library=lib,anchor_m=receipt['home_pose'][:3],
        task_basis=np.eye(3),target_rotation=rotvec_to_matrix(np.array(receipt['home_pose'][3:])),deadline_s=None)
    return runtime,robot


def test_measured_frame_and_actual_jacobian_produce_contact_command(lib):
    with ContactLaw.from_config('LAC') as law:
        runtime,robot=fixture(law,lib)
        out=runtime.step(robot=robot,wrench_tcp=[0,0,-5,0,0,0],sensor_observed_at_s=100.,
                         sample_time_s=100.,phase='path',path_time_s=0.)
        assert out['normal_load_n']==pytest.approx(5.)
        assert abs(out['twist_base'][2])<1e-12
        assert len(out['qdot_rad_s'])==6 and max(abs(v) for v in out['qdot_rad_s'])<.05
        assert out['qp_equality_residual']<1e-7


def test_sensor_age_held_is_admitted_but_stale_raw_force_and_tool_binding_reject(lib):
    with ContactLaw.from_config('MSFC') as law:
        runtime,robot=fixture(law,lib)
        args=dict(robot=robot,wrench_tcp=[0,0,-5,0,0,0],sensor_observed_at_s=100.,sample_time_s=100.,phase='baseline')
        held=runtime.step(**{**args,'sensor_observed_at_s':99.97})
        assert held['age_band']=='held'
        assert held['observation_age_s']==pytest.approx(.03)
        assert runtime.freshness_summary()['held_count']==1
        before=runtime.snapshot()
        robot['observed_at_s']=100.003
        robot['timestamp']=1234.001
        args={**args,'sample_time_s':100.003,'sensor_observed_at_s':100.003}
        with pytest.raises(ValueError,match='raw sensor'):runtime.step(**{**args,'wrench_tcp':[0,0,-21,0,0,0]})
        robot['payload']=1.56
        with pytest.raises(ValueError,match='tool'):runtime.step(**args)
        assert runtime.snapshot()==before

        with ContactLaw.from_config('MSFC') as stale_law:
            stale_runtime,stale_robot=fixture(stale_law,lib)
            stale_args=dict(robot=stale_robot,wrench_tcp=[0,0,-5,0,0,0],
                             sensor_observed_at_s=99.9,sample_time_s=100.,phase='baseline')
            with pytest.raises(ValueError,match='older than 80ms'):
                stale_runtime.step(**stale_args)
            assert stale_runtime.freshness_summary()['stale_stop_count']==1


def test_complete_adapter_deadline_rolls_back_state_and_clocks(lib):
    with ContactLaw.from_config('MSFC') as law:
        runtime,robot=fixture(law,lib);runtime.deadline_s=1e-12;before=runtime.snapshot()
        with pytest.raises(KernelDeadlineError):
            runtime.step(robot=robot,wrench_tcp=[1,0,-1,0,0,0],sensor_observed_at_s=100.,
                         sample_time_s=100.,phase='baseline',force_reference_n=1.)
        assert runtime.snapshot()==before


def test_actual_joint_speed_rejection_keeps_pairing_diagnostics(lib):
    with ContactLaw.from_config('MSFC') as law:
        runtime, robot = fixture(law, lib)
        robot['actual_qd'] = [0.0, 0.061, 0.0, -0.02, 0.0, 0.0]
        with pytest.raises(ValueError, match='observed joint speed exceeds envelope') as caught:
            validate_measured_observation(
                robot=robot,
                wrench_tcp=[0, 0, -5, 0, 0, 0],
                sensor_observed_at_s=100.0,
                sample_time_s=100.0,
                last_sample_s=None,
                last_controller_timestamp=None,
                last_sensor_timestamp=None,
                freshness=runtime.freshness,
                model=runtime.model,
                diagnostic_context={
                    'consumed_packet_sequence': 42877,
                    'paired_command_qdot_rad_s': (0.0,) * 6,
                },
            )
        message = str(caught.value)
        assert '"actual_qd_rad_s":[0.0,0.061,0.0,-0.02,0.0,0.0]' in message
        assert '"consumed_packet_sequence":42877' in message
        assert '"paired_command_qdot_rad_s":[0.0,0.0,0.0,0.0,0.0,0.0]' in message
