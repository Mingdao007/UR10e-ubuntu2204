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
    source_builder_physical_io_enabled: bool
    controller_runtime_physical_io_enabled: bool


def build_live_receiver_source(
    tube: LiveTubeContract,
    *,
    heartbeat_timeout_ticks: int = DEFAULT_HEARTBEAT_TIMEOUT_TICKS,
) -> str:
    """Build one controller-resident 500 Hz program; sending is a separate gate."""

    if not isinstance(tube, LiveTubeContract):
        raise TypeError("tube must be a LiveTubeContract")
    if not 5 <= int(heartbeat_timeout_ticks) <= 100:
        raise ValueError("heartbeat timeout must be between 5 and 100 robot ticks")
    center = _urscript_vector(tube.center_base_m)
    anchor = _urscript_vector(tube.anchor_pose_base)
    u_axis = _urscript_vector(tube.u_axis_base)
    v_axis = _urscript_vector(tube.v_axis_base)
    return f'''def tacdiffusion_remote_direct_torque_v4_program():
  # generated Remote-Control Direct Torque receiver; sending is separately authorized
  local receiver_schema = "{LIVE_RECEIVER_SCHEMA}"
  local receiver_protocol_token = {LIVE_PROTOCOL_TOKEN}
  local control_rate_hz = {CONTROL_RATE_HZ}
  local heartbeat_timeout_ticks = {int(heartbeat_timeout_ticks)}
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
  local virtual_mass = [2.0, 2.0, 2.0, 0.2, 0.2, 0.2]
  local damping_ratio = 1.0
  local critical_natural_frequency_rad_s = 92.10340371976183
  local critical_decay = 0.8317438636116526
  local entry_blend_ticks = 50
  local command_idle = 0
  local command_run = 1
  local command_end = 2
  local command_abort = 3
  local running = True
  local torque_entered = False
  local entry_tick = 0
  local exit_fault = 0
  local exit_reason = 0
  local last_sequence = 0
  local held_age_ticks = 0
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
  local last_raw_force = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local filtered_force = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local filter_velocity = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local entry_pose = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
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
        held_age_ticks = held_age_ticks + 1
        if held_age_ticks > heartbeat_timeout_ticks:
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
        held_age_ticks = 0
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
      local axis = 0
      while axis < 6:
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
          last_raw_force = raw_force
        end
        local actual_pose = get_actual_tcp_pose()
        local actual_speed = get_actual_tcp_speed()
        local actual_force = get_tcp_force()
        local q = get_actual_joint_positions()
        local qd = get_actual_joint_speeds()
        local actual_dx = actual_pose[0] - tube_center_base[0]
        local actual_dy = actual_pose[1] - tube_center_base[1]
        local actual_dz = actual_pose[2] - tube_center_base[2]
        local actual_u = actual_dx*tube_u_axis_base[0] + actual_dy*tube_u_axis_base[1] + actual_dz*tube_u_axis_base[2]
        local actual_v = actual_dx*tube_v_axis_base[0] + actual_dy*tube_v_axis_base[1] + actual_dz*tube_v_axis_base[2]
        local actual_orientation_error = pose_sub(tube_anchor_pose_base, actual_pose)
        local actual_orientation_norm = sqrt(actual_orientation_error[3]*actual_orientation_error[3] + actual_orientation_error[4]*actual_orientation_error[4] + actual_orientation_error[5]*actual_orientation_error[5])
        local actual_force_norm = sqrt(actual_force[0]*actual_force[0] + actual_force[1]*actual_force[1] + actual_force[2]*actual_force[2])
        local actual_torque_norm = sqrt(actual_force[3]*actual_force[3] + actual_force[4]*actual_force[4] + actual_force[5]*actual_force[5])
        local control_ok = True
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
        if actual_force_norm > 10.0 or actual_torque_norm > 1.0:
          control_ok = False
        end
        if not torque_entered:
          local release_error = pose_sub(p[last_eq[0], last_eq[1], last_eq[2], last_eq[3], last_eq[4], last_eq[5]], actual_pose)
          local release_translation = sqrt(release_error[0]*release_error[0] + release_error[1]*release_error[1] + release_error[2]*release_error[2])
          local release_speed = sqrt(actual_speed[0]*actual_speed[0] + actual_speed[1]*actual_speed[1] + actual_speed[2]*actual_speed[2])
          if release_translation > release_ready_tolerance_m or release_speed > 0.005:
            control_ok = False
          end
          axis = 0
          while axis < 6:
            if qd[axis] > 0.02 or qd[axis] < -0.02:
              control_ok = False
            end
            entry_pose[axis] = actual_pose[axis]
            axis = axis + 1
          end
        end
        if not control_ok:
          exit_fault = 6
          exit_reason = 6
          running = False
        else:
          local blend = 1.0
          if entry_tick < entry_blend_ticks:
            blend = entry_tick / entry_blend_ticks
          end
          local control_eq = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
          local control_k = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
          axis = 0
          while axis < 6:
            control_eq[axis] = entry_pose[axis] + blend*(last_eq[axis] - entry_pose[axis])
            control_k[axis] = k_min[axis] + blend*(last_k[axis] - k_min[axis])
            local filter_c = filter_velocity[axis] + critical_natural_frequency_rad_s*(filtered_force[axis] - last_raw_force[axis])
            local next_force = last_raw_force[axis] + critical_decay*((filtered_force[axis] - last_raw_force[axis]) + filter_c/500.0)
            local next_velocity = critical_decay*(filter_velocity[axis] - critical_natural_frequency_rad_s*filter_c/500.0)
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
            direct_torque(tau, friction_comp=True)
            torque_entered = True
            write_output_integer_register(24, 2)
            if entry_tick < entry_blend_ticks:
              write_output_integer_register(24, 1)
            end
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
            write_output_float_register(24, max_abs_tau)
            write_output_float_register(25, get_steptime())
            axis = 0
            while axis < 6:
              write_output_float_register(26 + axis, filtered_force[axis])
              write_output_float_register(32 + axis, control_k[axis])
              write_output_float_register(38 + axis, tau[axis])
              axis = axis + 1
            end
            sync()
            if entry_tick < entry_blend_ticks:
              entry_tick = entry_tick + 1
            end
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
    stopj(10.0)
  end
  if exit_fault == 0:
    write_output_integer_register(24, 5)
  else:
    write_output_integer_register(24, 4)
  end
end
tacdiffusion_remote_direct_torque_v4_program()
'''


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
        "held_age_ticks > heartbeat_timeout_ticks",
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
        "direct_torque(tau, friction_comp=True)",
        "stopj(10.0)",
        "def tacdiffusion_remote_direct_torque_v4_program():",
        "tacdiffusion_remote_direct_torque_v4_program()",
        "entry_pose[axis] = actual_pose[axis]",
        "entry_tick < entry_blend_ticks",
        "control_k[axis] = k_min[axis] + blend*(last_k[axis] - k_min[axis])",
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
        r"\bssh\b",
        r"\bhttp\b",
        r"time\.sleep",
    )
    if any(re.search(pattern, source, flags=re.IGNORECASE) for pattern in forbidden):
        raise ValueError("live receiver source contains a forbidden primitive")
    if re.search(r"\bget_.*gravity", source, flags=re.IGNORECASE):
        raise ValueError("live receiver source must not include gravity compensation terms")
    if len(re.findall(r"(?m)^\s*def\s+", source)) != 1:
        raise ValueError("live receiver requires one top-level program and no nested helpers")
    if re.search(r"\bdirect_torque\s*\(\s*\[\s*0(?:\.0)?", source):
        raise ValueError("live receiver must not use a zero-torque startup or exit")
    if re.search(r"(?m)^\s*return\b", source):
        raise ValueError("live receiver active state machine must use one common exit")
    if re.search(r"\babs\s*\(", source):
        raise ValueError("live receiver must avoid unsupported abs() parser calls")
    if source.count("direct_torque(tau, friction_comp=True)") != 1:
        raise ValueError("live receiver requires one continuous torque command site")
    if source.count("stopj(10.0)") != 1:
        raise ValueError("live receiver requires exactly one explicit position handoff")
    timeout_match = re.search(
        r"^\s*(?:local\s+)?heartbeat_timeout_ticks = (\d+)$",
        source,
        re.MULTILINE,
    )
    if timeout_match is None:
        raise ValueError("live receiver heartbeat timeout is not parseable")
    program_invocation_matches = re.findall(
        r"(?m)^tacdiffusion_remote_direct_torque_v4_program\(\)\s*$",
        source,
    )
    if len(program_invocation_matches) != 1:
        raise ValueError("live receiver program invocation is missing or not unique")
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
        source_builder_physical_io_enabled=False,
        controller_runtime_physical_io_enabled=True,
    )
