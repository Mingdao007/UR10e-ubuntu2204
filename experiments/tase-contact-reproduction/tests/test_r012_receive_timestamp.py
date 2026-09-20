"""Real receive clocks are recorded only for distinct controller frames."""
from types import SimpleNamespace
from step5d_autotune_v4_r004.contracts import load_contract
from step5d_autotune_v4_r004.fake_rtde import FakeLiveRTDETransport
import step5d_autotune_v4_r012.register_transport as transport_module


def test_r012_receive_timestamp_does_not_refresh_a_cached_frame(monkeypatch):
    raw = FakeLiveRTDETransport(load_contract())._mapping()
    frames = iter([{**raw,'timestamp':100.}, {**raw,'timestamp':100.}, {**raw,'timestamp':101.}])
    clock = SimpleNamespace(now=12.)
    monkeypatch.setattr(transport_module.time,'monotonic',lambda:clock.now)
    monkeypatch.setattr(transport_module,'_wait_for_rtde_readable',lambda *a:True)
    transport = transport_module.R012LiveRTDETransport('unused-no-network')
    transport.client = SimpleNamespace(recv_latest_sample=lambda *a:next(frames))
    transport.output_recipe = 1
    transport.output_types = []
    first = transport.poll_output()
    assert first.received_monotonic_s == 12.
    clock.now = 13.
    assert transport.poll_output() is None
    assert transport.latest is first
    assert first.received_monotonic_s == 12.
    clock.now = 14.
    assert transport.poll_output().received_monotonic_s == 14.
