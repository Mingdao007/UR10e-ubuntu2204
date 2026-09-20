"""Synthetic ledger lifecycle only: no scientific/physical budget is spent."""
import pytest
from yield_contact_tuner import YieldContactTuner, METHODS
from yield_contact_ledger import YieldContactLedger
from contact_benchmark_ledger import ContactLedger


def make(path, cell='training-cell'):
    t=YieldContactTuner(training_cell_id=cell,selection_contract_id='selection-v1')
    return YieldContactLedger(path,tuner=t,campaign_protocol_sha256='a'*64)


def evidence(ledger, success=True, objective=1.):
    return {**ledger.bindings,'artifact_sha256':'b'*64,
            'objective_eligible':success,'nominal_feasible':success,
            'objective':objective if success else None}


def seal_pair(ledger,method,index,proposal,failed=False,objective=1.):
    candidate=proposal.candidate
    u=ledger.begin(attempt_id=f'{method}-{index}-n',controller=method,
                   candidate=candidate,condition='nominal')
    assert u==index
    ledger.seal(f'{method}-{index}-n',status='failed' if failed else 'complete',
                evidence=evidence(ledger,not failed,objective))
    ledger.begin(attempt_id=f'{method}-{index}-d',controller=method,
                 candidate=candidate,condition='disturbed',unit=u)
    ledger.seal(f'{method}-{index}-d',status='failed' if failed else 'complete',
                evidence=evidence(ledger,not failed,objective))


def test_restart_retains_inflight_budget_and_failed_pair(tmp_path):
    path=tmp_path/'training.sqlite';ledger=make(path);t=ledger.tuner
    p=t.propose('SFC',[],0)
    assert ledger.begin(attempt_id='n',controller='SFC',candidate=p.candidate,condition='nominal')==0
    ledger.close();ledger=make(path)
    assert ledger.progress()['SFC']['used_units']==1
    assert ledger.begin(attempt_id='n',controller='SFC',candidate=p.candidate,condition='nominal')==0
    with pytest.raises(ValueError,match='inflight'):
        ledger.begin(attempt_id='other',controller='DSFC',candidate=t.propose('DSFC',[],0).candidate,condition='nominal')
    with pytest.raises(ValueError,match='not sealed'):
        ledger.training_observations('SFC')
    e=evidence(ledger,False)
    ledger.seal('n',status='interrupted',evidence=e)
    ledger.seal('n',status='interrupted',evidence=e)
    with pytest.raises(ValueError,match='incomplete'):
        ledger.begin(attempt_id='new',controller='SFC',candidate=p.candidate,condition='nominal')
    ledger.begin(attempt_id='d',controller='SFC',candidate=p.candidate,condition='disturbed',unit=0)
    ledger.seal('d',status='failed',evidence=e)
    rows=ledger.training_observations('SFC')
    assert rows[0].status=='failed' and rows[0].objective is None
    assert ledger.tuner.propose('SFC',rows,1).unit_index==1
    ledger.close()


def test_seal_and_reopen_reject_holdout_or_different_contract(tmp_path):
    path=tmp_path/'training.sqlite';ledger=make(path)
    p=ledger.tuner.propose('DSFC',[],0)
    ledger.begin(attempt_id='n',controller='DSFC',candidate=p.candidate,condition='nominal')
    for key,value in [('split','holdout'),('training_cell_id','other'),('tuner_config_sha256','c'*64)]:
        e=evidence(ledger);e[key]=value
        with pytest.raises(ValueError,match='differs'):ledger.seal('n',status='complete',evidence=e)
    assert ledger.progress()['DSFC']['completed_trials']==0
    ledger.close()
    with pytest.raises(ValueError,match='protocol differs'):make(path,cell='different')


def test_controller_scope_cannot_reinterpret_legacy_ledger(tmp_path):
    path=tmp_path/'legacy.sqlite';old=ContactLedger(path,protocol_sha256='a'*64)
    old.db.execute("DELETE FROM metadata WHERE key='controllers'");old.close()
    with pytest.raises(ValueError,match='unbound legacy'):
        ContactLedger(path,protocol_sha256='a'*64,controllers=METHODS)


def test_three_method_full_schedule_and_immutable_freeze(tmp_path):
    ledger=make(tmp_path/'training.sqlite');selected={}
    for method in METHODS:
        for index in range(24):
            rows=ledger.training_observations(method)
            p=ledger.tuner.propose(method,rows,index)
            seal_pair(ledger,method,index,p,objective=float(index+1))
            if index==20:selected[method]=p.candidate
    assert set(ledger.progress())==set(METHODS)
    assert all(v['used_units']==24 and v['completed_trials']==48 for v in ledger.progress().values())
    frozen=ledger.freeze(selected)
    assert ledger.freeze(selected)==frozen
    with pytest.raises(ValueError,match='frozen'):
        ledger.begin(attempt_id='extra',controller='SFC',candidate=selected['SFC'],condition='nominal')
    changed=dict(selected);changed['SFC']=ledger.tuner.initial_candidates('SFC')[1]
    with pytest.raises(ValueError,match='incumbent'):ledger.freeze(changed)
    ledger.close()


def test_off_schedule_candidates_cannot_be_promoted(tmp_path):
    ledger=make(tmp_path/'training.sqlite')
    # Registration retains actual trials even if a caller bypasses proposer.
    # Freeze must not mislabel that history as the predeclared fair schedule.
    for method in METHODS:
        proposal=ledger.tuner.propose(method,[],0)
        for index in range(24):seal_pair(ledger,method,index,proposal)
    selected={m:ledger.tuner.propose(m,[],0).candidate for m in METHODS}
    with pytest.raises(ValueError,match='deterministic proposal schedule'):ledger.freeze(selected)
    ledger.close()


def test_bound_configuration_cannot_change_after_open(tmp_path):
    ledger=make(tmp_path/'training.sqlite')
    with pytest.raises(TypeError):ledger.bindings['training_cell_id']='other'
    ledger.tuner.training_cell_id='other'
    with pytest.raises(ValueError,match='binding changed'):ledger.training_observations('SFC')
    assert ledger.progress()['SFC']['used_units']==0
    ledger.close()
