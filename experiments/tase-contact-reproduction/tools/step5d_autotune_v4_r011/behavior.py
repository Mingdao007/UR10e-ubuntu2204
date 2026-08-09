"""Frozen, manual R011 Wave behavior.

Wave is a qualification dimension, never an optimizer variable.  The speed
composition intentionally preserves the R010 monotone travel sigmoid and the
force-latch-before-creep precedence while giving R011 its own identity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .common import R011ValueError, finite, freeze_tree, json_tree, positive


R011_PROGRAM = "step5d_strict_rnn_autotune_v4_r011"
R011_LINEAGE = "step5d_strict_rnn_autotune_v4_r011"
R011_RUNTIME_PROTOCOL = 609009
WAVE_SCHEMA = "step5d.autotune-v4/r011-manual-wave-v1"
WAVE_VERSION = "r011-manual-wave-v1"

TRAVEL_SIGMOID_SEMANTICS = {
    "schema": "step5d.autotune-v4/r011-travel-sigmoid-v1",
    "input": "travel_m",
    "midpoint": "0.5 span",
    "steepness": 8.0,
    "direction": "decreasing speed",
    "endpoint_semantics": "start/end are target range, not exact finite-interval endpoints",
    "normalized_endpoints": False,
}

FORCE_LATCH_SEMANTICS = {
    "schema": "step5d.autotune-v4/r011-force-latched-creep-v1",
    "primitive": "force_latched_speed_precedence",
    "is_sigmoid": False,
    "force_latch_clamps_to_near": True,
    "creep_latch_overrides_travel_and_force_latch": True,
    "latches_reset_per_attempt": True,
}


DEFAULT_MANUAL_WAVE: dict[str, Any] = {
    "schema": WAVE_SCHEMA,
    "version": WAVE_VERSION,
    "program": R011_PROGRAM,
    "runtime_protocol": R011_RUNTIME_PROTOCOL,
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
    "qualification": {
        "wave_is_bo_variable": False,
        "trainable": False,
        "matched_baseline_runs": 3,
        "matched_candidate_runs": 3,
        "settling_window_s": 0.5,
        "settling_band_fraction": 0.10,
    },
}


class R011BehaviorError(R011ValueError):
    """R011 manual Wave behavior is invalid."""


@dataclass(frozen=True)
class ManualWaveSchedule:
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", freeze_tree(json_tree(self.raw)))

    def as_dict(self) -> dict[str, Any]:
        return json_tree(self.raw)

    def __getattr__(self, name: str) -> Any:
        aliases = {"f_far_n": "F_far_n", "f_soft_n": "F_soft_n"}
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


def validate_manual_wave(value: Mapping[str, Any]) -> ManualWaveSchedule:
    if not isinstance(value, Mapping) or set(value) != set(DEFAULT_MANUAL_WAVE):
        raise R011BehaviorError("R011 manual Wave fields differ")
    if (
        value.get("schema") != WAVE_SCHEMA
        or value.get("version") != WAVE_VERSION
        or value.get("program") != R011_PROGRAM
        or value.get("runtime_protocol") != R011_RUNTIME_PROTOCOL
    ):
        raise R011BehaviorError("R011 manual Wave identity differs")
    numeric = (
        "max_travel_m", "timeout_s", "d_near_start_travel_m", "near_margin_m",
        "d_blend_m", "v_far_m_s", "v_near_m_s", "v_creep_m_s",
        "far_acceleration_m_s2", "near_acceleration_m_s2", "F_far_n", "F_soft_n",
        "f_far_hold_s", "confirm_normal_n", "confirm_force_norm_n", "confirm_hold_s",
        "force_fuse_n",
    )
    bounds = {
        "max_travel_m": (1e-4, 0.050), "timeout_s": (0.1, 180.0),
        "d_near_start_travel_m": (1e-5, 0.049), "near_margin_m": (1e-6, 0.025),
        "d_blend_m": (1e-6, 0.049), "v_far_m_s": (1e-5, 0.050),
        "v_near_m_s": (1e-6, 0.025), "v_creep_m_s": (1e-7, 0.010),
        "far_acceleration_m_s2": (1e-6, 1.0), "near_acceleration_m_s2": (1e-6, 1.0),
        "F_far_n": (1e-6, 5.0), "F_soft_n": (1e-6, 10.0),
        "f_far_hold_s": (1e-6, 5.0), "confirm_normal_n": (1e-6, 10.0),
        "confirm_force_norm_n": (1e-6, 10.0), "confirm_hold_s": (1e-6, 5.0),
        "force_fuse_n": (1e-6, 75.0),
    }
    for key in numeric:
        observed = positive(value.get(key), key)
        lower, upper = bounds[key]
        if not lower <= observed <= upper:
            raise R011BehaviorError(f"R011 manual Wave {key} is outside bounded schedule range")
    if value.get("force_fuse_reason") != 75:
        raise R011BehaviorError("R011 force fuse reason differs")
    if not float(value["v_far_m_s"]) > float(value["v_near_m_s"]) > float(value["v_creep_m_s"]):
        raise R011BehaviorError("Wave speeds must be far > near > creep")
    if not float(value["F_far_n"]) < float(value["F_soft_n"]) < float(value["confirm_normal_n"]):
        raise R011BehaviorError("Wave force latch ordering differs")
    if not float(value["force_fuse_n"]) > max(float(value["F_soft_n"]), float(value["confirm_normal_n"]), float(value["confirm_force_norm_n"])):
        raise R011BehaviorError("Wave force fuse must exceed all contact thresholds")
    if float(value["confirm_force_norm_n"]) < float(value["confirm_normal_n"]):
        raise R011BehaviorError("Wave force-norm confirmation must not be below normal confirmation")
    if float(value["d_blend_m"]) >= float(value["d_near_start_travel_m"]):
        raise R011BehaviorError("Wave blend starts after the travel origin")
    if float(value["d_near_start_travel_m"]) + float(value["near_margin_m"]) >= float(value["max_travel_m"]):
        raise R011BehaviorError("Wave sigmoid exceeds the travel limit")
    if float(value["d_near_start_travel_m"]) + float(value["near_margin_m"]) <= float(value["d_blend_m"]):
        raise R011BehaviorError("Wave sigmoid span is empty")
    if value.get("travel_sigmoid") != TRAVEL_SIGMOID_SEMANTICS:
        raise R011BehaviorError("Wave travel sigmoid semantics differ")
    if value.get("force_latched_creep") != FORCE_LATCH_SEMANTICS:
        raise R011BehaviorError("Wave latch precedence differs")
    if value.get("qualification") != DEFAULT_MANUAL_WAVE["qualification"]:
        raise R011BehaviorError("Wave qualification semantics differ")
    return ManualWaveSchedule(value)


def default_manual_wave() -> ManualWaveSchedule:
    return validate_manual_wave(DEFAULT_MANUAL_WAVE)


def travel_sigmoid_speed_m_s(travel_m: float, schedule: ManualWaveSchedule | None = None) -> float:
    active = default_manual_wave() if schedule is None else schedule
    travel = finite(travel_m, "travel_m")
    if travel < active.travel_blend0_m:
        return float(active.v_far_m_s)
    if travel >= active.travel_sigmoid_end_m:
        return float(active.v_near_m_s)
    alpha = min(1.0, max(0.0, (travel - active.travel_blend0_m) / active.travel_sigmoid_span_m))
    sigma = 1.0 / (1.0 + math.exp(-8.0 * (alpha - 0.5)))
    return float(active.v_far_m_s) + (float(active.v_near_m_s) - float(active.v_far_m_s)) * sigma


def command_speed_m_s(
    travel_m: float,
    *,
    force_latched: bool = False,
    creep_latched: bool = False,
    schedule: ManualWaveSchedule | None = None,
) -> float:
    active = default_manual_wave() if schedule is None else schedule
    speed = travel_sigmoid_speed_m_s(travel_m, active)
    if force_latched:
        speed = min(speed, float(active.v_near_m_s))
    if creep_latched:
        speed = float(active.v_creep_m_s)
    return speed


__all__ = [
    "DEFAULT_MANUAL_WAVE", "FORCE_LATCH_SEMANTICS", "ManualWaveSchedule",
    "R011BehaviorError", "R011_LINEAGE", "R011_PROGRAM", "R011_RUNTIME_PROTOCOL",
    "TRAVEL_SIGMOID_SEMANTICS", "WAVE_SCHEMA", "WAVE_VERSION", "command_speed_m_s",
    "default_manual_wave", "travel_sigmoid_speed_m_s", "validate_manual_wave",
]
