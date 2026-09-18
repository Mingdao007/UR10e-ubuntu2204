"""Synthetic receipt rejection tests; no device access or qualification claim."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from step5d_autotune_v4_r013.live_owner import (
    R013OwnerError, _validate_resident_ready_binding,
)


def receipts(tmp_path):
    program='step5d_contact_six_qp_v1'
    target=f'/programs/andyl/kunwei/step5/{program}.urp'
    triplet={k:'a'*64 for k in ('script','txt','urp')}
    contract=SimpleNamespace(sha256='b'*64,campaign_fingerprint='c'*64)
    shared=dict(program=program,route_id='route',attempt_id='attempt',
        resident_session_id='session',session_epoch=1,controller_target=target)
    ready={**shared,'status':'resident_ready_no_arm','runtime_protocol':606006,
        'triplet':triplet,'resident_ready_evidence':str(tmp_path/'resident_ready_evidence.json')}
    launch={**shared,'campaign_id':'campaign','run_id':'run','session_id':'session',
        'triplet':triplet,'contract_sha256':contract.sha256,
        'campaign_fingerprint':contract.campaign_fingerprint}
    evidence={**shared,'schema':'step5d.autotune-v4/r013-resident-ready-evidence-v2',
        'status':'resident_ready_no_arm','observation_source':'fresh_rtde_observation',
        'triplet_sha256':triplet,'contract_sha256':contract.sha256,
        'campaign_fingerprint':contract.campaign_fingerprint,
        'runtime_output_registers':{'32':606006,'33':18,'34':618001},
        'runtime_protocol':606006,'runtime_revision':18,'runtime_extension_protocol':618001,
        'program_running':True,'stationary':True,'safety_normal':True,
        'no_arm':True,'arm_dispatched':False,'trial_dispatched':False,'observed_at_s':123.,
        'dashboard':{'is in remote control':'true','safetymode':'Safetymode: NORMAL',
            'get loaded program':f'Loaded program: {target}','running':'Program running: true'}}
    controller={**shared,**{f'{k}_sha256':v for k,v in triplet.items()},
        'observation_source':'fresh_controller_readback_and_rtde'}
    runtime={**shared,'script_sha256':triplet['script'],'program_running':True,
        'uninterrupted':True,'observed_at_s':123.,'runtime_identity_projection':{
        'kind':'contact_six_legacy_wire_abi','physically_read':False,
        'source_evidence':'resident_ready_evidence.json'}}
    for name,data in [('resident_ready_evidence',evidence),('controller_receipt',controller),('runtime_evidence',runtime)]:
        (tmp_path/f'{name}.json').write_text(json.dumps(data))
    return dict(run_dir=tmp_path,ready=ready,launch=launch,campaign_id='campaign',
        run_id='run',attempt_id='attempt',contract=contract,expected_program=program)


def test_contact_identity_requires_actual_new_registers(tmp_path):
    kwargs=receipts(tmp_path)
    assert _validate_resident_ready_binding(**kwargs)==kwargs['ready']['triplet']
    path=tmp_path/'resident_ready_evidence.json'
    data=json.loads(path.read_text())
    data.update(runtime_output_registers={'32':606006,'33':13,'34':613013},
        runtime_revision=13,runtime_extension_protocol=613013)
    path.write_text(json.dumps(data))
    with pytest.raises(R013OwnerError,match='physical output identity'):
        _validate_resident_ready_binding(**kwargs)


def test_contact_identity_cannot_masquerade_as_old_runtime(tmp_path):
    kwargs=receipts(tmp_path)
    path=tmp_path/'runtime_evidence.json'
    data=json.loads(path.read_text())
    data['runtime_identity_projection']['kind']='r013_legacy_runtime_limbs'
    path.write_text(json.dumps(data))
    with pytest.raises(R013OwnerError,match='compatibility projection'):
        _validate_resident_ready_binding(**kwargs)


def test_unknown_program_is_not_admitted(tmp_path):
    kwargs=receipts(tmp_path)
    kwargs['expected_program']='unregistered'
    with pytest.raises(R013OwnerError,match='no registered physical identity'):
        _validate_resident_ready_binding(**kwargs)
