"""The contact owner reuses R013's collector without changing evidence gates."""
from contact_yield_live_writer import NativeYieldLiveWriter
from step5d_autotune_v4_r004_live_writer import LiveR004Writer
from step5d_autotune_v4_r004.timing import TimingEvidenceCollector
from step5d_autotune_v4_r013.live_runtime import R013LightweightTimingEvidenceCollector


def test_contact_uses_existing_collector_without_global_patch():
    assert isinstance(NativeYieldLiveWriter._new_qualification_timing_collector(), R013LightweightTimingEvidenceCollector)
    assert type(LiveR004Writer._new_qualification_timing_collector()) is TimingEvidenceCollector


def test_distinct_echo_replay_keeps_rates_and_failure_identical():
    collectors = [TimingEvidenceCollector(), NativeYieldLiveWriter._new_qualification_timing_collector()]
    for i in range(1000):
        # TP repeats do not prove intermediate commands were consumed.
        sample = dict(source_sequences={'writer':i,'rtde':i*.002,'kunwei':2*i,'tp':i//2},
                      source_ages_s={'writer':0.,'rtde':.001,'tp':.008,'kunwei':.03},epoch=1)
        for c in collectors:
            c.observe_layered_sample(i*.002, **sample)
    evidence = [c.finalize(duration_s=2.) for c in collectors]
    assert evidence[0].as_dict() == evidence[1].as_dict()
    assert evidence[1].distinct_tp_consumed_packet_echoes == 500
    assert evidence[1].successful is False
