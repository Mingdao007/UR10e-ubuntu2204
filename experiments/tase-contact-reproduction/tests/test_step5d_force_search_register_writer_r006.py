from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_force_search_register_writer_r006 as writer  # noqa: E402


def test_writer_contract_is_live_writer_not_observer() -> None:
    contract = writer.load_contract()

    assert contract.resource_id == "step5d-bridge-writer"
    assert contract.rtde_hz == pytest.approx(125.0)
    assert contract.period_s == pytest.approx(0.008)
    assert contract.baseline_s == pytest.approx(5.0)
    assert contract.stale_s == pytest.approx(0.08)
    assert contract.terminal_exit_timeout_s == pytest.approx(2.0)
    assert contract.active_stages == (10, 11, 12, 13, 14)
    assert contract.terminal_stages == (15, 90)


def test_controller_get_ack_requires_exact_payload_cog_tcp() -> None:
    contract = writer.load_contract()
    exact = {
        "payload": 0.413,
        "payload_cog": [0.0011, 0.0031, 0.0163],
        "tcp_offset": [0.0, 0.0, 0.0874, 0.0, 0.0, 0.0],
    }

    assert writer.controller_get_matches(contract, exact)
    assert not writer.controller_get_matches(
        contract, {**exact, "tcp_offset": [0.0, 0.0, 0.05914, 0.0, 0.0, 0.0]}
    )
    assert not writer.controller_get_matches(
        contract, {**exact, "payload": float("nan")}
    )


def test_packet_uses_positive_normal_and_hard_absolute_guard() -> None:
    contract = writer.load_contract()
    packet, reason = writer.register_packet(
        contract,
        wrench=(0.0, 0.0, -0.8, 0.0, 0.0, 0.0),
        sensor_fresh=True,
        heartbeat=10.0,
        eoat_get_ack=True,
    )

    assert packet["normal_load_n"] == pytest.approx(0.8)
    assert packet["sensor_fresh"] == 1.0
    assert packet["eoat_get_ack"] == 1.0
    assert packet["stop_request"] == 0.0
    assert reason is None

    guarded, reason = writer.register_packet(
        contract,
        wrench=(0.0, 0.0, 3.0, 0.0, 0.0, 0.0),
        sensor_fresh=True,
        heartbeat=11.0,
        eoat_get_ack=True,
    )
    assert guarded["normal_load_n"] == pytest.approx(-3.0)
    assert guarded["stop_request"] == 1.0
    assert reason == "hard_abs_normal"


@pytest.mark.parametrize(
    "wrench",
    [
        (math.nan, 0.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, math.inf, 0.0, 0.0, 0.0, 0.0),
    ],
)
def test_nonfinite_packet_fails_closed(wrench: tuple[float, ...]) -> None:
    contract = writer.load_contract()
    packet, reason = writer.register_packet(
        contract,
        wrench=wrench,
        sensor_fresh=True,
        heartbeat=1.0,
        eoat_get_ack=True,
    )

    assert packet["sensor_fresh"] == 0.0
    assert packet["stop_request"] == 1.0
    assert reason == "sensor_stale_or_unready"


def test_zeroed_wrench_converts_manual_units_then_subtracts_baseline() -> None:
    baseline = (9.80665, 0.0, -9.80665, 0.980665, 0.0, 0.0)

    result = writer.zeroed_wrench((2.0, 0.0, -2.0, 0.2, 0.0, 0.0), baseline)

    assert result == pytest.approx(
        (9.80665, 0.0, -9.80665, 0.980665, 0.0, 0.0)
    )
