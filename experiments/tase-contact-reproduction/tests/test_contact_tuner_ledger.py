from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from contact_benchmark_tuner import ContactBenchmarkTuner
from contact_benchmark_ledger import ContactLedger
from contact_benchmark_protocol import protocol


def test_proposer_consumes_only_complete_pairs_and_counts_failures(tmp_path):
    tuner=ContactBenchmarkTuner();ledger=ContactLedger(tmp_path/'ledger.db',protocol_sha256=protocol()['sha256'])
    try:
        first=tuner.propose('MSFC',[],0)
        unit=ledger.begin(attempt_id='nominal-0',controller='MSFC',candidate=first.candidate.as_dict(),condition='nominal')
        ledger.seal('nominal-0',status='failed',evidence={'artifact_sha256':'a'*64,'objective_eligible':False})
        with pytest.raises(ValueError,match='not sealed'):ledger.training_observations('MSFC')
        ledger.begin(attempt_id='disturbed-0',controller='MSFC',candidate=first.candidate.as_dict(),condition='disturbed',unit=unit)
        ledger.seal('disturbed-0',status='interrupted',evidence={'artifact_sha256':'b'*64,'objective_eligible':False})
        rows=ledger.training_observations('MSFC')
        assert len(rows)==1 and rows[0].status=='failed' and rows[0].objective is None
        second=tuner.propose('MSFC',rows,len(rows))
        assert second.unit_index==1 and second.candidate.key!=first.candidate.key
    finally:ledger.close()


def test_eligible_pair_cannot_invent_missing_objective(tmp_path):
    tuner=ContactBenchmarkTuner();ledger=ContactLedger(tmp_path/'ledger.db',protocol_sha256=protocol()['sha256'])
    try:
        candidate=tuner.seed_candidate('LAC').as_dict()
        n=ledger.begin(attempt_id='n',controller='LAC',candidate=candidate,condition='nominal')
        ledger.seal('n',status='complete',evidence={'artifact_sha256':'a'*64,'objective_eligible':True,'nominal_feasible':True})
        ledger.begin(attempt_id='d',controller='LAC',candidate=candidate,condition='disturbed',unit=n)
        ledger.seal('d',status='complete',evidence={'artifact_sha256':'b'*64,'objective_eligible':True})
        with pytest.raises(ValueError,match='lacks measured'):ledger.training_observations('LAC')
    finally:ledger.close()
