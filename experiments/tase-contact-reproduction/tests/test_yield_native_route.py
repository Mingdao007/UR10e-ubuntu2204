from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import sys
import numpy as np
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
from step5d_autotune_v4_r004.runtime import RuntimeGuardError,gate_qdot
from yield_contact_provider import YieldContactProvider
from yield_contact_runtime import YieldContactRuntime
from yield_native_route import (
    CLAIM_SCOPE,
    METHODS,
    YieldNativeRouteError,
    create_native_yield_provider,
    create_native_yield_runtime,
    load_observer_parameters,
    load_route_config,
    measure_actual_model,
    validate_home_observations,
)
from test_contact_benchmark_runtime import lib


def _pose():
    receipt=json.loads((ROOT/'report/contact-six-qp-20260917/preserved-home.json').read_text())
    rotation=rotvec_to_matrix(np.array(receipt['home_pose'][3:]))
    return receipt,rotation


def _runtime_args(lib,method='SFC',**kwargs):
    receipt,rotation=_pose()
    args=dict(method=method,qp_library=lib,anchor_m=receipt['home_pose'][:3],
              task_basis=rotation,approach_inward_base=rotation[:,2],deadline_s=None)
    args.update(kwargs)
    return args


def test_config_binds_frozen_transfer_laws_and_explicit_v3_observer():
    config=load_route_config()
    protocol=json.loads((ROOT/'report/yield-frozen-transfer-v1/protocol.json').read_text())
    observer=json.loads((ROOT/'config/yield_normal_observer_v3.json').read_text())
    assert tuple(config['methods'])==METHODS
    assert config['law_parameters']==protocol['parameters']
    assert config['law_parameters']['MSFC']['g']==pytest.approx(0.08583341909109758)
    assert config['law_parameters']['MSFC']['g']!=pytest.approx(0.17166683818219516)
    assert config['law_parameters']['MSFC']['minimum_metric_eigenvalue']==pytest.approx(0.026926929041248004)
    assert load_observer_parameters(config)==observer
    assert config['claim_scope']==CLAIM_SCOPE
    assert 'not fresh physical qualification' in config['claim_scope']


def test_actual_model_matches_reviewed_receipt_hashes():
    config=load_route_config()
    actual=measure_actual_model()
    desired=config['model']
    assert actual['expanded_urdf_sha256']==desired['expanded_urdf_sha256']
    assert actual['calibration_yaml_sha256']==desired['calibration_yaml_sha256']
    assert actual['xacro_sha256']==desired['xacro_sha256']
    assert actual['calibration_hash']==desired['calibration_hash']


@pytest.mark.parametrize('method',['SFC','DSFC','MSFC'])
def test_native_runtime_identity_binds_config_law_and_v3_observer(lib,method):
    config=load_route_config()
    runtime,binding=create_native_yield_runtime(**_runtime_args(lib,method))
    with runtime:
        payload=runtime.controller.identity_payload
        assert payload['parameters']=={key:float(value) for key,value in config['law_parameters'][method].items()}
        observer=load_observer_parameters(config)
        stored=payload['estimator_parameters']
        for key,value in observer.items():
            assert stored[key]==value
        assert stored['motion_gain']==pytest.approx(0.3)
        assert stored['coplanarity_gain_s_inv']==pytest.approx(0.3)
        assert binding.claim_scope==CLAIM_SCOPE
        assert binding.model_hashes['expanded_urdf']==runtime.model_urdf_sha256
        assert binding.controller_identity==runtime.controller.identity
        assert 'not fresh physical qualification' in binding.claim_scope


def test_production_deadlines_remain_1p5ms_runtime_and_1ms_qp(lib):
    args=_runtime_args(lib)
    args.pop('deadline_s')
    runtime,binding=create_native_yield_runtime(**args)
    with runtime:
        assert runtime.deadline_s==0.0015
        assert runtime.controller.qp.qp.deadline_s==0.001
        assert runtime.solver_profile.deadline_s==0.001


def test_legacy_runtime_keeps_seed_msfc_and_implicit_estimator(lib):
    receipt,rotation=_pose()
    runtime=YieldContactRuntime(method='MSFC',qp_library=lib,anchor_m=receipt['home_pose'][:3],
        task_basis=rotation,approach_inward_base=rotation[:,2],deadline_s=None)
    with runtime:
        payload=runtime.controller.identity_payload
        assert payload['parameters']['g']==pytest.approx(0.17166683818219516)
        assert payload['estimator_parameters']['motion_gain']==pytest.approx(8.0)
        assert 'coplanarity_gain_s_inv' not in payload['estimator_parameters']


def test_gate_qdot_accepts_genuine_binding_and_rejects_changed_hashes(lib):
    runtime,binding=create_native_yield_runtime(**_runtime_args(lib,'DSFC'))
    jacobian=[[1. if i==j else 0. for j in range(6)] for i in range(6)]
    with runtime:
        allowed=gate_qdot(binding,qdot=(0.,)*6,jacobian_6x6=jacobian,normal_base=(0.,0.,1.),
            observed_model_hashes=binding.model_hashes)
        assert allowed.allowed
        changed=dict(binding.model_hashes)
        changed['expanded_urdf']='0'*64
        with pytest.raises(RuntimeGuardError,match='hash binding differs'):
            gate_qdot(binding,qdot=(0.,)*6,jacobian_6x6=jacobian,normal_base=(0.,0.,1.),
                observed_model_hashes=changed)


def test_changed_native_binding_is_rejected_before_prepared_state(lib):
    from types import SimpleNamespace
    from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE
    from step5d_autotune_v4_r004.contracts import load_contract
    from step5d_autotune_v4_r006.live_adapter import (
        R006Candidate,_R006NativeCanonicalQualificationControl,_R006ScopedRuntimeInjection,
    )
    runtime,binding=create_native_yield_runtime(**_runtime_args(lib,'SFC'))
    provider=create_native_yield_provider(runtime=runtime,binding=binding)
    tampered=replace(binding,expanded_urdf_sha256='0'*64,
        model_hashes={**dict(binding.model_hashes),'expanded_urdf':'0'*64})
    provider.native_binding=tampered
    provider.model_hashes=dict(tampered.model_hashes)
    raw=deepcopy(load_contract().raw);raw['program']='step5d_contact_six_qp_v1'
    inj=_R006ScopedRuntimeInjection(motion_profile=R004_MOTION_PROFILE,
        path_reference=lambda *_args,**_kwargs:None,
        contact_command_provider_factory=lambda **_kwargs:provider)
    inj.active=True
    inj._original_control=_R006NativeCanonicalQualificationControl
    inj._patch_path_reference=lambda:None
    with runtime:
        with pytest.raises(Exception,match='expanded URDF|native binding'):
            inj.prepare_control(candidate=R006Candidate(),attempt_id='changed-binding',
                release_contract=SimpleNamespace(raw=raw),path_requested=True,
                canonical_runtime_only=True)
        assert inj._prepared_control is None and inj._prepared_key is None


def test_changed_calibration_config_is_rejected_without_prepared_runtime(lib,tmp_path):
    config=load_route_config()
    tampered=deepcopy(config)
    tampered['model']['calibration_yaml_sha256']='0'*64
    path=tmp_path/'yield_native_route_v1.json'
    path.write_text(json.dumps(tampered))
    with pytest.raises(YieldNativeRouteError,match='calibration or expanded model'):
        create_native_yield_runtime(**_runtime_args(lib),config_path=path)


def test_changed_urdf_config_is_rejected_without_prepared_runtime(lib,tmp_path):
    config=load_route_config()
    tampered=deepcopy(config)
    tampered['model']['expanded_urdf_sha256']='0'*64
    path=tmp_path/'yield_native_route_v1.json'
    path.write_text(json.dumps(tampered))
    with pytest.raises(YieldNativeRouteError,match='calibration or expanded model'):
        create_native_yield_runtime(**_runtime_args(lib),config_path=path)


def test_home_observations_are_accepted_explicitly_and_invalid_structure_fails(lib):
    receipt,_=_pose()
    validate_home_observations({'home_pose':receipt['home_pose'],'home_q':receipt['home_q']})
    with pytest.raises(YieldNativeRouteError,match='missing home_q'):
        validate_home_observations({'home_pose':receipt['home_pose']})
    with pytest.raises(YieldNativeRouteError,match='finite length-6'):
        create_native_yield_runtime(**_runtime_args(lib),
            home_observations={'home_pose':[0,0,0],'home_q':receipt['home_q']})


def test_provider_rejects_mismatched_caller_hash_assertion(lib):
    runtime,binding=create_native_yield_runtime(**_runtime_args(lib,'MSFC'))
    with runtime:
        with pytest.raises(ValueError,match='differ from native binding'):
            YieldContactProvider(runtime=runtime,native_binding=binding,
                model_hashes={'expanded_urdf':'0'*64})


@pytest.mark.parametrize("change", ["anchor", "basis"])
def test_binding_rejects_other_task_runtime(lib, change):
    args = _runtime_args(lib)
    first, binding = create_native_yield_runtime(**args)
    changed = dict(args)
    if change == "anchor":
        changed["anchor_m"] = np.asarray(args["anchor_m"]) + [0.001, 0., 0.]
    else:
        changed["task_basis"] = np.asarray(args["task_basis"]) @ rotvec_to_matrix(np.array([0., 0., 0.01]))
    second, _ = create_native_yield_runtime(**changed)
    with first, second:
        assert first.controller.identity == second.controller.identity
        assert first.identity != second.identity
        assert binding.model_hashes["runtime_identity"] == first.identity
        with pytest.raises(YieldNativeRouteError, match="runtime identity"):
            create_native_yield_provider(runtime=second, binding=binding)


def test_historical_receipt_digest_is_checked(tmp_path):
    config = load_route_config()
    config["model"]["receipt_sha256"] = "0" * 64
    path = tmp_path / "route.json"
    path.write_text(json.dumps(config))
    with pytest.raises(YieldNativeRouteError, match="historical model receipt binding"):
        load_route_config(path)
