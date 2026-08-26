"""R012 PATH-error CBF adapter for the mature r004 command seam."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from step5d_autotune_v4_r004.path_reference import (
    PATH_STAGE_ID,
    R004_PATH_FRAME_SNAPSHOT,
    step5_path_reference,
)

from .safety_filter import SafetyFilterConfig, SafetyFilterResult, filter_path_error_twist


R012_SOFT_CBF_AXES_M = (0.022, 0.012)
R012_HARD_TUBE_AXES_M = (0.025, 0.015)


def _base_to_path_xy(value: Sequence[float]) -> tuple[float, float]:
    along = R004_PATH_FRAME_SNAPSHOT["basis"]["u_along_xy"]
    lateral = R004_PATH_FRAME_SNAPSHOT["basis"]["p_lateral_xy"]
    return (
        float(value[0]) * float(along[0]) + float(value[1]) * float(along[1]),
        float(value[0]) * float(lateral[0]) + float(value[1]) * float(lateral[1]),
    )


def _path_to_base_xy(value: Sequence[float]) -> tuple[float, float]:
    along = R004_PATH_FRAME_SNAPSHOT["basis"]["u_along_xy"]
    lateral = R004_PATH_FRAME_SNAPSHOT["basis"]["p_lateral_xy"]
    return (
        float(value[0]) * float(along[0]) + float(value[1]) * float(lateral[0]),
        float(value[0]) * float(along[1]) + float(value[1]) * float(lateral[1]),
    )


def _point_to_path_xy(value: Sequence[float]) -> tuple[float, float]:
    origin = R004_PATH_FRAME_SNAPSHOT["basis"]["origin_xy_m"]
    return _base_to_path_xy((float(value[0]) - float(origin[0]), float(value[1]) - float(origin[1])))


@dataclass(frozen=True)
class R012PathCbfOutcome:
    desired_twist: tuple[float, float, float, float, float, float]
    applied: bool
    mode: str
    result: SafetyFilterResult | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r012-path-cbf-live-outcome-v1",
            "mode": self.mode,
            "applied": self.applied,
            "soft": None if self.result is None else self.result.as_dict(),
            "fail_closed": bool(self.result is not None and not self.result.valid),
        }


@dataclass
class R012PathCbfLiveFilter:
    """Stateful active R012 filter; PATH-only and fail-closed on bad inputs."""

    config: SafetyFilterConfig = field(default_factory=SafetyFilterConfig)
    previous_path_command_m_s: tuple[float, float] = (0.0, 0.0)

    def apply(
        self,
        desired_twist: Sequence[float],
        *,
        mode: str,
        actual_tcp_pose: Sequence[float],
        path_time_s: float,
    ) -> R012PathCbfOutcome:
        twist = tuple(float(desired_twist[index]) for index in range(6))
        if str(mode).strip().lower() != "path":
            return R012PathCbfOutcome(twist, False, "active", None)  # type: ignore[arg-type]
        try:
            if len(actual_tcp_pose) < 2 or not math.isfinite(float(path_time_s)):
                raise ValueError("invalid PATH pose/time")
            reference = step5_path_reference(
                PATH_STAGE_ID,
                (float(actual_tcp_pose[0]), float(actual_tcp_pose[1])),
                float(path_time_s),
            )
            actual_path = _point_to_path_xy(actual_tcp_pose[:2])
            reference_path = _point_to_path_xy(reference["desired_xy"])
            reference_velocity_path = _base_to_path_xy(reference["desired_velocity_xy"])
            nominal_path = _base_to_path_xy(twist[:2])
            path_twist = nominal_path + twist[2:]
            axes = self.config.tightened_axes_m
            error_path = (
                actual_path[0] - reference_path[0],
                actual_path[1] - reference_path[1],
            )
            rho = sum((error_path[index] / axes[index]) ** 2 for index in range(2))
            if rho < self.config.engage_deadband:
                # Match the mature R008 seam: the soft layer is an exact
                # no-op in the ellipse interior.  Keeping the previous value
                # synchronized avoids inventing a second PATH rate limiter
                # when the barrier later engages.
                self.previous_path_command_m_s = nominal_path
                result = SafetyFilterResult(
                    True,
                    nominal_path,
                    1.0 - rho,
                    (),
                    0,
                    0.0,
                    0.0,
                    None,
                    error_xy_m=error_path,
                )
                return R012PathCbfOutcome(twist, False, "active", result)  # type: ignore[arg-type]
            filtered_path, result = filter_path_error_twist(
                actual_path,
                reference_path,
                reference_velocity_path,
                path_twist,
                self.previous_path_command_m_s,
                state_age_s=0.0,
                config=self.config,
            )
        except Exception:
            # Freeze PATH XY; never pass invalid live input or a stale outward
            # command through to the robot command seam.
            self.previous_path_command_m_s = (0.0, 0.0)
            fallback = (0.0, 0.0) + twist[2:]
            result = SafetyFilterResult(
                False,
                (0.0, 0.0),
                None,
                (),
                0,
                0.0,
                0.0,
                "invalid_live_input",
            )
            return R012PathCbfOutcome(fallback, True, "active", result)  # type: ignore[arg-type]
        if result.valid:
            self.previous_path_command_m_s = tuple(result.command_m_s)
            base_xy = _path_to_base_xy(filtered_path[:2])
        else:
            # A previous outward command is not a safe fallback when the
            # feasible set becomes invalid.  Freeze PATH XY for this tick;
            # inherited hard safety remains independently authoritative.
            self.previous_path_command_m_s = (0.0, 0.0)
            base_xy = (0.0, 0.0)
        output = base_xy + tuple(filtered_path[2:])
        applied = (not result.valid) or result.intervention_norm_m_s > 0.0
        return R012PathCbfOutcome(output, applied, "active", result)  # type: ignore[arg-type]


@dataclass(frozen=True)
class R012GuardStackOutcome:
    desired_twist: tuple[float, float, float, float, float, float]
    soft: R012PathCbfOutcome
    hard_value: float | None
    terminal_stop: bool
    reason: str | None = None

    @property
    def valid(self) -> bool:
        return not self.terminal_stop

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r012-composed-path-guards-v1",
            "soft": self.soft.as_dict(),
            "hard": {
                "axes_m": list(R012_HARD_TUBE_AXES_M),
                "value": self.hard_value,
                "terminal_stop": self.terminal_stop,
                "reason": self.reason,
            },
            "terminal_stop": self.terminal_stop,
            "reason": self.reason,
        }


class R012GuardStop(RuntimeError):
    """Terminal fail-closed result raised through the mature writer loop."""

    def __init__(self, outcome: R012GuardStackOutcome) -> None:
        super().__init__(outcome.reason or "R012 hard guard terminal stop")
        self.outcome = outcome


@dataclass
class R012PathGuardStack:
    """R012's machine-side PATH stack: hard outer ellipse, then soft CBF-QP."""

    soft_filter: R012PathCbfLiveFilter = field(default_factory=R012PathCbfLiveFilter)

    def __post_init__(self) -> None:
        if not all(math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12) for actual, expected in zip(self.soft_filter.config.tightened_axes_m, R012_SOFT_CBF_AXES_M)):
            raise ValueError("R012 soft CBF axes must be exactly 0.022 m x 0.012 m")
        if not all(math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12) for actual, expected in zip(self.soft_filter.config.ellipse_axes_m, R012_HARD_TUBE_AXES_M)):
            raise ValueError("R012 hard ellipse source axes must be exactly 0.025 m x 0.015 m")

    @staticmethod
    def _hard_value(actual_tcp_pose: Sequence[float], reference_xy: Sequence[float]) -> float:
        if len(actual_tcp_pose) < 2 or len(reference_xy) < 2:
            raise ValueError("PATH pose/reference is incomplete")
        error_base = (
            float(actual_tcp_pose[0]) - float(reference_xy[0]),
            float(actual_tcp_pose[1]) - float(reference_xy[1]),
        )
        if not all(math.isfinite(value) for value in error_base):
            raise ValueError("PATH pose/reference is not finite")
        error_path = _base_to_path_xy(error_base)
        return sum((error_path[index] / R012_HARD_TUBE_AXES_M[index]) ** 2 for index in range(2))

    def apply(
        self,
        desired_twist: Sequence[float],
        *,
        mode: str,
        actual_tcp_pose: Sequence[float],
        path_time_s: float,
        state_age_s: float = 0.0,
    ) -> R012GuardStackOutcome:
        twist = tuple(float(desired_twist[index]) for index in range(6))
        if str(mode).strip().lower() != "path":
            soft = self.soft_filter.apply(
                twist, mode=mode, actual_tcp_pose=actual_tcp_pose, path_time_s=path_time_s
            )
            return R012GuardStackOutcome(twist, soft, None, False)
        try:
            age = float(state_age_s)
            path_time = float(path_time_s)
            if not math.isfinite(age) or age < 0.0 or age > self.soft_filter.config.max_state_age_s:
                raise ValueError("stale PATH state")
            if not math.isfinite(path_time):
                raise ValueError("invalid PATH time")
            reference = step5_path_reference(
                PATH_STAGE_ID,
                (float(actual_tcp_pose[0]), float(actual_tcp_pose[1])),
                path_time,
            )
            hard_value = self._hard_value(actual_tcp_pose, reference["desired_xy"])
        except Exception as exc:
            soft = R012PathCbfOutcome(twist, True, "active", None)
            outcome = R012GuardStackOutcome(
                (0.0, 0.0, twist[2], twist[3], twist[4], twist[5]),
                soft,
                None,
                True,
                f"hard_guard_fail_closed:{exc}",
            )
            raise R012GuardStop(outcome)
        if hard_value > 1.0:
            soft = R012PathCbfOutcome(twist, False, "active", None)
            outcome = R012GuardStackOutcome(
                (0.0, 0.0, twist[2], twist[3], twist[4], twist[5]),
                soft,
                hard_value,
                True,
                "hard_outer_ellipse_breach",
            )
            raise R012GuardStop(outcome)
        soft = self.soft_filter.apply(
            twist,
            mode=mode,
            actual_tcp_pose=actual_tcp_pose,
            path_time_s=path_time,
        )
        return R012GuardStackOutcome(soft.desired_twist, soft, hard_value, False)


def install_r012_guard_stack(writer: Any, guard_stack: R012PathGuardStack | None = None) -> R012PathGuardStack:
    """Bind both R012 guards to controls created by the mature writer seam."""

    injection = getattr(writer, "injection", None)
    original = getattr(injection, "prepare_control", None)
    if injection is None or not callable(original):
        raise RuntimeError("R012 mature writer has no guard-bound control-preparation seam")
    stack = guard_stack or R012PathGuardStack()
    if getattr(injection, "_r012_guard_stack_installed", False):
        existing = getattr(injection, "_r012_guard_stack", None)
        if not isinstance(existing, R012PathGuardStack):
            raise RuntimeError("R012 mature writer guard binding is unreadable")
        return existing

    def prepare_control(*args: Any, **kwargs: Any) -> Any:
        control = original(*args, **kwargs)
        control._tube_cbf = stack
        control._r012_guard_stack = stack
        control._r012_guard_bound = True
        control.last_tube_cbf = None
        return control

    injection.prepare_control = prepare_control
    injection._r012_guard_stack = stack
    injection._r012_guard_stack_installed = True
    writer._r012_guard_stack = stack
    writer._r012_guard_bound = True
    return stack


def is_r012_guard_bound(writer: Any) -> bool:
    injection = getattr(writer, "injection", None)
    return bool(
        getattr(writer, "_r012_guard_bound", False)
        and getattr(injection, "_r012_guard_stack_installed", False)
        and isinstance(getattr(injection, "_r012_guard_stack", None), R012PathGuardStack)
    )


def install_r012_path_cbf(writer: Any) -> None:
    """Compatibility spelling for the composed R012 guard stack."""

    install_r012_guard_stack(writer)


__all__ = [
    "R012_HARD_TUBE_AXES_M",
    "R012_SOFT_CBF_AXES_M",
    "R012GuardStackOutcome",
    "R012GuardStop",
    "R012PathCbfLiveFilter",
    "R012PathCbfOutcome",
    "R012PathGuardStack",
    "install_r012_guard_stack",
    "install_r012_path_cbf",
    "is_r012_guard_bound",
]
