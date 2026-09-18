import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from contact_benchmark_ledger import ContactLedger
from contact_benchmark_protocol import protocol


def ledger(tmp_path):return ContactLedger(tmp_path/'ledger.sqlite',protocol_sha256=protocol()['sha256'])

def test_failure_consumes_budget_and_retry_does_not_duplicate(tmp_path):
    l=ledger(tmp_path);kw=dict(attempt_id='a',controller='LAC',candidate={'m':4},condition='nominal')
    assert l.begin(**kw)==0;assert l.begin(**kw)==0
    with pytest.raises(ValueError,match='inflight'):l.begin(**{**kw,'attempt_id':'b'})
    e={'artifact_sha256':'a'*64,'objective_eligible':False}
    l.seal('a',status='failed',evidence=e);l.seal('a',status='failed',evidence=e)
    assert l.progress()['LAC']['used_units']==1
    with pytest.raises(ValueError,match='incomplete'):l.begin(**{**kw,'attempt_id':'b'})
    assert l.begin(attempt_id='b',controller='LAC',candidate={'m':4},condition='disturbed',unit=0)==0
    l.close()
    restored=ledger(tmp_path);assert restored.progress()['LAC']['failed_trials']==1
    with pytest.raises(ValueError,match='inflight'):restored.begin(attempt_id='c',controller='NAC',candidate={'m':4},condition='nominal')


def test_conflicting_identity_and_false_success_rejected(tmp_path):
    l=ledger(tmp_path);l.begin(attempt_id='a',controller='MSFC',candidate={'m':4},condition='nominal')
    with pytest.raises(ValueError,match='identity'):l.begin(attempt_id='a',controller='MSFC',candidate={'m':5},condition='nominal')
    with pytest.raises(ValueError,match='uncensored'):l.seal('a',status='complete',evidence={'artifact_sha256':'b'*64,'objective_eligible':False})
    with pytest.raises(ValueError,match='six'):l.freeze({'MSFC':{'m':4}})


def test_freeze_uses_successful_repeat_and_requires_measured_nominal_feasibility(tmp_path):
    from contact_benchmark_protocol import CONTROLLERS
    l=ledger(tmp_path)
    for c in CONTROLLERS:
        for n in range(24):
            for condition in ('nominal','disturbed'):
                attempt=f'{c}-{n}-{condition}'
                l.begin(attempt_id=attempt,controller=c,candidate={'m':4 if n in (0,23) else 5},
                        condition=condition,unit=n if condition=='disturbed' else None)
                failed=n==0
                l.seal(attempt,status='failed' if failed else 'complete',evidence={
                    'artifact_sha256':'a'*64,'objective_eligible':not failed,
                    'nominal_feasible':n==23,'objective':1.})
    # Completed but measured infeasible candidates must not become holdout picks.
    with pytest.raises(ValueError,match='feasible'):
        l.freeze({c:{'m':5} for c in CONTROLLERS})
    # First occurrence failed; the later same-candidate pair is valid evidence.
    selected={c:{'m':4} for c in CONTROLLERS}
    assert l.freeze(selected)==l.freeze(selected)
    l.close()
