from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Step5dRemoteState(str, Enum):
    IDLE = "IDLE"
    DRIVER_READY_NO_MOTION = "DRIVER_READY_NO_MOTION"
    REPLAY_SHADOW = "REPLAY_SHADOW"
    CONTACT_TRACK = "CONTACT_TRACK"
    SHORT_DWELL_HOLD = "SHORT_DWELL_HOLD"
    ACTIVE_REACQUIRE = "ACTIVE_REACQUIRE"
    FAIL_FAST_STOP_REQUEST = "FAIL_FAST_STOP_REQUEST"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class Step5dRemoteConfig:
    enable_motion: bool = False
    force_target_n: float = 5.0
    short_dwell_cycles: int = 2
    reacquire_speed_m_s: float = 0.0012
    reacquire_duration_limit_s: float = 0.30
    reacquire_distance_limit_m: float = 0.0005
    cage_tau_s: float = 0.1
    cage_a_stop_m_s2: float = 1.0
    model_margin_m: float = 0.003
    contact_margin_m: float = 0.002
    hard_low_load_n: float = 0.25
    soft_low_load_n: float = 0.50
    low_load_speed_load_n: float = 1.0
    low_load_speed_stop_m_s: float = 0.025
    absolute_speed_stop_m_s: float = 0.050
    early_escape_speed_hold_m_s: float = 0.0099
    force_norm_hard_stop_n: float = 60.0
    torque_norm_hard_stop_nm: float = 2.0
    cage_padding_m: float = 0.020

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "Step5dRemoteConfig":
        fields = cls.__dataclass_fields__
        values: dict[str, Any] = {}
        for key in fields:
            if key in data:
                values[key] = data[key]
        if "short_dwell_cycles" in values:
            values["short_dwell_cycles"] = int(values["short_dwell_cycles"])
        if "enable_motion" in values:
            values["enable_motion"] = bool(values["enable_motion"])
        return cls(**values)


@dataclass(frozen=True)
class CageBounds:
    min_xyz: tuple[float, float, float]
    max_xyz: tuple[float, float, float]
    source_rows: int = 0
    source_csvs: tuple[str, ...] = ()

    def evaluate(
        self,
        tcp_xyz: tuple[float, float, float],
        *,
        actual_tcp_speed_m_s: float,
        predicted_tcp_speed_m_s: float | None,
        config: Step5dRemoteConfig,
    ) -> dict[str, float | str]:
        if not _all_finite(tcp_xyz) or not math.isfinite(actual_tcp_speed_m_s):
            return _invalid_cage("nonfinite_tcp_cage_input")
        speed = max(0.0, actual_tcp_speed_m_s)
        if predicted_tcp_speed_m_s is not None and math.isfinite(predicted_tcp_speed_m_s):
            speed = max(speed, predicted_tcp_speed_m_s)
        d = min(
            tcp_xyz[0] - self.min_xyz[0],
            self.max_xyz[0] - tcp_xyz[0],
            tcp_xyz[1] - self.min_xyz[1],
            self.max_xyz[1] - tcp_xyz[1],
            tcp_xyz[2] - self.min_xyz[2],
            self.max_xyz[2] - tcp_xyz[2],
        )
        braking = (
            speed * config.cage_tau_s
            + speed * speed / (2.0 * config.cage_a_stop_m_s2)
            + config.model_margin_m
            + config.contact_margin_m
        )
        margin = d - braking
        if d <= 0.0:
            reason = "outside_broad_tcp_cage"
        elif margin <= 0.0:
            reason = "tcp_cage_braking_margin_exhausted"
        else:
            reason = "inside_broad_tcp_cage"
        return {
            "distance_m": max(0.0, d),
            "signed_distance_m": d,
            "braking_margin_m": margin,
            "cell_index": 0.0,
            "reason": reason,
        }


@dataclass(frozen=True)
class ShadowSample:
    source_csv: str
    row_index: int
    t_rel_s: float
    normal_load_n: float
    force_norm_n: float
    torque_norm_nm: float
    tcp_xyz: tuple[float, float, float]
    actual_tcp_speed_m_s: float
    predicted_tcp_speed_m_s: float | None
    reaction_normal: tuple[float, float, float] = (0.0, 0.0, -1.0)
    dt_s: float = 0.002


@dataclass
class Step5dRemoteMachine:
    config: Step5dRemoteConfig
    cage: CageBounds | None = None
    state: Step5dRemoteState = Step5dRemoteState.IDLE
    hold_cycles: int = 0
    reacquire_count: int = 0
    reacquire_elapsed_s: float = 0.0
    reacquire_distance_m: float = 0.0
    hold_duty_s: float = 0.0
    active_s: float = 0.0
    _last_reason: str = ""

    def start_shadow(self) -> None:
        self.state = Step5dRemoteState.DRIVER_READY_NO_MOTION
        self.state = Step5dRemoteState.REPLAY_SHADOW

    def step(self, sample: ShadowSample) -> dict[str, Any]:
        if self.state == Step5dRemoteState.IDLE:
            self.start_shadow()
        dt_s = _safe_dt(sample.dt_s)
        self.active_s += dt_s
        cage_eval = self._cage_eval(sample)
        approach = approach_normal_from_reaction(sample.reaction_normal)
        hard_reason = self._hard_stop_reason(sample, cage_eval)

        transition_reason = "ok"
        reacquire_speed = 0.0
        if self.state == Step5dRemoteState.FAIL_FAST_STOP_REQUEST:
            transition_reason = "latched_fail_fast_stop_request"
        elif hard_reason:
            self.state = Step5dRemoteState.FAIL_FAST_STOP_REQUEST
            transition_reason = hard_reason
        elif self._valid_contact(sample):
            self.state = Step5dRemoteState.CONTACT_TRACK
            self.hold_cycles = 0
            self.reacquire_elapsed_s = 0.0
            self.reacquire_distance_m = 0.0
            transition_reason = "valid_contact_track"
        elif self.hold_cycles < self.config.short_dwell_cycles:
            self.state = Step5dRemoteState.SHORT_DWELL_HOLD
            self.hold_cycles += 1
            self.hold_duty_s += dt_s
            transition_reason = "short_dwell_debounce"
        else:
            self.state = Step5dRemoteState.ACTIVE_REACQUIRE
            self.reacquire_count += 1
            self.reacquire_elapsed_s += dt_s
            reacquire_speed = self.config.reacquire_speed_m_s
            self.reacquire_distance_m += reacquire_speed * dt_s
            transition_reason = "active_reacquire_low_load"
            if self.reacquire_elapsed_s > self.config.reacquire_duration_limit_s:
                self.state = Step5dRemoteState.FAIL_FAST_STOP_REQUEST
                transition_reason = "reacquire_duration_limit"
            elif self.reacquire_distance_m > self.config.reacquire_distance_limit_m:
                self.state = Step5dRemoteState.FAIL_FAST_STOP_REQUEST
                transition_reason = "reacquire_distance_limit"

        self._last_reason = transition_reason
        cmd_enabled = bool(self.config.enable_motion and self.state == Step5dRemoteState.CONTACT_TRACK)
        if not self.config.enable_motion:
            cmd_enabled = False
        return {
            "source_csv": sample.source_csv,
            "row_index": sample.row_index,
            "t_rel_s": sample.t_rel_s,
            "would_state": self.state.value,
            "transition_reason": transition_reason,
            "normal_load_n": sample.normal_load_n,
            "force_norm_n": sample.force_norm_n,
            "torque_norm_nm": sample.torque_norm_nm,
            "cage_margin_m": cage_eval["braking_margin_m"],
            "cage_reason": cage_eval["reason"],
            "reacquire_direction_x": approach[0],
            "reacquire_direction_y": approach[1],
            "reacquire_direction_z": approach[2],
            "reacquire_speed_m_s": reacquire_speed,
            "hold_duty": self.hold_duty_s / self.active_s if self.active_s > 0.0 else 0.0,
            "reacquire_count": self.reacquire_count,
            "cmd_enabled": cmd_enabled,
            "would_command_qdot_0": 0.0,
            "would_command_qdot_1": 0.0,
            "would_command_qdot_2": 0.0,
            "would_command_qdot_3": 0.0,
            "would_command_qdot_4": 0.0,
            "would_command_qdot_5": 0.0,
            "would_command_twist_x": approach[0] * reacquire_speed,
            "would_command_twist_y": approach[1] * reacquire_speed,
            "would_command_twist_z": approach[2] * reacquire_speed,
            "would_command_twist_rx": 0.0,
            "would_command_twist_ry": 0.0,
            "would_command_twist_rz": 0.0,
        }

    def _valid_contact(self, sample: ShadowSample) -> bool:
        return (
            self.config.soft_low_load_n <= sample.normal_load_n <= self.config.force_target_n * 3.0
            and sample.force_norm_n < self.config.force_norm_hard_stop_n
            and sample.actual_tcp_speed_m_s <= self.config.early_escape_speed_hold_m_s
        )

    def _hard_stop_reason(self, sample: ShadowSample, cage_eval: dict[str, float | str]) -> str:
        finite_values = [
            sample.normal_load_n,
            sample.force_norm_n,
            sample.torque_norm_nm,
            sample.actual_tcp_speed_m_s,
            sample.t_rel_s,
        ]
        if not all(math.isfinite(value) for value in finite_values):
            return "nonfinite_shadow_input"
        if sample.force_norm_n >= self.config.force_norm_hard_stop_n:
            return "force_norm_hard_stop"
        if sample.torque_norm_nm >= self.config.torque_norm_hard_stop_nm:
            return "torque_norm_hard_stop"
        if not math.isfinite(float(cage_eval["braking_margin_m"])):
            return "nonfinite_tcp_cage_braking_margin"
        if float(cage_eval["braking_margin_m"]) <= 0.0:
            return "tcp_cage_braking_margin_exhausted"
        if sample.actual_tcp_speed_m_s > self.config.absolute_speed_stop_m_s:
            return "actual_tcp_speed_hard_stop"
        if (
            sample.normal_load_n < self.config.low_load_speed_load_n
            and sample.actual_tcp_speed_m_s > self.config.low_load_speed_stop_m_s
        ):
            return "low_load_actual_tcp_speed_hard_stop"
        return ""

    def _cage_eval(self, sample: ShadowSample) -> dict[str, float | str]:
        if self.cage is None:
            return _invalid_cage("missing_tcp_cage")
        return self.cage.evaluate(
            sample.tcp_xyz,
            actual_tcp_speed_m_s=sample.actual_tcp_speed_m_s,
            predicted_tcp_speed_m_s=sample.predicted_tcp_speed_m_s,
            config=self.config,
        )


def signed_normal_load_n(force_base: tuple[float, float, float], reaction_normal: tuple[float, float, float]) -> float:
    reaction = normalize3(reaction_normal, name="reaction_normal")
    return sum(force_base[idx] * reaction[idx] for idx in range(3))


def approach_normal_from_reaction(reaction_normal: tuple[float, float, float]) -> tuple[float, float, float]:
    reaction = normalize3(reaction_normal, name="reaction_normal")
    return (-reaction[0], -reaction[1], -reaction[2])


def normalize3(values: tuple[float, float, float], *, name: str) -> tuple[float, float, float]:
    if len(values) != 3 or not _all_finite(values):
        raise ValueError(f"{name} must be a finite length-3 vector")
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 1e-12:
        raise ValueError(f"{name} norm is too small")
    return tuple(value / norm for value in values)  # type: ignore[return-value]


def _all_finite(values: tuple[float, ...]) -> bool:
    return all(math.isfinite(float(value)) for value in values)


def _safe_dt(value: float) -> float:
    if not math.isfinite(value) or value <= 0.0:
        return 0.002
    return min(value, 0.010)


def _invalid_cage(reason: str) -> dict[str, float | str]:
    return {
        "distance_m": math.nan,
        "signed_distance_m": math.nan,
        "braking_margin_m": math.nan,
        "cell_index": -1.0,
        "reason": reason,
    }
