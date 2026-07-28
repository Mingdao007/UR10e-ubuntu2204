"""Remote-Control Direct Torque receiver v4 and its fail-closed protocol.

Version 3 remains immutable historical evidence.  Version 4 fixes the live
transport contract: a controller-resident 500 Hz loop may hold an unchanged
RTDE packet for a bounded number of robot ticks, echoes the complete command
identity, publishes the applied 12D action, invokes its own program entrypoint,
and explicitly returns to position control through ``stopj``.

This module only builds and parses URScript.  It never opens a socket, writes
RTDE inputs, sends URScript, or moves the robot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
from pathlib import Path
import re
from typing import Mapping, Sequence

from .unknown_surface_episode import rotation_vector_distance_rad


LIVE_RECEIVER_SCHEMA = "ur10e_direct_torque_receiver/v4"
LIVE_REFERENCE_SCHEMA = "ur10e_tacdiffusion_unknown_surface_episode_artifact/v1"
LIVE_PROTOCOL_TOKEN = 4_000_004
COMPILE_PROBE_SCHEMA = "ur10e_direct_torque_compile_probe/v1"
COMPILE_PROBE_PROTOCOL_TOKEN = 4_000_104
WRENCH_FRAME_TOKEN = 5_252_001
CONTROL_RATE_HZ = 500
DEFAULT_HEARTBEAT_TIMEOUT_TICKS = 10
NO_CONTACT_RELEASE_TOLERANCE_M = 0.001
ORIENTATION_POLICY_HOLD_ENTRY = "hold_entry_orientation"
ORIENTATION_POLICY_INTERPOLATE_POSE = "interpolate_pose_geodesic"
ORIENTATION_INTERPOLATION_POLICIES = (
    ORIENTATION_POLICY_HOLD_ENTRY,
    ORIENTATION_POLICY_INTERPOLATE_POSE,
)
FRICTION_PROFILE_ZERO_ISOLATION = "zero_isolation"
FRICTION_PROFILE_UR_DEFAULT_V2_DIAGNOSTIC = "ur_default_v2_diagnostic"
FRICTION_PROFILES = {
    FRICTION_PROFILE_ZERO_ISOLATION: (
        (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    ),
    FRICTION_PROFILE_UR_DEFAULT_V2_DIAGNOSTIC: (
        (0.9, 0.9, 0.8, 0.9, 0.9, 0.9),
        (0.8, 0.8, 0.7, 0.8, 0.8, 0.8),
    ),
}


def build_compile_probe_source() -> str:
    """Build a bounded Secondary Client parser/connectivity probe with no motion API."""

    return f'''def tacdiffusion_direct_torque_compile_probe_v1():
  local probe_schema = "{COMPILE_PROBE_SCHEMA}"
  local probe_protocol_token = {COMPILE_PROBE_PROTOCOL_TOKEN}
  local probe_tick = 0
  while probe_tick < 50:
    write_output_integer_register(24, 77)
    write_output_integer_register(25, probe_tick)
    write_output_integer_register(26, 0)
    write_output_integer_register(32, probe_protocol_token)
    sync()
    probe_tick = probe_tick + 1
  end
  write_output_integer_register(24, 78)
  write_output_integer_register(25, probe_tick)
  write_output_integer_register(32, probe_protocol_token)
end
tacdiffusion_direct_torque_compile_probe_v1()
'''


def parse_compile_probe_source(source: str) -> None:
    required = (
        f'probe_schema = "{COMPILE_PROBE_SCHEMA}"',
        f"probe_protocol_token = {COMPILE_PROBE_PROTOCOL_TOKEN}",
        "probe_tick < 50",
        "write_output_integer_register(24, 77)",
        "tacdiffusion_direct_torque_compile_probe_v1()",
    )
    if any(token not in source for token in required):
        raise ValueError("compile probe source is incomplete")
    forbidden = (
        r"\bdirect_torque\s*\(",
        r"\bstopj\s*\(",
        r"\b(movej|movel|movec|speedj|speedl|servoj|force_mode)\s*\(",
        r"\bread_input_(?:float|integer)_register\s*\(",
    )
    if any(re.search(pattern, source, flags=re.IGNORECASE) for pattern in forbidden):
        raise ValueError("compile probe must not contain torque, motion, or input APIs")
    if len(re.findall(r"(?m)^\s*def\s+", source)) != 1:
        raise ValueError("compile probe requires one top-level program")


def _finite(values: Sequence[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


def _positive(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _urscript_vector(values: Sequence[float]) -> str:
    return "[" + ", ".join(f"{float(value):.17g}" for value in values) + "]"


@dataclass(frozen=True)
class LiveTubeContract:
    center_base_m: tuple[float, ...] | Sequence[float]
    anchor_pose_base: tuple[float, ...] | Sequence[float]
    u_axis_base: tuple[float, ...] | Sequence[float]
    v_axis_base: tuple[float, ...] | Sequence[float]
    safe_u_half_width_m: float
    safe_v_half_width_m: float
    normal_half_width_m: float
    orientation_tolerance_rad: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "center_base_m", _finite(self.center_base_m, 3, "center_base_m")
        )
        object.__setattr__(
            self, "anchor_pose_base", _finite(self.anchor_pose_base, 6, "anchor_pose_base")
        )
        object.__setattr__(
            self, "u_axis_base", _finite(self.u_axis_base, 3, "u_axis_base")
        )
        object.__setattr__(
            self, "v_axis_base", _finite(self.v_axis_base, 3, "v_axis_base")
        )
        for name in (
            "safe_u_half_width_m",
            "safe_v_half_width_m",
            "normal_half_width_m",
            "orientation_tolerance_rad",
        ):
            object.__setattr__(self, name, _positive(getattr(self, name), name))
        for name in ("u_axis_base", "v_axis_base"):
            axis = getattr(self, name)
            norm = math.sqrt(sum(value * value for value in axis))
            if abs(norm - 1.0) > 1e-6 or abs(axis[2]) > 1e-9:
                raise ValueError(f"{name} must be a unit in-plane axis")
        dot = sum(a * b for a, b in zip(self.u_axis_base, self.v_axis_base))
        if abs(dot) > 1e-6:
            raise ValueError("tube axes must be orthogonal")
        anchor_delta = tuple(
            self.anchor_pose_base[index] - self.center_base_m[index]
            for index in range(3)
        )
        anchor_u = sum(
            anchor_delta[index] * self.u_axis_base[index] for index in range(3)
        )
        anchor_v = sum(
            anchor_delta[index] * self.v_axis_base[index] for index in range(3)
        )
        if (
            abs(anchor_u) > self.safe_u_half_width_m
            or abs(anchor_v) > self.safe_v_half_width_m
            or abs(anchor_delta[2]) > self.normal_half_width_m
        ):
            raise ValueError("episode anchor lies outside the hard tube")

    @classmethod
    def from_reference_artifact(
        cls,
        path: str | Path,
    ) -> "LiveTubeContract":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != LIVE_REFERENCE_SCHEMA
            or payload.get("cad_height_or_normal_feedforward") is not False
        ):
            raise ValueError("unknown-surface reference artifact is incompatible")
        tube = payload.get("tube")
        if not isinstance(tube, Mapping):
            raise ValueError("unknown-surface reference tube is missing")
        return cls(
            center_base_m=tube["center_base_m"],
            anchor_pose_base=payload["anchor_pose_base"],
            u_axis_base=tube["u_axis_base"],
            v_axis_base=tube["v_axis_base"],
            safe_u_half_width_m=tube["safe_u_half_width_m"],
            safe_v_half_width_m=tube["safe_v_half_width_m"],
            normal_half_width_m=tube["normal_half_width_m"],
            orientation_tolerance_rad=tube["orientation_tolerance_rad"],
        )

    def assert_contains_pose(self, pose_base: Sequence[float], *, role: str) -> None:
        pose = _finite(pose_base, 6, f"{role} pose")
        delta = tuple(pose[index] - self.center_base_m[index] for index in range(3))
        local_u = sum(
            delta[index] * self.u_axis_base[index] for index in range(3)
        )
        local_v = sum(
            delta[index] * self.v_axis_base[index] for index in range(3)
        )
        if abs(local_u) > self.safe_u_half_width_m + 1e-12:
            raise RuntimeError(f"{role}_tube_u_guard")
        if abs(local_v) > self.safe_v_half_width_m + 1e-12:
            raise RuntimeError(f"{role}_tube_v_guard")
        if abs(delta[2]) > self.normal_half_width_m + 1e-12:
            raise RuntimeError(f"{role}_tube_normal_guard")
        if (
            rotation_vector_distance_rad(pose[3:], self.anchor_pose_base[3:])
            > self.orientation_tolerance_rad + 1e-12
        ):
            raise RuntimeError(f"{role}_tube_orientation_guard")

    def rebased(self, anchor_pose_base: Sequence[float]) -> "LiveTubeContract":
        """Keep the certified envelope but bind it to a fresh episode entry pose."""

        anchor = _finite(anchor_pose_base, 6, "rebase anchor pose")
        return LiveTubeContract(
            center_base_m=anchor[:3],
            anchor_pose_base=anchor,
            u_axis_base=self.u_axis_base,
            v_axis_base=self.v_axis_base,
            safe_u_half_width_m=self.safe_u_half_width_m,
            safe_v_half_width_m=self.safe_v_half_width_m,
            normal_half_width_m=self.normal_half_width_m,
            orientation_tolerance_rad=self.orientation_tolerance_rad,
        )


class SequenceDecision(str, Enum):
    NEW = "NEW"
    HELD = "HELD"
    FAULT = "FAULT"


@dataclass(frozen=True)
class SequenceResult:
    decision: SequenceDecision
    next_age_ticks: int
    reason: str = ""


def evaluate_sequence(
    *,
    last_sequence: int,
    incoming_sequence: int,
    held_age_ticks: int,
    heartbeat_timeout_ticks: int = DEFAULT_HEARTBEAT_TIMEOUT_TICKS,
) -> SequenceResult:
    """Python oracle for the v4 new/held/gap/stale receiver semantics."""

    if (
        last_sequence < 0
        or incoming_sequence <= 0
        or held_age_ticks < 0
        or heartbeat_timeout_ticks <= 0
    ):
        raise ValueError("sequence oracle arguments are invalid")
    if incoming_sequence == last_sequence + 1:
        return SequenceResult(SequenceDecision.NEW, 0)
    if last_sequence > 0 and incoming_sequence == last_sequence:
        next_age = held_age_ticks + 1
        if next_age <= heartbeat_timeout_ticks:
            return SequenceResult(SequenceDecision.HELD, next_age)
        return SequenceResult(
            SequenceDecision.FAULT,
            next_age,
            "heartbeat_stale",
        )
    return SequenceResult(SequenceDecision.FAULT, held_age_ticks, "sequence_gap_or_replay")


@dataclass(frozen=True)
class LiveReceiverContract:
    schema: str
    control_rate_hz: int
    protocol_token: int
    heartbeat_timeout_ticks: int
    invocation_present: bool
    explicit_position_handoff: bool
    complete_identity_echo: bool
    applied_action_echo: bool
    hard_tube_guard: bool
    dedicated_torque_thread: bool
    orientation_interpolation_policy: str
    friction_profile: str
    viscous_scale: tuple[float, ...]
    coulomb_scale: tuple[float, ...]
    source_builder_physical_io_enabled: bool
    controller_runtime_physical_io_enabled: bool


def build_live_receiver_source(
    tube: LiveTubeContract,
    *,
    heartbeat_timeout_ticks: int = DEFAULT_HEARTBEAT_TIMEOUT_TICKS,
    orientation_interpolation_policy: str = ORIENTATION_POLICY_HOLD_ENTRY,
    friction_profile: str = FRICTION_PROFILE_ZERO_ISOLATION,
) -> str:
    """Build one controller-resident 500 Hz program; sending is a separate gate."""

    if not isinstance(tube, LiveTubeContract):
        raise TypeError("tube must be a LiveTubeContract")
    if not 5 <= int(heartbeat_timeout_ticks) <= 100:
        raise ValueError("heartbeat timeout must be between 5 and 100 robot ticks")
    if orientation_interpolation_policy not in ORIENTATION_INTERPOLATION_POLICIES:
        raise ValueError(
            "orientation interpolation policy must be one of "
            + ", ".join(ORIENTATION_INTERPOLATION_POLICIES)
        )
    if friction_profile not in FRICTION_PROFILES:
        raise ValueError(
            "friction profile must be one of " + ", ".join(FRICTION_PROFILES)
        )
    viscous_scale, coulomb_scale = FRICTION_PROFILES[friction_profile]
    center = _urscript_vector(tube.center_base_m)
    anchor = _urscript_vector(tube.anchor_pose_base)
    u_axis = _urscript_vector(tube.u_axis_base)
    v_axis = _urscript_vector(tube.v_axis_base)
    if orientation_interpolation_policy == ORIENTATION_POLICY_HOLD_ENTRY:
        orientation_policy_declaration = ""
        orientation_prelude = ""
        orientation_assignment = """            if axis < 3:
              control_eq[axis] = entry_pose[axis] + blend*(last_eq[axis] - entry_pose[axis])
            else:
              # Axis-angle coordinates have a branch cut at +/-pi.  The
              # no-contact canary has no orientation trajectory, so hold the
              # measured entry orientation instead of interpolating two
              # equivalent rotvec representations through a 2*pi excursion.
              control_eq[axis] = entry_pose[axis]
            end"""
    else:
        orientation_policy_declaration = (
            "  local orientation_interpolation_policy = "
            f'"{orientation_interpolation_policy}"\n'
        )
        orientation_prelude = """          local interpolated_control_pose = interpolate_pose(
            p[entry_pose[0], entry_pose[1], entry_pose[2], entry_pose[3], entry_pose[4], entry_pose[5]],
            p[last_eq[0], last_eq[1], last_eq[2], last_eq[3], last_eq[4], last_eq[5]],
            blend)
"""
        orientation_assignment = (
            "            control_eq[axis] = interpolated_control_pose[axis]"
        )
    wrapped_source = f'''def tacdiffusion_remote_direct_torque_v4_program():
  torque_thread_run = False
  torque_command = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  friction_profile = "{friction_profile}"
  viscous_scale = {_urscript_vector(viscous_scale)}
  coulomb_scale = {_urscript_vector(coulomb_scale)}
  torque_thread_tick_count = 0
  control_update_count = 0
  torque_thread_last_control_update_count = -1
  torque_thread_stale_ticks = 0
  torque_thread_watchdog_fault = False

  thread torqueThread():
    while torque_thread_run:
      if control_update_count == torque_thread_last_control_update_count:
        torque_thread_stale_ticks = torque_thread_stale_ticks + 1
      else:
        torque_thread_last_control_update_count = control_update_count
        torque_thread_stale_ticks = 0
      end
      if torque_thread_stale_ticks >= 25:
        torque_thread_watchdog_fault = True
        torque_thread_run = False
      else:
        local torque = torque_command
        direct_torque(torque, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)
        torque_thread_tick_count = torque_thread_tick_count + 1
      end
    end
    stopj(10.0)
  end

  # generated Remote-Control Direct Torque receiver; sending is separately authorized
  local receiver_schema = "{LIVE_RECEIVER_SCHEMA}"
  local receiver_protocol_token = {LIVE_PROTOCOL_TOKEN}
  local control_rate_hz = {CONTROL_RATE_HZ}
  local heartbeat_timeout_ticks = {int(heartbeat_timeout_ticks)}
{orientation_policy_declaration}\
  local source_builder_physical_io_enabled = False
  local controller_runtime_physical_io_enabled = True
  local wrench_frame_token = {WRENCH_FRAME_TOKEN}
  local tube_center_base = {center}
  local tube_anchor_pose_base = p{anchor}
  local tube_u_axis_base = {u_axis}
  local tube_v_axis_base = {v_axis}
  local tube_u_half_width_m = {tube.safe_u_half_width_m:.17g}
  local tube_v_half_width_m = {tube.safe_v_half_width_m:.17g}
  local tube_normal_half_width_m = {tube.normal_half_width_m:.17g}
  local tube_orientation_tolerance_rad = {tube.orientation_tolerance_rad:.17g}
  local release_ready_tolerance_m = {NO_CONTACT_RELEASE_TOLERANCE_M:.17g}
  local k_min = [25.0, 25.0, 25.0, 0.5, 0.5, 0.5]
  local k_max = [1000.0, 1000.0, 1000.0, 60.0, 60.0, 60.0]
  local receiver_force_limit = [20.0, 20.0, 20.0, 2.0, 2.0, 2.0]
  # Gravity is compensated internally by direct_torque().  Friction/stiction
  # compensation is bound explicitly by the selected immutable profile.
  local virtual_mass = [2.0, 2.0, 2.0, 0.2, 0.2, 0.2]
  local damping_ratio = 1.0
  local critical_natural_frequency_rad_s = 92.10340371976183
  local entry_blend_duration_s = 0.1
  local entry_stable_duration_s = 0.05
  local entry_velocity_filter_tau_s = 0.05
  local entry_velocity_filter_warmup_s = 0.15
  local entry_tcp_translation_speed_limit_m_s = 0.001
  local entry_tcp_rotation_speed_limit_rad_s = 0.002
  local entry_joint_speed_limit_rad_s = 0.001
  local entry_transition_tcp_translation_limit_m = 0.0003
  local entry_transition_joint_excursion_limit_rad = 0.0005
  local active_joint_speed_limit_rad_s = 0.02
  local active_joint_acceleration_limit_rad_s2 = 5.0
  local active_tcp_translation_speed_limit_m_s = 0.01
  local active_tcp_rotation_speed_limit_rad_s = 0.02
  local command_idle = 0
  local command_run = 1
  local command_end = 2
  local command_abort = 3
  local running = True
  local torque_entered = False
  local tube_rebased = False
  local entry_elapsed_s = 0.0
  local entry_stable_elapsed_s = 0.0
  local entry_velocity_filter_elapsed_s = 0.0
  local exit_fault = 0
  local exit_reason = 0
  local last_sequence = 0
  local held_age_s = 0.0
  local heartbeat_timeout_s = heartbeat_timeout_ticks*get_steptime()
  local lease_id = 0
  local episode_identity = 0
  local episode_latched = 0
  local last_observed_command = 0
  local last_model_sequence = 0
  local last_model_period_us = 0
  local last_model_mode = 0
  local last_model_timestamp_us = 0
  local last_eq = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local last_k = [600.0, 600.0, 600.0, 30.0, 30.0, 30.0]
  local last_guard_wrench = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local last_raw_force = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local filtered_force = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local filter_velocity = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local entry_pose = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local entry_joint_positions = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local filtered_entry_tcp_speed = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local filtered_entry_joint_speed = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local torque_thread_handle = 0
  local maximum_control_update_gap_s = 0.0
  local action_publish_generation = 0
  local initial_control_clock = time()
  local last_control_time_s = initial_control_clock.sec + initial_control_clock.nanosec/1000000000.0
  write_output_integer_register(24, 0)
  write_output_integer_register(25, 0)
  write_output_integer_register(26, 0)
  write_output_integer_register(27, 0)
  write_output_integer_register(28, 0)
  write_output_integer_register(29, 0)
  write_output_integer_register(30, 0)
  write_output_integer_register(31, 0)
  write_output_integer_register(32, receiver_protocol_token)
  write_output_integer_register(33, 0)
  write_output_integer_register(34, last_observed_command)
  write_output_integer_register(35, episode_latched)
  while running:
    local control_clock = time()
    local control_time_s = control_clock.sec + control_clock.nanosec/1000000000.0
    local control_dt_s = control_time_s - last_control_time_s
    if control_dt_s < get_steptime():
      control_dt_s = get_steptime()
    end
    last_control_time_s = control_time_s
    if control_dt_s > maximum_control_update_gap_s:
      maximum_control_update_gap_s = control_dt_s
    end
    local command = read_input_integer_register(24)
    local sequence_before = read_input_integer_register(25)
    local heartbeat = read_input_integer_register(26)
    local packet_lease = read_input_integer_register(27)
    local model_sequence = read_input_integer_register(28)
    local model_period_us = read_input_integer_register(29)
    local model_mode = read_input_integer_register(30)
    local frame_token = read_input_integer_register(31)
    local model_timestamp_us = read_input_integer_register(32)
    local packet_episode = read_input_integer_register(35)
    local sequence_after = read_input_integer_register(25)
    last_observed_command = command
    if episode_latched == 0:
      local startup_packet_ok = command == command_idle and sequence_before == 0 and sequence_after == 0 and heartbeat == 0 and packet_lease > 0 and packet_episode > 0 and frame_token == wrench_frame_token
      if startup_packet_ok:
        lease_id = packet_lease
        episode_identity = packet_episode
        episode_latched = 1
      end
      write_output_integer_register(24, 0)
      write_output_integer_register(25, 0)
      write_output_integer_register(26, 0)
      write_output_integer_register(27, lease_id)
      write_output_integer_register(28, 0)
      write_output_integer_register(29, 0)
      write_output_integer_register(30, 0)
      write_output_integer_register(31, episode_identity)
      write_output_integer_register(32, receiver_protocol_token)
      write_output_integer_register(33, 0)
      write_output_integer_register(34, last_observed_command)
      write_output_integer_register(35, episode_latched)
      sync()
    elif packet_lease != lease_id or packet_episode != episode_identity:
      exit_fault = 10
      exit_reason = 10
      running = False
    elif command == command_end:
      exit_reason = 2
      running = False
    elif command == command_abort:
      exit_fault = 9
      exit_reason = 3
      running = False
    elif command != command_run:
      if torque_entered:
        exit_fault = 8
        exit_reason = 8
        running = False
      else:
        write_output_integer_register(24, 0)
        write_output_integer_register(25, last_sequence)
        write_output_integer_register(26, 0)
        write_output_integer_register(27, lease_id)
        write_output_integer_register(28, last_model_sequence)
        write_output_integer_register(29, 0)
        write_output_integer_register(30, 0)
        write_output_integer_register(31, episode_identity)
        write_output_integer_register(32, receiver_protocol_token)
        write_output_integer_register(33, 0)
        write_output_integer_register(34, last_observed_command)
        write_output_integer_register(35, episode_latched)
        sync()
      end
    else:
      local eq = [read_input_float_register(24), read_input_float_register(25), read_input_float_register(26), read_input_float_register(27), read_input_float_register(28), read_input_float_register(29)]
      local desired_k = [read_input_float_register(30), read_input_float_register(31), read_input_float_register(32), read_input_float_register(33), read_input_float_register(34), read_input_float_register(35)]
      local guard_wrench = [read_input_float_register(36), read_input_float_register(37), read_input_float_register(38), read_input_float_register(39), read_input_float_register(40), read_input_float_register(41)]
      local raw_force = [read_input_float_register(42), read_input_float_register(43), read_input_float_register(44), read_input_float_register(45), read_input_float_register(46), read_input_float_register(47)]
      local coherent = sequence_before == sequence_after and heartbeat == sequence_after
      local new_packet = sequence_after == last_sequence + 1
      local held_packet = last_sequence > 0 and sequence_after == last_sequence
      local packet_ok = coherent and sequence_after > 0 and packet_lease > 0 and packet_episode > 0 and frame_token == wrench_frame_token
      if lease_id > 0 and packet_lease != lease_id:
        packet_ok = False
      end
      if episode_identity > 0 and packet_episode != episode_identity:
        packet_ok = False
      end
      if not new_packet and not held_packet:
        packet_ok = False
      end
      if held_packet:
        held_age_s = held_age_s + control_dt_s
        if held_age_s > heartbeat_timeout_s:
          packet_ok = False
        end
        local compare_axis = 0
        while compare_axis < 6:
          if eq[compare_axis] != last_eq[compare_axis]:
            packet_ok = False
          end
          if desired_k[compare_axis] != last_k[compare_axis]:
            packet_ok = False
          end
          if guard_wrench[compare_axis] != last_guard_wrench[compare_axis]:
            packet_ok = False
          end
          if raw_force[compare_axis] != last_raw_force[compare_axis]:
            packet_ok = False
          end
          compare_axis = compare_axis + 1
        end
        if model_sequence != last_model_sequence:
          packet_ok = False
        end
        if model_period_us != last_model_period_us:
          packet_ok = False
        end
        if model_mode != last_model_mode:
          packet_ok = False
        end
        if model_timestamp_us != last_model_timestamp_us:
          packet_ok = False
        end
      else:
        held_age_s = 0.0
      end
      if model_mode == 0:
        if model_sequence != 0 or model_period_us != 0 or model_timestamp_us != 0:
          packet_ok = False
        end
        local zero_axis = 0
        while zero_axis < 6:
          if raw_force[zero_axis] != 0.0:
            packet_ok = False
          end
          zero_axis = zero_axis + 1
        end
      elif model_mode == 1:
        if model_sequence <= 0:
          packet_ok = False
        end
        if model_period_us != 10000 and model_period_us != 20000:
          packet_ok = False
        end
        if model_timestamp_us < last_model_timestamp_us:
          packet_ok = False
        end
      else:
        packet_ok = False
      end
      local force_norm = sqrt(raw_force[0]*raw_force[0] + raw_force[1]*raw_force[1] + raw_force[2]*raw_force[2])
      local torque_norm = sqrt(raw_force[3]*raw_force[3] + raw_force[4]*raw_force[4] + raw_force[5]*raw_force[5])
      local guard_force_norm = sqrt(guard_wrench[0]*guard_wrench[0] + guard_wrench[1]*guard_wrench[1] + guard_wrench[2]*guard_wrench[2])
      local guard_torque_norm = sqrt(guard_wrench[3]*guard_wrench[3] + guard_wrench[4]*guard_wrench[4] + guard_wrench[5]*guard_wrench[5])
      local axis = 0
      while axis < 6:
        if guard_wrench[axis] != guard_wrench[axis]:
          packet_ok = False
        end
        if raw_force[axis] != raw_force[axis]:
          packet_ok = False
        end
        if raw_force[axis] > receiver_force_limit[axis] or raw_force[axis] < -receiver_force_limit[axis]:
          packet_ok = False
        end
        if desired_k[axis] != desired_k[axis]:
          packet_ok = False
        end
        if desired_k[axis] > k_max[axis] or desired_k[axis] < k_min[axis]:
          packet_ok = False
        end
        axis = axis + 1
      end
      if force_norm > 20.0 or torque_norm > 2.0:
        packet_ok = False
      end
      if guard_force_norm > 6.0 or guard_torque_norm > 0.5:
        packet_ok = False
      end
      local actual_pose = get_actual_tcp_pose()
      if not tube_rebased:
        tube_center_base = [actual_pose[0], actual_pose[1], actual_pose[2]]
        tube_anchor_pose_base = p[actual_pose[0], actual_pose[1], actual_pose[2], actual_pose[3], actual_pose[4], actual_pose[5]]
        tube_rebased = True
      end
      local desired_dx = eq[0] - tube_center_base[0]
      local desired_dy = eq[1] - tube_center_base[1]
      local desired_dz = eq[2] - tube_center_base[2]
      local desired_u = desired_dx*tube_u_axis_base[0] + desired_dy*tube_u_axis_base[1] + desired_dz*tube_u_axis_base[2]
      local desired_v = desired_dx*tube_v_axis_base[0] + desired_dy*tube_v_axis_base[1] + desired_dz*tube_v_axis_base[2]
      local desired_orientation_error = pose_sub(tube_anchor_pose_base, p[eq[0], eq[1], eq[2], eq[3], eq[4], eq[5]])
      local desired_orientation_norm = sqrt(desired_orientation_error[3]*desired_orientation_error[3] + desired_orientation_error[4]*desired_orientation_error[4] + desired_orientation_error[5]*desired_orientation_error[5])
      if desired_u > tube_u_half_width_m or desired_u < -tube_u_half_width_m:
        packet_ok = False
      end
      if desired_v > tube_v_half_width_m or desired_v < -tube_v_half_width_m:
        packet_ok = False
      end
      if desired_dz > tube_normal_half_width_m or desired_dz < -tube_normal_half_width_m:
        packet_ok = False
      end
      if desired_orientation_norm > tube_orientation_tolerance_rad:
        packet_ok = False
      end
      if not packet_ok:
        exit_fault = 5
        exit_reason = 5
        running = False
      else:
        if new_packet:
          if lease_id == 0:
            lease_id = packet_lease
            episode_identity = packet_episode
          end
          last_sequence = sequence_after
          last_model_sequence = model_sequence
          last_model_period_us = model_period_us
          last_model_mode = model_mode
          last_model_timestamp_us = model_timestamp_us
          last_eq = eq
          last_k = desired_k
          last_guard_wrench = guard_wrench
          last_raw_force = raw_force
        end
        local actual_speed = get_actual_tcp_speed()
        local q = get_actual_joint_positions()
        local qd = get_actual_joint_speeds()
        local qdd = get_actual_joint_accelerations()
        local actual_translation_speed = sqrt(actual_speed[0]*actual_speed[0] + actual_speed[1]*actual_speed[1] + actual_speed[2]*actual_speed[2])
        local actual_rotation_speed = sqrt(actual_speed[3]*actual_speed[3] + actual_speed[4]*actual_speed[4] + actual_speed[5]*actual_speed[5])
        local actual_dx = actual_pose[0] - tube_center_base[0]
        local actual_dy = actual_pose[1] - tube_center_base[1]
        local actual_dz = actual_pose[2] - tube_center_base[2]
        local actual_u = actual_dx*tube_u_axis_base[0] + actual_dy*tube_u_axis_base[1] + actual_dz*tube_u_axis_base[2]
        local actual_v = actual_dx*tube_v_axis_base[0] + actual_dy*tube_v_axis_base[1] + actual_dz*tube_v_axis_base[2]
        local actual_orientation_error = pose_sub(tube_anchor_pose_base, actual_pose)
        local actual_orientation_norm = sqrt(actual_orientation_error[3]*actual_orientation_error[3] + actual_orientation_error[4]*actual_orientation_error[4] + actual_orientation_error[5]*actual_orientation_error[5])
        local control_ok = True
        local active_speed_violation = False
        local active_acceleration_violation = False
        local entry_excursion_violation = False
        if actual_u > tube_u_half_width_m or actual_u < -tube_u_half_width_m:
          control_ok = False
        end
        if actual_v > tube_v_half_width_m or actual_v < -tube_v_half_width_m:
          control_ok = False
        end
        if actual_dz > tube_normal_half_width_m or actual_dz < -tube_normal_half_width_m:
          control_ok = False
        end
        if actual_orientation_norm > tube_orientation_tolerance_rad:
          control_ok = False
        end
        if torque_entered:
          if entry_elapsed_s < entry_blend_duration_s:
            local entry_transition_error = pose_sub(p[entry_pose[0], entry_pose[1], entry_pose[2], entry_pose[3], entry_pose[4], entry_pose[5]], actual_pose)
            local entry_transition_translation = sqrt(entry_transition_error[0]*entry_transition_error[0] + entry_transition_error[1]*entry_transition_error[1] + entry_transition_error[2]*entry_transition_error[2])
            if entry_transition_translation > entry_transition_tcp_translation_limit_m:
              entry_excursion_violation = True
            end
          end
          if actual_translation_speed > active_tcp_translation_speed_limit_m_s or actual_rotation_speed > active_tcp_rotation_speed_limit_rad_s:
            active_speed_violation = True
          end
          axis = 0
          while axis < 6:
            if entry_elapsed_s < entry_blend_duration_s:
              local entry_joint_delta = q[axis] - entry_joint_positions[axis]
              if entry_joint_delta > entry_transition_joint_excursion_limit_rad or entry_joint_delta < -entry_transition_joint_excursion_limit_rad:
                entry_excursion_violation = True
              end
            end
            if qd[axis] > active_joint_speed_limit_rad_s or qd[axis] < -active_joint_speed_limit_rad_s:
              active_speed_violation = True
            end
            if qdd[axis] > active_joint_acceleration_limit_rad_s2 or qdd[axis] < -active_joint_acceleration_limit_rad_s2:
              active_acceleration_violation = True
            end
            axis = axis + 1
          end
          if active_speed_violation or active_acceleration_violation:
            control_ok = False
          end
        end
        local entry_ready = torque_entered
        if not torque_entered:
          entry_ready = True
          local entry_velocity_alpha = control_dt_s/(entry_velocity_filter_tau_s + control_dt_s)
          axis = 0
          while axis < 6:
            filtered_entry_tcp_speed[axis] = filtered_entry_tcp_speed[axis] + entry_velocity_alpha*(actual_speed[axis] - filtered_entry_tcp_speed[axis])
            filtered_entry_joint_speed[axis] = filtered_entry_joint_speed[axis] + entry_velocity_alpha*(qd[axis] - filtered_entry_joint_speed[axis])
            axis = axis + 1
          end
          entry_velocity_filter_elapsed_s = entry_velocity_filter_elapsed_s + control_dt_s
          local release_error = pose_sub(p[last_eq[0], last_eq[1], last_eq[2], last_eq[3], last_eq[4], last_eq[5]], actual_pose)
          local release_translation = sqrt(release_error[0]*release_error[0] + release_error[1]*release_error[1] + release_error[2]*release_error[2])
          if release_translation > release_ready_tolerance_m:
            control_ok = False
          end
          actual_translation_speed = sqrt(filtered_entry_tcp_speed[0]*filtered_entry_tcp_speed[0] + filtered_entry_tcp_speed[1]*filtered_entry_tcp_speed[1] + filtered_entry_tcp_speed[2]*filtered_entry_tcp_speed[2])
          actual_rotation_speed = sqrt(filtered_entry_tcp_speed[3]*filtered_entry_tcp_speed[3] + filtered_entry_tcp_speed[4]*filtered_entry_tcp_speed[4] + filtered_entry_tcp_speed[5]*filtered_entry_tcp_speed[5])
          if actual_translation_speed > entry_tcp_translation_speed_limit_m_s or actual_rotation_speed > entry_tcp_rotation_speed_limit_rad_s:
            entry_ready = False
          end
          axis = 0
          while axis < 6:
            if filtered_entry_joint_speed[axis] > entry_joint_speed_limit_rad_s or filtered_entry_joint_speed[axis] < -entry_joint_speed_limit_rad_s:
              entry_ready = False
            end
            entry_pose[axis] = actual_pose[axis]
            entry_joint_positions[axis] = q[axis]
            axis = axis + 1
          end
          if entry_velocity_filter_elapsed_s < entry_velocity_filter_warmup_s:
            entry_ready = False
          end
          if entry_ready:
            entry_stable_elapsed_s = entry_stable_elapsed_s + control_dt_s
          else:
            entry_stable_elapsed_s = 0.0
          end
          if entry_stable_elapsed_s < entry_stable_duration_s:
            entry_ready = False
          end
        end
        if torque_thread_watchdog_fault:
          exit_fault = 13
          exit_reason = 13
          running = False
        elif entry_excursion_violation:
          exit_fault = 14
          exit_reason = 14
          running = False
        elif not control_ok:
          if active_acceleration_violation:
            exit_fault = 12
            exit_reason = 12
          elif active_speed_violation:
            exit_fault = 11
            exit_reason = 11
          else:
            exit_fault = 6
            exit_reason = 6
          end
          running = False
        elif not entry_ready:
          write_output_integer_register(24, 0)
          write_output_integer_register(25, last_sequence)
          write_output_integer_register(26, 0)
          write_output_integer_register(27, lease_id)
          write_output_integer_register(28, last_model_sequence)
          write_output_integer_register(29, 0)
          write_output_integer_register(30, frame_token)
          write_output_integer_register(31, episode_identity)
          write_output_integer_register(32, receiver_protocol_token)
          write_output_integer_register(33, 0)
          write_output_integer_register(34, last_observed_command)
          write_output_integer_register(35, episode_latched)
          sync()
        else:
          local blend = 1.0
          if entry_elapsed_s < entry_blend_duration_s:
            blend = entry_elapsed_s / entry_blend_duration_s
          end
          local control_eq = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
          local control_k = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
{orientation_prelude}\
          axis = 0
          while axis < 6:
{orientation_assignment}
            control_k[axis] = last_k[axis]
            local filter_c = filter_velocity[axis] + critical_natural_frequency_rad_s*(filtered_force[axis] - last_raw_force[axis])
            local critical_decay = pow(2.718281828459045, -critical_natural_frequency_rad_s*control_dt_s)
            local next_force = last_raw_force[axis] + critical_decay*((filtered_force[axis] - last_raw_force[axis]) + filter_c*control_dt_s)
            local next_velocity = critical_decay*(filter_velocity[axis] - critical_natural_frequency_rad_s*filter_c*control_dt_s)
            filtered_force[axis] = next_force
            filter_velocity[axis] = next_velocity
            axis = axis + 1
          end
          local coriolis = get_coriolis_and_centrifugal_torques(q, qd)
          local jacobian = get_jacobian(q)
          local tcp_rotation_base = p[0.0, 0.0, 0.0, actual_pose[3], actual_pose[4], actual_pose[5]]
          local feedforward_base = wrench_trans(tcp_rotation_base, filtered_force)
          local pose_error = pose_sub(p[control_eq[0], control_eq[1], control_eq[2], control_eq[3], control_eq[4], control_eq[5]], actual_pose)
          local damping = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
          local control_wrench = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
          local tau = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
          local tau_limit = [20.0, 20.0, 20.0, 8.0, 8.0, 8.0]
          local joint_damping = [1.5, 1.5, 1.2, 0.3, 0.3, 0.2]
          axis = 0
          while axis < 6:
            damping[axis] = 2.0*damping_ratio*sqrt(virtual_mass[axis]*control_k[axis])
            control_wrench[axis] = feedforward_base[axis] + control_k[axis]*pose_error[axis] - damping[axis]*actual_speed[axis]
            axis = axis + 1
          end
          local joint = 0
          local max_abs_tau = 0.0
          while joint < 6:
            tau[joint] = coriolis[joint] - joint_damping[joint]*qd[joint]
            axis = 0
            while axis < 6:
              tau[joint] = tau[joint] + jacobian[axis, joint]*control_wrench[axis]
              axis = axis + 1
            end
            if tau[joint] != tau[joint]:
              control_ok = False
            end
            if tau[joint] > tau_limit[joint] or tau[joint] < -tau_limit[joint]:
              control_ok = False
            end
            if tau[joint] > max_abs_tau:
              max_abs_tau = tau[joint]
            end
            if -tau[joint] > max_abs_tau:
              max_abs_tau = -tau[joint]
            end
            joint = joint + 1
          end
          if not control_ok:
            exit_fault = 7
            exit_reason = 7
            running = False
          else:
            torque_command = tau
            if not torque_entered:
              torque_thread_run = True
              torque_thread_handle = run torqueThread()
              torque_entered = True
            end
            action_publish_generation = action_publish_generation + 1
            write_output_integer_register(29, action_publish_generation)
            write_output_integer_register(24, 2)
            if entry_elapsed_s < entry_blend_duration_s:
              write_output_integer_register(24, 1)
            end
            write_output_integer_register(25, last_sequence)
            write_output_integer_register(26, 0)
            write_output_integer_register(27, lease_id)
            write_output_integer_register(28, last_model_sequence)
            write_output_integer_register(30, frame_token)
            write_output_integer_register(31, episode_identity)
            write_output_integer_register(32, receiver_protocol_token)
            write_output_integer_register(34, last_observed_command)
            write_output_integer_register(35, episode_latched)
            write_output_float_register(24, max_abs_tau)
            write_output_float_register(25, get_steptime())
            control_update_count = control_update_count + 1
            write_output_float_register(44, control_dt_s)
            write_output_float_register(45, control_update_count)
            write_output_float_register(46, maximum_control_update_gap_s)
            write_output_float_register(47, torque_thread_tick_count)
            axis = 0
            while axis < 6:
              write_output_float_register(26 + axis, filtered_force[axis])
              write_output_float_register(32 + axis, control_k[axis])
              write_output_float_register(38 + axis, tau[axis])
              axis = axis + 1
            end
            write_output_integer_register(33, action_publish_generation)
            if entry_elapsed_s < entry_blend_duration_s:
              entry_elapsed_s = entry_elapsed_s + control_dt_s
            end
            sync()
          end
        end
      end
    end
  end
  write_output_integer_register(24, 3)
  write_output_integer_register(25, last_sequence)
  write_output_integer_register(26, exit_fault)
  write_output_integer_register(27, lease_id)
  write_output_integer_register(28, last_model_sequence)
  write_output_integer_register(29, 0)
  write_output_integer_register(30, wrench_frame_token)
  write_output_integer_register(31, episode_identity)
  write_output_integer_register(32, receiver_protocol_token)
  write_output_integer_register(33, exit_reason)
  write_output_integer_register(34, last_observed_command)
  write_output_integer_register(35, episode_latched)
  if torque_entered:
    torque_thread_run = False
    join torque_thread_handle
  end
  if exit_fault == 0:
    write_output_integer_register(24, 5)
  else:
    write_output_integer_register(24, 4)
  end
end
'''
    return wrapped_source


def parse_live_receiver_source(source: str) -> LiveReceiverContract:
    required = (
        f'receiver_schema = "{LIVE_RECEIVER_SCHEMA}"',
        f"receiver_protocol_token = {LIVE_PROTOCOL_TOKEN}",
        "control_rate_hz = 500",
        "source_builder_physical_io_enabled = False",
        "controller_runtime_physical_io_enabled = True",
        "heartbeat_timeout_ticks = ",
        "episode_latched == 0",
        "startup_packet_ok = command == command_idle",
        "packet_lease != lease_id or packet_episode != episode_identity",
        "sequence_after == last_sequence + 1",
        "sequence_after == last_sequence",
        "held_age_s > heartbeat_timeout_s",
        "write_output_integer_register(27, lease_id)",
        "write_output_integer_register(28, last_model_sequence)",
        "write_output_integer_register(30, frame_token)",
        "write_output_integer_register(31, episode_identity)",
        "write_output_integer_register(32, receiver_protocol_token)",
        "write_output_integer_register(34, last_observed_command)",
        "write_output_integer_register(35, episode_latched)",
        "write_output_float_register(26 + axis, filtered_force[axis])",
        "write_output_float_register(32 + axis, control_k[axis])",
        "write_output_float_register(38 + axis, tau[axis])",
        "thread torqueThread():",
        "torque = torque_command",
        "direct_torque(torque, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)",
        "torque_thread_tick_count = torque_thread_tick_count + 1",
        "torque_thread_handle = run torqueThread()",
        "torque_thread_run = False",
        "join torque_thread_handle",
        "stopj(10.0)",
        f'receiver_schema = "{LIVE_RECEIVER_SCHEMA}"',
        "entry_pose[axis] = actual_pose[axis]",
        "tube_rebased = False",
        "tube_center_base = [actual_pose[0], actual_pose[1], actual_pose[2]]",
        "tube_anchor_pose_base = p[actual_pose[0], actual_pose[1], actual_pose[2], actual_pose[3], actual_pose[4], actual_pose[5]]",
        "entry_stable_duration_s = 0.05",
        "entry_velocity_filter_tau_s = 0.05",
        "entry_velocity_filter_warmup_s = 0.15",
        "entry_velocity_alpha = control_dt_s/(entry_velocity_filter_tau_s + control_dt_s)",
        "entry_velocity_filter_elapsed_s < entry_velocity_filter_warmup_s",
        "filtered_entry_tcp_speed[axis] = filtered_entry_tcp_speed[axis] + entry_velocity_alpha*(actual_speed[axis] - filtered_entry_tcp_speed[axis])",
        "filtered_entry_joint_speed[axis] = filtered_entry_joint_speed[axis] + entry_velocity_alpha*(qd[axis] - filtered_entry_joint_speed[axis])",
        "torque_thread_stale_ticks >= 25",
        "torque_thread_watchdog_fault = True",
        "exit_fault = 13",
        "action_publish_generation = action_publish_generation + 1",
        "write_output_integer_register(29, action_publish_generation)",
        "write_output_integer_register(33, action_publish_generation)",
        "entry_joint_speed_limit_rad_s = 0.001",
        "entry_transition_tcp_translation_limit_m = 0.0003",
        "entry_transition_joint_excursion_limit_rad = 0.0005",
        "entry_joint_positions[axis] = q[axis]",
        "entry_excursion_violation = True",
        "exit_fault = 14",
        "entry_stable_elapsed_s < entry_stable_duration_s",
        "guard_wrench = [read_input_float_register(36)",
        "guard_force_norm > 6.0 or guard_torque_norm > 0.5",
        "entry_elapsed_s < entry_blend_duration_s",
        "control_k[axis] = last_k[axis]",
        "viscous_scale = [",
        "coulomb_scale = [",
        "actual_translation_speed > active_tcp_translation_speed_limit_m_s",
        "actual_rotation_speed > active_tcp_rotation_speed_limit_rad_s",
        "active_speed_violation",
        "qdd = get_actual_joint_accelerations()",
        "control_clock = time()",
        "critical_decay = pow(2.718281828459045, -critical_natural_frequency_rad_s*control_dt_s)",
        "write_output_float_register(44, control_dt_s)",
        "write_output_float_register(45, control_update_count)",
        "write_output_float_register(46, maximum_control_update_gap_s)",
        "write_output_float_register(47, torque_thread_tick_count)",
        "active_joint_acceleration_limit_rad_s2 = 5.0",
        "active_acceleration_violation",
        "get_coriolis_and_centrifugal_torques(q, qd)",
        "get_jacobian(q)",
        "running = False",
    )
    for token in required:
        if token not in source:
            raise ValueError(f"live receiver source missing contract token: {token}")
    forbidden = (
        r"\bsocket\b",
        r"\bdashboard\b",
        r"\b(load|play)\s*\(",
        r"\b(movej|movel|movec|speedj|speedl|servoj|force_mode)\s*\(",
        r"\bget_tcp_force\s*\(",
        r"\bssh\b",
        r"\bhttp\b",
        r"time\.sleep",
        r"\bexp\s*\(",
    )
    if any(re.search(pattern, source, flags=re.IGNORECASE) for pattern in forbidden):
        raise ValueError("live receiver source contains a forbidden primitive")
    if re.search(r"\bget_.*gravity", source, flags=re.IGNORECASE):
        raise ValueError("live receiver source must not include gravity compensation terms")
    outer_program = "def tacdiffusion_remote_direct_torque_v4_program():"
    if not source.startswith(outer_program + "\n"):
        raise ValueError(
            "live receiver Secondary Client wire source requires one outer program"
        )
    if len(re.findall(r"(?m)^\s*def\s+", source)) != 1:
        raise ValueError(
            "live receiver Secondary Client wire source requires one outer program"
        )
    if re.search(r"(?m)^global\s+", source):
        raise ValueError("live receiver must not declare state outside its wire program")
    if re.search(r"\bdirect_torque\s*\(\s*\[\s*0(?:\.0)?", source):
        raise ValueError("live receiver must not use a zero-torque startup or exit")
    if re.search(r"(?m)^\s*return\b", source):
        raise ValueError("live receiver active state machine must use one common exit")
    if re.search(r"\babs\s*\(", source):
        raise ValueError("live receiver must avoid unsupported abs() parser calls")
    torque_call = (
        "direct_torque(torque, viscous_scale=viscous_scale, coulomb_scale=coulomb_scale)"
    )
    if source.count(torque_call) != 1:
        raise ValueError("live receiver requires one continuous torque command site")
    torque_thread_start = source.index("thread torqueThread():")
    program_start = source.index(f'receiver_schema = "{LIVE_RECEIVER_SCHEMA}"')
    torque_thread_source = source[torque_thread_start:program_start]
    program_source = source[program_start:]
    if "sync()" in torque_thread_source:
        raise ValueError(
            "live receiver torque thread must not leave an empty sync timestep"
        )
    if torque_thread_source.count(torque_call) != 1:
        raise ValueError("live receiver torque command must be owned by torqueThread")
    if source.count("torque_thread_handle = run torqueThread()") != 1:
        raise ValueError("live receiver requires exactly one torque thread launch site")
    if source.count("join torque_thread_handle") != 1:
        raise ValueError("live receiver requires exactly one torque thread join site")
    if source.count("stopj(10.0)") != 1:
        raise ValueError("live receiver requires exactly one explicit position handoff")
    timeout_match = re.search(
        r"^\s*(?:(?:local|global)\s+)?heartbeat_timeout_ticks = (\d+)$",
        source,
        re.MULTILINE,
    )
    if timeout_match is None:
        raise ValueError("live receiver heartbeat timeout is not parseable")
    orientation_policy_match = re.search(
        r'^\s*(?:local\s+)?orientation_interpolation_policy = "([^"]+)"$',
        source,
        re.MULTILINE,
    )
    orientation_interpolation_policy = (
        ORIENTATION_POLICY_HOLD_ENTRY
        if orientation_policy_match is None
        else orientation_policy_match.group(1)
    )
    if orientation_interpolation_policy == ORIENTATION_POLICY_HOLD_ENTRY:
        orientation_tokens = (
            "if axis < 3:",
            "control_eq[axis] = entry_pose[axis]",
            "blend*(last_eq[axis] - entry_pose[axis])",
        )
        if any(token not in source for token in orientation_tokens):
            raise ValueError("hold-entry orientation policy source is incomplete")
        if "interpolate_pose(" in source:
            raise ValueError("hold-entry orientation policy must not interpolate pose")
    elif orientation_interpolation_policy == ORIENTATION_POLICY_INTERPOLATE_POSE:
        orientation_tokens = (
            "local interpolated_control_pose = interpolate_pose(",
            "control_eq[axis] = interpolated_control_pose[axis]",
        )
        if any(token not in source for token in orientation_tokens):
            raise ValueError("geodesic orientation policy source is incomplete")
        if "if axis < 3:" in source:
            raise ValueError(
                "geodesic orientation policy must not interpolate rotvec components"
            )
    else:
        raise ValueError("live receiver orientation interpolation policy is unsupported")
    friction_profile_match = re.search(
        r'^\s*friction_profile = "([^"]+)"$',
        source,
        re.MULTILINE,
    )
    viscous_scale_match = re.search(
        r"^\s*viscous_scale = \[([^\]]+)\]$",
        source,
        re.MULTILINE,
    )
    coulomb_scale_match = re.search(
        r"^\s*coulomb_scale = \[([^\]]+)\]$",
        source,
        re.MULTILINE,
    )
    if viscous_scale_match is None or coulomb_scale_match is None:
        raise ValueError("live receiver friction scales are not parseable")
    viscous_scale = _finite(
        [float(value.strip()) for value in viscous_scale_match.group(1).split(",")],
        6,
        "viscous_scale",
    )
    coulomb_scale = _finite(
        [float(value.strip()) for value in coulomb_scale_match.group(1).split(",")],
        6,
        "coulomb_scale",
    )
    if friction_profile_match is None:
        if (
            viscous_scale
            != FRICTION_PROFILES[FRICTION_PROFILE_ZERO_ISOLATION][0]
            or coulomb_scale
            != FRICTION_PROFILES[FRICTION_PROFILE_ZERO_ISOLATION][1]
        ):
            raise ValueError("live receiver nonzero friction profile is undeclared")
        friction_profile = FRICTION_PROFILE_ZERO_ISOLATION
    else:
        friction_profile = friction_profile_match.group(1)
    if friction_profile not in FRICTION_PROFILES:
        raise ValueError("live receiver friction profile is unsupported")
    expected_viscous, expected_coulomb = FRICTION_PROFILES[friction_profile]
    if viscous_scale != expected_viscous or coulomb_scale != expected_coulomb:
        raise ValueError("live receiver friction scales do not match declared profile")
    if re.search(
        r"(?m)^\s*tacdiffusion_remote_direct_torque_v4_program\(\)\s*$",
        source,
    ):
        raise ValueError("live receiver outer program must not be explicitly invoked")
    return LiveReceiverContract(
        schema=LIVE_RECEIVER_SCHEMA,
        control_rate_hz=CONTROL_RATE_HZ,
        protocol_token=LIVE_PROTOCOL_TOKEN,
        heartbeat_timeout_ticks=int(timeout_match.group(1)),
        invocation_present=True,
        explicit_position_handoff=True,
        complete_identity_echo=True,
        applied_action_echo=True,
        hard_tube_guard=True,
        dedicated_torque_thread=True,
        orientation_interpolation_policy=orientation_interpolation_policy,
        friction_profile=friction_profile,
        viscous_scale=viscous_scale,
        coulomb_scale=coulomb_scale,
        source_builder_physical_io_enabled=False,
        controller_runtime_physical_io_enabled=True,
    )
