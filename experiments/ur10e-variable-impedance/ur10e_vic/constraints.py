"""Fail-closed stiffness, damping, slew, and stale-proposal supervision."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

from .contracts import ImpedanceProposal, PoseSample


def _six(values: Sequence[float], name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != 6 or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain six finite values")
    return result


@dataclass(frozen=True)
class ImpedanceBounds:
    minimum: tuple[float, ...] = (25.0, 25.0, 25.0, 0.5, 0.5, 0.5)
    maximum: tuple[float, ...] = (1000.0, 1000.0, 1000.0, 60.0, 60.0, 60.0)
    safe_low: tuple[float, ...] = (40.0, 40.0, 40.0, 1.0, 1.0, 1.0)
    max_slew_per_s: tuple[float, ...] = (
        400.0,
        400.0,
        400.0,
        20.0,
        20.0,
        20.0,
    )
    virtual_mass: tuple[float, ...] = (2.0, 2.0, 2.0, 0.2, 0.2, 0.2)
    damping_ratio: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "minimum",
            "maximum",
            "safe_low",
            "max_slew_per_s",
            "virtual_mass",
        ):
            object.__setattr__(self, name, _six(getattr(self, name), name))
        for index in range(6):
            if not 0.0 <= self.minimum[index] <= self.safe_low[index] <= self.maximum[index]:
                raise ValueError("minimum <= safe_low <= maximum must hold per axis")
            if self.max_slew_per_s[index] <= 0.0 or self.virtual_mass[index] <= 0.0:
                raise ValueError("slew and virtual mass must be positive")
        if not math.isfinite(self.damping_ratio) or self.damping_ratio <= 0.0:
            raise ValueError("damping_ratio must be finite and positive")


def derive_damping(
    stiffness: Sequence[float], bounds: ImpedanceBounds
) -> tuple[float, ...]:
    values = _six(stiffness, "stiffness")
    if any(value < 0.0 for value in values):
        raise ValueError("stiffness must be positive semidefinite")
    return tuple(
        2.0
        * bounds.damping_ratio
        * math.sqrt(values[index] * bounds.virtual_mass[index])
        for index in range(6)
    )


def limit_stiffness(
    desired: Sequence[float],
    previous: Sequence[float],
    dt_s: float,
    bounds: ImpedanceBounds,
    *,
    allow_increase: bool = False,
) -> tuple[float, ...]:
    desired_values = _six(desired, "desired")
    previous_values = _six(previous, "previous")
    if not math.isfinite(dt_s) or dt_s <= 0.0:
        raise ValueError("dt_s must be finite and positive")
    result: list[float] = []
    for index in range(6):
        clipped = min(bounds.maximum[index], max(bounds.minimum[index], desired_values[index]))
        if not allow_increase:
            clipped = min(clipped, previous_values[index])
        max_change = bounds.max_slew_per_s[index] * dt_s
        lower = previous_values[index] - max_change
        upper = previous_values[index] + max_change if allow_increase else previous_values[index]
        result.append(min(upper, max(lower, clipped)))
    return tuple(result)


@dataclass(frozen=True)
class SupervisionDecision:
    proposal: ImpedanceProposal
    mode: str
    request_stop: bool
    reason: str


class ProposalSupervisor:
    """Hold fresh output for two model periods, then ramp down and stop."""

    def __init__(
        self,
        initial_stiffness: Sequence[float],
        bounds: ImpedanceBounds,
        model_period_s: float,
        *,
        hold_periods: int = 2,
    ) -> None:
        self.bounds = bounds
        self.current_stiffness = _six(initial_stiffness, "initial_stiffness")
        if any(
            not bounds.minimum[index]
            <= self.current_stiffness[index]
            <= bounds.maximum[index]
            for index in range(6)
        ):
            raise ValueError("initial stiffness is outside bounds")
        if not math.isfinite(model_period_s) or model_period_s <= 0.0:
            raise ValueError("model_period_s must be finite and positive")
        if hold_periods != 2:
            raise ValueError("the first scaffold fixes stale hold to exactly two periods")
        self.model_period_s = model_period_s
        self.hold_periods = hold_periods
        self._fixed_orientation_stiffness = self.current_stiffness[3:]
        self._last_valid: ImpedanceProposal | None = None
        self._last_valid_time_s: float | None = None

    def _phase1_stiffness(
        self,
        desired: Sequence[float],
        dt_s: float,
    ) -> tuple[float, ...]:
        """Apply translational-only phase-1 limits and lock rotational K."""

        desired_values = _six(desired, "desired")
        phase1_desired = desired_values[:3] + self._fixed_orientation_stiffness
        limited = limit_stiffness(
            phase1_desired,
            self.current_stiffness,
            dt_s,
            self.bounds,
            allow_increase=False,
        )
        return limited[:3] + self._fixed_orientation_stiffness

    def step(
        self,
        incoming: ImpedanceProposal | None,
        *,
        now_s: float,
        dt_s: float,
        fallback_zft: PoseSample,
    ) -> SupervisionDecision:
        if not math.isfinite(now_s) or now_s < 0.0:
            raise ValueError("now_s must be finite and non-negative")
        fresh = (
            incoming is not None
            and incoming.valid
            and incoming.age_s <= self.model_period_s
            and incoming.generated_at_s <= now_s
            and now_s - incoming.generated_at_s <= self.model_period_s
            and incoming.stiffness[3:] == self._fixed_orientation_stiffness
        )
        if fresh:
            assert incoming is not None
            self.current_stiffness = self._phase1_stiffness(
                incoming.stiffness,
                dt_s,
            )
            accepted = ImpedanceProposal(
                generated_at_s=incoming.generated_at_s,
                s_zft=incoming.s_zft,
                stiffness=self.current_stiffness,
                damping=derive_damping(self.current_stiffness, self.bounds),
                confidence=incoming.confidence,
                age_s=max(incoming.age_s, now_s - incoming.generated_at_s),
                source=incoming.source,
                model_hash=incoming.model_hash,
                valid=True,
                shadow_only=incoming.shadow_only,
            )
            self._last_valid = accepted
            self._last_valid_time_s = now_s
            return SupervisionDecision(accepted, "tracking", False, "fresh_proposal")

        last_age = math.inf
        if self._last_valid_time_s is not None:
            last_age = now_s - self._last_valid_time_s
        if self._last_valid is not None and last_age <= self.hold_periods * self.model_period_s:
            held = ImpedanceProposal(
                generated_at_s=self._last_valid.generated_at_s,
                s_zft=self._last_valid.s_zft,
                stiffness=self.current_stiffness,
                damping=derive_damping(self.current_stiffness, self.bounds),
                confidence=self._last_valid.confidence,
                age_s=last_age,
                source=f"{self._last_valid.source}:held",
                model_hash=self._last_valid.model_hash,
                valid=True,
                shadow_only=self._last_valid.shadow_only,
            )
            return SupervisionDecision(held, "hold", False, "within_two_model_periods")

        self.current_stiffness = self._phase1_stiffness(
            self.bounds.safe_low[:3] + self._fixed_orientation_stiffness,
            dt_s,
        )
        at_safe_low = all(
            self.current_stiffness[index] <= self.bounds.safe_low[index] + 1e-12
            for index in range(3)
        )
        failed = ImpedanceProposal(
            generated_at_s=now_s,
            s_zft=self._last_valid.s_zft if self._last_valid else fallback_zft,
            stiffness=self.current_stiffness,
            damping=derive_damping(self.current_stiffness, self.bounds),
            confidence=0.0,
            age_s=(
                last_age
                if math.isfinite(last_age)
                else (self.hold_periods + 1) * self.model_period_s
            ),
            source="stale_failover",
            model_hash="",
            valid=False,
            shadow_only=True,
        )
        return SupervisionDecision(
            failed,
            "stop" if at_safe_low else "damping_only",
            True,
            "stale_beyond_two_model_periods",
        )
