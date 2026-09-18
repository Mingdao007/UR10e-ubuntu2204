from pathlib import Path
import sys
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from build_contact_qp import build
from contact_laws import ContactLaw
from contact_qp import QpError
from contact_benchmark_kernel import ContactKernel

@pytest.fixture(scope='module')
def lib(tmp_path_factory):return build(tmp_path_factory.mktemp('kernel-qp'))

def make(lib,law):
    return ContactKernel(law=law,qp_library=lib,anchor_m=[0,0,0],task_basis=np.eye(3),
        target_rotation=np.diag([1.,-1.,-1.]),raw_force_limit_n=20.,raw_torque_limit_nm=2.,qp_deadline_s=None,kernel_deadline_s=None)

def tick(k,t=0.,limit=.05):
    return k.step(jacobian=np.eye(6),joint_velocity_lower=np.full(6,-limit),joint_velocity_upper=np.full(6,limit),
        time_s=t,dt_s=.002,position_m=[0,0,0],rotation=np.diag([1.,-1.,-1.]),raw_force_base_n=[0,0,5],raw_torque_base_nm=[0,0,0])

def test_qp_rejection_restores_law_memory_and_outer(lib):
    with ContactLaw.from_config('MSFC') as law:
        k=make(lib,law);before=k.snapshot()
        with pytest.raises(QpError):tick(k,limit=.000001)
        assert k.snapshot()==before
        assert tick(k)['controller']=='MSFC'

def test_composed_snapshot_exact_replay(lib):
    with ContactLaw.from_config('MSFC') as law:
        k=make(lib,law);tick(k);snap=k.snapshot();a=tick(k,.002)
        k.restore(snap);b=tick(k,.002)
        np.testing.assert_allclose(a['qdot_rad_s'],b['qdot_rad_s'],atol=1e-10)
        assert a['law_velocity_task_m_s']==b['law_velocity_task_m_s']

def test_kernel_identity_refuses_cross_controller_restore(lib):
    with ContactLaw.from_config('LAC') as a,ContactLaw.from_config('MSFC') as b:
        ka,kb=make(lib,a),make(lib,b)
        with pytest.raises(ValueError,match='identity'): kb.restore(ka.snapshot())


def test_full_kernel_deadline_rejects_and_restores_complete_state(lib):
    from contact_benchmark_kernel import KernelDeadlineError
    with ContactLaw.from_config('MSFC') as law:
        k=make(lib,law);k.kernel_deadline_s=1e-12;before=k.snapshot()
        with pytest.raises(KernelDeadlineError):tick(k)
        assert k.snapshot()==before


def test_jittered_baseline_path_replay_preserves_msfc_memory(lib):
    with ContactLaw.from_config('MSFC') as law:
        k=make(lib,law)
        common=dict(jacobian=np.eye(6),joint_velocity_lower=np.full(6,-.05),joint_velocity_upper=np.full(6,.05),
                    position_m=[0,0,0],rotation=np.diag([1.,-1.,-1.]),raw_torque_base_nm=[0,0,0])
        k.step(time_s=70.,dt_s=.0015,phase='baseline',force_reference_n=1.,raw_force_base_n=[1,0,1],**common)
        snap=k.snapshot()
        a=k.step(time_s=70.003,dt_s=.003,phase='path',path_time_s=0.,raw_force_base_n=[1,0,5],**common)
        k.restore(snap)
        b=k.step(time_s=70.003,dt_s=.003,phase='path',path_time_s=0.,raw_force_base_n=[1,0,5],**common)
        assert a['law_velocity_task_m_s']==b['law_velocity_task_m_s']
        np.testing.assert_allclose(a['qdot_rad_s'],b['qdot_rad_s'],atol=1e-10)
