"""Fresh-input writer behavior after a measured compute overrun, no endpoints."""
import pytest
from contact_yield_live_writer import NativeYieldLiveWriter
from step5d_autotune_v4_r004_live_writer import LiveR004Writer
from step5d_autotune_v4_r004.wire import CommandMode
from test_contact_yield_writer_loop import exercise_writer_loop

@pytest.mark.parametrize('native,expected_gap', [(False,.004),(True,.0021)])
def test_small_overrun_does_not_force_an_extra_native_cycle(tmp_path,monkeypatch,native,expected_gap):
    original=LiveR004Writer._send_packet
    calls=[]
    injected=[False]
    def send(self,sensor,**kwargs):
        calls.append((self._mono_clock(),sensor.observed_at_s))
        packet=original(self,sensor,**kwargs)
        if kwargs.get('command_mode')==CommandMode.BASELINE and not injected[0]:
            injected[0]=True
            self._sleep(.0021)
        return packet
    monkeypatch.setattr(LiveR004Writer,'_send_packet',send)
    evidence,samples,clock=exercise_writer_loop(tmp_path,monkeypatch,
        writer_class=NativeYieldLiveWriter if native else LiveR004Writer)
    assert injected[0]
    assert calls[2][0]-calls[1][0]==pytest.approx(expected_gap)
    assert all(b[1]>a[1] for a,b in zip(calls,calls[1:]))
    assert evidence.path_duration_s>=62.83
    assert samples[-1].path_time_s>62.82

@pytest.mark.parametrize('lateness', [0.,.0001,.0021,.1001])
def test_native_keeps_only_latest_due_slot(lateness):
    now=.002+lateness
    due=NativeYieldLiveWriter._next_publish_deadline(0.,.002,now)
    assert due<=now+1e-12
    assert 0<=now-due<.002+1e-12
    # After servicing that slot, no backlog burst is scheduled at the same time.
    following=NativeYieldLiveWriter._next_publish_deadline(due,.002,now)
    assert following>now


def test_native_cached_frame_still_has_no_extra_consumption(tmp_path,monkeypatch):
    original=NativeYieldLiveWriter.execute_attempt
    waits=[]
    def execute(self,*args,**kwargs):
        sleep=self._sleep
        def capture(seconds):
            if not self._last_poll_was_fresh:
                waits.append(seconds)
            return sleep(seconds)
        self._sleep=capture
        return original(self,*args,**kwargs)
    monkeypatch.setattr(NativeYieldLiveWriter,'execute_attempt',execute)
    evidence,samples,clock=exercise_writer_loop(tmp_path,monkeypatch,
        entry_aware=False,cached_at_end=True,writer_class=NativeYieldLiveWriter)
    assert clock.cache_injected
    assert waits and all(s>0 for s in waits)
    consumed=[s.source_sequences['tp'] for s in samples]
    assert len(consumed)==len(set(consumed))
    assert samples[-1].path_time_s<60.
    assert evidence.path_duration_s>=60.
