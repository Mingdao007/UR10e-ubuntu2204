from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_force_search_canary_r001 as blocked_builder  # noqa: E402
import build_step5d_force_search_canary_r002 as blocked_r002_builder  # noqa: E402
import build_step5d_force_search_canary_r003 as blocked_r003_builder  # noqa: E402
import build_step5d_force_search_canary_r004 as blocked_r004_builder  # noqa: E402
import build_step5d_force_search_canary_r005 as builder  # noqa: E402
import step5d_force_search_bridge_contract as bridge_contract  # noqa: E402
import step5d_force_search_primitive as primitive  # noqa: E402


def test_canary_contract_is_slow_and_strictly_bounded() -> None:
    contract = primitive.load_contract()

    assert contract.search_speed_m_s == pytest.approx(-0.0005)
    assert contract.contact_force_norm_n == pytest.approx(1.0)
    assert contract.hard_force_norm_n == pytest.approx(3.0)
    assert contract.hard_torque_norm_nm == pytest.approx(0.2)
    assert contract.initial_abs_normal_force_max_n == pytest.approx(0.65)
    assert contract.max_travel_m == pytest.approx(0.025)
    assert contract.retract_distance_m == pytest.approx(0.005)


def test_hard_guard_precedes_contact_latch() -> None:
    contract = primitive.load_contract()

    assert (
        primitive.stop_reason(
            contract,
            normal_force_n=3.1,
            force_norm_n=3.1,
            torque_norm_nm=0.0,
            delta_normal_force_n=3.1,
            delta_force_norm_n=3.1,
            delta_torque_norm_nm=0.0,
            sensor_ok=True,
            stop_requested=False,
            heartbeat_stale_s=0.0,
            travel_m=0.001,
            elapsed_s=1.0,
        )
        == 5
    )


def test_one_newton_entry_relative_contact_latches_success() -> None:
    contract = primitive.load_contract()

    assert (
        primitive.stop_reason(
            contract,
            normal_force_n=-1.21,
            force_norm_n=1.3,
            torque_norm_nm=0.01,
            delta_normal_force_n=-0.81,
            delta_force_norm_n=0.9,
            delta_torque_norm_nm=0.01,
            sensor_ok=True,
            stop_requested=False,
            heartbeat_stale_s=0.0,
            travel_m=0.004,
            elapsed_s=8.0,
        )
        == 11
    )
    assert (
        primitive.stop_reason(
            contract,
            normal_force_n=-0.8,
            force_norm_n=1.41,
            torque_norm_nm=0.01,
            delta_normal_force_n=-0.4,
            delta_force_norm_n=1.01,
            delta_torque_norm_nm=0.01,
            sensor_ok=True,
            stop_requested=False,
            heartbeat_stale_s=0.0,
            travel_m=0.004,
            elapsed_s=8.0,
        )
        == 11
    )


def test_sensor_fault_and_external_stop_never_look_like_contact() -> None:
    contract = primitive.load_contract()

    common = {
        "normal_force_n": 0.0,
        "force_norm_n": 0.0,
        "torque_norm_nm": 0.0,
        "delta_normal_force_n": 0.0,
        "delta_force_norm_n": 0.0,
        "delta_torque_norm_nm": 0.0,
        "heartbeat_stale_s": 0.0,
        "travel_m": 0.0,
        "elapsed_s": 0.0,
    }
    assert primitive.stop_reason(contract, sensor_ok=False, stop_requested=False, **common) == 3
    assert primitive.stop_reason(contract, sensor_ok=True, stop_requested=True, **common) == 4


def test_nan_is_fail_closed_in_model_and_rendered_script() -> None:
    contract = primitive.load_contract()
    reason = primitive.stop_reason(
        contract,
        normal_force_n=float("nan"),
        force_norm_n=0.0,
        torque_norm_nm=0.0,
        delta_normal_force_n=0.0,
        delta_force_norm_n=0.0,
        delta_torque_norm_nm=0.0,
        sensor_ok=True,
        stop_requested=False,
        heartbeat_stale_s=0.0,
        travel_m=0.0,
        elapsed_s=0.0,
    )
    script = primitive.render_script()

    assert reason == 3
    assert script.index("if not codex_finite_value(normal_force") < script.index(
        "elif heartbeat_stale_s > stale_limit_s"
    )
    assert script.index("elif heartbeat_stale_s > stale_limit_s") < script.index(
        "speedl("
    )


def test_rendered_script_has_no_old_home_or_autotune_behavior() -> None:
    script = primitive.render_script()
    main = script[script.index(f"def {primitive.PROGRAM_NAME}():") :]

    assert main.index("set_target_payload(") < main.index("set_tcp(")
    assert main.index("set_tcp(") < main.index("speedl(")
    assert "0.487834547" not in script
    assert "0.129337053" not in script
    assert "force_mode(" not in script
    assert "speedj(" not in script
    assert "servoj(" not in script
    assert "zero_ftsensor(" not in script
    assert "write_output_float_register(34" not in script
    assert script.count("movel(retract_pose") == 1
    assert script.count("speedl(") == 1
    assert script.count("movel(") == 1
    assert "0.059140000" not in script
    assert script.count(
        "set_tcp(p[0.000000000, 0.000000000, 0.087400000,"
    ) == 1
    assert "fresh_heartbeat_count >= 3" in script
    assert "codex_abs(delta_normal)" in script
    assert "get_tcp_force(" not in script
    assert "actual_TCP_force" not in script
    assert "internal_force" not in script


def test_bridge_context_is_observer_only() -> None:
    contract = bridge_contract.load_bridge_contract()

    assert contract.artifact_id == "new-eoat-force-search-observer-20260730"
    assert "--write-rtde-inputs" in contract.argv
    assert "--normal-axis" in contract.argv


def test_r001_candidate_is_blocked() -> None:
    assert blocked_builder.BLOCKED_MARKER.is_file()
    with pytest.raises(RuntimeError, match="blocked by Opus review"):
        blocked_builder.write_triplet(
            ROOT / "runs/should-not-exist",
            "2026-07-30T0400HKT_STEP5D_FORCE_SEARCH_CANARY_R001",
        )


def test_r002_candidate_is_superseded() -> None:
    assert blocked_r002_builder.BLOCKED_MARKER.is_file()
    with pytest.raises(RuntimeError, match="superseded after live preflight"):
        blocked_r002_builder.write_triplet(
            ROOT / "runs/should-not-exist",
            "2026-07-30T0410HKT_STEP5D_FORCE_SEARCH_CANARY_R002",
        )


def test_r003_candidate_is_superseded() -> None:
    assert blocked_r003_builder.BLOCKED_MARKER.is_file()
    with pytest.raises(RuntimeError, match="superseded before live Play"):
        blocked_r003_builder.write_triplet(
            ROOT / "runs/should-not-exist",
            "2026-07-30T0350HKT_STEP5D_FORCE_SEARCH_CANARY_R003",
        )


def test_r004_candidate_is_superseded() -> None:
    assert blocked_r004_builder.BLOCKED_MARKER.is_file()
    with pytest.raises(RuntimeError, match="forbidden UR built-in force dependency"):
        blocked_r004_builder.write_triplet(
            ROOT / "runs/should-not-exist",
            "2026-07-30T0400HKT_STEP5D_FORCE_SEARCH_CANARY_R004",
        )


def test_triplet_round_trip(tmp_path: Path) -> None:
    result = builder.write_triplet(
        tmp_path,
        "2026-07-30T0400HKT_STEP5D_FORCE_SEARCH_CANARY_R005",
    )

    assert all(result["checks"].values())
    assert set(result["sha256"]) == {".script", ".txt", ".urp"}
