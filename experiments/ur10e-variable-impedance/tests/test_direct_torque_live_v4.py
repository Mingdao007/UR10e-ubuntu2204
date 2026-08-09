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
    FRICTION_PROFILE_UR_DEFAULT_V2_DIAGNOSTIC,
    FRICTION_PROFILE_ZERO_ISOLATION,
    LIVE_PROTOCOL_TOKEN,
    LIVE_RECEIVER_SCHEMA,
    ORIENTATION_POLICY_HOLD_ENTRY,
    ORIENTATION_POLICY_INTERPOLATE_POSE,
    LiveTubeContract,
    SequenceDecision,
    build_compile_probe_source,
    build_live_receiver_source,
    formal_contact_entry_rate_limits,
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
    assert contract.model_inactive_expert_feedforward_allowed is False
    assert "local model_inactive_expert_feedforward_allowed = False" in source
    assert "if not model_inactive_expert_feedforward_allowed:" in source
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
    assert "incoherent_age_s = 0.0" in source
    float_payload_end = source.index(
        "local raw_force = [read_input_float_register(42)"
    )
    final_sequence_read = source.index(
        "sequence_after = read_input_integer_register(25)",
        float_payload_end,
    )
    coherent_check = source.index(
        "local coherent = sequence_before == sequence_after", final_sequence_read
    )
    assert float_payload_end < final_sequence_read < coherent_check
    assert "if not coherent:\n        incoherent_age_s = incoherent_age_s + control_dt_s" in source
    assert "if incoherent_age_s > heartbeat_timeout_s:" in source
    assert "elif not packet_ok:" in source
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
    assert "local torque_generation_begin = torque_command_generation" in torque_thread_source
    assert "local torque_candidate = [torque_command[0]" in torque_thread_source
    assert "local torque_generation_end = torque_command_generation" in torque_thread_source
    assert (
        "torque_thread_tick_count = torque_thread_tick_count + 1"
        in torque_thread_source
    )
    assert "torque_thread_stale_ticks >= 25" in torque_thread_source
    assert "torque_thread_watchdog_fault = True" in torque_thread_source
    assert "sync()" not in torque_thread_source
    assert not re.search(r"(?m)^\s*direct_torque\(", program_source)
    assert source.count("torque_thread_handle = run torqueThread()") == 1
    assert source.count("join torque_thread_handle") == 1
    assert source.index("torque_command[axis] = tau[axis]") < source.index(
        "torque_thread_handle = run torqueThread()"
    )
    assert source.index("torque_thread_run = False", program_start) < source.index(
        "join torque_thread_handle"
    )
    assert "friction_comp=True" not in source
    assert contract.friction_profile == FRICTION_PROFILE_ZERO_ISOLATION
    assert contract.viscous_scale == (0.0,) * 6
    assert contract.coulomb_scale == (0.0,) * 6
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
    assert "entry_velocity_filter_tau_s = 0.05" in source
    assert "entry_velocity_filter_warmup_s = 0.15" in source
    assert (
        "entry_velocity_alpha = control_dt_s/(entry_velocity_filter_tau_s + control_dt_s)"
        in source
    )
    assert (
        "if not torque_entered:\n"
        "          entry_ready = True\n"
        "          local entry_velocity_alpha = "
        "control_dt_s/(entry_velocity_filter_tau_s + control_dt_s)"
    ) in source
    assert (
        "filtered_entry_joint_speed[axis] > entry_joint_speed_limit_rad_s"
        in source
    )
    assert (
        "entry_velocity_filter_elapsed_s < entry_velocity_filter_warmup_s"
        in source
    )
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
    assert "active_joint_speed_limit_rad_s = 0.29999999999999999" in source
    assert "active_joint_acceleration_limit_rad_s2 = 90.0" in source
    assert "active_tcp_translation_speed_limit_m_s = 0.14999999999999999" in source
    assert "active_tcp_rotation_speed_limit_rad_s = 0.59999999999999998" in source
    assert "qdd = get_actual_joint_accelerations()" in source
    assert "control_clock = time()" in source
    assert (
        "critical_decay = pow(2.718281828459045, -critical_natural_frequency_rad_s*control_dt_s)"
        in source
    )
    assert "exp(" not in source
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
    assert "exit_fault = 13" in source
    assert (
        source.index(
            "write_output_integer_register(29, action_publish_generation)"
        )
        < source.index("write_output_integer_register(24, 2)")
    )
    assert (
        source.index("write_output_float_register(38 + axis, tau[axis])")
        < source.index(
            "write_output_integer_register(33, action_publish_generation)"
        )
    )
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


def test_receiver_held_payload_seqlock_is_transient_and_timeout_bounded() -> None:
    source = build_live_receiver_source(
        LiveTubeContract(
            center_base_m=(0.4, 0.1, 0.03),
            anchor_pose_base=(0.4, 0.1, 0.03, 3.14, 0.0, 0.0),
            u_axis_base=(1.0, 0.0, 0.0),
            v_axis_base=(0.0, 1.0, 0.0),
            safe_u_half_width_m=0.01,
            safe_v_half_width_m=0.01,
            normal_half_width_m=0.002,
            orientation_tolerance_rad=0.05,
        )
    )
    parse_live_receiver_source(source)
    held_start = source.index("      if held_packet:\n")
    compare_start = source.index("        local compare_axis = 0\n", held_start)
    compare_end = source.index(
        "      else:\n        held_age_s = 0.0", compare_start
    )
    held_compare = source[compare_start:compare_end]
    assert source.index("local held_payload_equal = True") < held_start
    assert "held_payload_equal = False" in held_compare
    assert "packet_ok = False" not in held_compare

    incoherent_declaration = source.index(
        "      local held_payload_incoherent = held_packet and not held_payload_equal\n"
    )
    reset = source.index(
        "      if coherent and not held_payload_incoherent:\n",
        incoherent_declaration,
    )
    transient = source.index(
        "      if not coherent or held_payload_incoherent:\n",
        reset,
    )
    timeout = source.index(
        "if held_age_s > heartbeat_timeout_s or incoherent_age_s > heartbeat_timeout_s:",
        transient,
    )
    packet_fault = source.index("      elif not packet_ok:\n", transient)
    accept = source.index("        if new_packet:\n", packet_fault)
    transient_region = source[transient:packet_fault]

    assert reset < transient < timeout < packet_fault < accept
    assert "incoherent_age_s = incoherent_age_s + control_dt_s" in transient_region
    assert "sync()" in transient_region
    assert "last_eq = eq" not in transient_region
    assert "last_k = desired_k" not in transient_region
    assert "last_guard_wrench = guard_wrench" not in transient_region
    assert "last_raw_force = raw_force" not in transient_region
    assert "if held_age_s > heartbeat_timeout_s:" in source
    assert "if not new_packet and not held_packet:" in source


def test_receiver_contact_guard_profile_is_explicit_50n_4nm() -> None:
    tube = LiveTubeContract(
        center_base_m=(0.4, 0.1, 0.03),
        anchor_pose_base=(0.4, 0.1, 0.03, 3.14, 0.0, 0.0),
        u_axis_base=(1.0, 0.0, 0.0),
        v_axis_base=(0.0, 1.0, 0.0),
        safe_u_half_width_m=0.01,
        safe_v_half_width_m=0.01,
        normal_half_width_m=0.002,
        orientation_tolerance_rad=0.05,
    )
    source = build_live_receiver_source(
        tube, guard_force_limit_n=50.0, guard_torque_limit_nm=4.0
    )
    contract = parse_live_receiver_source(source)
    assert "guard_force_norm > 50.0 or guard_torque_norm > 4.0" in source
    assert contract.guard_force_limit_n == 50.0
    assert contract.guard_torque_limit_nm == 4.0
    assert contract.model_inactive_expert_feedforward_allowed is False
    mode_zero = source[source.index("if model_mode == 0:") : source.index("elif model_mode == 1:")]
    assert "model_sequence != 0 or model_period_us != 0 or model_timestamp_us != 0" in mode_zero
    assert "if not model_inactive_expert_feedforward_allowed:" in mode_zero
    assert mode_zero.index("model_sequence != 0") < mode_zero.index(
        "if not model_inactive_expert_feedforward_allowed:"
    )
    with pytest.raises(
        ValueError,
        match="model-inactive expert feedforward requires a typed full-friction",
    ):
        build_live_receiver_source(
            tube,
            friction_profile="ur_full_v3_formal_motion",
            guard_force_limit_n=50.0,
            guard_torque_limit_nm=4.0,
            model_inactive_expert_feedforward_allowed=True,
        )
    tampered = source.replace(
        "local model_inactive_expert_feedforward_allowed = False",
        "local model_inactive_expert_feedforward_allowed = True",
        1,
    )
    with pytest.raises(ValueError, match="requires formal contact identity"):
        parse_live_receiver_source(tampered)


def test_no_contact_full_motion_profile_keeps_6n_0p5nm_guard() -> None:
    tube = LiveTubeContract(
        center_base_m=(0.4, 0.1, 0.03),
        anchor_pose_base=(0.4, 0.1, 0.03, 3.14, 0.0, 0.0),
        u_axis_base=(1.0, 0.0, 0.0),
        v_axis_base=(0.0, 1.0, 0.0),
        safe_u_half_width_m=0.01,
        safe_v_half_width_m=0.01,
        normal_half_width_m=0.002,
        orientation_tolerance_rad=0.05,
    )
    source = build_live_receiver_source(
        tube,
        friction_profile="ur_full_v3_formal_motion",
        guard_force_limit_n=6.0,
        guard_torque_limit_nm=0.5,
        model_inactive_expert_feedforward_allowed=True,
    )
    parsed = parse_live_receiver_source(source)
    assert parsed.friction_profile == "ur_full_v3_formal_motion"
    assert parsed.formal_handoff_required is False
    assert parsed.guard_force_limit_n == 6.0
    assert parsed.guard_torque_limit_nm == 0.5


def test_formal_contact_entry_transition_is_one_shot_and_tick_bounded() -> None:
    tube = LiveTubeContract(
        center_base_m=(0.4, 0.1, 0.03),
        anchor_pose_base=(0.4, 0.1, 0.03, 3.14, 0.0, 0.0),
        u_axis_base=(1.0, 0.0, 0.0),
        v_axis_base=(0.0, 1.0, 0.0),
        safe_u_half_width_m=0.01,
        safe_v_half_width_m=0.01,
        normal_half_width_m=0.0255,
        orientation_tolerance_rad=0.05,
    )
    source = build_live_receiver_source(
        tube,
        friction_profile="ur_full_v3_formal_motion",
        guard_force_limit_n=50.0,
        guard_torque_limit_nm=4.0,
        formal_handoff_anchor_pose_base=tube.anchor_pose_base,
        model_inactive_expert_feedforward_allowed=True,
        formal_contact_entry_transition_profile="formal_contact_entry_transition_v1",
    )
    contract = parse_live_receiver_source(source)
    assert contract.formal_contact_entry_transition_profile == "formal_contact_entry_transition_v1"
    assert contract.formal_contact_entry_transition_ticks == 25
    assert "formal_contact_entry_joint_speed_limit_rad_s = 0.14999999999999999" in source
    assert "formal_contact_entry_joint_acceleration_limit_rad_s2 = 90" in source
    assert "formal_contact_entry_tcp_translation_speed_limit_m_s = 0.14999999999999999" in source
    assert "formal_contact_entry_tcp_rotation_speed_limit_rad_s = 0.29999999999999999" in source
    assert source.count(
        "formal_contact_entry_transition_tick_count = formal_contact_entry_transition_tick_count + 1"
    ) == 1
    assert "entry_transition_tcp_translation_limit_m = 0.01" in source
    assert "entry_transition_joint_excursion_limit_rad = 0.015" in source
    assert "formal_contact_entry_joint_damping = [5, 5, 4, 5, 1, 1]" in source
    assert "baseline_joint_damping = [1.5, 1.5, 1.2, 0.29999999999999999, 0.29999999999999999, 0.20000000000000001]" in source
    assert "torque_to_apply[transition_joint] = torque_to_apply[transition_joint] - (formal_contact_entry_joint_damping[transition_joint] - baseline_joint_damping[transition_joint])*transition_qd[transition_joint]" in source
    assert "tau[joint] = coriolis[joint] - joint_damping[joint]*qd[joint]" in source
    torque_thread_start = source.index("thread torqueThread():")
    program_start = source.index('receiver_schema = "ur10e_direct_torque_receiver/v4"')
    torque_thread_source = source[torque_thread_start:program_start]
    assert "local transition_active = formal_contact_entry_transition_tick_count < formal_contact_entry_transition_ticks" in torque_thread_source
    assert "selected_tcp_translation_speed_limit_m_s = formal_contact_baseline_tcp_translation_speed_limit_m_s" in torque_thread_source
    assert "selected_tcp_translation_speed_limit_m_s = formal_contact_entry_tcp_translation_speed_limit_m_s" in torque_thread_source
    assert "formal_contact_baseline_tcp_translation_speed_limit_m_s = 0.14999999999999999" in source
    assert "formal_contact_baseline_tcp_rotation_speed_limit_rad_s = 0.59999999999999998" in source
    assert "formal_contact_baseline_joint_speed_limit_rad_s = 0.29999999999999999" in source
    assert "formal_contact_baseline_joint_acceleration_limit_rad_s2 = 90.0" in source
    assert torque_thread_source.count(
        "direct_torque(torque_to_apply, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)"
    ) == 1

    transition = formal_contact_entry_rate_limits(torque_thread_tick_count=25, enabled=True)
    assert transition.transition_active is True
    assert transition.tcp_translation_m_s == 0.15
    baseline = formal_contact_entry_rate_limits(torque_thread_tick_count=26, enabled=True)
    assert baseline.transition_active is False
    assert baseline.tcp_translation_m_s == 0.15
    assert baseline.tcp_rotation_rad_s == 0.60
    assert baseline.joint_speed_rad_s == 0.30
    assert baseline.joint_acceleration_rad_s2 == 90.0

    tampered = source.replace(
        "formal_contact_entry_transition_ticks = 25",
        "formal_contact_entry_transition_ticks = 26",
        1,
    )
    with pytest.raises(ValueError, match="transition source identity mismatch"):
        parse_live_receiver_source(tampered)

    tampered_damping = source.replace(
        "formal_contact_entry_joint_damping = [5, 5, 4, 5, 1, 1]",
        "formal_contact_entry_joint_damping = [1, 1, 1, 1, 1, 1]",
        1,
    )
    with pytest.raises(ValueError, match="transition source identity mismatch"):
        parse_live_receiver_source(tampered_damping)

    tampered_baseline = source.replace(
        "selected_tcp_translation_speed_limit_m_s = formal_contact_baseline_tcp_translation_speed_limit_m_s",
        "selected_tcp_translation_speed_limit_m_s = formal_contact_entry_tcp_translation_speed_limit_m_s",
        1,
    )
    with pytest.raises(ValueError, match="missing contract token|transition source identity mismatch"):
        parse_live_receiver_source(tampered_baseline)


def test_formal_contact_entry_damping_restores_baseline_at_tick_26() -> None:
    from ur10e_vic.tacdiffusion.direct_torque_live_v4 import (
        formal_contact_entry_joint_damping,
    )

    assert formal_contact_entry_joint_damping(
        torque_thread_tick_count=1, enabled=True
    ) == (5.0, 5.0, 4.0, 5.0, 1.0, 1.0)
    assert formal_contact_entry_joint_damping(
        torque_thread_tick_count=25, enabled=True
    ) == (5.0, 5.0, 4.0, 5.0, 1.0, 1.0)
    assert formal_contact_entry_joint_damping(
        torque_thread_tick_count=26, enabled=True
    ) == (1.5, 1.5, 1.2, 0.3, 0.3, 0.2)


def test_fault11_recorded_peak_replay_uses_transition_then_restores_baseline() -> None:
    recorded = {
        "tcp_translation_m_s": 0.005525089118650993,
        "tcp_rotation_rad_s": 0.027072823876060963,
        "joint_speed_rad_s": 0.02636725641787052,
        "joint_acceleration_rad_s2": 11.506,
    }
    transition = formal_contact_entry_rate_limits(torque_thread_tick_count=4, enabled=True)
    assert transition.transition_active is True
    assert recorded["tcp_translation_m_s"] < transition.tcp_translation_m_s
    assert recorded["tcp_rotation_rad_s"] < transition.tcp_rotation_rad_s
    assert recorded["joint_speed_rad_s"] < transition.joint_speed_rad_s
    assert recorded["joint_acceleration_rad_s2"] < transition.joint_acceleration_rad_s2

    # Raised free-space baseline matches the entry-transition envelope so
    # host pose-tracking catch-up (and this historical fault-11 signature)
    # stays legal after tick 26.
    baseline = formal_contact_entry_rate_limits(torque_thread_tick_count=26, enabled=True)
    assert baseline.transition_active is False
    assert recorded["tcp_translation_m_s"] < baseline.tcp_translation_m_s
    assert recorded["tcp_rotation_rad_s"] < baseline.tcp_rotation_rad_s
    assert recorded["joint_speed_rad_s"] < baseline.joint_speed_rad_s
    assert recorded["joint_acceleration_rad_s2"] < baseline.joint_acceleration_rad_s2
    with pytest.raises(ValueError, match="positive"):
        formal_contact_entry_rate_limits(torque_thread_tick_count=0, enabled=True)


def test_entry_velocity_filter_rejects_drift_but_accepts_bounded_57hz_noise() -> None:
    dt_s = 0.005
    filter_tau_s = 0.05
    stable_duration_s = 0.05
    speed_limit = 0.001
    alpha = dt_s / (filter_tau_s + dt_s)

    def maximum_stable_dwell(signal: list[float]) -> tuple[float, float]:
        filtered = 0.0
        dwell = 0.0
        maximum_dwell = 0.0
        maximum_filtered = 0.0
        for sample in signal:
            filtered += alpha * (sample - filtered)
            maximum_filtered = max(maximum_filtered, abs(filtered))
            if abs(filtered) <= speed_limit:
                dwell += dt_s
                maximum_dwell = max(maximum_dwell, dwell)
            else:
                dwell = 0.0
        return maximum_dwell, maximum_filtered

    bounded_57hz = [
        0.0036 * math.cos(2.0 * math.pi * 57.0 * index * dt_s)
        for index in range(round(0.25 / dt_s))
    ]
    sustained_drift = [0.002] * round(0.25 / dt_s)

    noise_dwell, noise_peak = maximum_stable_dwell(bounded_57hz)
    drift_dwell, drift_peak = maximum_stable_dwell(sustained_drift)

    assert noise_peak < speed_limit
    assert noise_dwell >= stable_duration_s
    assert drift_peak > speed_limit
    assert drift_dwell < stable_duration_s


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


def test_v4_source_supports_explicit_ur_default_v2_friction_diagnostic() -> None:
    tube = LiveTubeContract.from_reference_artifact(reference_path())
    source = build_live_receiver_source(
        tube,
        friction_profile=FRICTION_PROFILE_UR_DEFAULT_V2_DIAGNOSTIC,
    )
    contract = parse_live_receiver_source(source)
    assert contract.friction_profile == FRICTION_PROFILE_UR_DEFAULT_V2_DIAGNOSTIC
    assert contract.viscous_scale == pytest.approx(
        [0.9, 0.9, 0.8, 0.9, 0.9, 0.9]
    )
    assert contract.coulomb_scale == pytest.approx(
        [0.8, 0.8, 0.7, 0.8, 0.8, 0.8]
    )
    assert f'friction_profile = "{FRICTION_PROFILE_ZERO_ISOLATION}"' not in source


def test_v4_parser_rejects_friction_profile_scale_mismatch() -> None:
    tube = LiveTubeContract.from_reference_artifact(reference_path())
    source = build_live_receiver_source(
        tube,
        friction_profile=FRICTION_PROFILE_UR_DEFAULT_V2_DIAGNOSTIC,
    )
    mismatched = source.replace(
        "viscous_scale = [0.90000000000000002",
        "viscous_scale = [0.80000000000000004",
        1,
    )
    with pytest.raises(ValueError, match="do not match declared profile"):
        parse_live_receiver_source(mismatched)


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
    with pytest.raises(ValueError, match="forbidden"):
        parse_live_receiver_source(source + "\nexp(0.0)\n")
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
    assert manifest["register_contract"]["applied_action"][
        "publication_seqlock"
    ] == {
        "generation_begin_output_integer_register": 29,
        "generation_end_output_integer_register": 33,
        "coherent_when": "active_and_begin_eq_end_gt_zero",
    }
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
