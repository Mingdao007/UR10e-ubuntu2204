from __future__ import annotations

from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
VIC_ROOT = ROOT.parent / "ur10e-variable-impedance"
for import_root in (TOOLS, VIC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

import run_tacdiffusion_formal_v4 as formal  # noqa: E402
import run_tacdiffusion_remote_direct_torque_v4 as legacy  # noqa: E402
from ur10e_vic.tacdiffusion.direct_torque_live_v4 import (  # noqa: E402
    build_live_receiver_source,
    parse_live_receiver_source,
)
from ur10e_vic.tacdiffusion.trajectory import TRAJECTORY_FAMILIES  # noqa: E402


def _qualification_row(*, timestamp_s: float) -> dict[str, float | int]:
    row: dict[str, float | int] = {
        "receiver_state": legacy.STATE_TORQUE,
        "controller_timestamp_s": timestamp_s,
        "ack_command_lineage_missing": 0,
        "robot_mode": legacy.ROBOT_MODE_RUNNING,
        "safety_mode": legacy.SAFETY_MODE_NORMAL,
        "action_echo_coherent": 1,
    }
    row.update(
        {
            "ack_desired_pose_x": 0.48,
            "ack_desired_pose_y": 0.13,
            "ack_desired_pose_z": 0.02,
            "ack_desired_pose_rx": 3.12,
            "ack_desired_pose_ry": 0.0,
            "ack_desired_pose_rz": 0.068,
            "echo_desired_pose_x": 0.48,
            "echo_desired_pose_y": 0.13,
            "echo_desired_pose_z": 0.02,
            "echo_desired_pose_rx": 3.12,
            "echo_desired_pose_ry": 0.0,
            "echo_desired_pose_rz": 0.068,
            "ack_command": legacy.MODE_RUN,
            "echo_command": legacy.MODE_RUN,
            "ack_command_sequence": 1,
            "echo_command_sequence": 1,
            "ack_lease_id": 2,
            "echo_lease_id": 2,
            "ack_episode_identity": 3,
            "echo_episode_identity": 3,
        }
    )
    return row


def test_formal_tool_has_exactly_all_seven_families_and_no_model_active_path() -> None:
    source = Path(formal.__file__).read_text(encoding="utf-8")
    assert len(TRAJECTORY_FAMILIES) == 7
    assert "model_active\": False" in source
    assert "actual_TCP_force" not in source
    assert "get_tcp_force" not in source


def test_formal_contact_receiver_uses_kunwei_20n_2nm_guard() -> None:
    tube = formal._episode_tube((0.48, 0.13, 0.02, 3.12, 0.0, 0.068))
    receiver = build_live_receiver_source(
        tube, guard_force_limit_n=20.0, guard_torque_limit_nm=2.0
    )
    contract = parse_live_receiver_source(receiver)
    assert contract.guard_force_limit_n == 20.0
    assert contract.guard_torque_limit_nm == 2.0
    assert "get_tcp_force" not in receiver


def test_command_packet_carries_typed_expert_force_and_stiffness() -> None:
    packet = legacy._command_packet(
        command=legacy.MODE_RUN,
        sequence=1,
        progress_s=0.1,
        pose=(0.48, 0.13, 0.02, 3.12, 0.0, 0.068),
        lease_id=1,
        episode_identity=2,
        stiffness_6d=(700.0, 700.0, 700.0, 30.0, 30.0, 30.0),
        raw_f_ff_6d=(0.0, 0.0, 5.0, 0.0, 0.0, 0.0),
    )
    assert packet.lineage.commanded_k == (
        700.0,
        700.0,
        700.0,
        30.0,
        30.0,
        30.0,
    )
    assert packet.lineage.commanded_raw_f_ff == (0.0, 0.0, 5.0, 0.0, 0.0, 0.0)


def test_strict_episode_gate_uses_zeroed_kunwei_summary_norms() -> None:
    rows = [_qualification_row(timestamp_s=index / 500.0) for index in range(501)]
    gate = formal._strict_episode_gate(
        rows=rows,
        kunwei={
            "parse_errors": 0,
            "dropped_sync_bytes": 0,
            "rate_hz_by_first_last": 1000.0,
            "max_zeroed_force_norm_n": 5.5,
            "max_zeroed_torque_norm_nm": 0.4,
        },
        complete=True,
        duration_s=1.0,
    )
    assert gate["ok"] is True


def test_contact_tube_contains_full_search_and_retract_corridor() -> None:
    contract = formal.ContactAcquisitionContractV1()
    entry = (0.48, 0.13, 0.033, 3.12, 0.0, 0.068)
    tube = formal._contact_episode_tube(entry, contract)
    search_end = contract.search_pose(entry, 1000.0)
    retract_end = contract.retract_pose(search_end, 1.0)
    tube.assert_contains_pose(entry, role="entry")
    tube.assert_contains_pose(search_end, role="search_end")
    tube.assert_contains_pose(retract_end, role="retract_end")
    assert search_end[2] == pytest.approx(entry[2] - 0.025)
    assert retract_end[2] == pytest.approx(search_end[2] + 0.010)


def test_contact_receiver_tube_accounts_for_controller_entry_rebase() -> None:
    contract = formal.ContactAcquisitionContractV1()
    entry = (0.48, 0.13, 0.033, 3.12, 0.0, 0.068)
    tube = formal._contact_receiver_tube(entry, contract)
    receiver = build_live_receiver_source(
        tube,
        friction_profile=legacy.FRICTION_PROFILE_UR_DEFAULT_V2_FORMAL_CONTACT,
        guard_force_limit_n=20.0,
        guard_torque_limit_nm=2.0,
    )
    parsed = parse_live_receiver_source(receiver)

    assert tube.center_base_m == entry[:3]
    assert tube.normal_half_width_m == pytest.approx(0.0255)
    assert "local tube_normal_half_width_m = 0.025500000000000002" in receiver
    assert parsed.friction_profile == "ur_default_v2_formal_contact"


def test_approach_feedforward_is_named_base_minus_z_transformed_to_tcp() -> None:
    identity_pose = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert formal._approach_feedforward_tcp(identity_pose, 5.0) == pytest.approx(
        (0.0, 0.0, -5.0, 0.0, 0.0, 0.0)
    )
    downward_tool = (0.48, 0.13, 0.033, 3.141592653589793, 0.0, 0.0)
    assert formal._approach_feedforward_tcp(downward_tool, 5.0) == pytest.approx(
        (0.0, 0.0, 5.0, 0.0, 0.0, 0.0), abs=1.0e-9
    )


def test_formal_parser_exposes_resumable_contact_campaign_without_default_live() -> None:
    parser = formal.build_parser()
    parsed = parser.parse_args(
        [
            "collect-campaign",
            "--kind",
            "fixed_k",
            "--campaign-root",
            "/tmp/formal-fixed",
            "--dynamics-evidence",
            "/tmp/dynamics.json",
        ]
    )
    assert parsed.live is False
    assert parsed.allow_contact is False
    assert parsed.max_attempts == 0
    assert parsed.sensor_delivery_watchdog_s == 0.080

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "collect-campaign",
                "--kind",
                "fixed_k",
                "--campaign-root",
                "/tmp/formal-fixed",
                "--dynamics-evidence",
                "/tmp/dynamics.json",
                "--sensor-delivery-watchdog-s",
                "0.010",
            ]
        )
