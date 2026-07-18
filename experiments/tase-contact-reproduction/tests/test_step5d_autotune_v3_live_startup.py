from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_live as live  # noqa: E402


def _row(*, runtime_state: int, state: int = 10) -> dict[str, str]:
    row = {
        "ur_output_int_register_26": str(state),
        "ur_runtime_state": str(runtime_state),
        "ur_safety_mode": "1",
    }
    for index in (24, 25, 27, 28, 29, 30):
        row[f"ur_output_int_register_{index}"] = "0"
    return row


def test_preplay_home_and_postplay_runtime_are_distinct_contracts() -> None:
    stopped = _row(runtime_state=1)
    playing = _row(runtime_state=2)

    assert live._ready_home_zero_identity(stopped, require_playing=False) is True
    assert live._ready_home_zero_identity(stopped, require_playing=True) is False
    assert live._ready_home_zero_identity(playing, require_playing=False) is False
    assert live._runtime_playing_normal(playing) is True


def test_operator_play_signal_follows_runner_readiness_and_is_unique() -> None:
    source = (ROOT / "tools/run_step5d_autotune_v3_live.py").read_text(
        encoding="utf-8"
    )
    runner_ready = source.index(
        '_wait_file(runner_ready, runner, args.ready_timeout_s, "campaign runner")'
    )
    play_signal = source.index('print("READY_FOR_ONE_PLAY_TO_MOVE"')
    play_observed = source.index("if _runtime_playing_normal", play_signal)

    assert runner_ready < play_signal < play_observed
    assert source.count("READY_FOR_ONE_PLAY_TO_MOVE") == 1
    assert 'READY_FOR_TP_PLAY_V3"' not in source


def test_fault_after_play_has_one_immediate_operator_action(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        live,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {"programState": "PLAYING program.urp"},
    )

    assert live._announce_stop_if_playing("robot") is True
    assert capsys.readouterr().out.strip() == "ACTION_REQUIRED_PRESS_TP_STOP_NOW"
