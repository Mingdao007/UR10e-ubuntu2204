import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import re

import pytest

from ur10e_vic.tacdiffusion.direct_torque_live_v4 import (
    COMPILE_PROBE_PROTOCOL_TOKEN,
    LIVE_PROTOCOL_TOKEN,
    LIVE_RECEIVER_SCHEMA,
    ORIENTATION_POLICY_HOLD_ENTRY,
    ORIENTATION_POLICY_INTERPOLATE_POSE,
    LiveTubeContract,
    SequenceDecision,
    build_compile_probe_source,
    build_live_receiver_source,
    evaluate_sequence,
    parse_compile_probe_source,
    parse_live_receiver_source,
)


ROOT = Path(__file__).resolve().parents[1]
PASSIVE_RUN = (
    ROOT.parent
    / "tase-contact-reproduction"
    / "runs"
    / "tacdiffusion"
    / "passive_remote_baseline_20260726"
)
REFERENCE = PASSIVE_RUN / "unknown_surface_anchor_circle_no_contact_2s_reference_v2.json"
BUILDER = ROOT / "tools" / "build_tacdiffusion_direct_torque_live_v4.py"


def reference_path() -> Path:
    if REFERENCE.is_file():
        return REFERENCE
    pytest.skip("fresh passive reference artifact is unavailable")


def test_live_tube_loads_fresh_anchor_without_cad_feedforward() -> None:
    path = reference_path()
    tube = LiveTubeContract.from_reference_artifact(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert tube.anchor_pose_base == tuple(payload["anchor_pose_base"])
    assert tube.normal_half_width_m == pytest.approx(0.002)
    assert payload["cad_height_or_normal_feedforward"] is False


def test_live_tube_rebases_certified_envelope_to_fresh_entry_pose() -> None:
    tube = LiveTubeContract.from_reference_artifact(reference_path())
    entry = list(tube.anchor_pose_base)
    entry[0] += 0.001
    entry[1] += 0.006
    entry[2] -= 0.025
    rebased = tube.rebased(entry)
    assert rebased.center_base_m == pytest.approx(entry[:3])
    assert rebased.anchor_pose_base == pytest.approx(entry)
    assert rebased.u_axis_base == tube.u_axis_base
    assert rebased.v_axis_base == tube.v_axis_base
    assert rebased.safe_u_half_width_m == tube.safe_u_half_width_m
    assert rebased.safe_v_half_width_m == tube.safe_v_half_width_m
    assert rebased.normal_half_width_m == tube.normal_half_width_m
    rebased.assert_contains_pose(entry, role="actual")


def test_receiver_v4_is_invoked_holds_packets_and_returns_through_stopj() -> None:
    source = build_live_receiver_source(
        LiveTubeContract.from_reference_artifact(reference_path())
    )
    contract = parse_live_receiver_source(source)
    assert contract.schema == LIVE_RECEIVER_SCHEMA
    assert contract.protocol_token == LIVE_PROTOCOL_TOKEN
    assert contract.invocation_present
    assert contract.explicit_position_handoff
    assert contract.complete_identity_echo
    assert contract.applied_action_echo
    assert contract.hard_tube_guard
    assert contract.dedicated_torque_thread
    assert contract.orientation_interpolation_policy == ORIENTATION_POLICY_HOLD_ENTRY
    assert not contract.source_builder_physical_io_enabled
    assert contract.controller_runtime_physical_io_enabled
    assert source.rstrip().endswith("end")
    assert source.startswith("def tacdiffusion_remote_direct_torque_v4_program():\n")
    assert len(re.findall(r"(?m)^\s*def\s+", source)) == 1
    assert not re.search(r"(?m)^(?:local|global)\s+", source)
    assert not re.search(
        r"(?m)^\s*tacdiffusion_remote_direct_torque_v4_program\(\)\s*$",
        source,
    )
    assert f'receiver_schema = "{LIVE_RECEIVER_SCHEMA}"' in source
    assert "running = True" in source
    assert "entry_elapsed_s = 0.0" in source
    assert "last_sequence = 0" in source
    assert "held_age_s = 0.0" in source
    assert "torque_thread_handle = 0" in source
    assert source.count("local compare_axis = 0") == 1
    assert source.count("local zero_axis = 0") == 1
    assert source.count("local filter_c = ") == 1
    assert "local feedforward_base = wrench_trans(" in source
    assert source.count("thread torqueThread():") == 1
    assert source.count("stopj(10.0)") == 1
    assert "release_ready_tolerance_m = 0.001" in source
    assert "write_output_float_register(32 + axis, control_k[axis])" in source
    assert "write_output_float_register(26 + axis, filtered_force[axis])" in source
    assert "write_output_float_register(38 + axis, tau[axis])" in source
    assert source.count(
        "direct_torque(torque, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)"
    ) == 1
    torque_thread_start = source.index("thread torqueThread():")
    program_start = source.index(f'receiver_schema = "{LIVE_RECEIVER_SCHEMA}"')
    torque_thread_source = source[torque_thread_start:program_start]
    program_source = source[program_start:]
    assert "local torque = torque_command" in torque_thread_source
    assert (
        "torque_thread_tick_count = torque_thread_tick_count + 1"
        in torque_thread_source
    )
    assert "sync()" not in torque_thread_source
    assert not re.search(r"(?m)^\s*direct_torque\(", program_source)
    assert source.count("torque_thread_handle = run torqueThread()") == 1
    assert source.count("join torque_thread_handle") == 1
    assert source.index("torque_command = tau") < source.index(
        "torque_thread_handle = run torqueThread()"
    )
    assert source.index("torque_thread_run = False", program_start) < source.index(
        "join torque_thread_handle"
    )
    assert "friction_comp=True" not in source
    assert "viscous_scale = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]" in source
    assert "coulomb_scale = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]" in source
    assert "blend*viscous_scale_target[axis]" not in source
    assert "blend*coulomb_scale_target[axis]" not in source
    assert "direct_torque([0.0" not in source
    assert "entry_pose[axis] = actual_pose[axis]" in source
    assert "tube_rebased = False" in source
    assert (
        "tube_center_base = [actual_pose[0], actual_pose[1], actual_pose[2]]"
        in source
    )
    assert (
        "tube_anchor_pose_base = p[actual_pose[0], actual_pose[1], actual_pose[2], "
        "actual_pose[3], actual_pose[4], actual_pose[5]]"
        in source
    )
    assert "entry_stable_duration_s = 0.05" in source
    assert "entry_tcp_translation_speed_limit_m_s = 0.001" in source
    assert "entry_tcp_rotation_speed_limit_rad_s = 0.002" in source
    assert "entry_joint_speed_limit_rad_s = 0.001" in source
    assert "entry_stable_elapsed_s < entry_stable_duration_s" in source
    assert "elif not entry_ready:" in source
    assert (
        source.index("elif not entry_ready:")
        < source.index("write_output_integer_register(24, 0)", source.index("elif not entry_ready:"))
    )
    assert "guard_wrench = [read_input_float_register(36)" in source
    assert "guard_force_norm > 6.0 or guard_torque_norm > 0.5" in source
    assert "get_tcp_force()" not in source
    assert "entry_elapsed_s < entry_blend_duration_s" in source
    assert "blend = entry_elapsed_s / entry_blend_duration_s" in source
    assert "orientation_interpolation_policy =" not in source
    assert "if axis < 3:" in source
    assert "control_eq[axis] = entry_pose[axis]" in source
    assert (
        "control_eq[axis] = entry_pose[axis] + "
        "blend*(last_eq[axis] - entry_pose[axis])"
    ) in source
    assert "control_k[axis] = last_k[axis]" in source
    assert "k_min[axis] + blend*(last_k[axis] - k_min[axis])" not in source
    assert "active_joint_speed_limit_rad_s = 0.02" in source
    assert "active_joint_acceleration_limit_rad_s2 = 5.0" in source
    assert "active_tcp_translation_speed_limit_m_s = 0.01" in source
    assert "active_tcp_rotation_speed_limit_rad_s = 0.02" in source
    assert "qdd = get_actual_joint_accelerations()" in source
    assert "control_clock = time()" in source
    assert (
        "critical_decay = exp(-critical_natural_frequency_rad_s*control_dt_s)"
        in source
    )
    assert "filter_c*control_dt_s" in source
    assert "write_output_float_register(44, control_dt_s)" in source
    assert "write_output_float_register(45, control_update_count)" in source
    assert "write_output_float_register(46, maximum_control_update_gap_s)" in source
    assert "write_output_float_register(47, torque_thread_tick_count)" in source
    assert "if torque_entered:" in source
    assert "active_speed_violation = True" in source
    assert "active_acceleration_violation = True" in source
    assert "exit_fault = 11" in source
    assert "exit_fault = 12" in source
    assert "virtual_mass[axis]*control_k[axis]" in source
    assert "control_k[axis]*pose_error[axis]" in source
    assert "episode_latched == 0" in source
    assert "startup_packet_ok = command == command_idle" in source
    assert "packet_lease != lease_id or packet_episode != episode_identity" in source
    assert source.index("episode_latched == 0") < source.index(
        "torque_thread_handle = run torqueThread()"
    )
    assert "write_output_integer_register(34, last_observed_command)" in source
    assert "write_output_integer_register(35, episode_latched)" in source
    assert not re.search(r"(?m)^\s*return\b", source)
    assert source.count("sync()") == 4


def test_geodesic_orientation_policy_uses_ur_pose_interpolation() -> None:
    source = build_live_receiver_source(
        LiveTubeContract.from_reference_artifact(reference_path()),
        orientation_interpolation_policy=ORIENTATION_POLICY_INTERPOLATE_POSE,
    )
    contract = parse_live_receiver_source(source)
    assert (
        contract.orientation_interpolation_policy
        == ORIENTATION_POLICY_INTERPOLATE_POSE
    )
    assert (
        f'orientation_interpolation_policy = "'
        f'{ORIENTATION_POLICY_INTERPOLATE_POSE}"'
        in source
    )
    assert source.count("interpolate_pose(") == 1
    assert "local interpolated_control_pose = interpolate_pose(" in source
    assert "control_eq[axis] = interpolated_control_pose[axis]" in source
    assert "if axis < 3:" not in source
    assert "blend*(last_eq[axis] - entry_pose[axis])" not in source


def test_orientation_policy_rejects_unknown_or_mismatched_source() -> None:
    tube = LiveTubeContract.from_reference_artifact(reference_path())
    with pytest.raises(ValueError, match="orientation interpolation policy"):
        build_live_receiver_source(
            tube,
            orientation_interpolation_policy="componentwise_rotvec",
        )
    source = build_live_receiver_source(
        tube,
        orientation_interpolation_policy=ORIENTATION_POLICY_INTERPOLATE_POSE,
    )
    mismatched = source.replace(
        f'orientation_interpolation_policy = "'
        f'{ORIENTATION_POLICY_INTERPOLATE_POSE}"',
        f'orientation_interpolation_policy = "{ORIENTATION_POLICY_HOLD_ENTRY}"',
        1,
    )
    with pytest.raises(ValueError, match="hold-entry orientation policy"):
        parse_live_receiver_source(mismatched)


def test_compile_probe_is_bounded_and_contains_no_motion_api() -> None:
    source = build_compile_probe_source()
    parse_compile_probe_source(source)
    assert str(COMPILE_PROBE_PROTOCOL_TOKEN) in source
    assert source.count("sync()") == 1
    assert not re.search(
        r"\b(direct_torque|stopj|movej|movel|speedj|speedl|servoj|force_mode)\s*\(",
        source,
    )


def test_compile_probe_parser_rejects_torque_or_motion() -> None:
    source = build_compile_probe_source()
    with pytest.raises(ValueError, match="must not contain"):
        parse_compile_probe_source(source + "\ndirect_torque([0,0,0,0,0,0])\n")
    with pytest.raises(ValueError, match="must not contain"):
        parse_compile_probe_source(source + "\nstopj(10.0)\n")


def test_v4_source_is_episode_bound_and_deterministic() -> None:
    tube = LiveTubeContract.from_reference_artifact(reference_path())
    first = build_live_receiver_source(tube)
    second = build_live_receiver_source(tube)
    assert first == second
    assert hashlib.sha256(first.encode("utf-8")).hexdigest() == hashlib.sha256(
        second.encode("utf-8")
    ).hexdigest()
    for value in tube.anchor_pose_base:
        assert f"{value:.17g}" in first


def test_receiver_parser_rejects_empty_sync_after_direct_torque() -> None:
    source = build_live_receiver_source(
        LiveTubeContract.from_reference_artifact(reference_path())
    )
    injected = source.replace(
        "    direct_torque(torque, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)",
        "    direct_torque(torque, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)\n"
        "    sync()",
    )
    with pytest.raises(ValueError, match="empty sync timestep"):
        parse_live_receiver_source(injected)


def test_sequence_oracle_accepts_new_and_bounded_hold() -> None:
    first = evaluate_sequence(last_sequence=0, incoming_sequence=1, held_age_ticks=0)
    assert first.decision == SequenceDecision.NEW
    held = evaluate_sequence(last_sequence=1, incoming_sequence=1, held_age_ticks=0)
    assert held.decision == SequenceDecision.HELD
    assert held.next_age_ticks == 1
    for age in range(1, 10):
        held = evaluate_sequence(
            last_sequence=1,
            incoming_sequence=1,
            held_age_ticks=age,
        )
        assert held.decision == SequenceDecision.HELD


def test_sequence_oracle_rejects_stale_gap_and_replay() -> None:
    stale = evaluate_sequence(
        last_sequence=1,
        incoming_sequence=1,
        held_age_ticks=10,
    )
    assert stale.decision == SequenceDecision.FAULT
    assert stale.reason == "heartbeat_stale"
    assert (
        evaluate_sequence(
            last_sequence=1,
            incoming_sequence=3,
            held_age_ticks=0,
        ).reason
        == "sequence_gap_or_replay"
    )
    assert (
        evaluate_sequence(
            last_sequence=2,
            incoming_sequence=1,
            held_age_ticks=0,
        ).decision
        == SequenceDecision.FAULT
    )


def test_live_tube_rejects_nonorthogonal_and_outside_anchor() -> None:
    with pytest.raises(ValueError, match="orthogonal"):
        LiveTubeContract(
            center_base_m=(0.0, 0.0, 0.0),
            anchor_pose_base=(0.0, 0.0, 0.0, math.pi, 0.0, 0.0),
            u_axis_base=(1.0, 0.0, 0.0),
            v_axis_base=(1.0, 0.0, 0.0),
            safe_u_half_width_m=0.01,
            safe_v_half_width_m=0.01,
            normal_half_width_m=0.002,
            orientation_tolerance_rad=0.1,
        )


def test_live_tube_checks_actual_and_desired_pose() -> None:
    tube = LiveTubeContract.from_reference_artifact(reference_path())
    tube.assert_contains_pose(tube.anchor_pose_base, role="actual")
    outside = list(tube.anchor_pose_base)
    outside[2] += tube.normal_half_width_m + 0.001
    with pytest.raises(RuntimeError, match="actual_tube_normal_guard"):
        tube.assert_contains_pose(outside, role="actual")
    with pytest.raises(ValueError, match="outside"):
        LiveTubeContract(
            center_base_m=(0.0, 0.0, 0.0),
            anchor_pose_base=(0.02, 0.0, 0.0, math.pi, 0.0, 0.0),
            u_axis_base=(1.0, 0.0, 0.0),
            v_axis_base=(0.0, 1.0, 0.0),
            safe_u_half_width_m=0.01,
            safe_v_half_width_m=0.01,
            normal_half_width_m=0.002,
            orientation_tolerance_rad=0.1,
        )


def test_parser_rejects_wrapped_main_or_high_level_motion() -> None:
    source = build_live_receiver_source(
        LiveTubeContract.from_reference_artifact(reference_path())
    )
    with pytest.raises(ValueError, match="missing contract token"):
        parse_live_receiver_source(
            source.replace(
                f'receiver_schema = "{LIVE_RECEIVER_SCHEMA}"',
                'receiver_schema = "missing"',
            )
        )
    with pytest.raises(ValueError, match="forbidden"):
        parse_live_receiver_source(source + "\nmovel(p[0,0,0,0,0,0])\n")
    with pytest.raises(ValueError, match="explicitly invoked"):
        parse_live_receiver_source(
            source + "\ntacdiffusion_remote_direct_torque_v4_program()\n"
        )
    with pytest.raises(ValueError, match="gravity"):
        parse_live_receiver_source(source + "\n    get_gravity_torques(q)\n")
    with pytest.raises(ValueError, match="one outer program"):
        parse_live_receiver_source(
            source.replace(
                f'  local receiver_schema = "{LIVE_RECEIVER_SCHEMA}"',
                "  def accidental_wrapper():\n"
                f'    local receiver_schema = "{LIVE_RECEIVER_SCHEMA}"\n'
                "  end",
                1,
            )
        )
    with pytest.raises(ValueError, match="zero-torque"):
        parse_live_receiver_source(
            source + "\ndirect_torque([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])\n"
        )
    with pytest.raises(ValueError, match="common exit"):
        parse_live_receiver_source(source + "\nreturn\n")
    with pytest.raises(ValueError, match=r"abs\(\)"):
        parse_live_receiver_source(source + "\nx = abs(1.0)\n")


def test_bundle_builder_binds_reference_source_and_refuses_overwrite(tmp_path) -> None:
    source_path = tmp_path / "receiver.script"
    manifest_path = tmp_path / "receiver.manifest.json"
    command = [
        sys.executable,
        str(BUILDER),
        "--reference",
        str(reference_path()),
        "--receiver-source",
        str(source_path),
        "--manifest",
        str(manifest_path),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT.parents[1],
        env={"PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = source_path.read_text(encoding="utf-8")
    assert report["live_actions"] is False
    assert manifest["receiver_source_sha256"] == hashlib.sha256(
        source.encode("utf-8")
    ).hexdigest()
    assert manifest["reference_artifact_sha256"] == hashlib.sha256(
        reference_path().read_bytes()
    ).hexdigest()
    assert manifest["gates"]["controller_runtime_physical_io"] is True
    assert manifest["gates"]["source_builder_physical_io"] is False
    assert manifest["register_contract"]["applied_action"]["stiffness_k"] == list(
        range(32, 38)
    )
    assert manifest["register_contract"]["applied_action"][
        "commanded_joint_torque_nm"
    ] == list(range(38, 44))
    assert manifest["register_contract"]["output_double_registers"] == list(
        range(24, 48)
    )
    assert manifest["register_contract"]["applied_action"]["cadence"] == {
        "control_update_dt_s": 44,
        "control_update_count": 45,
        "maximum_control_update_gap_s": 46,
        "torque_thread_tick_count": 47,
    }
    assert manifest["gates"]["single_top_level_program"] is True
    assert (
        manifest["gates"]["secondary_wire_outer_program"]
        == "def_tacdiffusion_remote_direct_torque_v4_program"
    )
    assert manifest["gates"]["dedicated_torque_thread"] is True
    assert (
        manifest["gates"]["orientation_interpolation_policy"]
        == ORIENTATION_POLICY_HOLD_ENTRY
    )
    assert manifest["gates"]["continuous_500hz_torque_site"] is True
    assert manifest["gates"]["zero_torque_startup_or_exit"] is False
    repeated = subprocess.run(
        command,
        cwd=ROOT.parents[1],
        env={"PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert repeated.returncode == 2
    assert "already exists" in repeated.stderr
