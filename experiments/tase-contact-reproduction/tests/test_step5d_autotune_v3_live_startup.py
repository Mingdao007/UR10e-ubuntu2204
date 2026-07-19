from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_live as live  # noqa: E402
import preflight_step5d_autotune_v3 as preflight  # noqa: E402


def _row(*, runtime_state: int, state: int = 10) -> dict[str, str]:
    row = {
        "ur_output_int_register_26": str(state),
        "ur_runtime_state": str(runtime_state),
        "ur_safety_mode": "1",
    }
    for index in (24, 25, 27, 28, 29, 30):
        row[f"ur_output_int_register_{index}"] = "0"
    return row


def test_postplay_runtime_requires_playing_normal() -> None:
    playing = _row(runtime_state=2)

    assert live._runtime_playing_normal(playing) is True


def test_preplay_does_not_wait_for_stale_stopped_tp_output_registers() -> None:
    source = (ROOT / "tools/run_step5d_autotune_v3_live.py").read_text(
        encoding="utf-8"
    )

    assert "_ready_home_zero_identity" not in source
    assert "pre-Play READY_HOME" not in source
    assert "stationary zero-identity READY_HOME" not in source


def test_live_consumer_accepts_the_complete_production_preflight_schema(
    tmp_path: Path,
) -> None:
    identity = {
        "contract_sha256": "a" * 64,
        "control_fingerprint": "b" * 64,
        "orchestration_fingerprint": "c" * 64,
    }
    payload = {
        "schema": preflight.SCHEMA,
        "ok": True,
        "fresh": True,
        "candidate_stage_id": live.RELEASE_STAGE_ID,
        "control_profile_id": live.CONTROL_PROFILE_ID,
        "tp_program_id": live.TP_PROGRAM_ID,
        "identity": identity,
        "predicates": {
            name: {"ok": True} for name in preflight.PREDICATE_NAMES
        },
    }
    path = tmp_path / "live_preflight.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    observed = live._validate_preflight(path, identity)

    assert observed == payload
    assert "prealign_start_clearance" in observed["predicates"]

    del payload["predicates"]["prealign_start_clearance"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(live.LiveLaunchError, match="predicates are incomplete"):
        live._validate_preflight(path, identity)


def test_operator_play_signal_precedes_runner_recovery_from_fresh_tp_state() -> None:
    source = (ROOT / "tools/run_step5d_autotune_v3_live.py").read_text(
        encoding="utf-8"
    )
    runner_ready = source.index(
        '_wait_file(runner_ready, runner, args.ready_timeout_s, "campaign runner")'
    )
    bridge_ready = source.index(
        '_wait_file(bridge_run / "bridge_ready.json", bridge, args.ready_timeout_s, "bridge")'
    )
    play_signal = source.index('print("READY_FOR_ONE_PLAY_TO_MOVE"')
    play_observed = source.index("if _runtime_playing_normal", play_signal)

    assert bridge_ready < play_signal < play_observed < runner_ready
    assert source.count("READY_FOR_ONE_PLAY_TO_MOVE") == 1
    assert 'READY_FOR_TP_PLAY_V3"' not in source
    assert "campaign_authorization.json" not in source
    assert '"--authorization-file"' not in source
    assert '"--campaign-binding"' in source
    assert "V3_BATCH_10_COMPLETE_STOPPING_TP_NOW" in source


def test_fault_after_play_has_one_immediate_operator_action(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        live,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {"programState": "PLAYING program.urp"},
    )

    assert live._announce_stop_if_playing("robot") is True
    assert capsys.readouterr().out.strip() == "ACTION_REQUIRED_PRESS_TP_STOP_NOW"


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.value += duration


def test_dashboard_stop_proves_stopped() -> None:
    replies: list[dict[str, Any]] = [
        {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"},
        {"stop": "Stopped", "programState": "STOPPED step5d_strict_rnn_autotune_v3.urp"},
    ]

    result = live._stop_v3_program(
        "robot", exchange=lambda *_args, **_kwargs: replies.pop(0)
    )
    assert result["ok"] is True
    assert result["method"] == "dashboard_stop"


def test_local_stop_rejection_then_observed_tp_stop_is_success() -> None:
    replies: list[dict[str, Any]] = [
        {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"},
        {
            "stop": "Command is not allowed in Local Control",
            "programState": "PLAYING step5d_strict_rnn_autotune_v3.urp",
        },
        {"programState": "STOPPED step5d_strict_rnn_autotune_v3.urp"},
    ]
    clock = _Clock()
    result = live._stop_v3_program(
        "robot",
        exchange=lambda *_args, **_kwargs: replies.pop(0),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result["ok"] is True
    assert result["method"] == "observed_stopped_after_stop_rejection"


def test_persistent_playing_requires_one_explicit_tp_stop_action() -> None:
    clock = _Clock()

    def exchange(_host, commands, **_kwargs):
        result = {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"}
        if commands[0] == "stop":
            result["stop"] = "Command is not allowed in Local Control"
        return result

    result = live._stop_v3_program(
        "robot",
        timeout_s=0.3,
        poll_interval_s=0.1,
        exchange=exchange,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result["ok"] is False
    assert result["required_operator_action"] == "PRESS_TP_STOP"
