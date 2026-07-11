"""Interchangeable offline policies sharing one directional adaptor."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Protocol, Sequence

from .constraints import (
    ImpedanceBounds,
    derive_damping,
    limit_stiffness,
)
from .contracts import ImpedanceObservation, ImpedanceProposal, PoseSample
from .math3d import pose_error


DEFAULT_BASELINE_STIFFNESS = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)


class ImpedancePolicy(Protocol):
    def propose(self, observation: ImpedanceObservation) -> ImpedanceProposal: ...


def _validated_baseline(
    values: Sequence[float], bounds: ImpedanceBounds
) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != 6 or not all(math.isfinite(value) for value in result):
        raise ValueError("baseline stiffness must contain six finite values")
    if any(
        not bounds.minimum[index] <= result[index] <= bounds.maximum[index]
        for index in range(6)
    ):
        raise ValueError("baseline stiffness is outside configured bounds")
    return result


class FixedImpedancePolicy:
    def __init__(
        self,
        bounds: ImpedanceBounds,
        stiffness: Sequence[float] = DEFAULT_BASELINE_STIFFNESS,
    ) -> None:
        self.bounds = bounds
        self.stiffness = _validated_baseline(stiffness, bounds)

    def propose(self, observation: ImpedanceObservation) -> ImpedanceProposal:
        return ImpedanceProposal(
            generated_at_s=observation.timestamp_s,
            s_zft=observation.nominal_zft,
            stiffness=self.stiffness,
            damping=derive_damping(self.stiffness, self.bounds),
            confidence=1.0,
            age_s=0.0,
            source="fixed_baseline",
            model_hash="",
            valid=True,
            shadow_only=True,
        )


@dataclass(frozen=True)
class ScriptedPhase:
    start_s: float
    stiffness_scale: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.start_s) or self.start_s < 0.0:
            raise ValueError("phase start_s must be finite and non-negative")
        if not math.isfinite(self.stiffness_scale) or not 0.0 <= self.stiffness_scale <= 1.0:
            raise ValueError("stiffness_scale must be in [0, 1]")


class ScriptedPhasePolicy:
    """A deterministic non-increasing phase schedule for offline ablation."""

    def __init__(
        self,
        bounds: ImpedanceBounds,
        phases: Sequence[ScriptedPhase],
        baseline: Sequence[float] = DEFAULT_BASELINE_STIFFNESS,
        *,
        nominal_rate_hz: float = 200.0,
    ) -> None:
        self.bounds = bounds
        self.baseline = _validated_baseline(baseline, bounds)
        self.phases = tuple(phases)
        if not self.phases or self.phases[0].start_s != 0.0:
            raise ValueError("scripted phases must start at t=0")
        if any(
            self.phases[index].start_s >= self.phases[index + 1].start_s
            for index in range(len(self.phases) - 1)
        ):
            raise ValueError("phase start times must be strictly increasing")
        if any(
            self.phases[index].stiffness_scale
            < self.phases[index + 1].stiffness_scale
            for index in range(len(self.phases) - 1)
        ):
            raise ValueError("stiffness scales may only decrease within a run")
        if nominal_rate_hz <= 0.0:
            raise ValueError("nominal_rate_hz must be positive")
        self.nominal_dt_s = 1.0 / nominal_rate_hz
        self._start_s: float | None = None
        self._last_timestamp_s: float | None = None
        self._current = self.baseline

    def propose(self, observation: ImpedanceObservation) -> ImpedanceProposal:
        if self._start_s is None:
            self._start_s = observation.timestamp_s
        elapsed = observation.timestamp_s - self._start_s
        selected = self.phases[0]
        for phase in self.phases:
            if elapsed >= phase.start_s:
                selected = phase
        desired = tuple(value * selected.stiffness_scale for value in self.baseline)
        dt_s = self.nominal_dt_s
        if self._last_timestamp_s is not None:
            dt_s = max(self.nominal_dt_s, observation.timestamp_s - self._last_timestamp_s)
        self._current = limit_stiffness(
            desired,
            self._current,
            dt_s,
            self.bounds,
            allow_increase=False,
        )
        self._last_timestamp_s = observation.timestamp_s
        return ImpedanceProposal(
            generated_at_s=observation.timestamp_s,
            s_zft=observation.nominal_zft,
            stiffness=self._current,
            damping=derive_damping(self._current, self.bounds),
            confidence=1.0,
            age_s=0.0,
            source=f"scripted_phase:{selected.start_s:.6f}",
            model_hash="",
            valid=True,
            shadow_only=True,
        )


class DirectionalStiffnessAdaptor:
    """Shared bounded directional heuristic for nominal and DBIL ZFT.

    The first active VIC phase guarantees finite, diagonal, bounded,
    slew-limited, non-increasing stiffness.  This heuristic is not an energy
    tank, passivity observer/controller, or proof of passivity; a moving
    equilibrium and the non-zero safe-low floor preclude that claim.
    """

    def __init__(
        self,
        bounds: ImpedanceBounds,
        baseline: Sequence[float] = DEFAULT_BASELINE_STIFFNESS,
        *,
        denominator_epsilon: float = 1e-8,
        zero_error_threshold: float = 1e-6,
        adapt_rotation: bool = True,
    ) -> None:
        self.bounds = bounds
        self.baseline = _validated_baseline(baseline, bounds)
        if denominator_epsilon <= 0.0 or zero_error_threshold <= 0.0:
            raise ValueError("denominator epsilon and zero-error threshold must be positive")
        self.denominator_epsilon = denominator_epsilon
        self.zero_error_threshold = zero_error_threshold
        self.adapt_rotation = adapt_rotation

    def estimate(
        self,
        observation: ImpedanceObservation,
        zft: PoseSample,
        previous_stiffness: Sequence[float],
        dt_s: float,
    ) -> tuple[float, ...]:
        error = pose_error(zft, observation.pose)
        wrench = observation.wrench
        desired: list[float] = []
        for index in range(6):
            if index >= 3 and not self.adapt_rotation:
                desired.append(previous_stiffness[index])
                continue
            deformation = abs(error[index])
            if deformation <= self.zero_error_threshold:
                directional_limit = self.baseline[index]
            else:
                # Contact support heuristic only.  The safe-low floor means
                # this must not be described as a guaranteed energy bound.
                directional_limit = (
                    2.0
                    * abs(wrench[index] * error[index])
                    / (error[index] * error[index] + self.denominator_epsilon)
                )
            desired.append(
                min(
                    self.baseline[index],
                    max(self.bounds.safe_low[index], directional_limit),
                )
            )
        return limit_stiffness(
            desired,
            previous_stiffness,
            dt_s,
            self.bounds,
            allow_increase=False,
        )


class DirectionalVICPolicy:
    def __init__(
        self,
        adaptor: DirectionalStiffnessAdaptor,
        *,
        nominal_rate_hz: float = 200.0,
    ) -> None:
        self.adaptor = adaptor
        self._current = adaptor.baseline
        self._last_timestamp_s: float | None = None
        self.nominal_dt_s = 1.0 / nominal_rate_hz

    def _propose_for_zft(
        self,
        observation: ImpedanceObservation,
        zft: PoseSample,
        *,
        source: str,
        confidence: float,
        model_hash: str,
        shadow_only: bool,
    ) -> ImpedanceProposal:
        dt_s = self.nominal_dt_s
        if self._last_timestamp_s is not None:
            dt_s = max(self.nominal_dt_s, observation.timestamp_s - self._last_timestamp_s)
        self._current = self.adaptor.estimate(observation, zft, self._current, dt_s)
        self._last_timestamp_s = observation.timestamp_s
        return ImpedanceProposal(
            generated_at_s=observation.timestamp_s,
            s_zft=zft,
            stiffness=self._current,
            damping=derive_damping(self._current, self.adaptor.bounds),
            confidence=confidence,
            age_s=0.0,
            source=source,
            model_hash=model_hash,
            valid=True,
            shadow_only=shadow_only,
        )

    def propose(self, observation: ImpedanceObservation) -> ImpedanceProposal:
        return self._propose_for_zft(
            observation,
            observation.nominal_zft,
            source="deterministic_nominal_zft",
            confidence=1.0,
            model_hash="",
            shadow_only=True,
        )


@dataclass(frozen=True)
class DBILPrediction:
    s_zft: PoseSample
    confidence: float
    model_hash: str


class DBILPredictor(Protocol):
    def predict(self, observation: ImpedanceObservation) -> DBILPrediction: ...


class DBILShadowPolicy(DirectionalVICPolicy):
    """DBIL can change evidence only; this policy can never command a backend."""

    def __init__(
        self,
        adaptor: DirectionalStiffnessAdaptor,
        predictor: DBILPredictor,
        *,
        nominal_rate_hz: float = 200.0,
    ) -> None:
        super().__init__(adaptor, nominal_rate_hz=nominal_rate_hz)
        self.predictor = predictor

    def propose(self, observation: ImpedanceObservation) -> ImpedanceProposal:
        try:
            prediction = self.predictor.predict(observation)
            return self._propose_for_zft(
                observation,
                prediction.s_zft,
                source="dbil_shadow",
                confidence=prediction.confidence,
                model_hash=prediction.model_hash,
                shadow_only=True,
            )
        except Exception:
            return ImpedanceProposal(
                generated_at_s=observation.timestamp_s,
                s_zft=observation.nominal_zft,
                stiffness=self._current,
                damping=derive_damping(self._current, self.adaptor.bounds),
                confidence=0.0,
                age_s=0.0,
                source="dbil_shadow_error",
                model_hash="",
                valid=False,
                shadow_only=True,
            )
