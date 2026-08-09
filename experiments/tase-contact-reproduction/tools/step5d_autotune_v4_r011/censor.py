"""Typed exact/censored rows and causal R009 550-bin shadow semantics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .common import R011ValueError, digest, finite, freeze_tree, json_tree, require_digest


CENSOR_SCHEMA = "step5d.autotune-v4/r011-censored-observation-v2"
EXACT_SCHEMA = "step5d.autotune-v4/r011-exact-observation-v2"
PROTOCOL_SCHEMA = "step5d.autotune-v4/r011-censored-observation-protocol-v2"
R009_CLOSED_BINS = 550
DENOMINATOR_BINS = 550
BIN_WIDTH_S = 0.1
KAPPA_START = 3.0
KAPPA_END = 1.3
KAPPA_MIDPOINT = 0.5
KAPPA_STEEPNESS = 10.0
GUARD_FRACTION = 0.1


class CensoringError(R011ValueError):
    """An exact/censored row violates its typed identity boundary."""


def _identity(value: Any, role: str) -> str:
    return require_digest(value, role)


def causal_lower_bound(bin_absolute_errors: Sequence[float], closed_bin_count: int, *, denominator_bins: int = DENOMINATOR_BINS) -> float:
    """Causal lower bound: closed 0.1 s absolute errors over fixed 550 bins."""
    if isinstance(closed_bin_count, bool) or not 0 <= closed_bin_count <= DENOMINATOR_BINS:
        raise CensoringError("closed_bin_count must be within 0..550")
    if denominator_bins != DENOMINATOR_BINS:
        raise CensoringError("denominator_bins is fixed at 550")
    if len(bin_absolute_errors) < closed_bin_count:
        raise CensoringError("closed bin count exceeds supplied bins")
    values = [finite(value, "closed bin absolute error") for value in bin_absolute_errors[:closed_bin_count]]
    if any(value < 0.0 for value in values):
        raise CensoringError("closed bin absolute errors must be non-negative")
    return sum(values) / float(denominator_bins)


@dataclass(frozen=True)
class CensoredMAEAccumulator:
    closed_absolute_errors: tuple[float, ...] = ()
    denominator_bins: int = DENOMINATOR_BINS

    def __post_init__(self) -> None:
        if self.denominator_bins != DENOMINATOR_BINS or len(self.closed_absolute_errors) > DENOMINATOR_BINS:
            raise CensoringError("censored accumulator denominator/count differs")
        values = tuple(finite(value, "closed bin absolute error") for value in self.closed_absolute_errors)
        if any(value < 0.0 for value in values): raise CensoringError("closed bin error is negative")
        object.__setattr__(self, "closed_absolute_errors", values)

    @property
    def closed_bin_count(self) -> int:
        return len(self.closed_absolute_errors)

    @property
    def lower_bound_n(self) -> float:
        return causal_lower_bound(self.closed_absolute_errors, self.closed_bin_count)

    def close_bin(self, absolute_error: float) -> "CensoredMAEAccumulator":
        if self.closed_bin_count >= DENOMINATOR_BINS: raise CensoringError("all 550 bins are already closed")
        return CensoredMAEAccumulator(self.closed_absolute_errors + (finite(absolute_error, "absolute_error"),), DENOMINATOR_BINS)


def kappa_for_progress(progress_fraction: float) -> float:
    """R009 sigmoid over raw progress with a 10% post-guard normalization."""
    progress = finite(progress_fraction, "post_guard_progress")
    if not 0.0 <= progress <= 1.0: raise CensoringError("post-guard progress must be within [0,1]")
    normalized = min(1.0, max(0.0, (progress - GUARD_FRACTION) / (1.0 - GUARD_FRACTION)))
    sigmoid = 1.0 / (1.0 + math.exp(KAPPA_STEEPNESS * (normalized - KAPPA_MIDPOINT)))
    return KAPPA_END + (KAPPA_START - KAPPA_END) * sigmoid


@dataclass(frozen=True)
class CensorProtocol:
    denominator_bins: int = DENOMINATOR_BINS
    kappa: float = KAPPA_START
    incumbent_threshold_n: float = 0.0
    mode: str = "shadow_only"
    active_early_abort_allowed: bool = False
    schema: str = PROTOCOL_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PROTOCOL_SCHEMA or self.denominator_bins != DENOMINATOR_BINS:
            raise CensoringError("R011 censor protocol denominator must be 550")
        if finite(self.kappa, "kappa") <= 0.0 or finite(self.incumbent_threshold_n, "incumbent_threshold_n") < 0.0:
            raise CensoringError("censor protocol thresholds are invalid")
        if self.mode != "shadow_only" or self.active_early_abort_allowed:
            raise CensoringError("R011 first release forbids active early abort")

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "denominator_bins": self.denominator_bins, "bin_width_s": BIN_WIDTH_S, "kappa": self.kappa, "kappa_semantics": {"start": KAPPA_START, "end": KAPPA_END, "midpoint": KAPPA_MIDPOINT, "steepness": KAPPA_STEEPNESS, "decreasing_with_post_guard_progress": True}, "incumbent_threshold_n": self.incumbent_threshold_n, "mode": self.mode, "active_early_abort_allowed": self.active_early_abort_allowed}


@dataclass(frozen=True)
class ExactObservation:
    observation_id: str
    candidate: Mapping[str, Any]
    objective_n: float
    dispatch_identity_sha256: str
    release_identity_sha256: str
    completed: bool = True
    sealed: bool = True
    schema: str = EXACT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != EXACT_SCHEMA or not isinstance(self.observation_id, str) or not self.observation_id or self.completed is not True or self.sealed is not True:
            raise CensoringError("exact observation is not a completed sealed R011 row")
        finite(self.objective_n, "objective_n"); _identity(self.dispatch_identity_sha256, "dispatch_identity_sha256"); _identity(self.release_identity_sha256, "release_identity_sha256")
        object.__setattr__(self, "candidate", freeze_tree(json_tree(self.candidate)))

    @property
    def censored(self) -> bool: return False

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "observation_id": self.observation_id, "candidate": json_tree(self.candidate), "objective_n": self.objective_n, "dispatch_identity_sha256": self.dispatch_identity_sha256, "release_identity_sha256": self.release_identity_sha256, "completed": True, "sealed": True, "censored": False}


@dataclass(frozen=True)
class CensoredObservation:
    observation_id: str
    candidate: Mapping[str, Any]
    lower_bound_n: float
    watermark_s: float
    closed_bin_count: int
    denominator_bins: int
    kappa: float
    incumbent_threshold_n: float
    dispatch_identity_sha256: str
    release_identity_sha256: str
    completed: bool = True
    sealed: bool = True
    nontrainable: bool = True
    noncontrol: bool = True
    noncompletion: bool = True
    schema: str = CENSOR_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CENSOR_SCHEMA or not isinstance(self.observation_id, str) or not self.observation_id or self.completed is not True or self.sealed is not True:
            raise CensoringError("censored observation is not a completed sealed R011 row")
        lower, watermark, threshold = finite(self.lower_bound_n, "lower_bound_n"), finite(self.watermark_s, "watermark_s"), finite(self.incumbent_threshold_n, "incumbent_threshold_n")
        if lower < 0.0 or watermark < 0.0 or threshold < 0.0 or self.denominator_bins != DENOMINATOR_BINS or isinstance(self.closed_bin_count, bool) or not 0 <= self.closed_bin_count <= DENOMINATOR_BINS:
            raise CensoringError("censored bound/watermark/bin semantics are invalid")
        if finite(self.kappa, "kappa") <= 0.0 or not all(value is True for value in (self.nontrainable, self.noncontrol, self.noncompletion)):
            raise CensoringError("censored row authority flags differ")
        _identity(self.dispatch_identity_sha256, "dispatch_identity_sha256"); _identity(self.release_identity_sha256, "release_identity_sha256")
        object.__setattr__(self, "candidate", freeze_tree(json_tree(self.candidate)))

    @property
    def censored(self) -> bool: return True

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "observation_id": self.observation_id, "candidate": json_tree(self.candidate), "lower_bound_n": self.lower_bound_n, "watermark_s": self.watermark_s, "closed_bin_count": self.closed_bin_count, "denominator_bins": self.denominator_bins, "kappa": self.kappa, "incumbent_threshold_n": self.incumbent_threshold_n, "dispatch_identity_sha256": self.dispatch_identity_sha256, "release_identity_sha256": self.release_identity_sha256, "completed": True, "sealed": True, "nontrainable": True, "noncontrol": True, "noncompletion": True, "censored": True}


Observation = ExactObservation | CensoredObservation


def validate_observation(value: Observation | Mapping[str, Any]) -> Observation:
    if isinstance(value, (ExactObservation, CensoredObservation)): return value
    if not isinstance(value, Mapping): raise CensoringError("observation must be typed")
    schema = value.get("schema")
    if schema == EXACT_SCHEMA:
        required = {"schema", "observation_id", "candidate", "objective_n", "dispatch_identity_sha256", "release_identity_sha256", "completed", "sealed", "censored"}
        if set(value) != required or value.get("censored") is not False: raise CensoringError("exact observation fields differ")
        return ExactObservation(value["observation_id"], value["candidate"], value["objective_n"], value["dispatch_identity_sha256"], value["release_identity_sha256"], value["completed"], value["sealed"])
    if schema == CENSOR_SCHEMA:
        required = {"schema", "observation_id", "candidate", "lower_bound_n", "watermark_s", "closed_bin_count", "denominator_bins", "kappa", "incumbent_threshold_n", "dispatch_identity_sha256", "release_identity_sha256", "completed", "sealed", "nontrainable", "noncontrol", "noncompletion", "censored"}
        if set(value) != required or value.get("censored") is not True: raise CensoringError("censored observation fields differ")
        return CensoredObservation(value["observation_id"], value["candidate"], value["lower_bound_n"], value["watermark_s"], value["closed_bin_count"], value["denominator_bins"], value["kappa"], value["incumbent_threshold_n"], value["dispatch_identity_sha256"], value["release_identity_sha256"], value["completed"], value["sealed"], value["nontrainable"], value["noncontrol"], value["noncompletion"])
    raise CensoringError("observation schema is neither exact nor censored")


def legacy_gp_training_rows(observations: Sequence[Observation | Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(row.as_dict() for row in (validate_observation(value) for value in observations) if isinstance(row, ExactObservation))


def require_exact_for_legacy_gp(value: Observation | Mapping[str, Any]) -> ExactObservation:
    row = validate_observation(value)
    if isinstance(row, CensoredObservation): raise CensoringError("legacy GP cannot consume a censored observation as exact")
    return row


def hypothetical_ts_value(value: Observation | Mapping[str, Any]) -> float:
    row = validate_observation(value)
    return row.lower_bound_n if isinstance(row, CensoredObservation) else row.objective_n


__all__ = ["BIN_WIDTH_S", "CENSOR_SCHEMA", "CensorProtocol", "CensoredMAEAccumulator", "CensoredObservation", "CensoringError", "DENOMINATOR_BINS", "EXACT_SCHEMA", "ExactObservation", "GUARD_FRACTION", "KAPPA_END", "KAPPA_MIDPOINT", "KAPPA_START", "KAPPA_STEEPNESS", "Observation", "PROTOCOL_SCHEMA", "R009_CLOSED_BINS", "causal_lower_bound", "hypothetical_ts_value", "kappa_for_progress", "legacy_gp_training_rows", "require_exact_for_legacy_gp", "validate_observation"]
