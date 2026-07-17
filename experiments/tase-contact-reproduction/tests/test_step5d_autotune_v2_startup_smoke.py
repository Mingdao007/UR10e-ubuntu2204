from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v2.startup_smoke import (
    RtdeHoldSession,
    StartupSmokeError,
    verify_play_startup,
)
from run_step5d_autotune_v2_hil_smoke import program_is_stopped


def _sample(*, runtime_state: int, state: int = 0, consumed: int = 0) -> dict:
    return {
        "runtime_state": runtime_state,
        "output_int_register_24": 0,
        "output_int_register_25": 0,
        "output_int_register_26": state,
        "output_int_register_27": 0,
        "output_int_register_28": 0,
        "output_int_register_29": 0,
        "output_int_register_30": consumed,
    }


class FakeHoldSession:
    def __init__(self, samples: list[dict]) -> None:
        self.samples = iter(samples)
        self.heartbeats: list[float] = []

    def send_hold(self, heartbeat: float) -> None:
        self.heartbeats.append(heartbeat)

    def receive_output(self) -> dict:
        return next(self.samples)


def test_hold_smoke_replays_preplay_starting_active_without_arm() -> None:
    session = FakeHoldSession(
        [
            _sample(runtime_state=1),
            _sample(runtime_state=2),
            _sample(runtime_state=2, state=10),
        ]
    )
    play_calls: list[bool] = []
    result = verify_play_startup(
        session,
        trigger_play=lambda: play_calls.append(True),
        timeout_s=2.0,
    )
    assert result.phases == ("preplay", "starting", "active")
    assert result.first_active_state == 10
    assert result.consumed_command_seq == 0
    assert play_calls == [True]
    assert session.heartbeats == [2.0, 3.0, 4.0]


def test_hold_smoke_rejects_missing_preplay_and_consumed_command() -> None:
    with pytest.raises(StartupSmokeError, match="initial STOPPED"):
        verify_play_startup(
            FakeHoldSession([_sample(runtime_state=2, state=10)]),
            trigger_play=lambda: None,
            timeout_s=2.0,
        )


def test_hil_accepts_stopped_latched_ready_but_requires_running_transition() -> None:
    session = FakeHoldSession(
        [
            _sample(runtime_state=1, state=10),
            _sample(runtime_state=1, state=10),
            _sample(runtime_state=2, state=10),
        ]
    )
    result = verify_play_startup(
        session,
        trigger_play=None,
        timeout_s=2.0,
        allow_latched_ready_baseline=True,
    )
    assert result.phases == ("preplay_ready_latched", "active")
    assert result.samples == 3

    with pytest.raises(StartupSmokeError, match="consumed a command"):
        verify_play_startup(
            FakeHoldSession(
                [
                    _sample(runtime_state=1),
                    _sample(runtime_state=2),
                    _sample(runtime_state=2, state=10, consumed=1),
                ]
            ),
            trigger_play=lambda: None,
            timeout_s=2.0,
        )


def test_rtde_recipe_wait_ignores_asynchronous_text_messages() -> None:
    session = RtdeHoldSession("127.0.0.1")
    packets = iter(
        [
            (ord("M"), b"controller startup notice"),
            (ord("O"), b"\x01UINT32"),
        ]
    )
    session._receive = lambda: next(packets)  # type: ignore[method-assign]
    assert session._receive_recipe("output", ord("O")) == (
        ord("O"),
        b"\x01UINT32",
    )


def test_hil_accepts_real_dashboard_stopped_response_with_program_name() -> None:
    assert program_is_stopped("STOPPED")
    assert program_is_stopped("STOPPED step5d_strict_rnn_autotune_v2.urp")
    assert not program_is_stopped("PLAYING step5d_strict_rnn_autotune_v2.urp")
