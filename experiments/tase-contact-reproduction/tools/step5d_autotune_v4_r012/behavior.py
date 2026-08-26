"""R012-owned fixed W9c contact schedule and runtime identity.

This module is deliberately small: the qualification wave is a frozen
contact-entry policy, not a Bayesian-optimization dimension.  The production
search variables live in :mod:`qlognei` and are therefore not duplicated here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .common import R012ValueError, finite, freeze_tree, json_tree, positive


R012_PROGRAM = "step5d_strict_rnn_autotune_v4_r012"
R012_LINEAGE = "step5d_strict_rnn_autotune_v4_r012"
R012_RUNTIME_PROTOCOL = 612012
WAVE_SCHEMA = "step5d.autotune-v4/r012-w9c-wave-v1"
WAVE_VERSION = "r012-w9c-v1"
WAVE_IDENTITY = "r012-w9c-contact-entry-v1"

FORMAL_OBJECTIVE_SEMANTICS = "force-mae-v2-sealed"


W9C_V2: dict[str, Any] = {
    "schema": WAVE_SCHEMA,
    "version": WAVE_VERSION,
    "identity": WAVE_IDENTITY,
    "program": R012_PROGRAM,
    "runtime_protocol": R012_RUNTIME_PROTOCOL,
    "max_travel_m": 0.025,
    "d_near_start_m": 0.010529311,
    "near_margin_m": 0.003,
    "s_contact_m": 0.013529311,
    "s_adm0_m": 0.013029311,
    "d_sig1_m": 0.013029311,
    "travel_blend0_m": 0.012029311,
    "s_brake0_m": 0.011029311,
    "L_brake_m": 0.001,
    "L_sig_m": 0.001,
    "L_adm_m": 0.0005,
    "v_far_m_s": 0.015,
    "far_acceleration_m_s2": 0.013355575,
    "prebrake_distance_m": 0.001,
    "v_brake_m_s": 0.00015,
    "blend_distance_m": 0.001,
    "v_air_m_s": 0.00015,
    "touch_region_m": 0.0005,
    "F_touch_n": 0.2,
    "F_star_n": 1.0,
    "adm_p": 0.003,
    "v_sat_m_s": 0.00015,
    "F_far_n": 1.0,
    "f_far_hold_s": 0.04,
    "force_latch": {"threshold_n": 1.0, "hold_s": 0.04, "latch_to_near": True, "state_owner": "R012_host_contact_entry"},
    "confirm_normal_n": 1.5,
    "confirm_force_norm_n": 1.7,
    "confirm_hold_s": 0.08,
    "near_acceleration_m_s2": 0.2,
    "qualification": {
        "wave_is_bo_variable": False,
        "trainable": False,
        "matched_baseline_runs": 3,
        "matched_candidate_runs": 3,
        "observed_mae_selection": False,
    },
}


class R012BehaviorError(R012ValueError):
    """R011 schedule or contact-entry behavior is invalid."""


@dataclass(frozen=True)
class ManualWaveSchedule:
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", freeze_tree(json_tree(self.raw)))

    def as_dict(self) -> dict[str, Any]:
        return json_tree(self.raw)

    @property
    def identity(self) -> str:
        return str(self.raw["identity"])


def default_manual_wave() -> ManualWaveSchedule:
    return ManualWaveSchedule(W9C_V2)


def validate_manual_wave(value: Mapping[str, Any]) -> ManualWaveSchedule:
    if not isinstance(value, Mapping):
        raise R012BehaviorError("R011 W9c schedule must be an object")
    required = set(W9C_V2)
    if set(value) != required or value.get("schema") != WAVE_SCHEMA or value.get("version") != WAVE_VERSION:
        raise R012BehaviorError("R011 W9c schedule identity differs")
    for key, expected in W9C_V2.items():
        if key in {"qualification", "force_latch"}:
            continue
        if isinstance(expected, (int, float)) and not math.isclose(float(value[key]), float(expected), rel_tol=0.0, abs_tol=1e-15):
            raise R012BehaviorError(f"R011 W9c {key} differs")
        if isinstance(expected, str) and value[key] != expected:
            raise R012BehaviorError(f"R011 W9c {key} differs")
    if value["qualification"] != W9C_V2["qualification"]:
        raise R012BehaviorError("R011 W9c qualification semantics differ")
    if value["force_latch"] != W9C_V2["force_latch"]:
        raise R012BehaviorError("R011 W9c force-latch semantics differ")
    forbidden = {"F_soft_n", "v_creep_m_s", "creep_latch", "force_latched_creep", "IntegralLimit", "integral_limit_n_s"}
    if forbidden.intersection(value):
        raise R012BehaviorError("Wave7 soft/creep/integrator primitives are forbidden")
    for key in ("max_travel_m", "v_far_m_s", "v_brake_m_s", "v_air_m_s", "F_touch_n", "F_star_n", "F_far_n", "confirm_normal_n", "confirm_force_norm_n"):
        positive(value[key], f"W9c {key}")
    if not (value["v_far_m_s"] > value["v_brake_m_s"] == value["v_air_m_s"] == value["v_sat_m_s"]):
        raise R012BehaviorError("W9c speed ordering is not fixed")
    if value["prebrake_distance_m"] != 0.001 or value["blend_distance_m"] != 0.001 or value["touch_region_m"] != 0.0005:
        raise R012BehaviorError("W9c distance regions differ")
    if value["F_touch_n"] >= value["F_star_n"] or value["F_far_n"] != value["F_star_n"]:
        raise R012BehaviorError("W9c force regions differ")
    if not math.isclose(value["s_contact_m"], value["d_near_start_m"] + value["near_margin_m"], rel_tol=0.0, abs_tol=1e-15) or not math.isclose(value["s_adm0_m"], value["d_sig1_m"], rel_tol=0.0, abs_tol=1e-15) or not math.isclose(value["travel_blend0_m"], value["d_sig1_m"] - value["L_sig_m"], rel_tol=0.0, abs_tol=1e-15) or not math.isclose(value["s_brake0_m"], value["travel_blend0_m"] - value["L_brake_m"], rel_tol=0.0, abs_tol=1e-15):
        raise R012BehaviorError("W9c historical region boundaries differ")
    return ManualWaveSchedule(value)


def travel_sigmoid_speed_m_s(travel_m: float, *, schedule: ManualWaveSchedule | None = None) -> float:
    """Return the continuous W9c far-to-air translational speed.

    The prebrake point, one-millimetre blend, and final touch region share
    exact boundary values.  The touch-admittance law is force-driven and is
    exposed separately so a controller cannot accidentally turn it into a BO
    variable.
    """

    wave = schedule or default_manual_wave()
    validate_manual_wave(wave.as_dict())
    travel = finite(travel_m, "travel_m")
    if travel < 0.0 or travel > wave.raw["max_travel_m"]:
        raise R012BehaviorError("travel is outside W9c range")
    s_brake0 = float(wave.raw["s_brake0_m"])
    travel_blend0 = float(wave.raw["travel_blend0_m"])
    s_adm0 = float(wave.raw["s_adm0_m"])
    if travel < s_brake0:
        return float(wave.raw["v_far_m_s"])
    if travel <= travel_blend0:
        return float(wave.raw["v_brake_m_s"])
    if travel < s_adm0:
        # This is the active URScript logistic, including its midpoint
        # offset.  It is not a smoothstep and is not inferred from max travel.
        fraction = min(1.0, max(0.0, (travel - travel_blend0) / float(wave.raw["L_sig_m"])))
        sigma = 1.0 / (1.0 + math.exp(-8.0 * (fraction - 0.5)))
        return float(wave.raw["v_far_m_s"] + (wave.raw["v_air_m_s"] - wave.raw["v_far_m_s"]) * sigma)
    return float(wave.raw["v_air_m_s"])


def touch_admittance_speed_m_s(force_n: float, *, schedule: ManualWaveSchedule | None = None) -> float:
    wave = schedule or default_manual_wave()
    force = finite(force_n, "force_n")
    if force < wave.raw["F_touch_n"]:
        return float(wave.raw["v_air_m_s"])
    raw = float(wave.raw["adm_p"]) * (float(wave.raw["F_star_n"]) - force)
    return float(max(0.0, min(float(wave.raw["v_sat_m_s"]), raw)))


def command_speed_m_s(travel_m: float, *, force_n: float = 0.0, schedule: ManualWaveSchedule | None = None) -> float:
    """Compose the frozen travel and touch laws without a creep/soft latch."""

    wave = schedule or default_manual_wave()
    travel = finite(travel_m, "travel_m")
    touch_start = float(wave.raw["s_contact_m"])
    if travel >= touch_start:
        return touch_admittance_speed_m_s(force_n, schedule=wave)
    return travel_sigmoid_speed_m_s(travel, schedule=wave)


# Compatibility aliases used by the old qualification helper.  They are
# explicitly W9c-only and intentionally do not expose Wave7's F_soft/v_creep.
TRAVEL_SIGMOID_SEMANTICS = {"schema": "r011-w9c-piecewise-c1-v1", "regions": ["far", "prebrake", "air", "touch"]}
FORCE_LATCH_SEMANTICS = {"schema": "r011-w9c-touch-admittance-v1", "force_driven": True, "creep_primitive": False}
DEFAULT_MANUAL_WAVE = W9C_V2


__all__ = [
    "DEFAULT_MANUAL_WAVE", "FORMAL_OBJECTIVE_SEMANTICS", "FORCE_LATCH_SEMANTICS",
    "ManualWaveSchedule", "R012_LINEAGE", "R012_PROGRAM", "R012_RUNTIME_PROTOCOL", "TRAVEL_SIGMOID_SEMANTICS",
    "W9C_V2", "WAVE_IDENTITY", "WAVE_SCHEMA", "WAVE_VERSION", "command_speed_m_s", "default_manual_wave",
    "touch_admittance_speed_m_s", "travel_sigmoid_speed_m_s", "validate_manual_wave",
]
