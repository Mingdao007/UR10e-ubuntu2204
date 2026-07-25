"""Offline builder/parser for the deployable controller-resident receiver.

The builder emits URScript containing the guarded 500 Hz Direct Torque loop;
this Python module never sends, uploads, or executes that source.  Parser
tests enforce that the source has no network, Dashboard, program-control, or
high-level motion primitive while permitting the required ``direct_torque``
primitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
import re
from typing import Mapping

from .action import ActionProfile
from .dynamic_filter import DynamicFilterProfile, RateInvariantForceFilter
from .promotion import validate_live_authorization


class ReceiverCommand(str, Enum):
    APPEND = "APPEND"
    END = "END"
    DRAIN = "DRAIN"


@dataclass(frozen=True)
class ReceiverContract:
    schema: str
    control_rate_hz: int
    empty_queue_behavior: str
    physical_io_enabled: bool
    commands: tuple[str, ...]
    settling_time_s: float
    damping_ratio: float
    direct_torque_required: bool


def build_receiver_source(
    *,
    filter_profile: DynamicFilterProfile = DynamicFilterProfile(),
    action_profile: ActionProfile = ActionProfile(),
    active_authorization_path: str | Path | None = None,
) -> str:
    """Build source only; deployment remains a separately gated owner action."""

    if filter_profile.rate_hz != 500 or action_profile.damping_ratio <= 0.0:
        raise ValueError("receiver requires 500 Hz and positive damping ratio")
    if isinstance(active_authorization_path, bool):
        raise TypeError("active authorization requires a validated path, not a boolean")
    active_allowed = False if active_authorization_path is None else validate_live_authorization(active_authorization_path).active_allowed
    omega = filter_profile.natural_frequency_rad_s
    k_min = ", ".join(f"{value:.9g}" for value in action_profile.stiffness_min)
    k_max = ", ".join(f"{value:.9g}" for value in action_profile.stiffness_max)
    k_slew = ", ".join(f"{value:.9g}" for value in action_profile.stiffness_slew_per_s)
    masses = ", ".join(f"{value:.9g}" for value in action_profile.virtual_mass)
    return f'''# generated controller source; deployment is separately authorized
receiver_schema = "ur10e_direct_torque_receiver/v3"
control_rate_hz = 500
settling_time_s = {filter_profile.settling_time_s:.12g}
damping_ratio = {filter_profile.damping_ratio:.12g}
critical_natural_frequency_rad_s = {omega:.12g}
empty_queue_behavior = "WAIT_FOREVER"
physical_io_enabled = False  # Python builder/parser never performs controller I/O
controller_direct_torque_runtime = True
commands = ["APPEND", "END", "DRAIN"]
k_min = [{k_min}]
k_max = [{k_max}]
k_slew_per_s = [{k_slew}]
virtual_mass = [{masses}]

def receiver_zero_torque(fault_code):
  local zero_tau = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  direct_torque(zero_tau)
  write_output_integer_register(26, fault_code)
  return True
end

def receiver_finite_bounded(value, bound):
  if value != value or abs(value) > bound:
    return False
  end
  return True
end

def receiver_critical_step(position, velocity, target, dt):
  local e = exp(-critical_natural_frequency_rad_s * dt)
  local c = velocity + critical_natural_frequency_rad_s * (position - target)
  local next_position = target + e * ((position - target) + c * dt)
  local next_velocity = e * (velocity - critical_natural_frequency_rad_s * c * dt)
  return [next_position, next_velocity]
end

def receiver_derive_damping(k):
  local damping = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local axis = 0
  while axis < 6:
    damping[axis] = 2.0 * damping_ratio * sqrt(virtual_mass[axis] * k[axis])
    axis = axis + 1
  end
  return damping
end

def receiver_apply_guarded(raw_force, k, filtered_force):
  local q = get_actual_joint_positions()
  local qd = get_actual_joint_speeds()
  local pose = get_actual_tcp_pose()
  local twist = get_actual_tcp_speed()
  local coriolis = get_coriolis_and_centrifugal_torques(q, qd)
  local jacobian = get_jacobian(q)
  # 24..29 is equilibrium pose; 36..41 is a host damping echo and is never
  # interpreted as pose.
  local desired_pose = [read_input_float_register(24), read_input_float_register(25), read_input_float_register(26), read_input_float_register(27), read_input_float_register(28), read_input_float_register(29)]
  local damping = receiver_derive_damping(k)
  local wrench = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  # F_df is TCP-frame; get_jacobian is base-frame.  Rotation-only wrench_trans
  # preserves the TCP point while aligning both force and torque axes.
  local tcp_pose_base = get_actual_tcp_pose()
  local tcp_rotation_base = p[0.0, 0.0, 0.0, tcp_pose_base[3], tcp_pose_base[4], tcp_pose_base[5]]
  local feedforward_base = wrench_trans(tcp_rotation_base, filtered_force)
  local force_limit = [20.0, 20.0, 20.0, 2.0, 2.0, 2.0]
  local force_norm = sqrt(raw_force[0]*raw_force[0] + raw_force[1]*raw_force[1] + raw_force[2]*raw_force[2])
  local torque_norm = sqrt(raw_force[3]*raw_force[3] + raw_force[4]*raw_force[4] + raw_force[5]*raw_force[5])
  if force_norm > 20.0 or torque_norm > 2.0:
    return False
  end
  local pose_error = pose_sub(desired_pose, pose)
  local tau = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local tau_limit = [20.0, 20.0, 20.0, 8.0, 8.0, 8.0]
  local joint_damping = [1.5, 1.5, 1.2, 0.3, 0.3, 0.2]
  local axis = 0
  while axis < 6:
    wrench[axis] = feedforward_base[axis] + k[axis] * pose_error[axis] - damping[axis] * twist[axis]
    if not receiver_finite_bounded(raw_force[axis], force_limit[axis]) or not receiver_finite_bounded(k[axis], k_max[axis]):
      return False
    end
    axis = axis + 1
  end
  local joint = 0
  while joint < 6:
    tau[joint] = coriolis[joint] - joint_damping[joint] * qd[joint]
    axis = 0
    while axis < 6:
      tau[joint] = tau[joint] + jacobian[axis, joint] * wrench[axis]
      axis = axis + 1
    end
    if not receiver_finite_bounded(tau[joint], tau_limit[joint]):
      return False
    end
    joint = joint + 1
  end
  direct_torque(tau)
  return True
end

def receiver_release_ready(eq):
  local actual_pose = get_actual_tcp_pose()
  local target_pose = p[eq[0], eq[1], eq[2], eq[3], eq[4], eq[5]]
  local error = pose_sub(target_pose, actual_pose)
  local wrench = get_tcp_force()
  return sqrt(error[0]*error[0] + error[1]*error[1] + error[2]*error[2]) <= 0.002 and sqrt(wrench[0]*wrench[0] + wrench[1]*wrench[1] + wrench[2]*wrench[2]) <= 2.0 and sqrt(wrench[3]*wrench[3] + wrench[4]*wrench[4] + wrench[5]*wrench[5]) <= 0.2
end

def receiver_episode_fault(fault_code, filter_position, filter_velocity, k):
  # Episode-local fault: exact filter state is driven toward zero over ten
  # bounded 500 Hz safe-exit ticks. The receiver stays alive for the next
  # explicit home ACK/consume identity; it never returns on an episode fault.
  local tick = 0
  while tick < 10:
    local axis = 0
    while axis < 6:
      local state = receiver_critical_step(filter_position[axis], filter_velocity[axis], 0.0, 1.0 / 500.0)
      filter_position[axis] = state[0]
      filter_velocity[axis] = state[1]
      axis = axis + 1
    end
    # Apply the decaying filtered F_ff through the guarded torque path.  The
    # path retains bounded damping and frame/norm/tau checks during safe exit.
    if not receiver_apply_guarded(filter_position, k, filter_position):
      local q = get_actual_joint_positions()
      local qd = get_actual_joint_speeds()
      local coriolis = get_coriolis_and_centrifugal_torques(q, qd)
      local joint_damping = [1.5, 1.5, 1.2, 0.3, 0.3, 0.2]
      local tau = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
      axis = 0
      while axis < 6:
        tau[axis] = coriolis[axis] - joint_damping[axis] * qd[axis]
        axis = axis + 1
      end
      direct_torque(tau)
    end
    sync()
    tick = tick + 1
  end
  receiver_zero_torque(fault_code)
  write_output_integer_register(24, 4)
  write_output_integer_register(26, fault_code)
  write_output_integer_register(29, fault_code)
  return True
end

def tacdiffusion_receiver():
  local command_append = 1
  local command_end = 2
  local command_drain = 3
  local model_disabled = 0
  local model_shadow = 1
  local model_active = 2
  local model_active_allowed = {str(active_allowed)}
  local mode = 0
  local lease_id = 0
  local waiting_home_ack = 0
  local fault_home_identity = 0
  local fault_identity_high_water = 0
  local current_episode_identity = 0
  local last_sequence = 0
  local last_model_sequence = 0
  local last_model_timestamp_us = 0
  local last_model_period_us = 0
  local last_model_mode = 0
  local last_raw_force = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local last_desired_k = [600.0, 600.0, 600.0, 30.0, 30.0, 30.0]
  local model_age_ticks = 0
  local startup_ticks = 0
  local filter_position = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local filter_velocity = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local k = [600.0, 600.0, 600.0, 30.0, 30.0, 30.0]
  local receiver_force_limit = [20.0, 20.0, 20.0, 2.0, 2.0, 2.0]
  while True:
    local command = read_input_integer_register(24)
    local requested_command = command
    if waiting_home_ack == 1:
      local home_ack_identity = read_input_integer_register(33)
      local home_consume_identity = read_input_integer_register(34)
      if requested_command != command_end and requested_command != command_drain and (home_ack_identity != fault_home_identity or home_consume_identity != fault_home_identity):
        command = 0
      else:
        waiting_home_ack = 0
        filter_position = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        filter_velocity = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        k = [600.0, 600.0, 600.0, 30.0, 30.0, 30.0]
        last_model_sequence = 0
        last_model_timestamp_us = 0
        last_model_period_us = 0
        last_model_mode = 0
      end
    end
    if command == command_end:
      mode = command_end
    elif command == command_drain:
      mode = command_drain
    end
    if command != command_append:
      if mode == command_end or mode == command_drain:
        receiver_zero_torque(0)
        return "RETURN_CAMPAIGN_HOME"
      end
      sync()
    else:
      local sequence_before = read_input_integer_register(25)
      local heartbeat = read_input_integer_register(26)
      local packet_lease = read_input_integer_register(27)
      local model_sequence = read_input_integer_register(28)
      local model_period_us = read_input_integer_register(29)
      local model_mode = read_input_integer_register(30)
      local frame_token = read_input_integer_register(31)
      local model_timestamp_us = read_input_integer_register(32)
      local episode_identity = read_input_integer_register(35)
      local sequence_after = read_input_integer_register(25)
      local coherent = sequence_before == sequence_after and heartbeat == sequence_after
      local eq = [read_input_float_register(24), read_input_float_register(25), read_input_float_register(26), read_input_float_register(27), read_input_float_register(28), read_input_float_register(29)]
      local raw_force = [read_input_float_register(42), read_input_float_register(43), read_input_float_register(44), read_input_float_register(45), read_input_float_register(46), read_input_float_register(47)]
      local desired_k = [read_input_float_register(30), read_input_float_register(31), read_input_float_register(32), read_input_float_register(33), read_input_float_register(34), read_input_float_register(35)]
      local packet_ok = coherent and sequence_after > 0 and sequence_after == last_sequence + 1 and packet_lease > 0 and episode_identity > 0 and (lease_id == 0 or packet_lease == lease_id) and (current_episode_identity == 0 or episode_identity == current_episode_identity)
      if packet_ok and lease_id == 0:
        lease_id = packet_lease
      end
      if packet_ok and current_episode_identity == 0:
        current_episode_identity = episode_identity
      end
      if packet_ok and frame_token != 5252001:
        packet_ok = False
      end
      if packet_ok and model_mode == model_active and not model_active_allowed:
        packet_ok = False
      end
      if packet_ok and model_mode == model_disabled:
        packet_ok = model_sequence == 0 and model_period_us == 0 and model_timestamp_us == 0 and raw_force[0] == 0.0 and raw_force[1] == 0.0 and raw_force[2] == 0.0 and raw_force[3] == 0.0 and raw_force[4] == 0.0 and raw_force[5] == 0.0
      elif packet_ok and (model_mode == model_shadow or model_mode == model_active):
        local model_new = model_sequence == last_model_sequence + 1
        local model_held = model_sequence == last_model_sequence
        local period_ok = model_period_us == 10000 or model_period_us == 20000
        packet_ok = model_sequence > 0 and period_ok and (model_new or model_held)
        if model_held and packet_ok:
          model_age_ticks = model_age_ticks + 1
          packet_ok = model_timestamp_us == last_model_timestamp_us and model_period_us == last_model_period_us and model_mode == last_model_mode and model_age_ticks * 2000 <= 2 * model_period_us
          local held_axis = 0
          while held_axis < 6:
            if raw_force[held_axis] != last_raw_force[held_axis] or desired_k[held_axis] != last_desired_k[held_axis]:
              packet_ok = False
            end
            held_axis = held_axis + 1
          end
        elif model_new and packet_ok:
          packet_ok = model_timestamp_us >= last_model_timestamp_us
          model_age_ticks = 0
        end
      else:
        packet_ok = False
      end
      local axis = 0
      while axis < 6:
        if not receiver_finite_bounded(raw_force[axis], receiver_force_limit[axis]) or not receiver_finite_bounded(desired_k[axis], k_max[axis]) or desired_k[axis] < k_min[axis]:
          packet_ok = False
        end
        axis = axis + 1
      end
      local force_norm = sqrt(raw_force[0]*raw_force[0] + raw_force[1]*raw_force[1] + raw_force[2]*raw_force[2])
      local torque_norm = sqrt(raw_force[3]*raw_force[3] + raw_force[4]*raw_force[4] + raw_force[5]*raw_force[5])
      if force_norm > 20.0 or torque_norm > 2.0:
        packet_ok = False
      end
      if not packet_ok:
        receiver_episode_fault(5, filter_position, filter_velocity, k)
        if episode_identity > 0 and episode_identity > fault_identity_high_water:
          fault_home_identity = episode_identity
          fault_identity_high_water = episode_identity
        else:
          fault_identity_high_water = fault_identity_high_water + 1
          fault_home_identity = fault_identity_high_water
        end
        waiting_home_ack = 1
        mode = 0
        lease_id = 0
        last_sequence = 0
        last_model_sequence = 0
        last_model_timestamp_us = 0
        last_model_period_us = 0
        last_model_mode = 0
        current_episode_identity = 0
        last_raw_force = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        last_desired_k = [600.0, 600.0, 600.0, 30.0, 30.0, 30.0]
        model_age_ticks = 0
        startup_ticks = 0
        sync()
      elif startup_ticks < 5:
        if not receiver_release_ready(eq):
          receiver_episode_fault(6, filter_position, filter_velocity, k)
          if episode_identity > 0 and episode_identity > fault_identity_high_water:
            fault_home_identity = episode_identity
            fault_identity_high_water = episode_identity
          else:
            fault_identity_high_water = fault_identity_high_water + 1
            fault_home_identity = fault_identity_high_water
          end
          waiting_home_ack = 1
          mode = 0
          lease_id = 0
          last_sequence = 0
          startup_ticks = 0
        else:
          receiver_zero_torque(0)
          startup_ticks = startup_ticks + 1
          last_sequence = sequence_after
          last_model_sequence = model_sequence
          last_model_timestamp_us = model_timestamp_us
          last_model_period_us = model_period_us
          last_model_mode = model_mode
          last_raw_force = raw_force
          last_desired_k = desired_k
          sync()
        end
      else:
        axis = 0
        while axis < 6:
          local delta = desired_k[axis] - k[axis]
          local slew = k_slew_per_s[axis] / 500.0
          k[axis] = k[axis] + max(-slew, min(slew, delta))
          local state = receiver_critical_step(filter_position[axis], filter_velocity[axis], raw_force[axis], 1.0 / 500.0)
          filter_position[axis] = state[0]
          filter_velocity[axis] = state[1]
          axis = axis + 1
        end
        if not receiver_apply_guarded(raw_force, k, filter_position):
          receiver_episode_fault(7, filter_position, filter_velocity, k)
          if episode_identity > 0 and episode_identity > fault_identity_high_water:
            fault_home_identity = episode_identity
            fault_identity_high_water = episode_identity
          else:
            fault_identity_high_water = fault_identity_high_water + 1
            fault_home_identity = fault_identity_high_water
          end
          waiting_home_ack = 1
          mode = 0
          lease_id = 0
          last_sequence = 0
          startup_ticks = 0
        else:
          last_sequence = sequence_after
          last_model_sequence = model_sequence
          last_model_timestamp_us = model_timestamp_us
          last_model_period_us = model_period_us
          last_model_mode = model_mode
          last_raw_force = raw_force
          last_desired_k = desired_k
          write_output_integer_register(25, sequence_after)
          write_output_integer_register(26, 0)
          sync()
        end
      end
    end
  end
end
'''


RECEIVER_SOURCE = build_receiver_source()


def parse_receiver_source(source: str = RECEIVER_SOURCE) -> ReceiverContract:
    required = (
        'receiver_schema = "ur10e_direct_torque_receiver/v3"',
        "control_rate_hz = 500",
        'empty_queue_behavior = "WAIT_FOREVER"',
        "physical_io_enabled = False",
        "controller_direct_torque_runtime = True",
        'commands = ["APPEND", "END", "DRAIN"]',
        "receiver_critical_step",
        "receiver_derive_damping",
        "receiver_apply_guarded",
        "pose_sub(target_pose, actual_pose)",
        "wrench_trans(tcp_rotation_base, filtered_force)",
        "[20.0, 20.0, 20.0, 8.0, 8.0, 8.0]",
        "read_input_integer_register(33)",
        "read_input_integer_register(34)",
        "read_input_integer_register(35)",
        "pose_sub(desired_pose, pose)",
        "receiver_apply_guarded(filter_position, k, filter_position)",
        "local fault_identity_high_water = 0",
        "local last_model_period_us = 0",
        "local last_raw_force = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]",
        "local last_desired_k = [600.0, 600.0, 600.0, 30.0, 30.0, 30.0]",
        "direct_torque(tau)",
        "direct_torque(zero_tau)",
        "sync()",
    )
    for token in required:
        if token not in source:
            raise ValueError(f"receiver source missing deployable contract: {token}")
    forbidden = (
        r"\bsocket\b", r"\bdashboard\b", r"\b(load|play)\s*\(",
        r"\b(movej|movel|movec|speedj|speedl|servoj|force_mode|stopj)\s*\(",
        r"\bssh\b", r"\bhttp\b", r"time\.sleep",
    )
    if any(re.search(pattern, source, flags=re.IGNORECASE) for pattern in forbidden):
        raise ValueError("receiver source violates offline controller parser contract")
    if 'return "RETURN_CAMPAIGN_HOME"' not in source or 'write_output_integer_register(29, fault_code)' not in source:
        raise ValueError("receiver source must provide return-home and episode-fault evidence handoffs")
    if 'return "EPISODE_FAILED"' in source:
        raise ValueError("episode faults must not terminate the receiver")
    if 'mode == command_end' not in source or 'mode == command_drain' not in source:
        raise ValueError("receiver source must distinguish END and DRAIN")
    return ReceiverContract(
        "ur10e_direct_torque_receiver/v3", 500, "WAIT_FOREVER", False,
        ("APPEND", "END", "DRAIN"), 0.05, 1.0, True,
    )


def receiver_empty_wait(*, queue_empty: bool, explicit_end: bool, explicit_drain: bool) -> str:
    if not queue_empty:
        return "DISPATCH"
    if explicit_end or explicit_drain:
        return "RETURN_CAMPAIGN_HOME"
    return "WAIT_FOREVER"


class ReceiverSemanticState:
    """Pure virtual receiver state machine used by offline semantic tests.

    It mirrors the deployable source's identity/sequence/fault contract and
    never calls controller, network, Dashboard, or motion APIs.
    """

    def __init__(self, *, active_authorization_path: str | Path | None = None) -> None:
        self.phase = "WAIT_FOREVER"
        if isinstance(active_authorization_path, bool):
            raise TypeError("active authorization requires a validated path, not a boolean")
        self.active_allowed = False if active_authorization_path is None else validate_live_authorization(active_authorization_path).active_allowed
        self.filter = RateInvariantForceFilter(DynamicFilterProfile(0.05, 1.0, 500))
        self.last_sequence = 0
        self.last_model_sequence = 0
        self.last_model_timestamp_us = 0
        self.last_model_period_us = 0
        self.last_model_mode = 0
        self.last_raw_force = (0.0,) * 6
        self.last_stiffness = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
        self.lease_id = 0
        self.episode_identity = 0
        self.home_identity: int | None = None
        self.fault_identity_high_water = 0
        self.fault_evidence: str | None = None
        self.safe_exit_trace: tuple[tuple[float, ...], ...] = ()

    @staticmethod
    def _six(values: object, name: str) -> tuple[float, ...]:
        try:
            result = tuple(float(value) for value in values)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}_invalid") from exc
        if len(result) != 6 or not all(math.isfinite(value) for value in result):
            raise ValueError(f"{name}_invalid")
        return result

    def _fault(self, reason: str, packet: Mapping[str, object]) -> str:
        self.phase = "SAFE_EXIT"
        trace = []
        for _ in range(10):
            trace.append(self.filter.smooth_to_zero(dt_s=0.002).filtered_f_ff)
        self.safe_exit_trace = tuple(trace)
        identity = int(packet.get("episode_identity", 0) or 0)
        if identity > self.fault_identity_high_water:
            self.fault_identity_high_water = identity
        else:
            self.fault_identity_high_water = max(1, self.fault_identity_high_water + 1)
        self.home_identity = self.fault_identity_high_water
        self.fault_evidence = f"fault:{self.home_identity}:{reason}"
        self.phase = "WAITING_HOME_ACK"
        self.last_sequence = 0
        self.last_model_sequence = 0
        self.last_model_timestamp_us = 0
        self.last_model_period_us = 0
        self.last_model_mode = 0
        self.last_raw_force = (0.0,) * 6
        self.last_stiffness = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
        self.lease_id = 0
        self.episode_identity = 0
        return "EPISODE_FAULT"

    def command(self, command: str) -> str:
        if command not in {"END", "DRAIN"}:
            raise ValueError("unknown receiver command")
        self.phase = "RETURN_CAMPAIGN_HOME"
        return "RETURN_CAMPAIGN_HOME"

    def append(self, packet: Mapping[str, object]) -> str:
        if self.phase in {"RETURN_CAMPAIGN_HOME", "SAFE_EXIT"}:
            return self.phase
        if self.phase == "WAITING_HOME_ACK":
            return "WAITING_HOME_ACK"
        try:
            sequence = int(packet["sequence"])
            heartbeat = int(packet["heartbeat"])
            lease = int(packet["lease_id"])
            episode = int(packet["episode_identity"])
            model_sequence = int(packet["model_sequence"])
            model_period = int(packet["model_period_us"])
            model_mode = int(packet["model_mode"])
            timestamp = int(packet["model_timestamp_us"])
            raw = self._six(packet["raw_f_df"], "raw_f_df")
            stiffness = self._six(packet["stiffness"], "stiffness")
        except (KeyError, TypeError, ValueError) as exc:
            return self._fault("packet_shape", packet)
        coherent = sequence > 0 and sequence == heartbeat and sequence == self.last_sequence + 1
        if lease <= 0 or episode <= 0 or (self.lease_id and lease != self.lease_id) or (self.episode_identity and episode != self.episode_identity):
            coherent = False
        if model_mode == 0:
            coherent = coherent and model_sequence == 0 and model_period == 0 and timestamp == 0 and raw == (0.0,) * 6
        elif model_mode in {1, 2}:
            coherent = coherent and model_period in {10_000, 20_000} and model_sequence > 0 and (model_mode != 2 or self.active_allowed)
            is_new = model_sequence == self.last_model_sequence + 1
            is_held = model_sequence == self.last_model_sequence
            coherent = coherent and (is_new or is_held)
            if is_held:
                coherent = coherent and timestamp == self.last_model_timestamp_us and model_period == self.last_model_period_us and model_mode == self.last_model_mode and raw == self.last_raw_force and stiffness == self.last_stiffness
            elif is_new:
                coherent = coherent and timestamp >= self.last_model_timestamp_us
        else:
            coherent = False
        if not coherent:
            return self._fault("coherence", packet)
        if not self.lease_id:
            self.lease_id = lease
            self.episode_identity = episode
        self.last_sequence = sequence
        self.last_model_sequence = model_sequence
        self.last_model_timestamp_us = timestamp
        self.last_model_period_us = model_period
        self.last_model_mode = model_mode
        self.last_raw_force = raw
        self.last_stiffness = stiffness
        self.filter.step(raw, dt_s=0.002)
        self.phase = "RUNNING"
        return "RUNNING"

    def acknowledge_home(self, identity: int, consume_identity: int) -> str:
        if self.phase != "WAITING_HOME_ACK" or self.home_identity is None:
            raise ValueError("no pending home identity")
        if identity != self.home_identity or consume_identity != self.home_identity:
            raise ValueError("stale home identity")
        self.filter.reset()
        self.home_identity = None
        self.fault_evidence = None
        self.phase = "WAIT_FOREVER"
        return self.phase
