"""Exact deterministic one-step 2-D PATH-frame predictive safety projection."""

from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .common import R012ValueError, finite, json_tree


SAFETY_SCHEMA = "step5d.autotune-v4/r011-path-error-cbf-v2"
INTERVENTION_SCHEMA = "step5d.autotune-v4/r011-safety-intervention-v1"
TIMING_SCHEMA = "step5d.autotune-v4/r011-safety-timing-receipt-v1"
FILTER_DT_S = 0.002
MAX_ROOT_ITERATIONS = 64
_STATUSES = (-1, 0, 1)  # lower, free, upper


class SafetyFilterError(R012ValueError):
    """Safety-filter configuration or input is invalid."""


def _pair(value: Sequence[float], role: str) -> tuple[float, float]:
    if not isinstance(value, Sequence) or len(value) != 2:
        raise SafetyFilterError(f"{role} must contain two values")
    return (finite(value[0], f"{role}[0]"), finite(value[1], f"{role}[1]"))


def _bounds(value: Sequence[float], role: str) -> tuple[float, float]:
    result = _pair(value, role)
    if result[0] > result[1]:
        raise SafetyFilterError(f"{role} lower bound exceeds upper bound")
    return result


def _uncertainty(value: float | Sequence[float], role: str) -> tuple[float, float]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        pair = _pair(value, role)
    else:
        number = finite(value, role)
        pair = (number, number)
    if pair[0] < 0.0 or pair[1] < 0.0:
        raise SafetyFilterError(f"{role} must not be negative")
    return pair


@dataclass(frozen=True)
class SafetyFilterConfig:
    ellipse_axes_m: tuple[float, float] = (0.025, 0.015)
    tracking_error_bound_m: float | tuple[float, float] = 0.001
    latency_error_bound_m: float | tuple[float, float] = 0.001
    frame_error_bound_m: float | tuple[float, float] = 0.001
    velocity_min_m_s: tuple[float, float] = (-0.15, -0.15)
    velocity_max_m_s: tuple[float, float] = (0.15, 0.15)
    acceleration_min_m_s2: tuple[float, float] = (-1.0, -1.0)
    acceleration_max_m_s2: tuple[float, float] = (1.0, 1.0)
    dt_s: float = FILTER_DT_S
    max_state_age_s: float = 0.020
    alpha_s_inv: float = 2.0
    engage_deadband: float = 0.67
    margin_tolerance: float = 1e-10
    schema: str = SAFETY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SAFETY_SCHEMA:
            raise SafetyFilterError("safety filter schema differs")
        dt = finite(self.dt_s, "dt_s")
        if not math.isclose(dt, FILTER_DT_S, rel_tol=0.0, abs_tol=1e-15):
            raise SafetyFilterError("R011 safety filter dt must be exactly 2 ms")
        axes = _pair(self.ellipse_axes_m, "ellipse_axes_m")
        if axes[0] <= 0.0 or axes[1] <= 0.0:
            raise SafetyFilterError("ellipse axes must be positive")
        for role, value in (
            ("tracking_error_bound_m", self.tracking_error_bound_m),
            ("latency_error_bound_m", self.latency_error_bound_m),
            ("frame_error_bound_m", self.frame_error_bound_m),
        ):
            _uncertainty(value, role)
        velocity_min = _pair(self.velocity_min_m_s, "velocity_min_m_s")
        velocity_max = _pair(self.velocity_max_m_s, "velocity_max_m_s")
        acceleration_min = _pair(self.acceleration_min_m_s2, "acceleration_min_m_s2")
        acceleration_max = _pair(self.acceleration_max_m_s2, "acceleration_max_m_s2")
        if velocity_min[0] > velocity_max[0] or velocity_min[1] > velocity_max[1]:
            raise SafetyFilterError("velocity boxes do not overlap")
        if acceleration_min[0] > acceleration_max[0] or acceleration_min[1] > acceleration_max[1]:
            raise SafetyFilterError("acceleration boxes do not overlap")
        if finite(self.max_state_age_s, "max_state_age_s") < 0.0 or finite(self.margin_tolerance, "margin_tolerance") < 0.0 or finite(self.alpha_s_inv, "alpha_s_inv") != 2.0 or not 0.0 < finite(self.engage_deadband, "engage_deadband") < 1.0:
            raise SafetyFilterError("safety age/margin bounds are invalid")

    @property
    def tightened_axes_m(self) -> tuple[float, float]:
        axes = _pair(self.ellipse_axes_m, "ellipse_axes_m")
        tracking = _uncertainty(self.tracking_error_bound_m, "tracking_error_bound_m")
        latency = _uncertainty(self.latency_error_bound_m, "latency_error_bound_m")
        frame = _uncertainty(self.frame_error_bound_m, "frame_error_bound_m")
        return (axes[0] - tracking[0] - latency[0] - frame[0], axes[1] - tracking[1] - latency[1] - frame[1])

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "dt_s": self.dt_s,
            "ellipse_axes_m": list(self.ellipse_axes_m),
            "tightening": {
                "tracking_error_bound_m": json_tree(self.tracking_error_bound_m),
                "latency_error_bound_m": json_tree(self.latency_error_bound_m),
                "frame_error_bound_m": json_tree(self.frame_error_bound_m),
                "rule": "componentwise_sum_subtracted_from_ellipse_axes",
            },
            "tightened_axes_m": list(self.tightened_axes_m),
            "velocity_min_m_s": list(self.velocity_min_m_s),
            "velocity_max_m_s": list(self.velocity_max_m_s),
            "acceleration_min_m_s2": list(self.acceleration_min_m_s2),
            "acceleration_max_m_s2": list(self.acceleration_max_m_s2),
            "previous_command_bound": "required_input",
            "scope": "2-D PATH-frame translational command only; z/angular nominal preserved",
            "barrier": {"h": "-phi", "phi": "sum((e_xy/axis)^2)-1", "alpha_s_inv": self.alpha_s_inv, "engage_deadband": self.engage_deadband, "error_dynamics": "e_dot=path_frame(command_xy-reference_velocity_xy)", "projection": "minimal_distance_intersection_cbf_one_step_ellipse_velocity_acceleration"},
            "force_cbf_claim": False,
        }


@dataclass(frozen=True)
class SafetyFilterResult:
    valid: bool
    command_m_s: tuple[float, float]
    remaining_margin: float | None
    active_set: tuple[str, ...]
    iterations: int
    intervention_norm_m_s: float
    elapsed_time_s: float
    reason: str | None = None
    schema: str = SAFETY_SCHEMA
    cbf_residual: float | None = None
    error_xy_m: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if self.schema != SAFETY_SCHEMA or not isinstance(self.valid, bool):
            raise SafetyFilterError("safety result schema/validity differs")
        if not isinstance(self.command_m_s, tuple) or len(self.command_m_s) != 2:
            raise SafetyFilterError("safety result command must be PATH XY")
        for value in self.command_m_s: finite(value, "safety result command")
        if self.remaining_margin is not None:
            margin = finite(self.remaining_margin, "remaining_margin")
            if margin < 0.0: raise SafetyFilterError("remaining margin must be non-negative")
        if not isinstance(self.active_set, tuple) or any(not isinstance(value, str) or not value for value in self.active_set) or len(set(self.active_set)) != len(self.active_set):
            raise SafetyFilterError("safety result active set differs")
        if isinstance(self.iterations, bool) or not isinstance(self.iterations, int) or not 0 <= self.iterations <= MAX_ROOT_ITERATIONS:
            raise SafetyFilterError("safety result iterations are out of bounds")
        if finite(self.intervention_norm_m_s, "intervention_norm_m_s") < 0.0 or finite(self.elapsed_time_s, "elapsed_time_s") < 0.0:
            raise SafetyFilterError("safety result timing/intervention is invalid")
        if self.cbf_residual is not None:
            finite(self.cbf_residual, "cbf_residual")
        if self.error_xy_m is not None:
            if len(self.error_xy_m) != 2:
                raise SafetyFilterError("error_xy_m must be PATH XY")
            for value in self.error_xy_m:
                finite(value, "error_xy_m")
        if self.valid:
            if self.remaining_margin is None or self.reason is not None: raise SafetyFilterError("valid safety result must carry finite margin and no reason")
        else:
            if self.remaining_margin is not None or not isinstance(self.reason, str) or not self.reason: raise SafetyFilterError("failed safety result must be canonical")

    @property
    def admitted(self) -> bool:
        return self.valid

    @property
    def feasible(self) -> bool:
        return self.valid

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "valid": self.valid,
            "feasible": self.valid,
            "command_m_s": list(self.command_m_s),
            "remaining_margin": self.remaining_margin,
            "active_set": list(self.active_set),
            "iterations": self.iterations,
            "intervention_norm_m_s": self.intervention_norm_m_s,
            "elapsed_time_s": self.elapsed_time_s,
            "reason": self.reason,
            "scope": "2-D PATH-frame only",
            "cbf_residual": self.cbf_residual,
            "error_xy_m": None if self.error_xy_m is None else list(self.error_xy_m),
            "force_cbf_claim": False,
        }


@dataclass(frozen=True)
class SafetyIntervention:
    campaign_id: str
    run_id: str
    attempt_id: str
    dispatch_id: str
    nominal_command_m_s: tuple[float, float]
    filtered_command_m_s: tuple[float, float]
    intervention_norm_m_s: float
    remaining_margin: float | None
    objective_penalty: bool = False
    force_objective_impact: bool = False
    schema: str = INTERVENTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != INTERVENTION_SCHEMA or self.objective_penalty or self.force_objective_impact:
            raise SafetyFilterError("safety intervention is not a separate no-penalty event")
        if not all(isinstance(value, str) and value for value in (self.campaign_id, self.run_id, self.attempt_id, self.dispatch_id)):
            raise SafetyFilterError("intervention dispatch identity is invalid")
        if len(self.nominal_command_m_s) != 2 or len(self.filtered_command_m_s) != 2:
            raise SafetyFilterError("intervention commands must be PATH XY")
        for value in tuple(self.nominal_command_m_s) + tuple(self.filtered_command_m_s): finite(value, "intervention command")
        finite(self.intervention_norm_m_s, "intervention_norm_m_s")
        if self.remaining_margin is not None: finite(self.remaining_margin, "remaining_margin")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SafetyIntervention":
        required = {"schema", "campaign_id", "run_id", "attempt_id", "dispatch_id", "nominal_command_m_s", "filtered_command_m_s", "intervention_norm_m_s", "remaining_margin", "objective_penalty", "force_objective_impact", "record_role"}
        if not isinstance(value, Mapping) or set(value) != required or value.get("record_role") != "safety_event_not_objective":
            raise SafetyFilterError("safety intervention fields differ")
        if not isinstance(value.get("objective_penalty"), bool) or not isinstance(value.get("force_objective_impact"), bool):
            raise SafetyFilterError("safety intervention flags must be strict booleans")
        for key in ("nominal_command_m_s", "filtered_command_m_s"):
            if not isinstance(value.get(key), (list, tuple)) or len(value[key]) != 2:
                raise SafetyFilterError("safety intervention commands must be two-element arrays")
        return cls(value["campaign_id"], value["run_id"], value["attempt_id"], value["dispatch_id"], tuple(value["nominal_command_m_s"]), tuple(value["filtered_command_m_s"]), value["intervention_norm_m_s"], value["remaining_margin"], value["objective_penalty"], value["force_objective_impact"], value["schema"])

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "campaign_id": self.campaign_id, "run_id": self.run_id, "attempt_id": self.attempt_id, "dispatch_id": self.dispatch_id, "nominal_command_m_s": list(self.nominal_command_m_s), "filtered_command_m_s": list(self.filtered_command_m_s), "intervention_norm_m_s": self.intervention_norm_m_s, "remaining_margin": self.remaining_margin, "objective_penalty": False, "force_objective_impact": False, "record_role": "safety_event_not_objective"}


def validate_safety_intervention(value: SafetyIntervention | Mapping[str, Any]) -> SafetyIntervention:
    if isinstance(value, SafetyIntervention): return value
    return SafetyIntervention.from_mapping(value)


@dataclass(frozen=True)
class SafetyTimingReceipt:
    p99_s: float
    max_s: float
    host_qualified: bool
    qualified: bool
    algorithm_bound_ok: bool
    sample_count: int
    schema: str = TIMING_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != TIMING_SCHEMA or not isinstance(self.host_qualified, bool) or not isinstance(self.qualified, bool) or self.sample_count <= 0:
            raise SafetyFilterError("safety timing receipt fields differ")
        finite(self.p99_s, "p99_s"); finite(self.max_s, "max_s")
        if self.qualified != (self.host_qualified and self.p99_s <= 0.0005 and self.max_s <= 0.001):
            raise SafetyFilterError("safety timing qualification gate differs")

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "p99_s": self.p99_s, "max_s": self.max_s, "host_qualified": self.host_qualified, "qualified": self.qualified, "algorithm_bound_ok": self.algorithm_bound_ok, "sample_count": self.sample_count, "gate": {"p99_le_0.5ms": self.p99_s <= 0.0005, "max_le_1ms": self.max_s <= 0.001}}


def _ellipse_value(position: tuple[float, float], velocity: tuple[float, float], dt: float, axes: tuple[float, float]) -> float:
    return sum(((position[index] + dt * velocity[index]) / axes[index]) ** 2 for index in range(2))


def _solve_face(
    nominal: tuple[float, float],
    position: tuple[float, float],
    dt: float,
    axes: tuple[float, float],
    lower: tuple[float, float],
    upper: tuple[float, float],
    status: tuple[int, int],
) -> tuple[tuple[float, float], int] | None:
    candidate = [0.0, 0.0]
    free = []
    for index, state in enumerate(status):
        if state < 0:
            candidate[index] = lower[index]
        elif state > 0:
            candidate[index] = upper[index]
        else:
            candidate[index] = nominal[index]
            free.append(index)
    if _ellipse_value(position, (candidate[0], candidate[1]), dt, axes) > 1.0:
        if not free:
            return None
        def at(lam: float) -> tuple[float, float]:
            value = list(candidate)
            for index in free:
                denominator = 1.0 + lam * dt * dt / (axes[index] * axes[index])
                value[index] = (
                    nominal[index] - lam * dt * position[index] / (axes[index] * axes[index])
                ) / denominator
            return (value[0], value[1])

        lo, hi = 0.0, 1.0
        for _ in range(MAX_ROOT_ITERATIONS):
            if _ellipse_value(position, at(hi), dt, axes) <= 1.0:
                break
            hi *= 2.0
        else:
            return None
        iterations = 0
        for _ in range(MAX_ROOT_ITERATIONS):
            iterations += 1
            mid = (lo + hi) * 0.5
            if _ellipse_value(position, at(mid), dt, axes) > 1.0:
                lo = mid
            else:
                hi = mid
        candidate = list(at(hi))
    else:
        iterations = 0
    result = (float(candidate[0]), float(candidate[1]))
    tolerance = 1e-9
    if any(result[index] < lower[index] - tolerance or result[index] > upper[index] + tolerance for index in range(2)):
        return None
    if _ellipse_value(position, result, dt, axes) > 1.0 + 1e-9:
        return None
    return result, iterations


def _invalid_result(start: float, previous: tuple[float, float], reason: str) -> SafetyFilterResult:
    return SafetyFilterResult(False, previous, None, (), 0, 0.0, time.perf_counter() - start, reason)


def project_path_command(
    path_position_m: Sequence[float],
    nominal_command_m_s: Sequence[float],
    previous_command_m_s: Sequence[float],
    *,
    state_age_s: float,
    config: SafetyFilterConfig = SafetyFilterConfig(),
) -> SafetyFilterResult:
    """Project one 2-D command onto the exact tightened one-step feasible set."""

    started = time.perf_counter()
    try:
        position = _pair(path_position_m, "path_position_m")
        nominal = _pair(nominal_command_m_s, "nominal_command_m_s")
        previous = _pair(previous_command_m_s, "previous_command_m_s")
        age = finite(state_age_s, "state_age_s")
        if age < 0.0 or age > config.max_state_age_s:
            return _invalid_result(started, previous, "stale_state")
        axes = config.tightened_axes_m
        if any(axis <= 0.0 for axis in axes):
            return _invalid_result(started, previous, "invalid_tightened_axes")
        velocity_lower = _pair(config.velocity_min_m_s, "velocity_min_m_s")
        velocity_upper = _pair(config.velocity_max_m_s, "velocity_max_m_s")
        acceleration_min = _pair(config.acceleration_min_m_s2, "acceleration_min_m_s2")
        acceleration_max = _pair(config.acceleration_max_m_s2, "acceleration_max_m_s2")
        lower = tuple(max(velocity_lower[i], previous[i] + acceleration_min[i] * config.dt_s) for i in range(2))
        upper = tuple(min(velocity_upper[i], previous[i] + acceleration_max[i] * config.dt_s) for i in range(2))
        if any(lower[i] > upper[i] for i in range(2)):
            return _invalid_result(started, previous, "infeasible_velocity_acceleration_box")
        candidates: list[tuple[float, int, tuple[str, ...], tuple[float, float], int]] = []
        for status in itertools.product(_STATUSES, repeat=2):
            solved = _solve_face(nominal, position, config.dt_s, axes, lower, upper, status)
            if solved is None:
                continue
            command, iterations = solved
            labels = tuple(
                ("face_lower_x" if status[0] < 0 else "face_upper_x" if status[0] > 0 else "free_x",
                 "face_lower_y" if status[1] < 0 else "face_upper_y" if status[1] > 0 else "free_y")
            )
            distance = sum((command[i] - nominal[i]) ** 2 for i in range(2))
            candidates.append((distance, status[0] * 3 + status[1], labels, command, iterations))
        if not candidates:
            return _invalid_result(started, previous, "infeasible_tightened_ellipse")
        _, _, _face, command, iterations = min(candidates, key=lambda item: (item[0], item[1]))
        margin = 1.0 - _ellipse_value(position, command, config.dt_s, axes)
        if margin < -config.margin_tolerance:
            return _invalid_result(started, previous, "margin_failure")
        margin = max(0.0, margin)
        intervention = math.hypot(command[0] - nominal[0], command[1] - nominal[1])
        active_labels: list[str] = []
        acceleration_min = _pair(config.acceleration_min_m_s2, "acceleration_min_m_s2")
        acceleration_max = _pair(config.acceleration_max_m_s2, "acceleration_max_m_s2")
        for index, axis in enumerate(("x", "y")):
            acceleration_lower = previous[index] + acceleration_min[index] * config.dt_s
            acceleration_upper = previous[index] + acceleration_max[index] * config.dt_s
            velocity_lower_i = velocity_lower[index]
            velocity_upper_i = velocity_upper[index]
            if abs(command[index] - velocity_lower_i) <= 1e-9:
                active_labels.append(f"velocity_lower_{axis}")
            if abs(command[index] - velocity_upper_i) <= 1e-9:
                active_labels.append(f"velocity_upper_{axis}")
            if abs(command[index] - acceleration_lower) <= 1e-9:
                active_labels.append(f"acceleration_lower_{axis}")
            if abs(command[index] - acceleration_upper) <= 1e-9:
                active_labels.append(f"acceleration_upper_{axis}")
        if iterations > 0 or margin <= 1e-9:
            active_labels.append("tightened_ellipsoid")
        return SafetyFilterResult(
            True, command, margin, tuple(active_labels), iterations, intervention,
            time.perf_counter() - started, None,
        )
    except (TypeError, ValueError, SafetyFilterError) as exc:
        try:
            previous = _pair(previous_command_m_s, "previous_command_m_s")
        except (TypeError, ValueError, SafetyFilterError):
            previous = (0.0, 0.0)
        return _invalid_result(started, previous, f"invalid_input:{exc}")


def filter_path_twist(
    path_position_m: Sequence[float],
    nominal_twist: Sequence[float],
    previous_path_command_m_s: Sequence[float],
    *,
    state_age_s: float,
    config: SafetyFilterConfig = SafetyFilterConfig(),
) -> tuple[tuple[float, ...], SafetyFilterResult]:
    """Filter PATH x/y only and preserve z/angular nominal components exactly."""

    if not isinstance(nominal_twist, Sequence) or len(nominal_twist) != 6:
        raise SafetyFilterError("nominal_twist must contain six components")
    result = project_path_command(
        path_position_m, nominal_twist[:2], previous_path_command_m_s,
        state_age_s=state_age_s, config=config,
    )
    output = tuple(result.command_m_s) + tuple(finite(value, "nominal_twist") for value in nominal_twist[2:])
    return output, result


def reference_project_path_command(
    path_position_m: Sequence[float],
    nominal_command_m_s: Sequence[float],
    previous_command_m_s: Sequence[float],
    *,
    config: SafetyFilterConfig = SafetyFilterConfig(),
) -> tuple[float, float] | None:
    """Independent, dense scalar-reference checker for tests and audit tooling."""

    position = _pair(path_position_m, "path_position_m")
    nominal = _pair(nominal_command_m_s, "nominal_command_m_s")
    previous = _pair(previous_command_m_s, "previous_command_m_s")
    axes = config.tightened_axes_m
    lower_v = _pair(config.velocity_min_m_s, "velocity_min_m_s")
    upper_v = _pair(config.velocity_max_m_s, "velocity_max_m_s")
    a_min = _pair(config.acceleration_min_m_s2, "acceleration_min_m_s2")
    a_max = _pair(config.acceleration_max_m_s2, "acceleration_max_m_s2")
    lower = tuple(max(lower_v[i], previous[i] + a_min[i] * config.dt_s) for i in range(2))
    upper = tuple(min(upper_v[i], previous[i] + a_max[i] * config.dt_s) for i in range(2))
    if any(lower[i] > upper[i] for i in range(2)):
        return None
    # Independent reference: enumerate faces and solve each free coordinate
    # with its own scalar bisection.  This intentionally does not call the
    # production _solve_face implementation.
    candidates = []
    for status in itertools.product(_STATUSES, repeat=2):
        command = [lower[i] if status[i] < 0 else upper[i] if status[i] > 0 else nominal[i] for i in range(2)]
        free = [i for i in range(2) if status[i] == 0]
        def value(lam: float) -> tuple[float, float]:
            answer = list(command)
            for index in free:
                a = config.dt_s * config.dt_s / (axes[index] * axes[index])
                b = config.dt_s * position[index] / (axes[index] * axes[index])
                answer[index] = (nominal[index] - lam * b) / (1.0 + lam * a)
            return (answer[0], answer[1])
        if _ellipse_value(position, (command[0], command[1]), config.dt_s, axes) > 1.0:
            if not free: continue
            lo, hi = 0.0, 1.0
            while _ellipse_value(position, value(hi), config.dt_s, axes) > 1.0 and hi < 2.0 ** 64: hi *= 2.0
            if hi >= 2.0 ** 64: continue
            for _ in range(MAX_ROOT_ITERATIONS):
                mid = (lo + hi) * 0.5
                if _ellipse_value(position, value(mid), config.dt_s, axes) > 1.0: lo = mid
                else: hi = mid
            command = list(value(hi))
        candidate = (float(command[0]), float(command[1]))
        if any(candidate[i] < lower[i] - 1e-9 or candidate[i] > upper[i] + 1e-9 for i in range(2)): continue
        if _ellipse_value(position, candidate, config.dt_s, axes) > 1.0 + 1e-9: continue
        candidates.append((sum((candidate[i] - nominal[i]) ** 2 for i in range(2)), candidate))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _error_ellipse_value(error_xy: tuple[float, float], e_dot: tuple[float, float], dt: float, axes: tuple[float, float]) -> float:
    return sum(((error_xy[index] + dt * e_dot[index]) / axes[index]) ** 2 for index in range(2))


def _barrier_terms(error_xy: tuple[float, float], axes: tuple[float, float], alpha: float) -> tuple[float, tuple[float, float]]:
    phi = sum((error_xy[index] / axes[index]) ** 2 for index in range(2)) - 1.0
    h = -phi
    gradient_h = tuple(-2.0 * error_xy[index] / (axes[index] * axes[index]) for index in range(2))
    return h, gradient_h


def _error_ellipse_projection(
    nominal: tuple[float, float], error_xy: tuple[float, float], dt: float, axes: tuple[float, float],
) -> tuple[tuple[float, float], int]:
    """Project onto the translated one-step ellipse with a scalar KKT root."""

    if _error_ellipse_value(error_xy, nominal, dt, axes) <= 1.0 + 1e-12:
        return nominal, 0

    def at(lam: float) -> tuple[float, float]:
        return tuple(
            (nominal[index] - lam * dt * error_xy[index] / (axes[index] * axes[index]))
            / (1.0 + lam * dt * dt / (axes[index] * axes[index]))
            for index in range(2)
        )

    lo, hi = 0.0, 1.0
    while _error_ellipse_value(error_xy, at(hi), dt, axes) > 1.0 and hi < 2.0 ** MAX_ROOT_ITERATIONS:
        hi *= 2.0
    if hi >= 2.0 ** MAX_ROOT_ITERATIONS:
        raise SafetyFilterError("one-step ellipse projection root is unbounded")
    for _ in range(MAX_ROOT_ITERATIONS):
        mid = (lo + hi) * 0.5
        if _error_ellipse_value(error_xy, at(mid), dt, axes) > 1.0:
            lo = mid
        else:
            hi = mid
    return at(hi), MAX_ROOT_ITERATIONS


def _error_projection(
    nominal: tuple[float, float], error_xy: tuple[float, float], axes: tuple[float, float],
    dt: float, lower: tuple[float, float], upper: tuple[float, float],
    gradient: tuple[float, float], cbf_bound: float,
) -> tuple[tuple[float, float], int] | None:
    """Exact 2-D Euclidean projection by enumerating KKT active sets.

    The feasible set is the intersection of a box, one linear CBF
    half-space, and a translated ellipse.  In two dimensions, a projection
    has at most two independent active boundaries; enumerating those
    boundaries gives a small deterministic candidate set.
    """

    # All linear constraints have the form a dot v <= b.
    constraints: list[tuple[tuple[float, float], float]] = [
        ((-1.0, 0.0), -lower[0]), ((1.0, 0.0), upper[0]),
        ((0.0, -1.0), -lower[1]), ((0.0, 1.0), upper[1]),
    ]
    # At e=0, grad(h)=0 and h=1, so the CBF inequality is strictly
    # redundant.  Do not insert its zero normal: projecting onto that face
    # would divide by zero and is not a mathematical constraint.
    if sum(value * value for value in gradient) > 1e-24:
        constraints.append(((-gradient[0], -gradient[1]), cbf_bound))
    tolerance = 1e-9

    def feasible(point: tuple[float, float]) -> bool:
        return (
            all(sum(a[index] * point[index] for index in range(2)) <= b + tolerance for a, b in constraints)
            and _error_ellipse_value(error_xy, point, dt, axes) <= 1.0 + tolerance
        )

    candidates: list[tuple[float, int, tuple[float, float]]] = []

    def add(point: tuple[float, float], iterations: int) -> None:
        if feasible(point):
            candidates.append((sum((point[index] - nominal[index]) ** 2 for index in range(2)), iterations, point))

    add(nominal, 0)
    # One active linear boundary.
    for normal, bound in constraints:
        norm2 = sum(value * value for value in normal)
        correction = (sum(normal[index] * nominal[index] for index in range(2)) - bound) / norm2
        add((nominal[0] - correction * normal[0], nominal[1] - correction * normal[1]), 0)

    try:
        ellipse_point, ellipse_iterations = _error_ellipse_projection(nominal, error_xy, dt, axes)
        add(ellipse_point, ellipse_iterations)
    except SafetyFilterError:
        pass

    # Two active linear boundaries.
    for left in range(len(constraints)):
        a, b = constraints[left]
        for right in range(left + 1, len(constraints)):
            c, d = constraints[right]
            determinant = a[0] * c[1] - a[1] * c[0]
            if abs(determinant) <= 1e-15:
                continue
            point = ((b * c[1] - a[1] * d) / determinant, (a[0] * d - b * c[0]) / determinant)
            add(point, 0)

    # One active linear boundary and the ellipse boundary.  Parameterize the
    # line and solve its quadratic intersection with the ellipse.
    for normal, bound in constraints:
        norm2 = sum(value * value for value in normal)
        origin = (normal[0] * bound / norm2, normal[1] * bound / norm2)
        direction = (-normal[1], normal[0])
        quadratic = sum((dt * direction[index] / axes[index]) ** 2 for index in range(2))
        linear = 2.0 * sum((error_xy[index] + dt * origin[index]) * dt * direction[index] / (axes[index] * axes[index]) for index in range(2))
        constant = _error_ellipse_value(error_xy, origin, dt, axes) - 1.0
        discriminant = linear * linear - 4.0 * quadratic * constant
        if discriminant < -tolerance:
            continue
        root = math.sqrt(max(0.0, discriminant))
        for parameter in ((-linear - root) / (2.0 * quadratic), (-linear + root) / (2.0 * quadratic)):
            add((origin[0] + parameter * direction[0], origin[1] + parameter * direction[1]), MAX_ROOT_ITERATIONS)

    if not candidates:
        return None
    distance, iterations, point = min(candidates, key=lambda item: (item[0], item[2]))
    del distance
    return point, iterations


def project_path_error_command(
    actual_xy_m: Sequence[float],
    reference_xy_m: Sequence[float],
    reference_velocity_xy_m_s: Sequence[float],
    nominal_command_xy_m_s: Sequence[float],
    previous_command_xy_m_s: Sequence[float],
    *,
    state_age_s: float,
    config: SafetyFilterConfig = SafetyFilterConfig(),
) -> SafetyFilterResult:
    """Project error velocity, then reconstruct absolute command velocity.

    ``e_xy = actual_xy - reference_xy`` and ``e_dot_nominal`` is computed in
    PATH coordinates before any barrier operation.  The public result carries
    the reconstructed command in base coordinates (the offline primitive uses
    an identity PATH frame), so a caller cannot accidentally treat absolute
    path velocity as error dynamics.
    """

    started = time.perf_counter()
    try:
        actual = _pair(actual_xy_m, "actual_xy_m")
        reference = _pair(reference_xy_m, "reference_xy_m")
        ref_velocity = _pair(reference_velocity_xy_m_s, "reference_velocity_xy_m_s")
        nominal_command = _pair(nominal_command_xy_m_s, "nominal_command_xy_m_s")
        previous = _pair(previous_command_xy_m_s, "previous_command_xy_m_s")
        age = finite(state_age_s, "state_age_s")
        if age < 0.0 or age > config.max_state_age_s:
            return _invalid_result(started, previous, "stale_state")
        axes = config.tightened_axes_m
        if any(axis <= 0.0 for axis in axes):
            return _invalid_result(started, previous, "invalid_tightened_axes")
        error = (actual[0] - reference[0], actual[1] - reference[1])
        current_value = sum((error[index] / axes[index]) ** 2 for index in range(2))
        if current_value > 1.0 + config.margin_tolerance:
            return _invalid_result(started, previous, "outside_tube")
        velocity_min = _pair(config.velocity_min_m_s, "velocity_min_m_s")
        velocity_max = _pair(config.velocity_max_m_s, "velocity_max_m_s")
        acceleration_min = _pair(config.acceleration_min_m_s2, "acceleration_min_m_s2")
        acceleration_max = _pair(config.acceleration_max_m_s2, "acceleration_max_m_s2")
        command_lower = tuple(max(velocity_min[index], previous[index] + acceleration_min[index] * config.dt_s) for index in range(2))
        command_upper = tuple(min(velocity_max[index], previous[index] + acceleration_max[index] * config.dt_s) for index in range(2))
        if any(command_lower[index] > command_upper[index] for index in range(2)):
            return _invalid_result(started, previous, "infeasible_velocity_acceleration_box")
        e_lower = tuple(command_lower[index] - ref_velocity[index] for index in range(2))
        e_upper = tuple(command_upper[index] - ref_velocity[index] for index in range(2))
        nominal_e_dot = (nominal_command[0] - ref_velocity[0], nominal_command[1] - ref_velocity[1])
        h, gradient = _barrier_terms(error, axes, config.alpha_s_inv)
        # Deep inside the tube the filter is byte-for-byte a no-op, subject to
        # the pre-existing bounded velocity/acceleration policy.
        nominal_feasible = all(e_lower[index] <= nominal_e_dot[index] <= e_upper[index] for index in range(2)) and _error_ellipse_value(error, nominal_e_dot, config.dt_s, axes) <= 1.0 + config.margin_tolerance and sum(gradient[index] * nominal_e_dot[index] for index in range(2)) + config.alpha_s_inv * h >= -config.margin_tolerance
        deep_interior = current_value <= config.engage_deadband * config.engage_deadband
        if deep_interior and nominal_feasible:
            return SafetyFilterResult(True, nominal_command, 1.0 - _error_ellipse_value(error, nominal_e_dot, config.dt_s, axes), (), 0, 0.0, time.perf_counter() - started, None, SAFETY_SCHEMA, sum(gradient[index] * nominal_e_dot[index] for index in range(2)) + config.alpha_s_inv * h, error)

        projected = _error_projection(nominal_e_dot, error, axes, config.dt_s, tuple(e_lower), tuple(e_upper), gradient, config.alpha_s_inv * h)
        if projected is None:
            return _invalid_result(started, previous, "infeasible_cbf_tube_projection")
        filtered_e_dot, iteration_count = projected
        cbf_residual = sum(gradient[index] * filtered_e_dot[index] for index in range(2)) + config.alpha_s_inv * h
        ellipse = _error_ellipse_value(error, filtered_e_dot, config.dt_s, axes)
        if cbf_residual < -1e-8 or ellipse > 1.0 + 1e-8 or any(filtered_e_dot[index] < e_lower[index] - 1e-8 or filtered_e_dot[index] > e_upper[index] + 1e-8 for index in range(2)):
            return _invalid_result(started, previous, "infeasible_cbf_tube_projection")
        command = (ref_velocity[0] + filtered_e_dot[0], ref_velocity[1] + filtered_e_dot[1])
        active = ["path_error_cbf"] if max(abs(command[index] - nominal_command[index]) for index in range(2)) > 1e-12 else []
        if ellipse >= 1.0 - 1e-8:
            active.append("tightened_ellipsoid")
        if abs(cbf_residual) <= 1e-8:
            active.append("cbf_halfspace")
        return SafetyFilterResult(True, command, max(0.0, 1.0 - ellipse), tuple(dict.fromkeys(active)), iteration_count, math.hypot(command[0] - nominal_command[0], command[1] - nominal_command[1]), time.perf_counter() - started, None, SAFETY_SCHEMA, cbf_residual, error)
    except (TypeError, ValueError, SafetyFilterError) as exc:
        try:
            previous = _pair(previous_command_xy_m_s, "previous_command_xy_m_s")
        except (TypeError, ValueError, SafetyFilterError):
            previous = (0.0, 0.0)
        return _invalid_result(started, previous, f"invalid_input:{exc}")


def reference_project_path_error_command(
    actual_xy_m: Sequence[float], reference_xy_m: Sequence[float], reference_velocity_xy_m_s: Sequence[float],
    nominal_command_xy_m_s: Sequence[float], previous_command_xy_m_s: Sequence[float], *,
    state_age_s: float = 0.0, config: SafetyFilterConfig = SafetyFilterConfig(), grid_points: int = 65,
) -> tuple[float, float] | None:
    """Independent dense feasibility oracle for the PATH-error composition.

    This deliberately works in error-velocity coordinates and uses a dense
    deterministic grid plus local refinement.  It does not call the active
    set implementation or the legacy absolute-path reference helper.
    """

    actual = _pair(actual_xy_m, "actual_xy_m"); reference = _pair(reference_xy_m, "reference_xy_m")
    ref_velocity = _pair(reference_velocity_xy_m_s, "reference_velocity_xy_m_s"); nominal = _pair(nominal_command_xy_m_s, "nominal_command_xy_m_s"); previous = _pair(previous_command_xy_m_s, "previous_command_xy_m_s")
    if finite(state_age_s, "state_age_s") > config.max_state_age_s:
        return None
    axes = config.tightened_axes_m; error = (actual[0] - reference[0], actual[1] - reference[1])
    if _error_ellipse_value(error, (0.0, 0.0), 0.0, axes) > 1.0 + config.margin_tolerance:
        return None
    vmin = _pair(config.velocity_min_m_s, "velocity_min_m_s"); vmax = _pair(config.velocity_max_m_s, "velocity_max_m_s")
    amin = _pair(config.acceleration_min_m_s2, "acceleration_min_m_s2"); amax = _pair(config.acceleration_max_m_s2, "acceleration_max_m_s2")
    command_lo = tuple(max(vmin[i], previous[i] + amin[i] * config.dt_s) for i in range(2)); command_hi = tuple(min(vmax[i], previous[i] + amax[i] * config.dt_s) for i in range(2))
    if any(command_lo[i] > command_hi[i] for i in range(2)): return None
    lo = (command_lo[0] - ref_velocity[0], command_lo[1] - ref_velocity[1]); hi = (command_hi[0] - ref_velocity[0], command_hi[1] - ref_velocity[1]); target = (nominal[0] - ref_velocity[0], nominal[1] - ref_velocity[1])
    h, gradient = _barrier_terms(error, axes, config.alpha_s_inv); cbf_bound = config.alpha_s_inv * h
    def feasible(point: tuple[float, float]) -> bool:
        return (lo[0] - 1e-9 <= point[0] <= hi[0] + 1e-9 and lo[1] - 1e-9 <= point[1] <= hi[1] + 1e-9 and _error_ellipse_value(error, point, config.dt_s, axes) <= 1.0 + 1e-8 and (sum(gradient[i] * point[i] for i in range(2)) + cbf_bound >= -1e-8 if sum(value * value for value in gradient) > 1e-24 else True))
    if feasible(target): return nominal
    points = max(9, int(grid_points)); best: tuple[float, float] | None = None; best_distance = float("inf")
    for ix in range(points):
        x = lo[0] + (hi[0] - lo[0]) * ix / (points - 1)
        for iy in range(points):
            y = lo[1] + (hi[1] - lo[1]) * iy / (points - 1); point = (x, y)
            if feasible(point):
                distance = sum((point[i] - target[i]) ** 2 for i in range(2))
                if distance < best_distance: best, best_distance = point, distance
    if best is None: return None
    # Deterministic local dense refinement around the best grid cell.
    span = ((hi[0] - lo[0]) / (points - 1), (hi[1] - lo[1]) / (points - 1))
    for _ in range(4):
        candidates = []
        for ix in range(-4, 5):
            for iy in range(-4, 5):
                point = (best[0] + ix * span[0] / 4.0, best[1] + iy * span[1] / 4.0)
                if feasible(point): candidates.append((sum((point[i] - target[i]) ** 2 for i in range(2)), point))
        if candidates: best = min(candidates, key=lambda item: item[0])[1]
        span = (span[0] / 4.0, span[1] / 4.0)
    return (ref_velocity[0] + best[0], ref_velocity[1] + best[1])


def filter_path_error_twist(
    actual_xy_m: Sequence[float],
    reference_xy_m: Sequence[float],
    reference_velocity_xy_m_s: Sequence[float],
    nominal_twist: Sequence[float],
    previous_command_xy_m_s: Sequence[float],
    *,
    state_age_s: float,
    config: SafetyFilterConfig = SafetyFilterConfig(),
) -> tuple[tuple[float, ...], SafetyFilterResult]:
    if not isinstance(nominal_twist, Sequence) or len(nominal_twist) != 6:
        raise SafetyFilterError("nominal_twist must contain six components")
    result = project_path_error_command(actual_xy_m, reference_xy_m, reference_velocity_xy_m_s, nominal_twist[:2], previous_command_xy_m_s, state_age_s=state_age_s, config=config)
    return tuple(result.command_m_s) + tuple(finite(value, "nominal_twist") for value in nominal_twist[2:]), result


def benchmark_safety_filter(*, sample_count: int = 300, config: SafetyFilterConfig = SafetyFilterConfig(), host_qualified: bool = False) -> SafetyTimingReceipt:
    if isinstance(sample_count, bool) or sample_count <= 0: raise SafetyFilterError("sample_count must be positive")
    durations = []
    for _ in range(sample_count):
        result = project_path_command((0.001, -0.002), (0.04, 0.03), (0.0, 0.0), state_age_s=0.0, config=config)
        if not result.valid: raise SafetyFilterError("timing benchmark encountered invalid filter result")
        durations.append(result.elapsed_time_s)
    ordered = sorted(durations); p99 = ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))]
    maximum = max(ordered)
    return SafetyTimingReceipt(p99, maximum, host_qualified, host_qualified and p99 <= 0.0005 and maximum <= 0.001, MAX_ROOT_ITERATIONS <= 64, sample_count)


def benchmark_path_error_filter(*, sample_count: int = 300, config: SafetyFilterConfig = SafetyFilterConfig(), host_qualified: bool = False) -> SafetyTimingReceipt:
    """Measure the R011 PATH-error composition, including reconstruction."""

    if isinstance(sample_count, bool) or sample_count <= 0:
        raise SafetyFilterError("sample_count must be positive")
    durations = []
    for _ in range(sample_count):
        result = project_path_error_command(
            (0.0, 0.0), (0.0, 0.0), (0.01, -0.01), (0.02, -0.02), (0.02, -0.02),
            state_age_s=0.0, config=config,
        )
        if not result.valid:
            raise SafetyFilterError("PATH-error timing benchmark encountered invalid filter result")
        durations.append(result.elapsed_time_s)
    ordered = sorted(durations)
    p99 = ordered[min(len(ordered) - 1, int(0.99 * len(ordered)))]
    maximum = max(ordered)
    return SafetyTimingReceipt(p99, maximum, host_qualified, host_qualified and p99 <= 0.0005 and maximum <= 0.001, MAX_ROOT_ITERATIONS <= 64, sample_count)


__all__ = [
    "FILTER_DT_S", "SAFETY_SCHEMA", "SafetyFilterConfig", "SafetyFilterError",
    "INTERVENTION_SCHEMA", "MAX_ROOT_ITERATIONS", "SafetyFilterResult", "SafetyIntervention", "SafetyTimingReceipt", "benchmark_path_error_filter", "benchmark_safety_filter", "filter_path_error_twist", "filter_path_twist", "project_path_command", "project_path_error_command", "reference_project_path_command", "reference_project_path_error_command",
]
