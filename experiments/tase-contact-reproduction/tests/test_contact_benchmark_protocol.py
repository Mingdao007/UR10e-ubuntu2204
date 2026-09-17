import math
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from contact_benchmark_protocol import Task, disturbance, holdout_schedule, protocol


def test_complete_period_and_geometry():
    t=Task(); assert t.duration_s == pytest.approx(62.8318530718)
    assert t.reference(t.duration_s)['position_m'] == pytest.approx((0,0,0),abs=1e-12)
    assert t.sanity()['speed_upper_bound_m_s'] == pytest.approx(math.sqrt(.004**2+.002**2))
    with pytest.raises(ValueError): t.reference(70)


def test_balanced_schedule_and_fixed_identity():
    a=holdout_schedule(17); assert a==holdout_schedule(17)
    assert len(a)==150
    assert len({r['ordinal'] for r in a})==150
    for block in range(5):
        assert len({(r['controller'],r['scenario']) for r in a if r['block']==block})==30
    assert 'RPSFC' not in protocol()['controllers']


def test_shared_waveform_bounds_and_hand_push_truth():
    for scenario in protocol()['scenarios']:
        for t in [0,20,20.25,20.5,21.25,24.5,24.75,25,30]:
            v=disturbance(scenario,t,amplitude_n=3)
            assert math.sqrt(sum(x*x for x in v)) <= 3+1e-12
    assert disturbance('human_push',20.25,amplitude_n=3)==(0,0,0)
    assert disturbance('normal_pulse',20.25,amplitude_n=3)==(0,0,3)
