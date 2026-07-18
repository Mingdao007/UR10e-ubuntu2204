from __future__ import annotations

import sys
from pathlib import Path

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


def test_hil_source_always_stops_program_after_ready_signal() -> None:
    source = (ROOT / "tools/run_step5d_autotune_v3_hil_hold.py").read_text(
        encoding="utf-8"
    )
    finally_body = source.split("        finally:", 1)[1].split(
        "    if dashboard_stop_error", 1
    )[0]
    assert "if ready_announced:" in finally_body
    assert "_stop_v3_program" in finally_body
    assert "process.send_signal(signal.SIGINT)" in finally_body
