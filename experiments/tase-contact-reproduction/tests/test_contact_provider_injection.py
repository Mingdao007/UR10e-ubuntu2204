from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
sys.path.insert(0,str(ROOT.parents[1]/'src/ur10e_experiment_runtime'))
from contact_benchmark_provider import ContactCommandProvider
from yield_contact_provider import YieldContactProvider
from step5d_autotune_v4_r006.live_adapter import _R006ScopedRuntimeInjection,R006LiveAdapterError
from test_contact_benchmark_provider import inputs
from test_contact_benchmark_runtime import lib


def injection(factory):
    inj=_R006ScopedRuntimeInjection(motion_profile=object(),path_reference=lambda:None,
                                    contact_command_provider_factory=factory)
    inj.active=True
    inj._original_control=lambda candidate,**kw:SimpleNamespace(candidate=candidate,**kw)
    inj._patch_path_reference=lambda:None
    return inj


@pytest.mark.parametrize('provider_type',[ContactCommandProvider,YieldContactProvider])
def test_provider_prepared_before_arm_and_consumed_exactly_once(provider_type):
    provider=object.__new__(provider_type);calls=[]
    inj=injection(lambda **kw:(calls.append(kw) or provider))
    control=inj.prepare_control(candidate=object(),attempt_id='a',
        release_contract=SimpleNamespace(raw={'program':'step5d_contact_six_qp_v1'}),
        path_requested=True,canonical_runtime_only=True)
    assert len(calls)==1 and control.contact_command_provider is provider
    key=inj._prepared_key
    assert inj.consume_prepared_control(key) is control
    with pytest.raises(R006LiveAdapterError,match='missing or already consumed'):inj.consume_prepared_control(key)


@pytest.mark.parametrize('provider_type',[ContactCommandProvider,YieldContactProvider])
def test_wrong_package_or_provider_swap_cannot_fall_back_to_pid(provider_type):
    provider=object.__new__(provider_type);inj=injection(lambda **kw:provider)
    args=dict(candidate=object(),attempt_id='a',path_requested=True,canonical_runtime_only=True)
    with pytest.raises(R006LiveAdapterError,match='dedicated TP'):
        inj.prepare_control(**args,release_contract=SimpleNamespace(raw={'program':'old_r013'}))
    control=inj.prepare_control(**args,release_contract=SimpleNamespace(raw={'program':'step5d_contact_six_qp_v1'}))
    key=inj._prepared_key;control.contact_command_provider=None
    with pytest.raises(R006LiveAdapterError,match='changed'):inj.consume_prepared_control(key)
    assert inj._prepared_control is None


def test_unknown_provider_is_rejected_before_prepared_state_exists():
    inj=injection(lambda **kw:SimpleNamespace(command=lambda **args:None))
    with pytest.raises(R006LiveAdapterError,match='different command provider'):
        inj.prepare_control(candidate=object(),attempt_id='a',path_requested=True,
            canonical_runtime_only=True,
            release_contract=SimpleNamespace(raw={'program':'step5d_contact_six_qp_v1'}))
    assert inj._prepared_control is None and inj._prepared_key is None


def _contact_release_contract():
    from copy import deepcopy
    from step5d_autotune_v4_r004.contracts import load_contract
    raw=deepcopy(load_contract().raw)
    raw['program']='step5d_contact_six_qp_v1'
    return SimpleNamespace(raw=raw)


def _native_pose():
    import json
    import numpy as np
    from step5c_calibrated_kinematics_audit import rotvec_to_matrix
    receipt=json.loads((ROOT/'report/contact-six-qp-20260917/preserved-home.json').read_text())
    rotation=rotvec_to_matrix(np.array(receipt['home_pose'][3:]))
    return receipt,rotation


@pytest.mark.parametrize('method',['SFC','DSFC','MSFC'])
def test_unmocked_native_yield_prepare_control_for_each_method(lib,method,monkeypatch):
    from test_contact_yield_provider import call
    from step5d_autotune_v4 import contracts as canonical_contracts
    from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE
    from step5d_autotune_v4_r006.live_adapter import R006Candidate
    from yield_native_route import create_native_yield_provider,create_native_yield_runtime
    monkeypatch.setattr(canonical_contracts,'load_contract',
        lambda **_kwargs:(_ for _ in ()).throw(AssertionError('legacy load_contract called on yield route')))
    receipt,rotation=_native_pose()
    runtime,binding=create_native_yield_runtime(
        method=method,qp_library=lib,anchor_m=receipt['home_pose'][:3],
        task_basis=rotation,approach_inward_base=rotation[:,2],deadline_s=None,
        home_observations={'home_pose':receipt['home_pose'],'home_q':receipt['home_q']})
    provider=create_native_yield_provider(runtime=runtime,binding=binding)
    robot=dict(receipt['rtde']);robot.update(actual_q=receipt['home_q'],actual_qd=[0.]*6,
        actual_TCP_pose=receipt['home_pose'],observed_at_s=100.,timestamp=1234.)
    output,sensor=inputs(robot)
    with runtime:
        call(provider,output,sensor,0)
        before=provider.snapshot()
        inj=_R006ScopedRuntimeInjection(motion_profile=R004_MOTION_PROFILE,
            path_reference=lambda *_args,**_kwargs:None,
            contact_command_provider_factory=lambda **_kwargs:provider)
        inj.activate()
        try:
            control=inj.prepare_control(candidate=R006Candidate(),attempt_id=f'yield-native-{method}',
                release_contract=_contact_release_contract(),path_requested=True,
                canonical_runtime_only=True)
            assert control._runtime is provider
            assert control._contract is binding
            assert control._path_controller is provider.lifecycle_observer
            assert inj.consume_prepared_control(inj._prepared_key) is control
            assert provider.snapshot()==before
        finally:
            inj.deactivate()


def test_yield_provider_without_native_binding_is_rejected_before_prepared_state(lib):
    from test_contact_yield_provider import setup
    from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE
    from step5d_autotune_v4_r006.live_adapter import R006Candidate,_R006NativeCanonicalQualificationControl
    runtime,provider,_,_=setup(lib)
    assert provider.native_binding is None
    inj=_R006ScopedRuntimeInjection(motion_profile=R004_MOTION_PROFILE,
        path_reference=lambda *_args,**_kwargs:None,
        contact_command_provider_factory=lambda **_kwargs:provider)
    inj.active=True
    inj._original_control=_R006NativeCanonicalQualificationControl
    inj._patch_path_reference=lambda:None
    with runtime:
        with pytest.raises(Exception,match='native binding'):
            inj.prepare_control(candidate=R006Candidate(),attempt_id='missing-binding',
                release_contract=_contact_release_contract(),path_requested=True,
                canonical_runtime_only=True)
        assert inj._prepared_control is None and inj._prepared_key is None


@pytest.mark.parametrize('factory',[None,lambda **_kwargs:object.__new__(ContactCommandProvider)])
def test_legacy_routes_still_call_canonical_loader(factory,monkeypatch):
    from step5d_autotune_v4 import contracts as canonical_contracts
    from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE
    from step5d_autotune_v4_r006.live_adapter import R006Candidate,_R006NativeCanonicalQualificationControl
    called=[]
    monkeypatch.setattr(canonical_contracts,'load_contract',
        lambda **kwargs:(called.append(kwargs) or (_ for _ in ()).throw(RuntimeError('legacy loader invoked'))))
    inj=_R006ScopedRuntimeInjection(motion_profile=R004_MOTION_PROFILE,
        path_reference=lambda *_args,**_kwargs:None,
        contact_command_provider_factory=factory)
    inj.active=True
    inj._original_control=_R006NativeCanonicalQualificationControl
    inj._patch_path_reference=lambda:None
    with pytest.raises(Exception,match='legacy loader invoked'):
        inj.prepare_control(candidate=R006Candidate(),attempt_id='legacy-loader',
            release_contract=_contact_release_contract(),path_requested=True,
            canonical_runtime_only=True)
    assert called and inj._prepared_control is None and inj._prepared_key is None
