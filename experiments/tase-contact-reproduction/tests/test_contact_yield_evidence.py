"""Full-period RTDE/TP coverage is stronger than the legacy 60 s campaign."""
from dataclasses import replace
import math
import pytest
from contact_yield_protocol import PERIOD_S
from yield_contact_evidence import YieldPathEvidenceCollector
from step5d_autotune_v4_r004_live_writer import BoundedPacketHistory, LiveWriterError
from step5d_autotune_v4_r004.evidence import EvidenceError
from test_step5d_autotune_v4_r004_evidence_ledger import _motion_sample


def collect(last_time):
    history=BoundedPacketHistory()
    history.record(0,published_at_s=100.,qdot=(0.,)*6,reference_phase='path',reference_time_s=0.)
    c=YieldPathEvidenceCollector(require_path_boundary=True,published_reference_lookup=history.consumed)
    c.mark_path_start(observed_at_s=100.,rtde_timestamp_s=0.,tp_sequence=0)
    count=round(last_time/.002)+1
    for i in range(count):
        t=i*.002
        if i:
            history.record(i,published_at_s=100.+t,qdot=(0.,)*6,reference_phase='path',reference_time_s=t)
        sample=_motion_sample(i,t,final=i==count-1)
        seq={'writer':i,'rtde':t,'kunwei':i,'tp':i}
        c.observe(replace(sample,observed_at_s=100.+t,source_sequences=seq,source_sequence=seq))
    return c


def test_complete_figure_eight_requires_all_formal_bins():
    c=collect(math.floor(PERIOD_S/.002)*.002)
    evidence=c.finalize(return_gate_passed=True,contact_gate_passed=True,home_proof={'stationary':True})
    assert evidence.path_duration_s>=PERIOD_S
    assert evidence.metrics['full_path_bin_count']==629
    assert not evidence.metrics['entry_in_formal_coverage']


@pytest.mark.parametrize('last',[59.998,62.828])
def test_legacy_sixty_seconds_and_one_tick_short_are_not_complete(last):
    c=collect(last)
    with pytest.raises(EvidenceError,match='below 62.8319 s'):
        c.finalize(return_gate_passed=True,contact_gate_passed=True,home_proof={'stationary':True})


def test_entry_echo_cannot_be_relabelled_as_formal_start():
    history=BoundedPacketHistory()
    history.record(1,published_at_s=100.,qdot=(0.,)*6,reference_phase='entry',reference_time_s=.998)
    c=YieldPathEvidenceCollector(require_path_boundary=True,published_reference_lookup=history.consumed)
    with pytest.raises(EvidenceError,match='published PATH command'):
        c.mark_path_start(observed_at_s=100.002,rtde_timestamp_s=20.,tp_sequence=1)
    assert not c.path_samples


def test_packet_reference_is_validated_before_history_changes():
    history=BoundedPacketHistory()
    with pytest.raises(LiveWriterError):
        history.record(1,published_at_s=100.,qdot=(0.,)*6,reference_phase='entry',reference_time_s=None)
