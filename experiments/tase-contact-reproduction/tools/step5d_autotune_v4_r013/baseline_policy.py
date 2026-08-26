"""R013-only baseline transition and residual projection contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Mapping, Sequence

from .feedforward import FeedforwardProfile


R013_BASELINE_RESIDUAL_POLICY_SCHEMA = (
    "step5d.autotune-v4/r013-baseline-residual-policy-v1"
)
R013_BASELINE_RESIDUAL_POLICY_ID = "r013-baseline-residual-projection-v1"
R013_BASELINE_TRANSITION_SCHEMA = (
    "step5d.autotune-v4/r013-baseline-transition-profile-v1"
)
R013_BASELINE_TRANSITION_PROFILE_ID = "r013-baseline-transition-v1"
R013_BASELINE_RESIDUAL_POLICY_VERSION = 1
R013_BASELINE_TRANSITION_VERSION = 1
R013_BASELINE_TANGENTIAL_LIMIT_M_S = 2e-6
R013_BASELINE_ANGULAR_LIMIT_RAD_S = 2e-6
R013_BASELINE_RAMP_S = 8.0
R013_HARD_ABS_NORMAL_N = 60.0
R013_HARD_FORCE_N = 100.0
R013_HARD_TORQUE_NM = 3.0


class R013BaselinePolicyError(ValueError):
    """An R013-only typed policy input or result is invalid."""


class ResidualDisposition(str, Enum):
    UNCHANGED = "UNCHANGED"
    PROJECTED = "PROJECTED"
    ZERO_FAIL_CLOSED = "ZERO_FAIL_CLOSED"


def _six(values: Sequence[float], name: str) -> tuple[float, ...]:
    if len(values) != 6:
        raise R013BaselinePolicyError(f"{name} must contain six values")
    result = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in result):
        raise R013BaselinePolicyError(f"{name} must be finite")
    return result


def _matrix(values: Sequence[Sequence[float]]) -> tuple[tuple[float, ...], ...]:
    if len(values) != 6 or any(len(row) != 6 for row in values):
        raise R013BaselinePolicyError("calibrated Jacobian must be 6x6")
    result = tuple(tuple(float(value) for value in row) for row in values)
    if not all(math.isfinite(value) for row in result for value in row):
        raise R013BaselinePolicyError("calibrated Jacobian must be finite")
    return result


def _normal(values: Sequence[float]) -> tuple[float, float, float]:
    if len(values) != 3:
        raise R013BaselinePolicyError("base reaction normal must contain three values")
    result = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in result):
        raise R013BaselinePolicyError("base reaction normal must be finite")
    norm = math.sqrt(sum(value * value for value in result))
    if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
        raise R013BaselinePolicyError("base reaction normal must be unit length")
    return result  # type: ignore[return-value]


def _twist(
    jacobian: tuple[tuple[float, ...], ...],
    qdot: tuple[float, ...],
) -> tuple[float, ...]:
    return tuple(
        sum(jacobian[row][column] * qdot[column] for column in range(6))
        for row in range(6)
    )


def _residual(
    jacobian: tuple[tuple[float, ...], ...],
    qdot: tuple[float, ...],
    normal: tuple[float, float, float],
) -> tuple[float, float, float]:
    twist = _twist(jacobian, qdot)
    signed_normal = sum(twist[index] * normal[index] for index in range(3))
    tangential = math.sqrt(
        sum(
            (twist[index] - signed_normal * normal[index]) ** 2
            for index in range(3)
        )
    )
    angular = math.sqrt(sum(value * value for value in twist[3:]))
    return signed_normal, tangential, angular


def _solve_6x6(
    matrix: tuple[tuple[float, ...], ...],
    vector: tuple[float, ...],
) -> tuple[float, ...]:
    """Solve one exact 6x6 linear system without introducing a new dependency."""

    augmented = [list(row) + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(6):
        pivot = max(range(column, 6), key=lambda index: abs(augmented[index][column]))
        if augmented[pivot][column] == 0.0:
            raise R013BaselinePolicyError("R013 baseline normal projection linear solve failed")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(6):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0.0:
                continue
            augmented[row] = [
                augmented[row][index] - factor * augmented[column][index]
                for index in range(7)
            ]
    result = tuple(row[6] for row in augmented)
    if not all(math.isfinite(value) for value in result):
        raise R013BaselinePolicyError("R013 baseline normal projection solve is nonfinite")
    return result


@dataclass(frozen=True)
class R013BaselineResidualResult:
    schema: str
    version: int
    policy_id: str
    disposition: ResidualDisposition
    qdot: tuple[float, ...]
    requested_normal_m_s: float
    achieved_normal_m_s: float
    tangential_m_s: float
    angular_rad_s: float
    strict_tangential_m_s: float
    strict_angular_rad_s: float
    reason: str = ""

    def __post_init__(self) -> None:
        if self.schema != R013_BASELINE_RESIDUAL_POLICY_SCHEMA or self.version != 1:
            raise R013BaselinePolicyError("R013 residual result schema/version differs")
        if self.policy_id != R013_BASELINE_RESIDUAL_POLICY_ID:
            raise R013BaselinePolicyError("R013 residual result policy identity differs")
        if not isinstance(self.disposition, ResidualDisposition):
            raise R013BaselinePolicyError("R013 residual disposition is not typed")
        object.__setattr__(self, "qdot", _six(self.qdot, "R013 residual qdot"))
        values = (
            self.requested_normal_m_s,
            self.achieved_normal_m_s,
            self.tangential_m_s,
            self.angular_rad_s,
            self.strict_tangential_m_s,
            self.strict_angular_rad_s,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise R013BaselinePolicyError("R013 residual diagnostics must be finite")
        if not isinstance(self.reason, str):
            raise R013BaselinePolicyError("R013 residual reason is not text")

    @property
    def failed_closed(self) -> bool:
        return self.disposition is ResidualDisposition.ZERO_FAIL_CLOSED

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "policy_id": self.policy_id,
            "disposition": self.disposition.value,
            "qdot": list(self.qdot),
            "requested_normal_m_s": self.requested_normal_m_s,
            "achieved_normal_m_s": self.achieved_normal_m_s,
            "tangential_m_s": self.tangential_m_s,
            "angular_rad_s": self.angular_rad_s,
            "strict_tangential_m_s": self.strict_tangential_m_s,
            "strict_angular_rad_s": self.strict_angular_rad_s,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class R013BaselineResidualPolicyV1:
    """Project only R013 baseline residuals; V4 gate authority remains final."""

    schema: str = R013_BASELINE_RESIDUAL_POLICY_SCHEMA
    version: int = R013_BASELINE_RESIDUAL_POLICY_VERSION
    policy_id: str = R013_BASELINE_RESIDUAL_POLICY_ID

    def __post_init__(self) -> None:
        if (
            self.schema != R013_BASELINE_RESIDUAL_POLICY_SCHEMA
            or type(self.version) is not int
            or self.version != R013_BASELINE_RESIDUAL_POLICY_VERSION
            or self.policy_id != R013_BASELINE_RESIDUAL_POLICY_ID
        ):
            raise R013BaselinePolicyError("R013 residual policy schema/version differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "policy_id": self.policy_id,
            "tangential_limit_m_s": R013_BASELINE_TANGENTIAL_LIMIT_M_S,
            "angular_limit_rad_s": R013_BASELINE_ANGULAR_LIMIT_RAD_S,
            "authority": "v4_rescale_slew_cap_final_gate_qdot",
        }

    def apply(
        self,
        *,
        contract: Any,
        strict_qdot: Sequence[float],
        jacobian_6x6: Sequence[Sequence[float]],
        normal_base: Sequence[float],
        previous_qdot: Sequence[float],
        actual_dt_s: float,
        observed_model_hashes: Mapping[str, str],
        motion_profile: Any,
    ) -> R013BaselineResidualResult:
        """Return a bounded command or an explicit zero/fail-closed result."""

        zero = (0.0,) * 6
        requested = 0.0
        strict_tangential = 0.0
        strict_angular = 0.0
        try:
            qdot = _six(strict_qdot, "strict-RNN qdot")
            previous = _six(previous_qdot, "previous published qdot")
            jacobian = _matrix(jacobian_6x6)
            normal = _normal(normal_base)
            dt = float(actual_dt_s)
            if not math.isfinite(dt) or not 0.0 < dt < 0.080:
                raise R013BaselinePolicyError("R013 baseline projection dt is invalid")
            if not isinstance(observed_model_hashes, Mapping):
                raise R013BaselinePolicyError("R013 observed model hashes are not typed")
            requested, strict_tangential, strict_angular = _residual(
                jacobian, qdot, normal
            )
            if not hasattr(contract, "model_hashes"):
                raise R013BaselinePolicyError("R013 V4 contract model hashes are missing")

            disposition = ResidualDisposition.UNCHANGED
            command = qdot
            if (
                strict_tangential > R013_BASELINE_TANGENTIAL_LIMIT_M_S
                or strict_angular > R013_BASELINE_ANGULAR_LIMIT_RAD_S
            ):
                pure_normal_twist = (
                    normal[0] * requested,
                    normal[1] * requested,
                    normal[2] * requested,
                    0.0,
                    0.0,
                    0.0,
                )
                command = _solve_6x6(jacobian, pure_normal_twist)
                disposition = ResidualDisposition.PROJECTED

            from step5d_autotune_v4_r004.runtime import (
                gate_qdot,
                rescale_qdot_to_gate,
            )

            ramped, _ = rescale_qdot_to_gate(
                contract,
                qdot=command,
                previous_qdot=previous,
                jacobian_6x6=jacobian,
                normal_base=normal,
                observed_model_hashes=observed_model_hashes,
                actual_dt_s=dt,
                motion_profile=motion_profile,
            )
            if not ramped.allowed:
                raise R013BaselinePolicyError(
                    f"R013 baseline projection authority rejected qdot: {ramped.reason}"
                )
            final = gate_qdot(
                contract,
                qdot=ramped.qdot,
                jacobian_6x6=jacobian,
                normal_base=normal,
                observed_model_hashes=observed_model_hashes,
                motion_profile=motion_profile,
            )
            if not final.allowed:
                raise R013BaselinePolicyError(
                    f"R013 baseline projection final gate rejected qdot: {final.reason}"
                )
            achieved, tangential, angular = _residual(jacobian, final.qdot, normal)
            if (
                tangential > R013_BASELINE_TANGENTIAL_LIMIT_M_S
                or angular > R013_BASELINE_ANGULAR_LIMIT_RAD_S
            ):
                raise R013BaselinePolicyError(
                    "R013 baseline projection final residual violates the fixed limit"
                )
            if disposition is ResidualDisposition.PROJECTED and not any(
                abs(value) > 0.0 for value in final.qdot
            ):
                raise R013BaselinePolicyError(
                    "R013 projected baseline command was reduced to zero"
                )
            return R013BaselineResidualResult(
                schema=self.schema,
                version=self.version,
                policy_id=self.policy_id,
                disposition=disposition,
                qdot=final.qdot,
                requested_normal_m_s=requested,
                achieved_normal_m_s=achieved,
                tangential_m_s=tangential,
                angular_rad_s=angular,
                strict_tangential_m_s=strict_tangential,
                strict_angular_rad_s=strict_angular,
            )
        except Exception as exc:
            return R013BaselineResidualResult(
                schema=self.schema,
                version=self.version,
                policy_id=self.policy_id,
                disposition=ResidualDisposition.ZERO_FAIL_CLOSED,
                qdot=zero,
                requested_normal_m_s=requested,
                achieved_normal_m_s=0.0,
                tangential_m_s=0.0,
                angular_rad_s=0.0,
                strict_tangential_m_s=strict_tangential,
                strict_angular_rad_s=strict_angular,
                reason=f"{type(exc).__name__}: {exc}",
            )


@dataclass(frozen=True)
class R013BaselineTransitionResult:
    schema: str
    version: int
    profile_id: str
    path_request_allowed: bool
    ramp_complete: bool
    sensor_fresh: bool
    timing_gate_passed: bool
    safety_normal: bool
    hard_limits_passed: bool
    narrow_readiness_passed: bool
    narrow_path_release_opened: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "profile_id": self.profile_id,
            "path_request_allowed": self.path_request_allowed,
            "ramp_complete": self.ramp_complete,
            "sensor_fresh": self.sensor_fresh,
            "timing_gate_passed": self.timing_gate_passed,
            "safety_normal": self.safety_normal,
            "hard_limits_passed": self.hard_limits_passed,
            "narrow_readiness_passed": self.narrow_readiness_passed,
            "narrow_path_release_opened": self.narrow_path_release_opened,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class R013BaselineTransitionProfileV1:
    """R013 opt-in transition after the canonical latch/ramp and hard gates."""

    schema: str = R013_BASELINE_TRANSITION_SCHEMA
    version: int = R013_BASELINE_TRANSITION_VERSION
    profile_id: str = R013_BASELINE_TRANSITION_PROFILE_ID

    def __post_init__(self) -> None:
        if (
            self.schema != R013_BASELINE_TRANSITION_SCHEMA
            or type(self.version) is not int
            or self.version != R013_BASELINE_TRANSITION_VERSION
            or self.profile_id != R013_BASELINE_TRANSITION_PROFILE_ID
        ):
            raise R013BaselinePolicyError("R013 transition profile schema/version differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "profile_id": self.profile_id,
            "post_latch_ramp_s": R013_BASELINE_RAMP_S,
            "hard_limits": {
                "abs_normal_n": R013_HARD_ABS_NORMAL_N,
                "force_norm_n": R013_HARD_FORCE_N,
                "torque_norm_nm": R013_HARD_TORQUE_NM,
            },
            "narrow_gates_telemetry_only": True,
            "tp_stationary_seam": "zero_qdot_until_state_25",
        }

    def evaluate(
        self,
        *,
        baseline_state: Any,
        observation: Any,
        timing_gate_passed: bool,
        safety_normal: bool,
        narrow_readiness_passed: bool = False,
        narrow_path_release_opened: bool = False,
    ) -> R013BaselineTransitionResult:
        values = (
            observation.dt_s,
            observation.filtered_normal_n,
            observation.raw_normal_n,
            observation.force_norm_n,
            observation.torque_norm_nm,
        )
        finite = all(math.isfinite(float(value)) for value in values)
        hard = bool(
            finite
            and observation.sensor_fresh
            and abs(float(observation.raw_normal_n)) < R013_HARD_ABS_NORMAL_N
            and float(observation.force_norm_n) < R013_HARD_FORCE_N
            and float(observation.torque_norm_nm) < R013_HARD_TORQUE_NM
        )
        ramp_complete = bool(
            observation.one_newton_latched
            and float(getattr(baseline_state, "after_latch_s", 0.0)) >= R013_BASELINE_RAMP_S
        )
        allowed = bool(ramp_complete and timing_gate_passed and safety_normal and hard)
        if allowed:
            reason = "r013_hard_transition_open"
        elif not ramp_complete:
            reason = "r013_latch_or_ramp_pending"
        elif not timing_gate_passed:
            reason = "r013_timing_gate_pending"
        elif not safety_normal:
            reason = "r013_safety_not_normal"
        elif not hard:
            reason = "r013_fresh_or_hard_limit_pending"
        else:
            reason = "r013_transition_blocked"
        return R013BaselineTransitionResult(
            schema=self.schema,
            version=self.version,
            profile_id=self.profile_id,
            path_request_allowed=allowed,
            ramp_complete=ramp_complete,
            sensor_fresh=bool(observation.sensor_fresh),
            timing_gate_passed=bool(timing_gate_passed),
            safety_normal=bool(safety_normal),
            hard_limits_passed=hard,
            narrow_readiness_passed=bool(narrow_readiness_passed),
            narrow_path_release_opened=bool(narrow_path_release_opened),
            reason=reason,
        )


@dataclass(frozen=True)
class MotionAdmissionDecisionV1:
    schema: str
    version: int
    profile_id: str
    accepted: bool
    motion_gate: bool
    reasons: tuple[str, ...]
    recorded_outcomes: Mapping[str, float | None]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "profile_id": self.profile_id,
            "accepted": self.accepted,
            "motion_gate": self.motion_gate,
            "reasons": list(self.reasons),
            "recorded_outcomes": dict(self.recorded_outcomes),
        }


def _value(result: Any, name: str, metrics: Mapping[str, Any]) -> Any:
    if isinstance(result, Mapping) and name in result:
        return result[name]
    if hasattr(result, name):
        return getattr(result, name)
    return metrics.get(name)


@dataclass(frozen=True)
class MotionAdmissionProfileV1:
    """Feedforward-bound admission; FF-off records tracking outcomes."""

    feedforward_profile: FeedforwardProfile
    schema: str = "step5d.autotune-v4/r013-motion-admission-profile-v1"
    version: int = 1
    profile_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.feedforward_profile, FeedforwardProfile):
            raise R013BaselinePolicyError("motion admission feedforward profile is not typed")
        expected = (
            "legacy-ff-on-v1"
            if self.feedforward_profile.enabled
            else "r013-ff-off-ablation-v1"
        )
        if self.schema != "step5d.autotune-v4/r013-motion-admission-profile-v1":
            raise R013BaselinePolicyError("motion admission schema differs")
        if type(self.version) is not int or self.version != 1 or self.profile_id not in {"", expected}:
            raise R013BaselinePolicyError("motion admission profile version/identity differs")
        object.__setattr__(self, "profile_id", expected)

    @classmethod
    def from_feedforward(cls, profile: FeedforwardProfile) -> "MotionAdmissionProfileV1":
        return cls(feedforward_profile=profile)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "profile_id": self.profile_id,
            "feedforward_profile": self.feedforward_profile.as_dict(),
            "xy_tracking_is_gate": self.feedforward_profile.enabled,
            "endpoint_tracking_is_gate": self.feedforward_profile.enabled,
            "integrity_gates": [
                "path_duration_phase",
                "qdot_correlation_lag",
                "hard_tube",
                "soft_tube",
                "timing",
                "identity",
                "return_home",
            ],
        }

    def evaluate(self, result: Any) -> MotionAdmissionDecisionV1:
        metrics = result.get("metrics", {}) if isinstance(result, Mapping) else getattr(result, "metrics", {})
        if not isinstance(metrics, Mapping):
            metrics = {}
        if self.feedforward_profile.enabled:
            accepted = bool(_value(result, "motion_gate", metrics))
            return MotionAdmissionDecisionV1(
                schema=self.schema,
                version=self.version,
                profile_id=self.profile_id,
                accepted=accepted,
                motion_gate=accepted,
                reasons=() if accepted else ("legacy_motion_gate=false",),
                recorded_outcomes={},
            )

        reasons: list[str] = []
        duration = _value(result, "duration_s", metrics)
        phase = metrics.get("path_phase", _value(result, "path_phase", metrics))
        correlation = _value(result, "qd_correlation", metrics)
        lag = _value(result, "qd_lag_s", metrics)
        if not isinstance(duration, (int, float)) or float(duration) < 60.0:
            reasons.append("path_duration")
        if phase != 6:
            reasons.append("path_phase")
        if not isinstance(correlation, (int, float)) or float(correlation) < 0.9:
            reasons.append("qdot_correlation")
        if not isinstance(lag, (int, float)) or float(lag) > 0.020:
            reasons.append("qdot_lag")
        for name in ("safety_gate", "contact_gate", "timing_gate", "identity_gate"):
            if _value(result, name, metrics) is not True:
                reasons.append(name)
        if _value(result, "safe_return", metrics) is not True or _value(result, "return_gate", metrics) is not True:
            reasons.append("return_home")
        if metrics.get("r013_force_lifecycle_complete") is not True:
            reasons.append("force_lifecycle")
        if _value(result, "hard_tube_passed", metrics) is not True:
            reasons.append("hard_tube")
        if _value(result, "soft_tube_passed", metrics) is not True:
            reasons.append("soft_tube")
        outcomes = {
            "xy_error_p95_m": metrics.get("xy_error_p95_m"),
            "xy_error_max_m": metrics.get("xy_error_max_m"),
            "endpoint_error_max_m": metrics.get("endpoint_error_max_m"),
        }
        return MotionAdmissionDecisionV1(
            schema=self.schema,
            version=self.version,
            profile_id=self.profile_id,
            accepted=not reasons,
            motion_gate=not reasons,
            reasons=tuple(reasons),
            recorded_outcomes=outcomes,
        )


__all__ = [
    "MotionAdmissionDecisionV1",
    "MotionAdmissionProfileV1",
    "R013BaselinePolicyError",
    "R013BaselineResidualPolicyV1",
    "R013BaselineResidualResult",
    "R013BaselineTransitionProfileV1",
    "R013BaselineTransitionResult",
    "ResidualDisposition",
    "R013_BASELINE_ANGULAR_LIMIT_RAD_S",
    "R013_BASELINE_RESIDUAL_POLICY_ID",
    "R013_BASELINE_RESIDUAL_POLICY_SCHEMA",
    "R013_BASELINE_TANGENTIAL_LIMIT_M_S",
    "R013_BASELINE_TRANSITION_PROFILE_ID",
    "R013_BASELINE_TRANSITION_SCHEMA",
]
