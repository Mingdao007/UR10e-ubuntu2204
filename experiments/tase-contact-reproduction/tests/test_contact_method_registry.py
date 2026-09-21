import copy
import json
from pathlib import Path
import numpy as np
import pytest
from contact_method_registry import MethodRegistry,MethodSpec,RegistryError,default_registry


def observations():
    return {'time_s':0.,'state_age_s':.001,'position_m':[0.]*3,'rotation':np.eye(3),
        'joint_position_rad':[0.]*6,'jacobian':np.eye(6),'raw_force_base_n':[0.,0.,5.],
        'raw_torque_base_nm':[0.]*3,'joint_velocity_lower':[-.05]*6,
        'joint_velocity_upper':[.05]*6,'linear_velocity_base_m_s':[0.]*3,
        'angular_velocity_base_rad_s':[0.]*3}


class Extension:
    def __init__(self): self.value=0
    def snapshot(self): return {'value':self.value}
    def restore(self,state): self.value=state['value']
    def step(self,observation,reference,dt):
        self.value += 1
        return {'qdot_rad_s':[float('nan') if reference.get('fail') else .001]*6}


def test_open_registration_complete_replay_and_failed_result_rollback():
    registry=MethodRegistry();spec=MethodSpec('NEW_CONTROLLER','candidate','test_extension')
    registry.register(spec,lambda:(Extension(),'extension'))
    with pytest.raises(RegistryError,match='already'):registry.register(spec,lambda:None)
    controller=registry.initialize('NEW_CONTROLLER');obs=observations()
    before=json.loads(json.dumps(controller.snapshot()))
    first=controller.step(obs,{'reference_force_n':5.},.002)
    controller.restore(before)
    assert first==controller.step(obs,{'reference_force_n':5.},.002)
    state=controller.snapshot();obs['time_s']=.002
    with pytest.raises(ValueError):controller.step(obs,{'fail':True,'reference_force_n':5.},.002)
    assert controller.snapshot()==state
    controller.stop()
    with pytest.raises(RegistryError,match='stopped'):controller.step(obs,{'reference_force_n':5.},.002)
    controller.restore(state)
    assert controller.step(obs,{'reference_force_n':5.},.002)['qdot_rad_s']==[.001]*6


def test_evaluator_truth_is_not_forwarded_to_any_method():
    registry=MethodRegistry();registry.register(MethodSpec('x','test','extension'),lambda:(Extension(),'extension'))
    controller=registry.initialize('x');obs=observations();obs['true_normal']=[0,0,1]
    with pytest.raises(RegistryError,match='evaluator truth'):controller.step(obs,{'reference_force_n':5.},.002)
    assert controller.backend.value==0


def test_registry_names_keep_tase_sign_adaptation_separate():
    rows={r['name']:r for r in default_registry().describe()}
    assert set(rows)=={'SFC','SFC_RADIAL','DSFC','MSFC','TASE_RNN','TASE_RNN_MATURE','TASE_RNN_MATURE_MINUS','TASE_QP','TASE_IMPROVED'}
    assert 'printed_sign' in rows['TASE_RNN']['role']
    assert 'adaptation' in rows['TASE_RNN_MATURE_MINUS']['role']
    assert all(r['qualification']=='software_only' for r in rows.values())


@pytest.mark.parametrize('name',['SFC','SFC_RADIAL','DSFC','MSFC'])
def test_real_native_methods_share_input_and_replay_complete_state(name):
    root=Path(__file__).resolve().parents[1]
    controller=default_registry().initialize(name,qp_library=root/'build/contact-qp/libcontact_qp.so')
    obs=observations();ref={'position_m':[0.]*3,'velocity_m_s':[0.]*3,
        'reference_force_n':5.,'phase':'path','path_time_s':0.}
    try:
        state=json.loads(json.dumps(controller.snapshot()))
        first=controller.step(obs,ref,.002)
        controller.restore(state)
        second=controller.step(obs,ref,.002)
        assert first['qdot_rad_s']==second['qdot_rad_s']
        assert first['reference_force_n']==5.
        assert first['hardware_evidence'] is False
        after=controller.snapshot()
        obs['time_s']=.002;ref['path_time_s']=.002;ref['reference_force_n']=4.
        with pytest.raises(Exception,match='5 N'):controller.step(obs,ref,.002)
        assert controller.snapshot()==after
    finally:
        controller.close()


def test_out_of_bounds_extension_rolls_back_without_clipping():
    class Violating(Extension):
        def step(self,obs,ref,dt):
            self.value += 1
            return {'qdot_rad_s':[.06]*6}
    registry=MethodRegistry()
    registry.register(MethodSpec('bad','candidate','test'),lambda:(Violating(),'extension'))
    handle=registry.initialize('bad');state=handle.snapshot()
    with pytest.raises(RegistryError,match='joint velocity bounds'):
        handle.step(observations(),{'reference_force_n':5.},.002)
    assert handle.snapshot()==state
