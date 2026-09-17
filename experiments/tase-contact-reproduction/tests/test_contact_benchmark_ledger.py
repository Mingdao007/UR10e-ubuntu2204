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
