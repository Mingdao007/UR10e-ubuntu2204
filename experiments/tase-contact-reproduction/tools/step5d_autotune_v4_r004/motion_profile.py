"""Typed offline adapter from the frozen V3 motion profile to V4.

The adapter is deliberately immutable and side-effect free. It carries the
V3 ``ExecutionProfile`` as the source of the dynamic limits while adding the
V4 task-space and cadence values needed by the calibrated runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from step5d_autotune_contract import ExecutionProfile

from .contracts import R004MotionRuntimeConfig, load_contract


V3_CONTROL_PROFILE_ID = "step5d_strict_rnn_autotune_v1"
R004_RUNTIME_CONFIG = load_contract().motion_runtime
R004_EXECUTION_PROFILE_ID = R004_RUNTIME_CONFIG.profile_id
R004_CADENCE_HZ = R004_RUNTIME_CONFIG.cadence_hz


def _finite_positive(value: object, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{role} must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{role} must be a finite positive number")
    return result


@dataclass(frozen=True)
class V4MotionProfile:
    """Immutable V4 view of one validated V3 ``ExecutionProfile``."""

    v3_control_profile_id: str
    execution_profile: ExecutionProfile
    xy_path_speed_m_s: float
    total_linear_cap_m_s: float
    normal_linear_cap_m_s: float
    angular_cap_rad_s: float
    qdot_cap_rad_s: float
    host_slew_rad_s2: float
    tp_acceleration_rad_s2: float
    normal_update_rate_rad_s: float
    cadence_hz: float

    def __post_init__(self) -> None:
        if self.v3_control_profile_id != V3_CONTROL_PROFILE_ID:
            raise ValueError("V4 motion profile must bind the frozen V3 control profile")
        if not isinstance(self.execution_profile, ExecutionProfile):
            raise TypeError("execution_profile must be the frozen V3 ExecutionProfile")
        if self.execution_profile.profile_id != R004_EXECUTION_PROFILE_ID:
            raise ValueError("V4 r004 motion profile is bound to the selected V3 profile")

        runtime = R004_RUNTIME_CONFIG
        expected_profile_values = {
            "normal_max_rate_rad_s": runtime.normal_update_rad_s,
            "host_qdot_slew_rad_s2": runtime.host_qdot_slew_rad_s2,
            "tp_speedj_accel_rad_s2": runtime.tp_speedj_acceleration_rad_s2,
            "qdot_cap_rad_s": runtime.qdot_abs_rad_s,
            "bridge_angular_limit_rad_s": runtime.angular_speed_rad_s,
        }
        for field, expected in expected_profile_values.items():
            actual = float(getattr(self.execution_profile, field))
            if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"V3 execution profile {field} differs")

        expected_values = {
            "xy_path_speed_m_s": runtime.path_xy_speed_m_s,
            "total_linear_cap_m_s": runtime.total_linear_speed_m_s,
            "normal_linear_cap_m_s": runtime.normal_linear_speed_m_s,
            "angular_cap_rad_s": runtime.angular_speed_rad_s,
            "qdot_cap_rad_s": runtime.qdot_abs_rad_s,
            "host_slew_rad_s2": runtime.host_qdot_slew_rad_s2,
            "tp_acceleration_rad_s2": runtime.tp_speedj_acceleration_rad_s2,
            "normal_update_rate_rad_s": runtime.normal_update_rad_s,
            "cadence_hz": runtime.cadence_hz,
        }
        for field, expected in expected_values.items():
            actual = _finite_positive(getattr(self, field), field)
            if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"V4 r004 motion cap {field} differs")
            object.__setattr__(self, field, actual)

    @classmethod
    def from_v3_execution_profile(
        cls,
        execution_profile: ExecutionProfile,
        *,
        v3_control_profile_id: str = V3_CONTROL_PROFILE_ID,
    ) -> "V4MotionProfile":
        """Adapt the selected frozen V3 profile without copying mutable state."""

        return cls.from_r004_runtime_config(
            execution_profile,
            R004_RUNTIME_CONFIG,
            v3_control_profile_id=v3_control_profile_id,
        )

    @classmethod
    def from_r004_runtime_config(
        cls,
        execution_profile: ExecutionProfile,
        runtime_config: R004MotionRuntimeConfig,
        *,
        v3_control_profile_id: str = V3_CONTROL_PROFILE_ID,
    ) -> "V4MotionProfile":
        """Construct the adapter from the validated r004 runtime section."""

        if not isinstance(runtime_config, R004MotionRuntimeConfig):
            raise TypeError("runtime_config must be the typed r004 motion runtime")
        if execution_profile.profile_id != runtime_config.profile_id:
            raise ValueError("execution profile is not the r004 runtime profile")
        return cls(
            v3_control_profile_id=v3_control_profile_id,
            execution_profile=execution_profile,
            xy_path_speed_m_s=runtime_config.path_xy_speed_m_s,
            total_linear_cap_m_s=runtime_config.total_linear_speed_m_s,
            normal_linear_cap_m_s=runtime_config.normal_linear_speed_m_s,
            angular_cap_rad_s=runtime_config.angular_speed_rad_s,
            qdot_cap_rad_s=runtime_config.qdot_abs_rad_s,
            host_slew_rad_s2=runtime_config.host_qdot_slew_rad_s2,
            tp_acceleration_rad_s2=runtime_config.tp_speedj_acceleration_rad_s2,
            normal_update_rate_rad_s=runtime_config.normal_update_rad_s,
            cadence_hz=runtime_config.cadence_hz,
        )

    @property
    def profile_id(self) -> str:
        return self.execution_profile.profile_id

    @property
    def execution_profile_id(self) -> str:
        return self.execution_profile.profile_id

    @property
    def normal_max_rate_rad_s(self) -> float:
        return self.normal_update_rate_rad_s

    @property
    def host_qdot_slew_rad_s2(self) -> float:
        return self.host_slew_rad_s2

    @property
    def tp_speedj_accel_rad_s2(self) -> float:
        return self.tp_acceleration_rad_s2

    @property
    def period_s(self) -> float:
        return 1.0 / self.cadence_hz

    @property
    def tangential_cap_m_s(self) -> float:
        return self.xy_path_speed_m_s

    @property
    def xy_path_cap_m_s(self) -> float:
        return self.xy_path_speed_m_s


R004_EXECUTION_PROFILE = ExecutionProfile(
    R004_RUNTIME_CONFIG.profile_id,
    R004_RUNTIME_CONFIG.normal_update_rad_s,
    R004_RUNTIME_CONFIG.host_qdot_slew_rad_s2,
    R004_RUNTIME_CONFIG.tp_speedj_acceleration_rad_s2,
    qdot_cap_rad_s=R004_RUNTIME_CONFIG.qdot_abs_rad_s,
    bridge_angular_limit_rad_s=R004_RUNTIME_CONFIG.angular_speed_rad_s,
)
R004_MOTION_PROFILE = V4MotionProfile.from_r004_runtime_config(
    R004_EXECUTION_PROFILE,
    R004_RUNTIME_CONFIG,
)
SELECTED_EXECUTION_PROFILE = R004_EXECUTION_PROFILE


@dataclass(frozen=True)
class SameDirectionQdotRescale:
    """Pure result of the V3 whole-vector qdot delta limiter."""

    qdot: tuple[float, float, float, float, float, float]
    delta: tuple[float, float, float, float, float, float]
    scale: float
    delta_limit_rad_s: float

    @property
    def limited(self) -> bool:
        return self.scale < 1.0


def _finite_six(
    values: Sequence[float], role: str
) -> tuple[float, float, float, float, float, float]:
    if len(values) != 6:
        raise ValueError(f"{role} must be a finite 6-vector")
    result = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{role} must be a finite 6-vector")
    return result  # type: ignore[return-value]


def same_direction_qdot_rescale(
    qdot: Sequence[float],
    previous_qdot: Sequence[float] | None = None,
    *,
    dt_s: float,
    max_slew_rad_s2: float,
    dt_max_s: float = 0.02,
) -> SameDirectionQdotRescale:
    """Apply V3 semantics: one scalar to the complete qdot delta."""

    requested = _finite_six(qdot, "qdot")
    previous = _finite_six(
        (0.0,) * 6 if previous_qdot is None else previous_qdot,
        "previous_qdot",
    )
    dt = float(dt_s)
    slew = float(max_slew_rad_s2)
    dt_max = float(dt_max_s)
    if not math.isfinite(dt) or dt < 0.0:
        raise ValueError("dt_s must be finite and non-negative")
    if not math.isfinite(slew) or slew <= 0.0:
        raise ValueError("max_slew_rad_s2 must be finite and positive")
    if not math.isfinite(dt_max) or dt_max <= 0.0:
        raise ValueError("dt_max_s must be finite and positive")

    delta = tuple(requested[index] - previous[index] for index in range(6))
    delta_limit = slew * min(dt, dt_max)
    max_delta = max(abs(value) for value in delta)
    scale = (
        1.0
        if max_delta <= delta_limit or max_delta == 0.0
        else delta_limit / max_delta
    )
    limited = tuple(previous[index] + scale * delta[index] for index in range(6))
    if not all(math.isfinite(value) for value in limited):
        raise ValueError("same-direction qdot rescale returned nonfinite values")
    return SameDirectionQdotRescale(
        qdot=limited,
        delta=delta,
        scale=scale,
        delta_limit_rad_s=delta_limit,
    )


def rescale_qdot_same_direction(
    qdot: Sequence[float],
    previous_qdot: Sequence[float] | None = None,
    *,
    dt_s: float,
    max_slew_rad_s2: float,
    dt_max_s: float = 0.02,
) -> SameDirectionQdotRescale:
    """Compatibility spelling for the V3-parity limiter."""

    return same_direction_qdot_rescale(
        qdot,
        previous_qdot,
        dt_s=dt_s,
        max_slew_rad_s2=max_slew_rad_s2,
        dt_max_s=dt_max_s,
    )


__all__ = [
    "R004_CADENCE_HZ",
    "R004_EXECUTION_PROFILE",
    "R004_EXECUTION_PROFILE_ID",
    "R004_MOTION_PROFILE",
    "R004_RUNTIME_CONFIG",
    "SameDirectionQdotRescale",
    "SELECTED_EXECUTION_PROFILE",
    "V3_CONTROL_PROFILE_ID",
    "V4MotionProfile",
    "rescale_qdot_same_direction",
    "same_direction_qdot_rescale",
]
