"""Pure stateful context residual correction for offline R013 experiments.

This module is deliberately not imported by the live runtime adapter.  It
models a target-load correction only; it is not the force integrator and has
no authority over force/frame construction.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence

from .floor_coordinator import (
    CORRECTION_NORMALIZATION_SCHEMA,
    CorrectionBlockV1,
)
from .path_context import PathContextSampleV1, PathContextError


FORCE_CORRECTION_SCHEMA = "step5d.autotune-v4/r013-force-correction-policy-v1"
FORCE_CORRECTION_STATE_SCHEMA = "step5d.autotune-v4/r013-force-correction-state-v1"
FORCE_CORRECTION_RECEIPT_SCHEMA = "step5d.autotune-v4/r013-force-correction-receipt-v1"
FORCE_FRAME_SEMANTICS = (
    "environment_on_tool_force|positive_reaction_normal_load|"
    "approach_normal_posture_press"
)
CORRECTION_FEATURE_NAMES = (
    "bias",
    "normalized_speed",
    "normalized_signed_acceleration",
    "normalized_signed_curvature",
    "sin_phase",
    "cos_phase",
)
RESET_BOUNDARIES = (
    "candidate_dispatch",
    "path_entry",
    "contact_loss",
    "abort",
    "home",
    "control_mode_exit",
    "invalid_state",
)


class ForceCorrectionError(ValueError):
    """A correction context, state, or policy is invalid."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ForceCorrectionError(f"R013 {name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ForceCorrectionError(f"R013 {name} must be numeric") from exc
    if not math.isfinite(result):
        raise ForceCorrectionError(f"R013 {name} must be finite")
    return result


def _bounded(value: float, low: float, high: float) -> tuple[float, bool]:
    clipped = min(high, max(low, value))
    return clipped, not math.isclose(clipped, value, rel_tol=0.0, abs_tol=0.0)


@dataclass(frozen=True)
class ForceCorrectionStateV1:
    fingerprint: str
    generation: int = 0
    last_path_time_s: float | None = None
    last_applied_correction_n: float = 0.0
    schema: str = FORCE_CORRECTION_STATE_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != FORCE_CORRECTION_STATE_SCHEMA or self.version != 1:
            raise ForceCorrectionError("R013 correction state schema/version differs")
        if not isinstance(self.fingerprint, str) or not self.fingerprint:
            raise ForceCorrectionError("R013 correction state fingerprint is missing")
        if type(self.generation) is not int or self.generation < 0:
            raise ForceCorrectionError("R013 correction state generation is invalid")
        if self.last_path_time_s is not None:
            _finite(self.last_path_time_s, "last_path_time_s")
        correction = _finite(self.last_applied_correction_n, "last_applied_correction_n")
        if not -1.25 <= correction <= 1.25:
            raise ForceCorrectionError("R013 correction state output is out of bounds")
        object.__setattr__(self, "last_applied_correction_n", correction)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "generation": self.generation,
            "last_path_time_s": self.last_path_time_s,
            "last_applied_correction_n": self.last_applied_correction_n,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ForceCorrectionStateV1":
        required = {
            "schema", "version", "fingerprint", "generation",
            "last_path_time_s", "last_applied_correction_n",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ForceCorrectionError("R013 correction state fields differ")
        return cls(**dict(value))


@dataclass(frozen=True)
class ForceCorrectionReceiptV1:
    raw_features: tuple[float, ...]
    normalized_features: tuple[float, ...]
    weights: tuple[float, ...]
    normalization_scales: tuple[float, ...]
    phase_component_n: float
    context_residual_n: float
    combined_unclipped_n: float
    combined_clipped_n: float
    applied_combined_correction_n: float
    slew_delta_n: float
    slew_limit_n: float
    effective_target_n: float
    clip_applied: bool
    slew_limited: bool
    state_generation: int
    zero_violations: tuple[str, ...]
    path_context: Mapping[str, Any]
    fingerprint: str
    diagnostics: Mapping[str, Any]
    schema: str = FORCE_CORRECTION_RECEIPT_SCHEMA
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "raw_features": list(self.raw_features),
            "normalized_features": list(self.normalized_features),
            "feature_names": list(CORRECTION_FEATURE_NAMES),
            "weights": list(self.weights),
            "normalization_scales": list(self.normalization_scales),
            "phase_component_n": self.phase_component_n,
            "context_residual_n": self.context_residual_n,
            "combined_unclipped_n": self.combined_unclipped_n,
            "combined_clipped_n": self.combined_clipped_n,
            "applied_combined_correction_n": self.applied_combined_correction_n,
            "slew_delta_n": self.slew_delta_n,
            "slew_limit_n": self.slew_limit_n,
            "effective_target_n": self.effective_target_n,
            "clip_applied": self.clip_applied,
            "slew_limited": self.slew_limited,
            "state_generation": self.state_generation,
            "zero_violations": list(self.zero_violations),
            "path_context": dict(self.path_context),
            "fingerprint": self.fingerprint,
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True)
class ForceCorrectionPolicyV1:
    weights: CorrectionBlockV1
    normalization_scales: tuple[float, float, float, float, float, float]
    base_target_n: float = 5.0
    correction_clip_n: float = 1.25
    slew_rate_n_s: float = 0.5
    reset_boundaries: tuple[str, ...] = RESET_BOUNDARIES
    normalization_schema: str = CORRECTION_NORMALIZATION_SCHEMA
    schema: str = FORCE_CORRECTION_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != FORCE_CORRECTION_SCHEMA or self.version != 1:
            raise ForceCorrectionError("R013 correction policy schema/version differs")
        if self.normalization_schema != CORRECTION_NORMALIZATION_SCHEMA:
            raise ForceCorrectionError("R013 correction normalization schema differs")
        if len(self.normalization_scales) != 6:
            raise ForceCorrectionError("R013 correction normalization needs six scales")
        scales = tuple(_finite(value, f"normalization scale {index}") for index, value in enumerate(self.normalization_scales))
        if any(value <= 0.0 or value > 1000.0 for value in scales):
            raise ForceCorrectionError("R013 correction normalization scale is out of bounds")
        if not isinstance(self.weights, CorrectionBlockV1) or self.weights.is_legacy_migration:
            raise ForceCorrectionError("R013 correction policy requires real v2 weights")
        if _finite(self.base_target_n, "base_target_n") != 5.0:
            raise ForceCorrectionError("R013 correction base target differs")
        if _finite(self.correction_clip_n, "correction_clip_n") != 1.25:
            raise ForceCorrectionError("R013 correction clip differs")
        if _finite(self.slew_rate_n_s, "slew_rate_n_s") != 0.5:
            raise ForceCorrectionError("R013 correction slew rate differs")
        if tuple(self.reset_boundaries) != RESET_BOUNDARIES:
            raise ForceCorrectionError("R013 correction reset boundaries differ")
        object.__setattr__(self, "normalization_scales", scales)

    @classmethod
    def from_floor_policy(cls, policy: Any, weights: CorrectionBlockV1) -> "ForceCorrectionPolicyV1":
        return cls(weights=weights, normalization_scales=tuple(policy.correction_normalization_scales))

    def initial_state(self, fingerprint: str) -> ForceCorrectionStateV1:
        return ForceCorrectionStateV1(fingerprint=fingerprint)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ForceCorrectionPolicyV1":
        required = {
            "schema", "version", "weights", "feature_names", "normalization_schema",
            "normalization_scales", "base_target_n", "correction_clip_n",
            "slew_rate_n_s", "reset_boundaries", "force_frame_semantics", "force_integrator",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ForceCorrectionError("R013 correction policy fields differ")
        if tuple(value["feature_names"]) != CORRECTION_FEATURE_NAMES:
            raise ForceCorrectionError("R013 correction feature names differ")
        if value["force_frame_semantics"] != FORCE_FRAME_SEMANTICS or value["force_integrator"] != "separate_unmodified_state":
            raise ForceCorrectionError("R013 correction force-frame contract differs")
        return cls(
            weights=CorrectionBlockV1.from_mapping(value["weights"]),
            normalization_scales=tuple(value["normalization_scales"]),
            base_target_n=value["base_target_n"],
            correction_clip_n=value["correction_clip_n"],
            slew_rate_n_s=value["slew_rate_n_s"],
            reset_boundaries=tuple(value["reset_boundaries"]),
            normalization_schema=value["normalization_schema"],
            schema=value["schema"], version=value["version"],
        )

    def reset(self, state: ForceCorrectionStateV1, *, boundary: str, fingerprint: str) -> ForceCorrectionStateV1:
        if boundary not in self.reset_boundaries:
            raise ForceCorrectionError("R013 unknown correction reset boundary")
        if state.fingerprint != fingerprint:
            raise ForceCorrectionError("R013 correction reset fingerprint differs")
        return ForceCorrectionStateV1(fingerprint=fingerprint, generation=state.generation + 1)

    def feature_vector(self, context: PathContextSampleV1) -> tuple[float, ...]:
        if not isinstance(context, PathContextSampleV1):
            raise ForceCorrectionError("R013 correction requires a typed path context")
        raw = (
            1.0,
            context.scalar_speed_m_s,
            context.signed_tangential_acceleration_m_s2,
            context.signed_planar_curvature_m_inv,
            math.sin(context.phase_rad),
            math.cos(context.phase_rad),
        )
        normalized: list[float] = []
        for index, (value, scale) in enumerate(zip(raw, self.normalization_scales, strict=True)):
            normalized_value, _clipped = _bounded(value / scale, -1.0, 1.0)
            normalized.append(normalized_value)
        return tuple(normalized)

    def apply(
        self,
        state: ForceCorrectionStateV1,
        context: PathContextSampleV1,
        *,
        existing_phase_correction_n: float,
        fingerprint: str,
        normal_load_n: float | None = None,
        signed_load_error_n: float | None = None,
    ) -> tuple[ForceCorrectionStateV1, ForceCorrectionReceiptV1]:
        if not isinstance(state, ForceCorrectionStateV1) or state.fingerprint != fingerprint:
            raise ForceCorrectionError("R013 correction state/fingerprint mismatch")
        if not isinstance(context, PathContextSampleV1):
            raise ForceCorrectionError("R013 correction context is invalid")
        try:
            path_context = context.as_dict()
        except (TypeError, ValueError, PathContextError) as exc:
            raise ForceCorrectionError("R013 correction context is invalid") from exc
        phase = _finite(existing_phase_correction_n, "existing_phase_correction_n")
        if state.last_path_time_s is not None and context.path_time_s <= state.last_path_time_s:
            raise ForceCorrectionError("R013 correction path time is non-monotonic")
        dt = context.path_time_s if state.last_path_time_s is None else context.path_time_s - state.last_path_time_s
        if dt < 0.0 or not math.isfinite(dt):
            raise ForceCorrectionError("R013 correction dt is invalid")
        raw_features = (
            1.0,
            context.scalar_speed_m_s,
            context.signed_tangential_acceleration_m_s2,
            context.signed_planar_curvature_m_inv,
            math.sin(context.phase_rad),
            math.cos(context.phase_rad),
        )
        normalized = self.feature_vector(context)
        residual = math.fsum(weight * feature for weight, feature in zip(self.weights.weights, normalized, strict=True))
        combined = phase + residual
        clipped, clip_applied = _bounded(combined, -self.correction_clip_n, self.correction_clip_n)
        slew_limit = self.slew_rate_n_s * dt
        delta = clipped - state.last_applied_correction_n
        applied_delta = min(slew_limit, max(-slew_limit, delta))
        applied = state.last_applied_correction_n + applied_delta
        slew_limited = not math.isclose(applied, clipped, rel_tol=0.0, abs_tol=0.0)
        effective_target = self.base_target_n - applied
        violations: list[str] = []
        if not -self.correction_clip_n <= applied <= self.correction_clip_n:
            violations.append("applied_correction_bound")
        if abs(applied_delta) > slew_limit + 1e-12:
            violations.append("slew_bound")
        if not 3.75 <= effective_target <= 6.25:
            violations.append("effective_target_bound")
        if violations:
            raise ForceCorrectionError(f"R013 correction invariant violation: {violations}")
        diagnostics = {
            "force_frame_semantics": FORCE_FRAME_SEMANTICS,
            "normal_load_n": None if normal_load_n is None else _finite(normal_load_n, "normal_load_n"),
            "signed_load_error_n": None if signed_load_error_n is None else _finite(signed_load_error_n, "signed_load_error_n"),
            "force_integrator_touched": False,
        }
        next_state = ForceCorrectionStateV1(
            fingerprint=fingerprint,
            generation=state.generation,
            last_path_time_s=context.path_time_s,
            last_applied_correction_n=applied,
        )
        receipt = ForceCorrectionReceiptV1(
            raw_features=raw_features,
            normalized_features=normalized,
            weights=self.weights.weights,
            normalization_scales=self.normalization_scales,
            phase_component_n=phase,
            context_residual_n=residual,
            combined_unclipped_n=combined,
            combined_clipped_n=clipped,
            applied_combined_correction_n=applied,
            slew_delta_n=applied_delta,
            slew_limit_n=slew_limit,
            effective_target_n=effective_target,
            clip_applied=clip_applied,
            slew_limited=slew_limited,
            state_generation=state.generation,
            zero_violations=tuple(violations),
            path_context=path_context,
            fingerprint=fingerprint,
            diagnostics=diagnostics,
        )
        return next_state, receipt

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "weights": self.weights.as_dict(),
            "feature_names": list(CORRECTION_FEATURE_NAMES),
            "normalization_schema": self.normalization_schema,
            "normalization_scales": list(self.normalization_scales),
            "base_target_n": self.base_target_n,
            "correction_clip_n": self.correction_clip_n,
            "slew_rate_n_s": self.slew_rate_n_s,
            "reset_boundaries": list(self.reset_boundaries),
            "force_frame_semantics": FORCE_FRAME_SEMANTICS,
            "force_integrator": "separate_unmodified_state",
        }


__all__ = [
    "CORRECTION_FEATURE_NAMES", "FORCE_CORRECTION_RECEIPT_SCHEMA",
    "FORCE_CORRECTION_SCHEMA", "FORCE_CORRECTION_STATE_SCHEMA",
    "FORCE_FRAME_SEMANTICS", "ForceCorrectionError", "ForceCorrectionPolicyV1",
    "ForceCorrectionReceiptV1", "ForceCorrectionStateV1", "RESET_BOUNDARIES",
]
