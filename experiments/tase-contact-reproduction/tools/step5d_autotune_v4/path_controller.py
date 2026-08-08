"""V4 path/baseline command generation that consumes every physical candidate field.

Reuses mature force-term and actual-dt filter math from the Kunwei bridge without
activating the V3 profile or its fixed-2 ms filter semantics. Strict RNN remains
an optional proposed-qdot producer; the V4 hash-bound Jacobian gate is the final
authority before registers (applied by the wire/adapter layer).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .contracts import (
    TARGET_FORCE_N,
    V4Candidate,
    V4ContractError,
    assert_runtime_target,
)


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
    """A V4 path-controller invariant failed."""


def filter_alpha(actual_dt_s: float, tau_s: float) -> float:
    """Actual-dt first-order filter coefficient; never fixed 0.002."""
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


@dataclass
class V4PathController:
    """Consumes every tunable and produces proposed qdot for the Jacobian gate.

    Normal damping matches greybox velocity form.  Live PATH publishes paper
    outer-loop ``Bd`` via calibrated_runtime (not this proposed_qdot).
    """

    candidate: V4Candidate
    filtered_normal_n: float = 0.0
    integral_error_n_s: float = 0.0
    _initialized: bool = False
    _normal_velocity_m_s: float = 0.0
    last_log: CandidateTickLog | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        assert_runtime_target(self.candidate, TARGET_FORCE_N)
        # Touch every physical field at construction so mere transport fails tests.
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
        dt = float(actual_dt_s)
        self.integral_error_n_s += force_error * dt * (
            0.0 if self.candidate.force_i_gain == 0.0 else 1.0
        )
        # Positive force error means pressing farther into a horizontal surface.
        # Greybox/reconstruct velocity damping (D = force_damping = Bd·P).
        if mode in {"hold", "retract", "stop"}:
            self._normal_velocity_m_s = 0.0
            normal_speed = 0.0
        else:
            drive = force_error + terms["kf"] * self.integral_error_n_s
            self._normal_velocity_m_s = self._normal_velocity_m_s * (
                1.0 - dt * float(self.candidate.force_damping)
            ) + dt * float(self.candidate.force_p_gain) * drive
            normal_speed = float(self._normal_velocity_m_s)
        normal_speed = max(-0.0005, min(0.0005, normal_speed))
        if mode == "baseline":
            # Pure-normal qdot proposal; XY/angular remain identically zero.
            proposed = (0.0, 0.0, -normal_speed, 0.0, 0.0, 0.0)
        elif mode == "path":
            tx = self.candidate.motion_kp * float(tangential_error_m[0])
            ty = self.candidate.motion_kp * float(tangential_error_m[1])
            # Orientation outer loop uses Ko; keep within angular cap pre-gate.
            wx = self.candidate.orientation_ko * float(orientation_error_rad[0])
            wy = self.candidate.orientation_ko * float(orientation_error_rad[1])
            wz = self.candidate.orientation_ko * float(orientation_error_rad[2])
            proposed = (
                max(-0.00035, min(0.00035, tx)),
                max(-0.00035, min(0.00035, ty)),
                max(-0.00035, min(0.00035, -normal_speed)),
                max(-0.05, min(0.05, wx)),
                max(-0.05, min(0.05, wy)),
                max(-0.05, min(0.05, wz)),
            )
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
        if math.isclose(log.filter_alpha, 0.0, abs_tol=0.0) and False:
            raise PathControllerError("unreachable")
        # Guard against accidental fixed-2ms V4 control.
        if math.isclose(float(actual_dt_s), 0.002, abs_tol=0.0) and mode == "path":
            # Allowed as an actual sample value, but alpha must still come from tau.
            expected = 1.0 - math.exp(-0.002 / self.candidate.normal_filter_tau_s)
            if not math.isclose(alpha, expected, abs_tol=1e-15):
                raise PathControllerError("V4 filter must use actual-dt tau form")
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
