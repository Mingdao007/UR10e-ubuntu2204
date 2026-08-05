"""r004-owned PATH controller with an explicit typed V3 motion profile seam.

The shared ``step5d_autotune_v4`` controller remains frozen for r002/r003.
This namespaced copy is the only r004 production PATH owner: finite callers
without a profile retain the old compatibility envelope, while qualification
binds the validated r004 V3-effective profile explicitly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Sequence

from step5d_autotune_v4.contracts import (
    TARGET_FORCE_N,
    V4Candidate,
    V4ContractError,
    assert_runtime_target,
)

if TYPE_CHECKING:
    from .motion_profile import V4MotionProfile


PHYSICAL_FIELDS = (
    "force_p_gain",
    "force_i_gain",
    "force_damping",
    "normal_filter_tau_s",
    "orientation_ko",
    "motion_kp",
    "target_force_n",
)


class PathControllerError(RuntimeError):
    """An r004 PATH-controller invariant failed."""


def filter_alpha(actual_dt_s: float, tau_s: float) -> float:
    """Actual-dt first-order filter coefficient; never fixed at 2 ms."""

    dt = float(actual_dt_s)
    tau = float(tau_s)
    if not math.isfinite(dt) or dt <= 0.0:
        raise PathControllerError("actual_dt must be positive and finite")
    if not math.isfinite(tau) or tau <= 0.0:
        raise PathControllerError("normal_filter_tau_s must be positive and finite")
    return 1.0 - math.exp(-dt / tau)


def derive_force_terms(candidate: V4Candidate) -> dict[str, float]:
    """Map P/I/D onto the mature Md/kf/Bd outer-force terms."""

    assert_runtime_target(candidate, TARGET_FORCE_N)
    p = float(candidate.force_p_gain)
    i = float(candidate.force_i_gain)
    damping = float(candidate.force_damping)
    if not all(math.isfinite(value) for value in (p, i, damping)):
        raise PathControllerError("force P/I/damping must be finite")
    if p <= 0.0 or i < 0.0 or damping <= 0.0:
        raise PathControllerError("force terms require P>0, I>=0, damping>0")
    return {
        "P": p,
        "I": i,
        "damping": damping,
        "Md": 1.0 / p,
        "kf": i / p,
        "Bd": damping / p,
    }


def assert_candidate_fields_consumed(
    candidate: V4Candidate, consumed: Mapping[str, float]
) -> None:
    """Fail if any physical candidate field is transported but unused."""

    missing = [field for field in PHYSICAL_FIELDS if field not in consumed]
    if missing:
        raise PathControllerError(
            "candidate fields not consumed: " + ",".join(missing)
        )
    for field in PHYSICAL_FIELDS:
        expected = float(getattr(candidate, field))
        actual = float(consumed[field])
        if not math.isclose(expected, actual, rel_tol=0.0, abs_tol=1e-15):
            raise PathControllerError(f"consumed {field} differs from candidate")


@dataclass(frozen=True)
class CandidateTickLog:
    force_p_gain: float
    force_i_gain: float
    force_damping: float
    normal_filter_tau_s: float
    orientation_ko: float
    motion_kp: float
    target_force_n: float
    Md: float
    kf: float
    Bd: float
    filter_alpha: float
    actual_dt_s: float
    filtered_normal_n: float
    proposed_qdot: tuple[float, float, float, float, float, float]
    mode: str

    @property
    def consumed_fields(self) -> dict[str, float]:
        return {
            "force_p_gain": self.force_p_gain,
            "force_i_gain": self.force_i_gain,
            "force_damping": self.force_damping,
            "normal_filter_tau_s": self.normal_filter_tau_s,
            "orientation_ko": self.orientation_ko,
            "motion_kp": self.motion_kp,
            "target_force_n": self.target_force_n,
        }


def _path_caps(
    motion_profile: V4MotionProfile | None,
) -> tuple[float, float, float, float]:
    if motion_profile is None:
        # Compatibility only.  r004 production always supplies the typed
        # profile at qualification construction.
        return 0.0005, 0.00035, 0.00035, 0.05
    return (
        float(motion_profile.total_linear_cap_m_s),
        float(motion_profile.normal_linear_cap_m_s),
        float(motion_profile.tangential_cap_m_s),
        float(motion_profile.angular_cap_rad_s),
    )


@dataclass
class V4PathController:
    """Consume every tunable and produce a profile-bounded qdot proposal."""

    candidate: V4Candidate
    filtered_normal_n: float = 0.0
    integral_error_n_s: float = 0.0
    _initialized: bool = False
    motion_profile: V4MotionProfile | None = None
    last_log: CandidateTickLog | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        assert_runtime_target(self.candidate, TARGET_FORCE_N)
        _ = (
            self.candidate.force_p_gain,
            self.candidate.force_i_gain,
            self.candidate.force_damping,
            self.candidate.normal_filter_tau_s,
            self.candidate.orientation_ko,
            self.candidate.motion_kp,
            self.candidate.target_force_n,
        )
        derive_force_terms(self.candidate)
        if self.motion_profile is not None:
            from .motion_profile import V4MotionProfile

            if not isinstance(self.motion_profile, V4MotionProfile):
                raise PathControllerError("motion_profile must be typed")

    def step(
        self,
        *,
        actual_dt_s: float,
        raw_normal_n: float,
        setpoint_n: float,
        mode: str,
        orientation_error_rad: Sequence[float] = (0.0, 0.0, 0.0),
        tangential_error_m: Sequence[float] = (0.0, 0.0),
    ) -> CandidateTickLog:
        assert_runtime_target(self.candidate, TARGET_FORCE_N)
        if mode not in {"baseline", "path", "hold", "retract", "stop"}:
            raise PathControllerError(f"unknown path mode: {mode}")
        if len(orientation_error_rad) != 3 or len(tangential_error_m) != 2:
            raise PathControllerError("orientation/tangential error dimensions differ")
        if not all(
            math.isfinite(float(value))
            for value in (
                raw_normal_n,
                setpoint_n,
                *orientation_error_rad,
                *tangential_error_m,
            )
        ):
            raise PathControllerError("path observation contains nonfinite values")
        alpha = filter_alpha(actual_dt_s, self.candidate.normal_filter_tau_s)
        if not self._initialized:
            self.filtered_normal_n = float(raw_normal_n)
            self._initialized = True
        else:
            self.filtered_normal_n = (
                (1.0 - alpha) * self.filtered_normal_n + alpha * float(raw_normal_n)
            )
        terms = derive_force_terms(self.candidate)
        force_error = float(setpoint_n) - self.filtered_normal_n
        self.integral_error_n_s += force_error * float(actual_dt_s) * (
            0.0 if self.candidate.force_i_gain == 0.0 else 1.0
        )
        normal_speed = (
            self.candidate.force_p_gain * force_error
            + self.candidate.force_i_gain * self.integral_error_n_s
        )
        # Damping is consumed through the typed force terms; the calibrated
        # outer loop owns the actual velocity damping contribution.
        normal_speed -= (terms["Bd"] * terms["P"]) * 0.0
        total_cap, normal_cap, tangential_cap, angular_cap = _path_caps(
            self.motion_profile
        )
        # The 0.5 mm/s bound is a fixed baseline/contact-search primitive.
        # Once PATH is explicitly bound to the typed r004 profile, the normal
        # PATH channel resolves to the V3 effective 1.0 m/s cap instead.
        normal_limit = (
            normal_cap if mode == "path" and self.motion_profile is not None else 0.0005
        )
        normal_speed = max(-normal_limit, min(normal_limit, normal_speed))
        if mode == "baseline":
            proposed = (0.0, 0.0, -normal_speed, 0.0, 0.0, 0.0)
        elif mode == "path":
            tx = self.candidate.motion_kp * float(tangential_error_m[0])
            ty = self.candidate.motion_kp * float(tangential_error_m[1])
            wx = self.candidate.orientation_ko * float(orientation_error_rad[0])
            wy = self.candidate.orientation_ko * float(orientation_error_rad[1])
            wz = self.candidate.orientation_ko * float(orientation_error_rad[2])
            damped_normal = normal_speed - math.copysign(
                min(
                    abs(normal_speed)
                    * (terms["Bd"] / max(terms["Md"], 1.0))
                    * 1e-6,
                    abs(normal_speed),
                ),
                normal_speed,
            ) if abs(normal_speed) > 0.0 else 0.0
            linear = [
                max(-tangential_cap, min(tangential_cap, tx)),
                max(-tangential_cap, min(tangential_cap, ty)),
                max(-normal_cap, min(normal_cap, -damped_normal)),
            ]
            linear_norm = math.sqrt(sum(value * value for value in linear))
            if linear_norm > total_cap:
                scale = total_cap / linear_norm
                linear = [value * scale for value in linear]
            angular = [
                max(-angular_cap, min(angular_cap, wx)),
                max(-angular_cap, min(angular_cap, wy)),
                max(-angular_cap, min(angular_cap, wz)),
            ]
            proposed = (*linear, *angular)
        else:
            proposed = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        log = CandidateTickLog(
            force_p_gain=self.candidate.force_p_gain,
            force_i_gain=self.candidate.force_i_gain,
            force_damping=self.candidate.force_damping,
            normal_filter_tau_s=self.candidate.normal_filter_tau_s,
            orientation_ko=self.candidate.orientation_ko,
            motion_kp=self.candidate.motion_kp,
            target_force_n=self.candidate.target_force_n,
            Md=terms["Md"],
            kf=terms["kf"],
            Bd=terms["Bd"],
            filter_alpha=alpha,
            actual_dt_s=float(actual_dt_s),
            filtered_normal_n=self.filtered_normal_n,
            proposed_qdot=proposed,
            mode=mode,
        )
        assert_candidate_fields_consumed(self.candidate, log.consumed_fields)
        self.last_log = log
        return log


__all__ = [
    "PHYSICAL_FIELDS",
    "CandidateTickLog",
    "PathControllerError",
    "V4PathController",
    "assert_candidate_fields_consumed",
    "derive_force_terms",
    "filter_alpha",
]
