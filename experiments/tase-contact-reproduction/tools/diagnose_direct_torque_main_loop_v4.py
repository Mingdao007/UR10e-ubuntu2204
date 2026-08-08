#!/usr/bin/env python3
"""Run a bounded no-contact main-loop Direct Torque diagnostic.

This tool changes exactly one controller execution detail relative to a
validated V4 bundle: the already-computed joint torque is applied by the main
500 Hz controller loop rather than copied to ``torqueThread``.  It reuses the
accepted Remote/RTDE/Kunwei runner, including the Kunwei-only 6 N / 0.5 Nm
guard, Tube, speed/acceleration guards, immutable output artifacts, and staged
hold -> 0.2 mm ramp ordering.  Its evidence is diagnostic only and is never a
formal campaign episode.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
VIC_ROOT = ROOT.parent / "ur10e-variable-impedance"
for import_root in (ROOT / "tools", VIC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

import run_tacdiffusion_remote_direct_torque_v4 as legacy  # noqa: E402
from ur10e_vic.tacdiffusion.direct_torque_live_v4 import (  # noqa: E402
    FRICTION_PROFILES,
    FRICTION_PROFILE_UR_DEFAULT_V2_DIAGNOSTIC,
    build_live_receiver_source,
    parse_live_receiver_source,
)


DIAGNOSTIC_SCHEMA = "ur10e_direct_torque_main_loop_diagnostic/v1"
HANDOFF_DELAY_S = 0.02
ENTRY_TRANSITION_ENVELOPE_S = 0.05
ENTRY_JOINT_SPEED_LIMIT_RAD_S = 0.05
ENTRY_JOINT_ACCELERATION_LIMIT_RAD_S2 = 30.0
ENTRY_TCP_TRANSLATION_SPEED_LIMIT_M_S = 0.05
ENTRY_TCP_ROTATION_SPEED_LIMIT_RAD_S = 0.10


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def transform_to_main_loop(source: str) -> str:
    """Make one deterministic, fail-closed execution-mode substitution."""

    thread_start_token = "  thread torqueThread():\n"
    program_marker = "  # generated Remote-Control Direct Torque receiver"
    thread_start = source.find(thread_start_token)
    program_start = source.find(program_marker)
    if thread_start < 0 or program_start <= thread_start:
        raise ValueError("diagnostic source does not contain the V4 torque thread")
    transformed = source[:thread_start] + source[program_start:]

    old_publish = """            torque_command = tau
            if not torque_entered:
              torque_thread_run = True
              torque_thread_handle = run torqueThread()
              torque_entered = True
            end
"""
    new_publish = """            direct_torque(tau, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)
            torque_thread_tick_count = torque_thread_tick_count + 1
            torque_entered = True
"""
    if old_publish not in transformed:
        # The formal branch may contain the seqlock form.  The diagnostic
        # remains one-variable by replacing the entire publish/launch block.
        begin = transformed.find(
            "            torque_command_generation = torque_command_generation + 1\n"
        )
        launch_end_token = "              torque_entered = True\n            end\n"
        launch_end = transformed.find(launch_end_token, begin)
        if begin < 0 or launch_end < 0:
            raise ValueError("diagnostic torque publish block is not recognized")
        launch_end += len(launch_end_token)
        transformed = transformed[:begin] + new_publish + transformed[launch_end:]
    else:
        transformed = transformed.replace(old_publish, new_publish, 1)

    active_tail = """            if entry_elapsed_s < entry_blend_duration_s:
              entry_elapsed_s = entry_elapsed_s + control_dt_s
            end
            sync()
"""
    main_loop_tail = """            if entry_elapsed_s < entry_blend_duration_s:
              entry_elapsed_s = entry_elapsed_s + control_dt_s
            end
"""
    if transformed.count(active_tail) != 1:
        raise ValueError("diagnostic active-loop sync site is not unique")
    transformed = transformed.replace(active_tail, main_loop_tail, 1)

    threaded_exit = """  if torque_entered:
    torque_thread_run = False
    join torque_thread_handle
  end
"""
    main_loop_exit = """  if torque_entered:
    stopj(10.0)
  end
"""
    if transformed.count(threaded_exit) != 1:
        raise ValueError("diagnostic threaded exit site is not unique")
    transformed = transformed.replace(threaded_exit, main_loop_exit, 1)
    transformed = transformed.replace(
        "def tacdiffusion_remote_direct_torque_v4_program():\n",
        "def tacdiffusion_direct_torque_main_loop_diagnostic_v1():\n"
        f"  # {DIAGNOSTIC_SCHEMA}; diagnostic_only=True; model_active=False\n",
        1,
    )
    validate_transformed_source(transformed)
    return transformed


def transform_to_thread_handoff_main_loop(source: str) -> str:
    """Enter safely in the proven thread, then hand off without a call gap."""

    transformed = source.replace(
        "  torque_thread_run = False\n",
        "  torque_thread_run = False\n"
        "  torque_main_loop_handoff = False\n"
        "  torque_main_loop_owner = False\n",
        1,
    )
    old_thread_exit = """    stopj(10.0)
  end

  # generated Remote-Control Direct Torque receiver"""
    new_thread_exit = """    if not torque_main_loop_handoff:
      stopj(10.0)
    end
  end

  # generated Remote-Control Direct Torque receiver"""
    if transformed.count(old_thread_exit) != 1:
        raise ValueError("diagnostic torque-thread exit site is not unique")
    transformed = transformed.replace(old_thread_exit, new_thread_exit, 1)

    launch = """            if not torque_entered:
              torque_thread_run = True
              torque_thread_handle = run torqueThread()
              torque_entered = True
            end
"""
    handoff = f"""            if not torque_entered:
              torque_thread_run = True
              torque_thread_handle = run torqueThread()
              torque_entered = True
            elif entry_elapsed_s >= {HANDOFF_DELAY_S:.17g} and not torque_main_loop_owner:
              torque_main_loop_handoff = True
              torque_thread_run = False
              join torque_thread_handle
              torque_main_loop_owner = True
            end
"""
    if transformed.count(launch) != 1:
        raise ValueError("diagnostic torque-thread launch site is not unique")
    transformed = transformed.replace(launch, handoff, 1)

    active_tail = """            if entry_elapsed_s < entry_blend_duration_s:
              entry_elapsed_s = entry_elapsed_s + control_dt_s
            end
            sync()
"""
    handoff_tail = """            if entry_elapsed_s < entry_blend_duration_s:
              entry_elapsed_s = entry_elapsed_s + control_dt_s
            end
            if torque_main_loop_owner:
              direct_torque(tau, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)
              torque_thread_tick_count = torque_thread_tick_count + 1
            else:
              sync()
            end
"""
    if transformed.count(active_tail) != 1:
        raise ValueError("diagnostic active-loop tail site is not unique")
    transformed = transformed.replace(active_tail, handoff_tail, 1)

    old_program_exit = """  if torque_entered:
    torque_thread_run = False
    join torque_thread_handle
  end
"""
    new_program_exit = """  if torque_entered:
    if torque_main_loop_owner:
      stopj(10.0)
    else:
      torque_thread_run = False
      join torque_thread_handle
    end
  end
"""
    if transformed.count(old_program_exit) != 1:
        raise ValueError("diagnostic program exit site is not unique")
    transformed = transformed.replace(old_program_exit, new_program_exit, 1)
    transformed = transformed.replace(
        "def tacdiffusion_remote_direct_torque_v4_program():\n",
        "def tacdiffusion_direct_torque_thread_handoff_diagnostic_v1():\n"
        f"  # {DIAGNOSTIC_SCHEMA}; thread_handoff_s={HANDOFF_DELAY_S}; "
        "diagnostic_only=True; model_active=False\n",
        1,
    )
    validate_thread_handoff_source(transformed)
    return transformed


def transform_to_disable_high_holding_torque(source: str) -> str:
    """Disable UR steady-mode holding only for the bounded receiver lifetime."""

    transformed = source.replace(
        "def tacdiffusion_remote_direct_torque_v4_program():\n",
        "def tacdiffusion_direct_torque_high_hold_ablation_v1():\n"
        f"  # {DIAGNOSTIC_SCHEMA}; disable_high_holding_torque=True; "
        "diagnostic_only=True; model_active=False\n"
        "  high_holding_torque_disable()\n",
        1,
    )
    final_state = """  if exit_fault == 0:
    write_output_integer_register(24, 5)
  else:
    write_output_integer_register(24, 4)
  end
end
"""
    restored_final_state = """  high_holding_torque_enable()
  if exit_fault == 0:
    write_output_integer_register(24, 5)
  else:
    write_output_integer_register(24, 4)
  end
end
"""
    if transformed.count(final_state) != 1:
        raise ValueError("diagnostic final state site is not unique")
    transformed = transformed.replace(final_state, restored_final_state, 1)
    required = (
        "high_holding_torque_disable()",
        "high_holding_torque_enable()",
        "thread torqueThread():",
        "run torqueThread()",
        "guard_force_norm > 6.0 or guard_torque_norm > 0.5",
        "entry_transition_tcp_translation_limit_m = 0.0003",
        "entry_transition_joint_excursion_limit_rad = 0.0005",
        "active_joint_acceleration_limit_rad_s2 = 5.0",
        "active_tcp_translation_speed_limit_m_s = 0.01",
    )
    missing = [token for token in required if token not in transformed]
    if missing:
        raise ValueError(f"high-hold diagnostic source missing tokens:{missing}")
    if transformed.count("high_holding_torque_disable()") != 1:
        raise ValueError("high-hold diagnostic disable call count mismatch")
    if transformed.count("high_holding_torque_enable()") != 1:
        raise ValueError("high-hold diagnostic enable call count mismatch")
    forbidden = ("get_tcp_force", "actual_TCP_force", "force_mode(", "speedl(", "servoj(")
    present = [token for token in forbidden if token in transformed]
    if present:
        raise ValueError(f"high-hold diagnostic contains forbidden tokens:{present}")
    return transformed


def transform_to_thread_applied_echo(source: str) -> str:
    """Echo the exact last coherent command selected by the torque thread."""

    transformed = source.replace(
        "  torque_thread_last_coherent_command = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]\n",
        "  torque_thread_last_coherent_command = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]\n"
        "  torque_thread_applied_command = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]\n",
        1,
    )
    torque_call = """        local torque = torque_thread_last_coherent_command
        direct_torque(torque, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)
"""
    echoed_torque_call = """        local torque = torque_thread_last_coherent_command
        torque_thread_applied_command = torque
        direct_torque(torque, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)
"""
    if transformed.count(torque_call) != 1:
        raise ValueError("thread-applied echo torque call site is not unique")
    transformed = transformed.replace(torque_call, echoed_torque_call, 1)
    main_echo = "              write_output_float_register(38 + axis, tau[axis])\n"
    thread_echo = (
        "              write_output_float_register(38 + axis, "
        "torque_thread_applied_command[axis])\n"
    )
    if transformed.count(main_echo) != 1:
        raise ValueError("thread-applied echo register site is not unique")
    transformed = transformed.replace(main_echo, thread_echo, 1)
    transformed = transformed.replace(
        "def tacdiffusion_remote_direct_torque_v4_program():\n",
        "def tacdiffusion_direct_torque_thread_applied_echo_v1():\n"
        f"  # {DIAGNOSTIC_SCHEMA}; thread_applied_echo=True; "
        "diagnostic_only=True; model_active=False\n",
        1,
    )
    required = (
        "torque_thread_applied_command = torque",
        "write_output_float_register(38 + axis, torque_thread_applied_command[axis])",
        "thread torqueThread():",
        "run torqueThread()",
        "guard_force_norm > 6.0 or guard_torque_norm > 0.5",
    )
    missing = [token for token in required if token not in transformed]
    if missing:
        raise ValueError(f"thread-applied echo source missing tokens:{missing}")
    forbidden = ("get_tcp_force", "actual_TCP_force", "force_mode(", "speedl(", "servoj(")
    present = [token for token in forbidden if token in transformed]
    if present:
        raise ValueError(f"thread-applied echo contains forbidden tokens:{present}")
    return transformed


def transform_to_joint0_bias_probe(source: str) -> str:
    """Apply one bounded 1 Nm base-joint bias through the proven thread."""

    transformed = transform_to_thread_applied_echo(source)
    original = "        local torque = torque_thread_last_coherent_command\n"
    biased = """        local torque = [torque_thread_last_coherent_command[0] + 1.0, torque_thread_last_coherent_command[1], torque_thread_last_coherent_command[2], torque_thread_last_coherent_command[3], torque_thread_last_coherent_command[4], torque_thread_last_coherent_command[5]]
"""
    if transformed.count(original) != 1:
        raise ValueError("joint-bias diagnostic torque selection site is not unique")
    transformed = transformed.replace(original, biased, 1)
    transformed = transformed.replace(
        "def tacdiffusion_direct_torque_thread_applied_echo_v1():\n",
        "def tacdiffusion_direct_torque_joint0_bias_probe_v1():\n"
        f"  # {DIAGNOSTIC_SCHEMA}; joint0_bias_nm=1.0; "
        "diagnostic_only=True; model_active=False\n",
        1,
    )
    required = (
        "torque_thread_last_coherent_command[0] + 1.0",
        "torque_thread_applied_command = torque",
        "entry_transition_tcp_translation_limit_m = 0.0003",
        "entry_transition_joint_excursion_limit_rad = 0.0005",
        "active_joint_speed_limit_rad_s = 0.02",
        "active_joint_acceleration_limit_rad_s2 = 5.0",
        "guard_force_norm > 6.0 or guard_torque_norm > 0.5",
    )
    missing = [token for token in required if token not in transformed]
    if missing:
        raise ValueError(f"joint-bias diagnostic source missing tokens:{missing}")
    return transformed


def validate_transformed_source(source: str) -> None:
    required = (
        f"# {DIAGNOSTIC_SCHEMA}",
        "direct_torque(tau, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)",
        "torque_thread_tick_count = torque_thread_tick_count + 1",
        "guard_force_norm > 6.0 or guard_torque_norm > 0.5",
        "active_tcp_translation_speed_limit_m_s = 0.01",
        "tube_rebased = False",
        "stopj(10.0)",
    )
    missing = [token for token in required if token not in source]
    if missing:
        raise ValueError(f"diagnostic source missing tokens:{missing}")
    pose_error_forms = (
        "pose_error[0] = control_eq[0] - actual_pose[0]",
        "local pose_error = pose_sub(p[control_eq[0]",
    )
    if not any(token in source for token in pose_error_forms):
        raise ValueError("diagnostic source is missing Cartesian pose feedback")
    forbidden = (
        "get_tcp_force",
        "actual_TCP_force",
        "force_mode(",
        "speedl(",
        "servoj(",
        "run torqueThread()",
        "thread torqueThread():",
        "join torque_thread_handle",
    )
    present = [token for token in forbidden if token in source]
    if present:
        raise ValueError(f"diagnostic source contains forbidden tokens:{present}")
    direct_call = (
        "            direct_torque(tau, viscous_scale=viscous_scale, "
        "coulomb_scale=coulomb_scale)"
    )
    if source.count(direct_call) != 1:
        raise ValueError("diagnostic source must contain one Direct Torque call site")


def validate_thread_handoff_source(source: str) -> None:
    required = (
        f"# {DIAGNOSTIC_SCHEMA}; thread_handoff_s={HANDOFF_DELAY_S}",
        "thread torqueThread():",
        "run torqueThread()",
        "torque_main_loop_handoff = True",
        "join torque_thread_handle",
        "if torque_main_loop_owner:",
        "direct_torque(tau, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)",
        "guard_force_norm > 6.0 or guard_torque_norm > 0.5",
        "active_tcp_translation_speed_limit_m_s = 0.01",
        "tube_rebased = False",
    )
    missing = [token for token in required if token not in source]
    if missing:
        raise ValueError(f"thread-handoff diagnostic source missing tokens:{missing}")
    forbidden = ("get_tcp_force", "actual_TCP_force", "force_mode(", "speedl(", "servoj(")
    present = [token for token in forbidden if token in source]
    if present:
        raise ValueError(f"thread-handoff diagnostic contains forbidden tokens:{present}")
    if source.count("direct_torque(") != 3:
        # One comment plus the thread and main-loop call sites.
        raise ValueError("thread-handoff diagnostic Direct Torque call topology mismatch")


def add_bounded_entry_transition_envelope(source: str) -> str:
    """Add state-specific transient rate limits without changing excursion gates."""

    declaration_anchor = "  local active_tcp_rotation_speed_limit_rad_s = 0.02\n"
    declarations = declaration_anchor + (
        f"  local diagnostic_entry_transition_envelope_s = {ENTRY_TRANSITION_ENVELOPE_S:.17g}\n"
        f"  local diagnostic_entry_joint_speed_limit_rad_s = {ENTRY_JOINT_SPEED_LIMIT_RAD_S:.17g}\n"
        f"  local diagnostic_entry_joint_acceleration_limit_rad_s2 = {ENTRY_JOINT_ACCELERATION_LIMIT_RAD_S2:.17g}\n"
        f"  local diagnostic_entry_tcp_translation_speed_limit_m_s = {ENTRY_TCP_TRANSLATION_SPEED_LIMIT_M_S:.17g}\n"
        f"  local diagnostic_entry_tcp_rotation_speed_limit_rad_s = {ENTRY_TCP_ROTATION_SPEED_LIMIT_RAD_S:.17g}\n"
    )
    if source.count(declaration_anchor) != 1:
        raise ValueError("diagnostic active-rate declaration anchor is not unique")
    transformed = source.replace(declaration_anchor, declarations, 1)

    speed_check = """          if actual_translation_speed > active_tcp_translation_speed_limit_m_s or actual_rotation_speed > active_tcp_rotation_speed_limit_rad_s:
            active_speed_violation = True
          end
          axis = 0
"""
    bounded_speed_check = """          local selected_tcp_translation_speed_limit_m_s = active_tcp_translation_speed_limit_m_s
          local selected_tcp_rotation_speed_limit_rad_s = active_tcp_rotation_speed_limit_rad_s
          if entry_elapsed_s < diagnostic_entry_transition_envelope_s:
            selected_tcp_translation_speed_limit_m_s = diagnostic_entry_tcp_translation_speed_limit_m_s
            selected_tcp_rotation_speed_limit_rad_s = diagnostic_entry_tcp_rotation_speed_limit_rad_s
          end
          if actual_translation_speed > selected_tcp_translation_speed_limit_m_s or actual_rotation_speed > selected_tcp_rotation_speed_limit_rad_s:
            active_speed_violation = True
          end
          axis = 0
"""
    if transformed.count(speed_check) != 1:
        raise ValueError("diagnostic TCP rate guard site is not unique")
    transformed = transformed.replace(speed_check, bounded_speed_check, 1)

    joint_checks = """            if qd[axis] > active_joint_speed_limit_rad_s or qd[axis] < -active_joint_speed_limit_rad_s:
              active_speed_violation = True
            end
            if qdd[axis] > active_joint_acceleration_limit_rad_s2 or qdd[axis] < -active_joint_acceleration_limit_rad_s2:
              active_acceleration_violation = True
            end
"""
    bounded_joint_checks = """            local selected_joint_speed_limit_rad_s = active_joint_speed_limit_rad_s
            local selected_joint_acceleration_limit_rad_s2 = active_joint_acceleration_limit_rad_s2
            if entry_elapsed_s < diagnostic_entry_transition_envelope_s:
              selected_joint_speed_limit_rad_s = diagnostic_entry_joint_speed_limit_rad_s
              selected_joint_acceleration_limit_rad_s2 = diagnostic_entry_joint_acceleration_limit_rad_s2
            end
            if qd[axis] > selected_joint_speed_limit_rad_s or qd[axis] < -selected_joint_speed_limit_rad_s:
              active_speed_violation = True
            end
            if qdd[axis] > selected_joint_acceleration_limit_rad_s2 or qdd[axis] < -selected_joint_acceleration_limit_rad_s2:
              active_acceleration_violation = True
            end
"""
    if transformed.count(joint_checks) != 1:
        raise ValueError("diagnostic joint rate guard site is not unique")
    transformed = transformed.replace(joint_checks, bounded_joint_checks, 1)
    if "entry_transition_tcp_translation_limit_m = 0.0003" not in transformed:
        raise ValueError("diagnostic entry TCP excursion guard was lost")
    if "entry_transition_joint_excursion_limit_rad = 0.0005" not in transformed:
        raise ValueError("diagnostic entry joint excursion guard was lost")
    return transformed


def _write_or_verify(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise RuntimeError(f"diagnostic immutable artifact mismatch:{path}")
        return
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _finite6(value: Any, name: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != 6:
        raise ValueError(f"{name} must be a six-element list")
    result = tuple(float(item) for item in value)
    if any(not math.isfinite(item) for item in result):
        raise ValueError(f"{name} must contain finite values")
    return result


def load_historical_base_bundle(manifest_path: Path) -> legacy.ValidatedBundle:
    """Verify an immutable pre-seqlock bundle without relaxing live checks.

    The current formal parser correctly requires the newer generation echo,
    while this diagnostic intentionally starts from the already-run preflight
    bytes.  Verify their hashes, reference, Tube, friction identity, protocol,
    force authority, and controller structure directly before transforming the
    single execution-context variable.
    """

    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping) or manifest.get("schema") != legacy.BUNDLE_SCHEMA:
        raise ValueError("historical bundle manifest schema mismatch")
    source_path = (manifest_path.parent / str(manifest["receiver_source"])).resolve()
    reference_path = Path(str(manifest["reference_artifact"])).resolve()
    source_sha = _sha256(source_path)
    reference_sha = _sha256(reference_path)
    if source_sha != manifest.get("receiver_source_sha256"):
        raise ValueError("historical receiver source SHA-256 mismatch")
    if reference_sha != manifest.get("reference_artifact_sha256"):
        raise ValueError("historical reference artifact SHA-256 mismatch")

    source = source_path.read_text(encoding="utf-8")
    required_source_tokens = (
        "thread torqueThread():",
        "run torqueThread()",
        "direct_torque(torque, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)",
        f"local receiver_protocol_token = {legacy.LIVE_PROTOCOL_TOKEN}",
        "guard_force_norm > 6.0 or guard_torque_norm > 0.5",
        "active_tcp_translation_speed_limit_m_s = 0.01",
    )
    missing = [token for token in required_source_tokens if token not in source]
    if missing:
        raise ValueError(f"historical receiver missing diagnostic prerequisites:{missing}")
    pose_error_forms = (
        "pose_error[0] = control_eq[0] - actual_pose[0]",
        "local pose_error = pose_sub(p[control_eq[0]",
    )
    if not any(token in source for token in pose_error_forms):
        raise ValueError("historical receiver is missing Cartesian pose feedback")
    forbidden_force_tokens = ("get_tcp_force", "actual_TCP_force", "force_mode(")
    present = [token for token in forbidden_force_tokens if token in source]
    if present:
        raise ValueError(f"historical receiver contains forbidden force source:{present}")

    friction_profile = str(manifest.get("friction_profile"))
    if friction_profile not in FRICTION_PROFILES:
        raise ValueError("historical bundle friction profile is unknown")
    viscous_scale = _finite6(manifest.get("viscous_scale"), "viscous_scale")
    coulomb_scale = _finite6(manifest.get("coulomb_scale"), "coulomb_scale")
    if (viscous_scale, coulomb_scale) != FRICTION_PROFILES[friction_profile]:
        raise ValueError("historical bundle friction values do not match profile")
    if f"viscous_scale = [{', '.join(format(value, 'g') for value in viscous_scale)}]" not in source:
        raise ValueError("historical receiver viscous scale mismatch")
    if f"coulomb_scale = [{', '.join(format(value, 'g') for value in coulomb_scale)}]" not in source:
        raise ValueError("historical receiver coulomb scale mismatch")

    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    if not isinstance(reference, Mapping):
        raise ValueError("historical reference artifact type mismatch")
    timeline = legacy.ReferenceTimeline.from_payload(reference)
    tube = legacy.LiveTubeContract.from_reference_artifact(reference_path)
    for row in timeline.rows:
        tube.assert_contains_pose(row["desired_pose_base"], role="desired")
    return legacy.ValidatedBundle(
        source_path=source_path,
        manifest_path=manifest_path,
        reference_path=reference_path,
        source_sha256=source_sha,
        manifest_sha256=_sha256(manifest_path),
        reference_sha256=reference_sha,
        friction_profile=friction_profile,
        viscous_scale=viscous_scale,
        coulomb_scale=coulomb_scale,
        source=source,
        reference=reference,
        timeline=timeline,
        tube=tube,
    )


def build_diagnostic_bundle(
    *,
    base_manifest: Path,
    output_dir: Path,
    source_mode: str,
    execution_mode: str,
    entry_transition_envelope: bool,
    reference_override: Path | None,
) -> legacy.ValidatedBundle:
    try:
        base = legacy.validate_bundle(base_manifest.resolve())
    except ValueError as exc:
        if "missing contract token: torque_generation_begin" not in str(exc):
            raise
        base = load_historical_base_bundle(base_manifest.resolve())
    if reference_override is not None:
        reference_path = reference_override.resolve()
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        if not isinstance(reference, Mapping):
            raise ValueError("diagnostic reference override type mismatch")
        supplied_content_sha = reference.get("content_sha256")
        unsigned_reference = dict(reference)
        unsigned_reference.pop("content_sha256", None)
        canonical_reference = json.dumps(
            unsigned_reference,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if supplied_content_sha != _sha256_bytes(canonical_reference):
            raise ValueError("diagnostic reference override content SHA-256 mismatch")
        timeline = legacy.ReferenceTimeline.from_payload(reference)
        tube = legacy.LiveTubeContract.from_reference_artifact(reference_path)
        origin = tuple(float(value) for value in timeline.rows[0]["desired_pose_base"][:3])
        maximum_excursion_m = max(
            math.dist(
                origin,
                tuple(float(value) for value in row["desired_pose_base"][:3]),
            )
            for row in timeline.rows
        )
        maximum_speed_m_s = max(
            math.sqrt(sum(float(value) ** 2 for value in row["desired_twist_base"][:3]))
            for row in timeline.rows
        )
        if maximum_excursion_m > 0.003 + 1e-12:
            raise ValueError("diagnostic reference override exceeds 3 mm envelope")
        if maximum_speed_m_s > 0.010 + 1e-12:
            raise ValueError("diagnostic reference override exceeds 10 mm/s")
        for row in timeline.rows:
            tube.assert_contains_pose(row["desired_pose_base"], role="desired")
        base = replace(
            base,
            reference_path=reference_path,
            reference_sha256=_sha256(reference_path),
            reference=reference,
            timeline=timeline,
            tube=tube,
        )
    threaded_baseline_path: Path | None = None
    if source_mode == "current_friction_diagnostic":
        current_source = build_live_receiver_source(
            base.tube,
            friction_profile=FRICTION_PROFILE_UR_DEFAULT_V2_DIAGNOSTIC,
            guard_force_limit_n=6.0,
            guard_torque_limit_nm=0.5,
        )
        current_contract = parse_live_receiver_source(current_source)
        threaded_baseline_path = (
            output_dir.resolve() / "receiver_threaded_baseline_v4.script"
        )
        _write_or_verify(threaded_baseline_path, current_source.encode("utf-8"))
        base = replace(
            base,
            source_path=threaded_baseline_path,
            source_sha256=_sha256(threaded_baseline_path),
            friction_profile=current_contract.friction_profile,
            viscous_scale=current_contract.viscous_scale,
            coulomb_scale=current_contract.coulomb_scale,
            source=current_source,
        )
    elif source_mode != "historical_zero_isolation":
        raise ValueError(f"unsupported diagnostic source mode:{source_mode}")
    if execution_mode == "immediate_main_loop":
        source = transform_to_main_loop(base.source)
        source_name = "receiver_main_loop_diagnostic_v1.script"
    elif execution_mode == "thread_handoff_main_loop":
        source = transform_to_thread_handoff_main_loop(base.source)
        source_name = "receiver_thread_handoff_main_loop_diagnostic_v1.script"
    elif execution_mode == "dedicated_thread_disable_high_hold":
        source = transform_to_disable_high_holding_torque(base.source)
        source_name = "receiver_thread_disable_high_hold_diagnostic_v1.script"
    elif execution_mode == "dedicated_thread_default":
        source = base.source
        source_name = "receiver_thread_default_diagnostic_v1.script"
    elif execution_mode == "dedicated_thread_applied_echo":
        source = transform_to_thread_applied_echo(base.source)
        source_name = "receiver_thread_applied_echo_diagnostic_v1.script"
    elif execution_mode == "dedicated_thread_joint0_bias_1nm":
        source = transform_to_joint0_bias_probe(base.source)
        source_name = "receiver_thread_joint0_bias_1nm_diagnostic_v1.script"
    else:
        raise ValueError(f"unsupported diagnostic execution mode:{execution_mode}")
    if entry_transition_envelope:
        source = add_bounded_entry_transition_envelope(source)
        if execution_mode == "thread_handoff_main_loop":
            validate_thread_handoff_source(source)
        elif execution_mode == "immediate_main_loop":
            validate_transformed_source(source)
    changed_variable = (
        "ur_high_holding_torque_state"
        if execution_mode == "dedicated_thread_disable_high_hold"
        else (
            "baseline_none"
            if execution_mode == "dedicated_thread_default"
            else (
                "thread_applied_torque_echo"
                if execution_mode == "dedicated_thread_applied_echo"
                else (
                    "joint0_torque_bias_1nm"
                    if execution_mode == "dedicated_thread_joint0_bias_1nm"
                    else "direct_torque_call_execution_context"
                )
            )
        )
    )
    source_path = output_dir.resolve() / source_name
    source_bytes = source.encode("utf-8")
    _write_or_verify(source_path, source_bytes)
    manifest_path = output_dir.resolve() / "diagnostic_bundle.json"
    manifest: dict[str, Any] = {
        "schema": DIAGNOSTIC_SCHEMA,
        "claim_class": "no_contact_execution_mode_ablation_only",
        "formal_campaign_eligible": False,
        "model_active": False,
        "ur_internal_ft_used": False,
        "force_guard_authority": "kunwei_kwr75_tcp_raw_stream_v1",
        "base_manifest": str(base.manifest_path),
        "base_manifest_sha256": base.manifest_sha256,
        "base_receiver_sha256": base.source_sha256,
        "threaded_baseline_source": (
            None if threaded_baseline_path is None else str(threaded_baseline_path)
        ),
        "source_mode": source_mode,
        "execution_mode": execution_mode,
        "high_holding_torque_disabled_during_receiver": (
            execution_mode == "dedicated_thread_disable_high_hold"
        ),
        "high_holding_torque_restored_before_complete": (
            execution_mode == "dedicated_thread_disable_high_hold"
        ),
        "thread_handoff_s": (
            HANDOFF_DELAY_S if execution_mode == "thread_handoff_main_loop" else None
        ),
        "entry_transition_envelope": entry_transition_envelope,
        "entry_transition_limits": (
            {
                "duration_s": ENTRY_TRANSITION_ENVELOPE_S,
                "joint_speed_rad_s": ENTRY_JOINT_SPEED_LIMIT_RAD_S,
                "joint_acceleration_rad_s2": ENTRY_JOINT_ACCELERATION_LIMIT_RAD_S2,
                "tcp_translation_speed_m_s": ENTRY_TCP_TRANSLATION_SPEED_LIMIT_M_S,
                "tcp_rotation_speed_rad_s": ENTRY_TCP_ROTATION_SPEED_LIMIT_RAD_S,
                "unchanged_tcp_excursion_m": 0.0003,
                "unchanged_joint_excursion_rad": 0.0005,
            }
            if entry_transition_envelope
            else None
        ),
        "friction_profile": base.friction_profile,
        "viscous_scale": list(base.viscous_scale),
        "coulomb_scale": list(base.coulomb_scale),
        "receiver_source": str(source_path),
        "receiver_source_sha256": _sha256_bytes(source_bytes),
        "reference_artifact": str(base.reference_path),
        "reference_artifact_sha256": base.reference_sha256,
        "reference_override": reference_override is not None,
        "single_changed_variable": changed_variable,
        "from": (
            "high_holding_torque_enabled_default"
            if execution_mode == "dedicated_thread_disable_high_hold"
            else (
                "nominal_thread_selected_torque"
                if execution_mode == "dedicated_thread_joint0_bias_1nm"
                else (
                    "main_computation_tau_echo"
                    if execution_mode == "dedicated_thread_applied_echo"
                    else (
                        "dedicated_torque_thread"
                        if execution_mode != "dedicated_thread_default"
                        else "baseline"
                    )
                )
            )
        ),
        "to": (
            "high_holding_torque_disabled_for_receiver_lifetime"
            if execution_mode == "dedicated_thread_disable_high_hold"
            else (
                "thread_selected_torque_plus_joint0_1nm"
                if execution_mode == "dedicated_thread_joint0_bias_1nm"
                else (
                    "torque_thread_selected_tau_echo"
                    if execution_mode == "dedicated_thread_applied_echo"
                    else (
                        "main_500hz_control_loop"
                        if execution_mode != "dedicated_thread_default"
                        else "unchanged_high_holding_torque_default"
                    )
                )
            )
        ),
    }
    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    _write_or_verify(manifest_path, manifest_bytes)
    return replace(
        base,
        source_path=source_path,
        manifest_path=manifest_path,
        source_sha256=_sha256(source_path),
        manifest_sha256=_sha256(manifest_path),
        source=source,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-bundle", type=Path, required=True)
    parser.add_argument("--reference-override", type=Path)
    parser.add_argument("--bundle-output", type=Path, required=True)
    parser.add_argument(
        "--source-mode",
        choices=("historical_zero_isolation", "current_friction_diagnostic"),
        default="historical_zero_isolation",
    )
    parser.add_argument(
        "--execution-mode",
        choices=(
            "immediate_main_loop",
            "thread_handoff_main_loop",
            "dedicated_thread_disable_high_hold",
            "dedicated_thread_default",
            "dedicated_thread_applied_echo",
            "dedicated_thread_joint0_bias_1nm",
        ),
        default="immediate_main_loop",
    )
    parser.add_argument("--entry-transition-envelope", action="store_true")
    parser.add_argument(
        "--stage",
        choices=legacy.CANARY_STAGE_ORDER,
        required=True,
    )
    parser.add_argument("--compile-probe-evidence", type=Path, required=True)
    parser.add_argument("--prior-stage-evidence", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument(
        "--kunwei-calibration",
        type=Path,
        default=ROOT / "config" / "step5d_tacdiffusion_sensor_frame_v4.json",
    )
    parser.add_argument("--connect-timeout-s", type=float, default=3.0)
    parser.add_argument("--receiver-wait-s", type=float, default=2.0)
    parser.add_argument("--sensor-delivery-watchdog-s", type=float, default=0.080)
    for gate in (
        "live",
        "send-urscript",
        "write-rtde-inputs",
        "allow-direct-torque",
        "allow-motion",
        "no-contact",
        "allow-kunwei-stream-command",
    ):
        parser.add_argument(f"--{gate}", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bundle = build_diagnostic_bundle(
        base_manifest=args.base_bundle,
        output_dir=args.bundle_output,
        source_mode=args.source_mode,
        execution_mode=args.execution_mode,
        entry_transition_envelope=args.entry_transition_envelope,
        reference_override=args.reference_override,
    )
    # Attributes consumed only by the optional recorder sidecar.
    args.canary_stage = args.stage
    args.recorder_output = None
    args.shadow_transition_receipt = None
    result = legacy.run_live(args, bundle)
    result["diagnostic_schema"] = DIAGNOSTIC_SCHEMA
    result["formal_campaign_eligible"] = False
    result["single_changed_variable"] = (
        "ur_high_holding_torque_state"
        if args.execution_mode == "dedicated_thread_disable_high_hold"
        else (
            "baseline_none"
            if args.execution_mode == "dedicated_thread_default"
            else (
                "thread_applied_torque_echo"
                if args.execution_mode == "dedicated_thread_applied_echo"
                else (
                    "joint0_torque_bias_1nm"
                    if args.execution_mode == "dedicated_thread_joint0_bias_1nm"
                    else "direct_torque_call_execution_context"
                )
            )
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
