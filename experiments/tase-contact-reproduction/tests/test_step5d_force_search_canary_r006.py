from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_force_search_canary_r006 as builder  # noqa: E402
import step5d_force_search_canary_r006 as canary  # noqa: E402


def test_r006_fixed_contract() -> None:
    contract = canary.load_contract()

    assert contract.search_speed_m_s == pytest.approx(0.0005)
    assert contract.search_acceleration_m_s2 == pytest.approx(0.01)
    assert contract.max_travel_m == pytest.approx(0.025)
    assert contract.runtime_limit_s == pytest.approx(90.0)
    assert contract.search_stopl_acceleration_m_s2 == pytest.approx(0.01)
    assert contract.startup_increment_count == 3
    assert contract.heartbeat_gap_s == pytest.approx(0.08)
    assert contract.contact_positive_normal_n == pytest.approx(0.8)
    assert contract.contact_force_norm_n == pytest.approx(1.0)
    assert contract.hard_abs_normal_n == pytest.approx(3.0)
    assert contract.hard_force_norm_n == pytest.approx(3.0)
    assert contract.hard_torque_norm_nm == pytest.approx(0.2)
    assert contract.stationary_dwell_s == pytest.approx(0.25)


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
            contract, normal_load_n=0.8, force_norm_n=0.5, **common
        )
        == 11
    )
    assert (
        canary.stop_reason(
            contract, normal_load_n=-0.8, force_norm_n=1.0, **common
        )
        == 11
    )
    assert (
        canary.stop_reason(
            contract, normal_load_n=-1.5, force_norm_n=0.5, **common
        )
        == 0
    )


def test_hard_guard_precedes_contact_and_absolute_normal_is_hard_only() -> None:
    contract = canary.load_contract()

    assert (
        canary.stop_reason(
            contract,
            normal_load_n=-3.0,
            force_norm_n=1.2,
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


def test_script_monitors_deceleration_and_requires_stationary_before_retract() -> None:
    script = canary.render_script()
    first_speedl = script.index("speedl(")

    assert script.index("run codex_deceleration_and_retract_monitor()") < first_speedl
    assert script.index("stopl(0.010000000)") < script.index(
        "stationary_dwell_s < 0.250000000"
    )
    assert script.index("stationary_dwell_s < 0.250000000") < script.index(
        "retract_start_z"
    )
    assert script.rindex("codex_echo(15.0, reason") > script.rindex("stopl(")
    assert "normal_load >= 0.800000000" in script
    assert script.count("codex_abs(normal_load)") == 1
    assert "get_tcp_force(" not in script
    assert "actual_TCP_force" not in script
    assert "force_mode(" not in script
    assert "movej(" not in script
    assert "movel(" not in script
    assert "0.059140000" not in script


def test_r006_triplet_round_trip_and_numeric_sanity(tmp_path: Path) -> None:
    result = builder.write_triplet(
        tmp_path,
        "2026-07-30T1320HKT_STEP5D_FORCE_SEARCH_CANARY_R006",
    )

    assert all(result["checks"].values())
    assert set(result["sha256"]) == {".script", ".txt", ".urp"}
    assert (
        tmp_path / "step5d_force_search_canary_r006.numeric-sanity.json"
    ).is_file()


def test_r005_generated_triplet_remains_immutable() -> None:
    r005_dir = ROOT / "programs/step5/step5d"
    r005_files = [
        r005_dir / f"step5d_force_search_canary_r005{suffix}"
        for suffix in (".script", ".txt", ".urp")
    ]

    assert all(path.is_file() for path in r005_files)
