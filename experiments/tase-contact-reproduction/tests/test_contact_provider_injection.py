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


def test_real_yield_provider_prepared_state_with_explicit_contract_fixture(lib,monkeypatch):
    from copy import deepcopy
    from test_contact_yield_provider import setup,call
    from step5d_autotune_v4_r004.contracts import load_contract
    from step5d_autotune_v4 import contracts as canonical_contracts
    from step5d_autotune_v4_r006.live_adapter import R006Candidate
    from step6_figure8_autotune_v1.live_composition import figure8_motion_profile
    runtime,provider,output,sensor=setup(lib)
    with runtime:
        call(provider,output,sensor,0)
        before=provider.snapshot()
        # Fixture contract projection only; it is never admitted to a device.
        raw=deepcopy(load_contract().raw)
        raw['program']='step5d_contact_six_qp_v1'
        # Isolate preparation from legacy source-closure admission, which is
        # diagnosed separately and must still pass before any live execution.
        monkeypatch.setattr(canonical_contracts,'load_contract',
            lambda **_kwargs:SimpleNamespace(model_hashes=dict(provider.model_hashes)))
        inj=_R006ScopedRuntimeInjection(motion_profile=figure8_motion_profile(),
            path_reference=lambda *_args,**_kwargs:None,
            contact_command_provider_factory=lambda **_kwargs:provider)
        inj.activate()
        try:
            control=inj.prepare_control(candidate=R006Candidate(),attempt_id='yield-prepared-test',
                release_contract=SimpleNamespace(raw=raw),path_requested=True,
                canonical_runtime_only=True)
            assert control._runtime is provider
            assert control._path_controller is provider.lifecycle_observer
            assert inj.consume_prepared_control(inj._prepared_key) is control
            assert provider.snapshot()==before
        finally:
            inj.deactivate()
