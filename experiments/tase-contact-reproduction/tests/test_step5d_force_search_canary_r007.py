from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import pytest

from step5d_force_search_canary_shared import ForceSearchCanaryAxisReturnPolicy

import build_step5d_force_search_canary_r007 as builder  # noqa: E402
import step5d_force_search_canary_r006 as canary006  # noqa: E402
import step5d_force_search_canary_r007 as canary  # noqa: E402


def test_r007_fixed_contract() -> None:
    contract = canary.load_contract()

    assert contract.axis == ForceSearchCanaryAxisReturnPolicy.NEGATIVE_Z_SEARCH_EXTERNAL_SCRIPT1_HOME
    assert contract.search_speed_m_s == pytest.approx(0.0002)
    assert contract.search_acceleration_m_s2 == pytest.approx(0.005)
    assert contract.max_travel_m == pytest.approx(0.018)
    assert contract.runtime_limit_s == pytest.approx(90.0)
    assert contract.search_stopl_acceleration_m_s2 == pytest.approx(0.01)
    assert contract.startup_increment_count == 3
    assert contract.heartbeat_gap_s == pytest.approx(0.08)
    assert contract.contact_positive_normal_n == pytest.approx(0.5)
    assert contract.contact_force_norm_n == pytest.approx(0.7)
    assert contract.hard_abs_normal_n == pytest.approx(3.0)
    assert contract.hard_force_norm_n == pytest.approx(3.0)
    assert contract.hard_torque_norm_nm == pytest.approx(0.2)
    assert contract.stationary_dwell_s == pytest.approx(0.25)
    assert contract.retract_distance_m == pytest.approx(0.0)


def test_shared_parser_and_reason_adapter() -> None:
    assert canary.CanaryR007Contract is canary006.CanaryR006Contract
    assert canary.stop_reason is canary006.stop_reason


def test_renderers_match_with_program_name_projection() -> None:
    shared = canary006.load_contract()
    contract = canary.load_contract()
    assert canary.render_script(contract).count("speedl(") == 1
    assert shared.search_speed_m_s != contract.search_speed_m_s

def test_positive_normal_or_force_norm_latches_contact() -> None:
    contract = canary.load_contract()
    common = {
        "torque_norm_nm": 0.01,
        "sensor_fresh": True,
        "stop_requested": False,
        "heartbeat_gap_s": 0.0,
        "travel_m": 0.001,
        "elapsed_s": 1.0,
    }

    assert (
        canary.stop_reason(
            contract, normal_load_n=0.5, force_norm_n=0.4, **common
        )
        == 11
    )
    assert (
        canary.stop_reason(
            contract, normal_load_n=-0.5, force_norm_n=0.7, **common
        )
        == 11
    )
    assert (
        canary.stop_reason(
            contract, normal_load_n=-1.5, force_norm_n=0.5, **common
        )
        == 0
    )


def test_hard_guard_precedes_contact_and_contact_is_positive_only() -> None:
    contract = canary.load_contract()

    assert (
        canary.stop_reason(
            contract,
            normal_load_n=-3.0,
            force_norm_n=0.8,
            torque_norm_nm=0.01,
            sensor_fresh=True,
            stop_requested=False,
            heartbeat_gap_s=0.0,
            travel_m=0.001,
            elapsed_s=1.0,
        )
        == 5
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"normal_load_n": float("nan")}, 3),
        ({"force_norm_n": float("inf")}, 3),
        ({"heartbeat_gap_s": 0.08}, 2),
        ({"sensor_fresh": False}, 3),
        ({"stop_requested": True}, 4),
    ],
)
def test_fail_closed_faults(kwargs: dict[str, object], expected: int) -> None:
    contract = canary.load_contract()
    values: dict[str, object] = {
        "normal_load_n": 0.0,
        "force_norm_n": 0.0,
        "torque_norm_nm": 0.0,
        "sensor_fresh": True,
        "stop_requested": False,
        "heartbeat_gap_s": 0.0,
        "travel_m": 0.0,
        "elapsed_s": 0.0,
    }
    values.update(kwargs)

    assert canary.stop_reason(contract, **values) == expected


def test_script_has_stationary_gate_and_no_forbidden_terms() -> None:
    script = canary.render_script()

    assert script.count("speedl(") == 1
    assert script.count("speedl([0.0, 0.0, -") == 1
    assert "while reason == 11.0 and retract_travel_m <" not in script
    assert "retract_start_z" not in script
    assert "retract_travel_m" not in script
    assert "codex_echo(14.0, reason" not in script
    assert "stage14" not in script
    assert "normal_load >= 0.500000000" in script
    assert "normal_load >= 0.800000000" not in script
    assert "force_norm >= 0.700000000" in script
    assert "force_norm >= 1.000000000" not in script
    assert "speedl([0.0, 0.0, 0.000" not in script
    assert "codex_abs(normal_load)" in script
    assert "stationary_dwell_s < 0.250000000" in script
    assert (
        script.index("stationary_dwell_s < 0.250000000")
        < script.index("codex_echo(15.0, reason")
    )
    assert "force_mode(" not in script
    assert "get_tcp_force(" not in script
    assert "actual_TCP_force" not in script
    assert "movej(" not in script
    assert "movel(" not in script
    assert "home(" not in script
    terminal_entry = "def step5d_force_search_canary_r007():"
    terminal_call = "step5d_force_search_canary_r007()\n"
    terminal_start = script.rfind(terminal_entry)
    terminal_call_index = script.rfind(terminal_call)
    assert terminal_start >= 0
    assert terminal_call_index > terminal_start
    terminal_body = script[terminal_start:terminal_call_index]
    terminal_end = terminal_body.rfind("end\n")
    assert terminal_end >= 0
    assert "halt" in terminal_body[:terminal_end]
    assert script.endswith(terminal_call)


def test_r007_triplet_round_trip_and_numeric_sanity(tmp_path: Path) -> None:
    result = builder.write_triplet(
        tmp_path,
        "2026-07-30T1320HKT_STEP5D_FORCE_SEARCH_CANARY_R007",
    )

    assert all(result["checks"].values())
    assert set(result["sha256"]) == {".script", ".txt", ".urp"}
    assert (
        tmp_path / "step5d_force_search_canary_r007.numeric-sanity.json"
    ).is_file()
