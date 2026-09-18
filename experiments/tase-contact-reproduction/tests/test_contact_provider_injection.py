from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
sys.path.insert(0,str(ROOT.parents[1]/'src/ur10e_experiment_runtime'))
from contact_benchmark_provider import ContactCommandProvider
from step5d_autotune_v4_r006.live_adapter import _R006ScopedRuntimeInjection,R006LiveAdapterError


def injection(factory):
    inj=_R006ScopedRuntimeInjection(motion_profile=object(),path_reference=lambda:None,
                                    contact_command_provider_factory=factory)
    inj.active=True
    inj._original_control=lambda candidate,**kw:SimpleNamespace(candidate=candidate,**kw)
    inj._patch_path_reference=lambda:None
    return inj


def test_provider_prepared_before_arm_and_consumed_exactly_once():
    provider=object.__new__(ContactCommandProvider);calls=[]
    inj=injection(lambda **kw:(calls.append(kw) or provider))
    control=inj.prepare_control(candidate=object(),attempt_id='a',
        release_contract=SimpleNamespace(raw={'program':'step5d_contact_six_qp_v1'}),
        path_requested=True,canonical_runtime_only=True)
    assert len(calls)==1 and control.contact_command_provider is provider
    key=inj._prepared_key
    assert inj.consume_prepared_control(key) is control
    with pytest.raises(R006LiveAdapterError,match='missing or already consumed'):inj.consume_prepared_control(key)


def test_wrong_package_or_provider_swap_cannot_fall_back_to_pid():
    provider=object.__new__(ContactCommandProvider);inj=injection(lambda **kw:provider)
    args=dict(candidate=object(),attempt_id='a',path_requested=True,canonical_runtime_only=True)
    with pytest.raises(R006LiveAdapterError,match='dedicated TP'):
        inj.prepare_control(**args,release_contract=SimpleNamespace(raw={'program':'old_r013'}))
    control=inj.prepare_control(**args,release_contract=SimpleNamespace(raw={'program':'step5d_contact_six_qp_v1'}))
    key=inj._prepared_key;control.contact_command_provider=None
    with pytest.raises(R006LiveAdapterError,match='changed'):inj.consume_prepared_control(key)
    assert inj._prepared_control is None
