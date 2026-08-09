"""R010 Wave7 contact-entry behavior and its two independent sigmoids.

The travel sigmoid controls contact-search speed.  ``F_soft`` is deliberately
represented as a separate force-latched primitive, not as a sigmoid.  The
early-abort kappa sigmoid is carried only as shadow configuration and never
enters the contact command path.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


WAVE7_SCHEDULE_SCHEMA = "step5d.autotune-v4/r010-wave7-contact-entry-v1"
WAVE7_SCHEDULE_VERSION = "r010-wave7-v1"
R010_PROGRAM = "step5d_strict_rnn_autotune_v4_r010"
R010_RUNTIME_PROTOCOL = 609009

TRAVEL_SIGMOID_SEMANTICS: dict[str, Any] = {
    "schema": "step5d.autotune-v4/r010-travel-sigmoid-v1",
    "input": "travel_m",
    "midpoint": "0.5 span",
    "steepness": 8.0,
    "direction": "decreasing speed",
    "endpoint_semantics": "start/end are target range, not exact finite-interval endpoints",
    "normalized_endpoints": False,
}

EARLY_ABORT_SIGMOID_SEMANTICS: dict[str, Any] = {
    "schema": "step5d.autotune-v4/r010-early-abort-kappa-sigmoid-v1",
    "input": "progress_fraction",
    "kappa_start": 3.0,
    "kappa_end": 1.3,
    "midpoint": 0.5,
    "steepness": 10.0,
    "direction": "decreasing kappa",
    "mode": "shadow",
    "active_allowed": False,
    "enters_gp_training": False,
    "enters_ledger": False,
    "enters_control": False,
}

FORCE_LATCH_SEMANTICS: dict[str, Any] = {
    "schema": "step5d.autotune-v4/r010-force-latched-creep-v1",
    "primitive": "force_latched_speed_precedence",
    "is_sigmoid": False,
    "force_latch_clamps_to_near": True,
    "creep_latch_overrides_travel_and_force_latch": True,
    "latches_reset_per_attempt": True,
}

DEFAULT_WAVE7_SCHEDULE: dict[str, Any] = {
    "schema": WAVE7_SCHEDULE_SCHEMA,
    "version": WAVE7_SCHEDULE_VERSION,
    "program": R010_PROGRAM,
    "runtime_protocol": R010_RUNTIME_PROTOCOL,
    "max_travel_m": 0.025,
    "timeout_s": 90.0,
    "d_near_start_travel_m": 0.010529311,
    "near_margin_m": 0.003,
    "d_blend_m": 0.002,
    "v_far_m_s": 0.015,
    "v_near_m_s": 0.005,
    "v_creep_m_s": 0.0007,
    "far_acceleration_m_s2": 0.013355575,
    "near_acceleration_m_s2": 0.2,
    "F_far_n": 1.0,
    "F_soft_n": 1.2,
    "f_far_hold_s": 0.04,
    "confirm_normal_n": 1.5,
    "confirm_force_norm_n": 1.7,
    "confirm_hold_s": 0.08,
    "force_fuse_n": 50.0,
    "force_fuse_reason": 75,
    "travel_sigmoid": TRAVEL_SIGMOID_SEMANTICS,
    "force_latched_creep": FORCE_LATCH_SEMANTICS,
    "early_abort_sigmoid": EARLY_ABORT_SIGMOID_SEMANTICS,
    "unchanged_first_cut": {
        "baseline_ramp": True,
        "anchor_force_gains": True,
        "path_entry_limiter": True,
        "formal_objective": True,
    },
}


class R010BehaviorError(ValueError):
    """R010 behavior bytes or typed values are invalid."""


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R010BehaviorError(f"{role} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise R010BehaviorError(f"{role} must be finite")
    return number


def _positive(value: Any, role: str) -> float:
    number = _finite(value, role)
    if number <= 0.0:
        raise R010BehaviorError(f"{role} must be positive")
    return number


def _json_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_tree(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True)
class Wave7Schedule:
    """Typed immutable Wave7 behavior used by both manifest and TP renderer."""

    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", MappingProxyType(_json_tree(self.raw)))

    def as_dict(self) -> dict[str, Any]:
        return _json_tree(self.raw)

    def __getattr__(self, name: str) -> Any:
        aliases = {
            "f_far_n": "F_far_n",
            "f_soft_n": "F_soft_n",
        }
        key = aliases.get(name, name)
        try:
            return self.raw[key]
        except KeyError as exc:
            raise AttributeError(name) from exc

    @property
    def travel_blend0_m(self) -> float:
        return float(self.raw["d_near_start_travel_m"]) - float(self.raw["d_blend_m"])

    @property
    def travel_sigmoid_end_m(self) -> float:
        return float(self.raw["d_near_start_travel_m"]) + float(self.raw["near_margin_m"])

    @property
    def travel_sigmoid_span_m(self) -> float:
        return self.travel_sigmoid_end_m - self.travel_blend0_m


def validate_wave7_schedule(document: Mapping[str, Any]) -> Wave7Schedule:
    if not isinstance(document, Mapping):
        raise R010BehaviorError("Wave7 schedule must be an object")
    required = set(DEFAULT_WAVE7_SCHEDULE)
    if set(document) != required:
        raise R010BehaviorError("Wave7 schedule fields differ")
    if (
        document.get("schema") != WAVE7_SCHEDULE_SCHEMA
        or document.get("version") != WAVE7_SCHEDULE_VERSION
        or document.get("program") != R010_PROGRAM
        or document.get("runtime_protocol") != R010_RUNTIME_PROTOCOL
    ):
        raise R010BehaviorError("Wave7 identity or runtime protocol differs")

    numeric = {
        key: _positive(document.get(key), key)
        for key in (
            "max_travel_m",
            "timeout_s",
            "d_near_start_travel_m",
            "near_margin_m",
            "d_blend_m",
            "v_far_m_s",
            "v_near_m_s",
            "v_creep_m_s",
            "far_acceleration_m_s2",
            "near_acceleration_m_s2",
            "F_far_n",
            "F_soft_n",
            "f_far_hold_s",
            "confirm_normal_n",
            "confirm_force_norm_n",
            "confirm_hold_s",
            "force_fuse_n",
        )
    }
    expected = {
        key: float(DEFAULT_WAVE7_SCHEDULE[key])
        for key in numeric
    }
    for key, number in numeric.items():
        if not math.isclose(number, expected[key], rel_tol=0.0, abs_tol=1e-12):
            raise R010BehaviorError(f"Wave7 {key} differs from the first-cut pin")
    if document.get("force_fuse_reason") != 75:
        raise R010BehaviorError("Wave7 force fuse reason differs")
    if not numeric["v_far_m_s"] > numeric["v_near_m_s"] > numeric["v_creep_m_s"]:
        raise R010BehaviorError("Wave7 speeds are not far > near > creep")
    if not numeric["F_far_n"] < numeric["F_soft_n"] < numeric["confirm_normal_n"]:
        raise R010BehaviorError("Wave7 force latch ordering differs")
    if numeric["d_blend_m"] >= numeric["d_near_start_travel_m"]:
        raise R010BehaviorError("Wave7 blend starts before travel origin")
    if numeric["d_near_start_travel_m"] + numeric["near_margin_m"] >= numeric["max_travel_m"]:
        raise R010BehaviorError("Wave7 sigmoid exceeds travel limit")
    if document.get("travel_sigmoid") != TRAVEL_SIGMOID_SEMANTICS:
        raise R010BehaviorError("Wave7 travel sigmoid semantics differ")
    if document.get("force_latched_creep") != FORCE_LATCH_SEMANTICS:
        raise R010BehaviorError("Wave7 force-latched creep semantics differ")
    if document.get("early_abort_sigmoid") != EARLY_ABORT_SIGMOID_SEMANTICS:
        raise R010BehaviorError("Wave7 early-abort sigmoid semantics differ")
    if document.get("unchanged_first_cut") != DEFAULT_WAVE7_SCHEDULE["unchanged_first_cut"]:
        raise R010BehaviorError("R010 first-cut unchanged controls differ")
    return Wave7Schedule(document)


def default_wave7_schedule() -> Wave7Schedule:
    return validate_wave7_schedule(DEFAULT_WAVE7_SCHEDULE)


def travel_sigmoid_speed_m_s(travel_m: float, schedule: Wave7Schedule | None = None) -> float:
    """Return the existing unnormalised Wave7 travel-logistic speed command."""

    active = default_wave7_schedule() if schedule is None else schedule
    travel = _finite(travel_m, "travel_m")
    v_far = float(active.v_far_m_s)
    v_near = float(active.v_near_m_s)
    if travel < active.travel_blend0_m:
        return v_far
    if travel >= active.travel_sigmoid_end_m:
        return v_near
    alpha = min(1.0, max(0.0, (travel - active.travel_blend0_m) / active.travel_sigmoid_span_m))
    sigma = 1.0 / (1.0 + math.exp(-8.0 * (alpha - 0.5)))
    return v_far + (v_near - v_far) * sigma


def command_speed_m_s(
    travel_m: float,
    *,
    force_latched: bool = False,
    creep_latched: bool = False,
    schedule: Wave7Schedule | None = None,
) -> float:
    """Compose travel speed, F_far clamp, then F_soft creep precedence."""

    active = default_wave7_schedule() if schedule is None else schedule
    speed = travel_sigmoid_speed_m_s(travel_m, active)
    if force_latched:
        speed = min(speed, float(active.v_near_m_s))
    if creep_latched:
        speed = float(active.v_creep_m_s)
    return speed


def render_wave7_contact_loop(schedule: Wave7Schedule | None = None) -> str:
    """Render only the counted URScript contact-search replacement block."""

    active = default_wave7_schedule() if schedule is None else schedule
    f = lambda value: f"{float(value):.9f}"  # noqa: E731 - local renderer primitive
    return f"""  # R010 Wave7 contact entry: travel sigmoid plus independent force latches.
  local contact_start = get_actual_tcp_pose()
  local contact_elapsed_s = 0.0
  local contact_confirm_s = 0.0
  local contact_done = False
  local force_latched = False
  local creep_latched = False
  local far_force_s = 0.0
  local travel_blend0_m = {f(active.travel_blend0_m)}
  local d_sig1_m = {f(active.travel_sigmoid_end_m)}
  local d_sig_span_m = {f(active.travel_sigmoid_span_m)}
  local f_far_n = {f(active.f_far_n)}
  local f_far_hold_s = {f(active.f_far_hold_s)}
  local f_soft_n = {f(active.f_soft_n)}
  local force_fuse_n = {f(active.force_fuse_n)}
  local v_far_m_s = {f(active.v_far_m_s)}
  local v_near_m_s = {f(active.v_near_m_s)}
  local v_creep_m_s = {f(active.v_creep_m_s)}
  local far_accel_m_s2 = {f(active.far_acceleration_m_s2)}
  local near_accel_m_s2 = {f(active.near_acceleration_m_s2)}
  while not contact_done:
    packet_reason = codex_r010_packet_observe()
    guard = codex_r010_packet_guard(packet_reason, 60.0, 100.0, 3.0)
    local contact_pose = get_actual_tcp_pose()
    local travel = contact_start[2] - contact_pose[2]
    local normal_force = read_input_float_register(24)
    local force_norm = read_input_float_register(25)
    if guard != 0:
      return codex_r010_fault(epoch, ordinal, token, kind, consumed, guard, runtime_hi, runtime_lo)
    elif normal_force >= force_fuse_n or force_norm >= force_fuse_n:
      stopl(0.010000000)
      return codex_r010_fault(epoch, ordinal, token, kind, consumed, {int(active.force_fuse_reason)}, runtime_hi, runtime_lo)
    elif travel >= {f(active.max_travel_m)}:
      return codex_r010_fault(epoch, ordinal, token, kind, consumed, 8, runtime_hi, runtime_lo)
    elif contact_elapsed_s >= {f(active.timeout_s)}:
      return codex_r010_fault(epoch, ordinal, token, kind, consumed, 10, runtime_hi, runtime_lo)
    else:
      local contact_dt = get_steptime()
      if normal_force >= {f(active.confirm_normal_n)} or force_norm >= {f(active.confirm_force_norm_n)}:
        contact_confirm_s = contact_confirm_s + contact_dt
      else:
        contact_confirm_s = 0.0
      end
      if contact_confirm_s >= {f(active.confirm_hold_s)}:
        contact_done = True
      else:
        if normal_force >= f_far_n or force_norm >= f_far_n:
          far_force_s = far_force_s + contact_dt
        else:
          far_force_s = 0.0
        end
        if far_force_s >= f_far_hold_s:
          force_latched = True
        end
        if normal_force >= f_soft_n or force_norm >= f_soft_n:
          creep_latched = True
        end
        local v_cmd_m_s = v_far_m_s
        local a_cmd_m_s2 = far_accel_m_s2
        if travel >= d_sig1_m:
          v_cmd_m_s = v_near_m_s
          a_cmd_m_s2 = near_accel_m_s2
        elif travel >= travel_blend0_m:
          local sig_alpha = (travel - travel_blend0_m) / d_sig_span_m
          if sig_alpha < 0.0:
            sig_alpha = 0.0
          elif sig_alpha > 1.0:
            sig_alpha = 1.0
          end
          local sigma = 1.0 / (1.0 + pow(2.718281828, -8.0 * (sig_alpha - 0.5)))
          v_cmd_m_s = v_far_m_s + (v_near_m_s - v_far_m_s) * sigma
          a_cmd_m_s2 = near_accel_m_s2
        end
        if force_latched and v_cmd_m_s > v_near_m_s:
          v_cmd_m_s = v_near_m_s
          a_cmd_m_s2 = near_accel_m_s2
        end
        if creep_latched:
          v_cmd_m_s = v_creep_m_s
          a_cmd_m_s2 = near_accel_m_s2
        end
        speedl([0.0, 0.0, -v_cmd_m_s, 0.0, 0.0, 0.0], a_cmd_m_s2, contact_dt)
        contact_elapsed_s = contact_elapsed_s + contact_dt
        codex_r010_echo(epoch, ordinal, 20, token, 0, consumed, kind, 0, runtime_hi, runtime_lo)
      end
    end
  end
  stopl(0.010000000)
"""


__all__ = [
    "DEFAULT_WAVE7_SCHEDULE",
    "EARLY_ABORT_SIGMOID_SEMANTICS",
    "FORCE_LATCH_SEMANTICS",
    "R010BehaviorError",
    "R010_PROGRAM",
    "R010_RUNTIME_PROTOCOL",
    "TRAVEL_SIGMOID_SEMANTICS",
    "WAVE7_SCHEDULE_SCHEMA",
    "WAVE7_SCHEDULE_VERSION",
    "Wave7Schedule",
    "command_speed_m_s",
    "default_wave7_schedule",
    "render_wave7_contact_loop",
    "travel_sigmoid_speed_m_s",
    "validate_wave7_schedule",
]
