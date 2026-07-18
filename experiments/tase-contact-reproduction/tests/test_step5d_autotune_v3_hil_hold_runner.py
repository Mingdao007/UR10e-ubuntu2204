from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_hil_hold as gate  # noqa: E402


def _row(t: float, *, command: int = 0, state: int = 10) -> dict[str, float]:
    row = {
        "t_monotonic_s": t,
        "command": command,
        "ur_output_int_register_24": 0,
        "ur_output_int_register_25": 0,
        "ur_output_int_register_26": state,
        "ur_output_int_register_27": 0,
        "ur_output_int_register_28": 0,
        "ur_output_int_register_29": 0,
        "ur_output_int_register_30": 0,
    }
    row.update({f"ur_actual_q_{index}": 0.0 for index in range(6)})
    row.update({f"ur_actual_TCP_pose_{index}": 0.0 for index in range(6)})
    row.update({f"ur_actual_TCP_speed_{index}": 0.0 for index in range(6)})
    return row


def test_stationary_ready_home_hold_acceptance() -> None:
    result = gate.evaluate_hold_rows([_row(index * 0.5) for index in range(12)])
    assert result["ready_home_dwell_s"] >= 5.0
    assert result["observed_maxima"]["tcp_speed_m_s"] == 0.0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: rows[2].update(command=1),
        lambda rows: rows[2].update(ur_output_int_register_24=1),
        lambda rows: rows[2].update(ur_actual_TCP_speed_0=0.002),
        lambda rows: rows[-1].update(ur_actual_q_0=0.003),
        lambda rows: rows[-1].update(ur_actual_TCP_pose_0=0.001),
    ],
)
def test_arm_identity_and_motion_mutations_fail_closed(mutation) -> None:
    rows = [_row(index * 0.5) for index in range(12)]
    mutation(rows)
    with pytest.raises(gate.HilHoldError):
        gate.evaluate_hold_rows(rows)


def test_offline_check_builds_zero_identity_wrapper_command(tmp_path: Path) -> None:
    output = tmp_path / "check"
    args = gate.parse_args(["--output-root", str(output), "--check", "--json"])
    result = gate.run_gate(args)
    assert result["ok"] is True
    assert result["claim"] == "offline_static_check_only"
    assert result["bridge_started"] is False


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.value += duration


def test_remote_dashboard_stop_proves_stopped() -> None:
    replies: list[dict[str, Any]] = [
        {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"},
        {"stop": "Stopped", "programState": "STOPPED step5d_strict_rnn_autotune_v3.urp"},
    ]

    def exchange(*_args, **_kwargs):
        return replies.pop(0)

    result = gate._stop_v3_program("robot", exchange=exchange)
    assert result["ok"] is True
    assert result["method"] == "dashboard_stop"


def test_local_stop_rejection_then_observed_stopped_is_cleanup_success() -> None:
    replies: list[dict[str, Any]] = [
        {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"},
        {
            "stop": "Command is not allowed; switch robot to Remote Control mode",
            "programState": "PLAYING step5d_strict_rnn_autotune_v3.urp",
        },
        {"programState": "STOPPED step5d_strict_rnn_autotune_v3.urp"},
    ]
    clock = _Clock()

    def exchange(*_args, **_kwargs):
        return replies.pop(0)

    result = gate._stop_v3_program(
        "robot",
        exchange=exchange,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result["ok"] is True
    assert result["method"] == "observed_stopped_after_stop_rejection"
    assert "Remote Control" in result["stop_request"]["stop"]


def test_local_stop_rejection_and_persistent_playing_requires_tp_stop() -> None:
    clock = _Clock()
    calls = 0

    def exchange(_host, commands, **_kwargs):
        nonlocal calls
        calls += 1
        result = {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"}
        if commands[0] == "stop":
            result["stop"] = "Command is not allowed in Local Control"
        return result

    result = gate._stop_v3_program(
        "robot",
        timeout_s=0.3,
        poll_interval_s=0.1,
        exchange=exchange,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert calls >= 3
    assert result["ok"] is False
    assert result["method"] == "tp_stop_required"
    assert result["required_operator_action"] == "PRESS_TP_STOP"
