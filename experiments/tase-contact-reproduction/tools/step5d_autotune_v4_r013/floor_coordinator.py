"""Replay-safe offline orchestration for the R013 budgeted floor campaign.

This module owns scheduling state only.  It never opens a transport, reads a
controller, or dispatches a trial.  The live owner can persist the typed
requests and results in the existing append-only R013 ledger and reconstruct
the coordinator from those records after a cold restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from scipy.stats import qmc

from .domain import (
    DOMAIN_LOG_BOUNDS,
    KI_LATTICE_ANCHOR,
    TARGET_FORCE_N,
    candidate_to_log_features,
    physical_candidate_key,
    snap_candidate_to_live_lattice,
)


FLOOR_POLICY_SCHEMA = "step5d.autotune-v4/r013-floor-discovery-policy-v1"
FLOOR_POLICY_VERSION = 1
FLOOR_TRIAL_SCHEMA = "step5d.autotune-v4/r013-floor-trial-spec-v1"
FLOOR_TRIAL_VERSION = 1
FLOOR_KEY_SCHEMA = "step5d.autotune-v4/r013-floor-trial-key-v1"
FLOOR_KEY_SCHEMA_V2 = "step5d.autotune-v4/r013-floor-trial-key-v2"
FLOOR_COORDINATOR_SCHEMA = "step5d.autotune-v4/r013-floor-coordinator-v1"
FLOOR_COORDINATOR_VERSION = 1
HANDOFF_AB_SCHEMA = "step5d.autotune-v4/r013-handoff-ab-plan-v1"
HANDOFF_AB_VERSION = 1
HANDOFF_SELECTION_RECEIPT_SCHEMA = (
    "step5d.autotune-v4/r013-handoff-selection-receipt-v1"
)
FLOOR_SOBOL_SEED = 13013
CORRECTION_BLOCK_SCHEMA_V1 = "step5d.autotune-v4/r013-correction-block-placeholder-v1"
CORRECTION_BLOCK_SCHEMA_V2 = "step5d.autotune-v4/r013-correction-block-weights-v2"
CORRECTION_BLOCK_VERSION = 2
CORRECTION_WEIGHT_BOUNDS_SCHEMA = "step5d.autotune-v4/r013-correction-weight-bounds-v1"
CORRECTION_NORMALIZATION_SCHEMA = "step5d.autotune-v4/r013-correction-normalization-v1"
LOCAL_RADIUS_POLICY_SCHEMA = "step5d.autotune-v4/r013-local-radius-policy-v1"
LOCAL_RADIUS_POLICY_VERSION = 1
DEFAULT_CORRECTION_WEIGHT_BOUNDS = ((-0.5, 0.5),) * 6
DEFAULT_CORRECTION_NORMALIZATION_SCALES = (1.0, 0.01, 0.002, 100.0, 1.0, 1.0)
_CORE_SOBOL_POINTS: list[tuple[float, ...]] = []
_CORE_SOBOL_ENGINE: Any | None = None


def _core_sobol_points(start: int, count: int) -> tuple[tuple[float, ...], ...]:
    """Generate the deterministic stream once; cursors remain ledger state."""
    end = int(start) + int(count)
    if start < 0 or count <= 0:
        raise FloorCoordinatorError("R013 Sobol cursor/count is invalid")
    if len(_CORE_SOBOL_POINTS) < end:
        global _CORE_SOBOL_ENGINE
        if _CORE_SOBOL_ENGINE is None:
            _CORE_SOBOL_ENGINE = qmc.Sobol(d=4, scramble=True, seed=FLOOR_SOBOL_SEED)
        points = _CORE_SOBOL_ENGINE.random(end - len(_CORE_SOBOL_POINTS))
        _CORE_SOBOL_POINTS.extend(tuple(float(value) for value in point) for point in points)
    return tuple(_CORE_SOBOL_POINTS[start:end])

BOUNDARY_NOVEL = "BOUNDARY_NOVEL"
CORE_SOBOL_NOVEL = "CORE_SOBOL_NOVEL"
CORE_BO_NOVEL = "CORE_BO_NOVEL"
CORRECTION_SOBOL_NOVEL = "CORRECTION_SOBOL_NOVEL"
CORRECTION_BO_NOVEL = "CORRECTION_BO_NOVEL"
POLISH_CORE_NOVEL = "POLISH_CORE_NOVEL"
POLISH_CORRECTION_NOVEL = "POLISH_CORRECTION_NOVEL"
SENTINEL = "SENTINEL"
REPEAT = "REPEAT"

NOVEL_ROLES = frozenset({
    BOUNDARY_NOVEL,
    CORE_SOBOL_NOVEL,
    CORE_BO_NOVEL,
    CORRECTION_SOBOL_NOVEL,
    CORRECTION_BO_NOVEL,
    POLISH_CORE_NOVEL,
    POLISH_CORRECTION_NOVEL,
})
ALL_ROLES = NOVEL_ROLES | {SENTINEL, REPEAT}


class FloorCoordinatorError(ValueError):
    """The typed floor policy, trial, or replay state is invalid."""


class RuntimePrimitiveNotInstalled(FloorCoordinatorError):
    """The scheduler reached a block with no installed runtime primitive."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise FloorCoordinatorError(f"R013 {name} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FloorCoordinatorError(f"R013 {name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise FloorCoordinatorError(f"R013 {name} must be finite")
    return parsed


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise FloorCoordinatorError("R013 floor value is not strict JSON") from exc


def _token(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LocalRadiusPolicyV1:
    """Bounded local Sobol boxes used by alternating block polish."""

    core_radius: tuple[float, float, float, float] = (0.5,) * 4
    correction_radius: tuple[float, float, float, float, float, float] = (0.125,) * 6
    schema: str = LOCAL_RADIUS_POLICY_SCHEMA
    version: int = LOCAL_RADIUS_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.schema != LOCAL_RADIUS_POLICY_SCHEMA or self.version != LOCAL_RADIUS_POLICY_VERSION:
            raise FloorCoordinatorError("R013 local-radius policy schema/version differs")
        if len(self.core_radius) != 4 or len(self.correction_radius) != 6:
            raise FloorCoordinatorError("R013 local-radius policy dimensions differ")
        core = tuple(_finite(value, "core local radius") for value in self.core_radius)
        correction = tuple(_finite(value, "correction local radius") for value in self.correction_radius)
        if any(value <= 0.0 or value > 2.0 for value in core):
            raise FloorCoordinatorError("R013 core local radius is out of bounds")
        if any(value <= 0.0 or value > 0.5 for value in correction):
            raise FloorCoordinatorError("R013 correction local radius is out of bounds")
        object.__setattr__(self, "core_radius", core)
        object.__setattr__(self, "correction_radius", correction)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "core_radius": list(self.core_radius),
            "correction_radius": list(self.correction_radius),
            "candidate_pool_size": 128,
            "fresh_pool_required": True,
            "q": 1,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LocalRadiusPolicyV1":
        required = {
            "schema", "version", "core_radius", "correction_radius",
            "candidate_pool_size", "fresh_pool_required", "q",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise FloorCoordinatorError("R013 local-radius policy fields differ")
        if (
            value["candidate_pool_size"] != 128
            or value["fresh_pool_required"] is not True
            or value["q"] != 1
        ):
            raise FloorCoordinatorError("R013 local-radius policy pool/q contract differs")
        return cls(
            core_radius=tuple(value["core_radius"]),
            correction_radius=tuple(value["correction_radius"]),
            schema=value["schema"],
            version=value["version"],
        )


@dataclass(frozen=True)
class BoundaryChallengeV1:
    name: str
    axis: str
    baseline_value: float
    outward_value: float
    schema: str = "step5d.autotune-v4/r013-boundary-challenge-v1"
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != "step5d.autotune-v4/r013-boundary-challenge-v1" or self.version != 1:
            raise FloorCoordinatorError("R013 boundary challenge schema/version differs")
        if not self.name or not self.axis:
            raise FloorCoordinatorError("R013 boundary challenge identity is missing")
        baseline = _finite(self.baseline_value, f"{self.name} baseline")
        outward = _finite(self.outward_value, f"{self.name} outward")
        if baseline == outward:
            raise FloorCoordinatorError("R013 boundary challenge must move outward")
        object.__setattr__(self, "baseline_value", baseline)
        object.__setattr__(self, "outward_value", outward)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "name": self.name,
            "axis": self.axis,
            "baseline_value": self.baseline_value,
            "outward_value": self.outward_value,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BoundaryChallengeV1":
        required = {"schema", "version", "name", "axis", "baseline_value", "outward_value"}
        if not isinstance(value, Mapping) or set(value) != required:
            raise FloorCoordinatorError("R013 boundary challenge fields differ")
        return cls(
            name=value["name"],
            axis=value["axis"],
            baseline_value=value["baseline_value"],
            outward_value=value["outward_value"],
            schema=value["schema"],
            version=value["version"],
        )


DEFAULT_BOUNDARY_CHALLENGES = (
    BoundaryChallengeV1("tau", "normal_filter_tau_s", 0.04375, 0.03094),
    BoundaryChallengeV1("Ko", "orientation_ko", 0.05, 0.03536),
    BoundaryChallengeV1("I/P", "force_i_gain_over_force_p_gain", 0.5, 0.7071),
)


@dataclass(frozen=True)
class FloorDiscoveryPolicyV1:
    """The complete, versioned budgeted-floor schedule contract."""

    novel_target: int = 200
    controller_core_target: int = 100
    correction_target: int = 60
    polish_target: int = 40
    core_initial_block_size: int = 16
    core_boundary_probe_count: int = 3
    core_initial_sobol_count: int = 13
    core_remaining_target: int = 84
    core_every_fifth_global_sobol: bool = True
    correction_sobol_count: int = 12
    correction_qlognei_count: int = 48
    polish_core_local_count: int = 20
    polish_correction_local_count: int = 20
    candidate_pool_size: int = 128
    sentinel_interval: int = 10
    adaptive_repeat_target_n: int = 3
    final_top_k: int = 3
    final_repeat_target_n: int = 5
    target_mae_n: float = 0.35
    checkpoint_only: bool = True
    no_mae_early_stop: bool = True
    same_failure_pause_after: int = 3
    sentinel_drift_threshold_n: float = 0.05
    sentinel_drift_sigma_multiplier: float = 3.0
    fixed_orientation_ko: float = 0.05
    fixed_motion_kp: float = 1.5
    target_force_n: float = TARGET_FORCE_N
    correction_weight_bounds: tuple[tuple[float, float], ...] = DEFAULT_CORRECTION_WEIGHT_BOUNDS
    correction_normalization_scales: tuple[float, ...] = DEFAULT_CORRECTION_NORMALIZATION_SCALES
    local_radius_policy: LocalRadiusPolicyV1 = field(default_factory=LocalRadiusPolicyV1)
    boundary_challenges: tuple[BoundaryChallengeV1, ...] = DEFAULT_BOUNDARY_CHALLENGES
    schema: str = FLOOR_POLICY_SCHEMA
    version: int = FLOOR_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.schema != FLOOR_POLICY_SCHEMA or type(self.version) is not int or self.version != FLOOR_POLICY_VERSION:
            raise FloorCoordinatorError("R013 floor discovery policy schema/version differs")
        integer_fields = (
            "novel_target", "controller_core_target", "correction_target", "polish_target",
            "core_initial_block_size", "core_boundary_probe_count", "core_initial_sobol_count",
            "core_remaining_target", "correction_sobol_count", "correction_qlognei_count",
            "polish_core_local_count", "polish_correction_local_count", "candidate_pool_size",
            "sentinel_interval", "adaptive_repeat_target_n", "final_top_k",
            "final_repeat_target_n", "same_failure_pause_after",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise FloorCoordinatorError(f"R013 floor policy {name} is invalid")
        if self.novel_target != 200 or self.controller_core_target != 100 or self.correction_target != 60 or self.polish_target != 40:
            raise FloorCoordinatorError("R013 floor policy novel block totals differ")
        if (
            self.core_initial_block_size != 16
            or self.core_boundary_probe_count != 3
            or self.core_initial_sobol_count != 13
            or self.core_remaining_target != 84
            or self.core_boundary_probe_count + self.core_initial_sobol_count != self.core_initial_block_size
            or self.core_initial_block_size + self.core_remaining_target != self.controller_core_target
            or self.correction_sobol_count + self.correction_qlognei_count != self.correction_target
            or self.polish_core_local_count + self.polish_correction_local_count != self.polish_target
        ):
            raise FloorCoordinatorError("R013 floor policy schedule totals differ")
        if self.core_every_fifth_global_sobol is not True:
            raise FloorCoordinatorError("R013 core policy must force every fifth global Sobol point")
        if self.candidate_pool_size != 128 or self.sentinel_interval != 10:
            raise FloorCoordinatorError("R013 floor policy pool/sentinel cadence differs")
        if self.adaptive_repeat_target_n != 3 or self.final_top_k != 3 or self.final_repeat_target_n != 5:
            raise FloorCoordinatorError("R013 floor policy repeat targets differ")
        if self.same_failure_pause_after != 3:
            raise FloorCoordinatorError("R013 floor policy failure pause threshold differs")
        if not isinstance(self.checkpoint_only, bool) or not isinstance(self.no_mae_early_stop, bool):
            raise FloorCoordinatorError("R013 floor policy completion flags are invalid")
        if not self.checkpoint_only or not self.no_mae_early_stop:
            raise FloorCoordinatorError("R013 floor policy must be checkpoint-only")
        if _finite(self.target_mae_n, "floor target") != 0.35:
            raise FloorCoordinatorError("R013 floor target must be exactly 0.35 N")
        if _finite(self.sentinel_drift_threshold_n, "sentinel drift threshold") != 0.05:
            raise FloorCoordinatorError("R013 sentinel drift threshold differs")
        if _finite(self.sentinel_drift_sigma_multiplier, "sentinel sigma multiplier") != 3.0:
            raise FloorCoordinatorError("R013 sentinel sigma multiplier differs")
        if _finite(self.fixed_orientation_ko, "fixed Ko") <= 0.0 or _finite(self.fixed_motion_kp, "fixed motion Kp") <= 0.0:
            raise FloorCoordinatorError("R013 fixed path values are invalid")
        if _finite(self.target_force_n, "target force") != TARGET_FORCE_N:
            raise FloorCoordinatorError("R013 target force differs")
        if len(self.correction_weight_bounds) != 6:
            raise FloorCoordinatorError("R013 correction weight bounds must have six entries")
        parsed_bounds: list[tuple[float, float]] = []
        for index, pair in enumerate(self.correction_weight_bounds):
            if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)) or len(pair) != 2:
                raise FloorCoordinatorError(f"R013 correction weight bound {index} is invalid")
            low = _finite(pair[0], f"correction weight bound {index} low")
            high = _finite(pair[1], f"correction weight bound {index} high")
            if low >= high or low < -0.5 or high > 0.5:
                raise FloorCoordinatorError("R013 correction weight bounds exceed the frozen contract")
            parsed_bounds.append((low, high))
        if len(self.correction_normalization_scales) != 6:
            raise FloorCoordinatorError("R013 correction normalization must have six entries")
        parsed_scales = tuple(
            _finite(value, f"correction normalization scale {index}")
            for index, value in enumerate(self.correction_normalization_scales)
        )
        if any(value <= 0.0 or value > 1000.0 for value in parsed_scales):
            raise FloorCoordinatorError("R013 correction normalization scales are out of bounds")
        object.__setattr__(self, "correction_weight_bounds", tuple(parsed_bounds))
        object.__setattr__(self, "correction_normalization_scales", parsed_scales)
        if isinstance(self.local_radius_policy, Mapping):
            object.__setattr__(
                self,
                "local_radius_policy",
                LocalRadiusPolicyV1.from_mapping(self.local_radius_policy),
            )
        if not isinstance(self.local_radius_policy, LocalRadiusPolicyV1):
            raise FloorCoordinatorError("R013 local-radius policy is not typed")
        if tuple(self.boundary_challenges) != DEFAULT_BOUNDARY_CHALLENGES:
            raise FloorCoordinatorError("R013 boundary challenge contract differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "novel_target": self.novel_target,
            "controller_core_target": self.controller_core_target,
            "correction_target": self.correction_target,
            "polish_target": self.polish_target,
            "core_initial_block_size": self.core_initial_block_size,
            "core_boundary_probe_count": self.core_boundary_probe_count,
            "core_initial_sobol_count": self.core_initial_sobol_count,
            "core_remaining_target": self.core_remaining_target,
            "core_every_fifth_global_sobol": self.core_every_fifth_global_sobol,
            "correction_sobol_count": self.correction_sobol_count,
            "correction_qlognei_count": self.correction_qlognei_count,
            "polish_core_local_count": self.polish_core_local_count,
            "polish_correction_local_count": self.polish_correction_local_count,
            "candidate_pool_size": self.candidate_pool_size,
            "sentinel_interval": self.sentinel_interval,
            "adaptive_repeat_target_n": self.adaptive_repeat_target_n,
            "final_top_k": self.final_top_k,
            "final_repeat_target_n": self.final_repeat_target_n,
            "target_mae_n": self.target_mae_n,
            "checkpoint_only": self.checkpoint_only,
            "no_mae_early_stop": self.no_mae_early_stop,
            "same_failure_pause_after": self.same_failure_pause_after,
            "sentinel_drift_threshold_n": self.sentinel_drift_threshold_n,
            "sentinel_drift_sigma_multiplier": self.sentinel_drift_sigma_multiplier,
            "fixed_orientation_ko": self.fixed_orientation_ko,
            "fixed_motion_kp": self.fixed_motion_kp,
            "target_force_n": self.target_force_n,
            "correction_weight_bounds_schema": CORRECTION_WEIGHT_BOUNDS_SCHEMA,
            "correction_weight_bounds": [list(value) for value in self.correction_weight_bounds],
            "correction_normalization_schema": CORRECTION_NORMALIZATION_SCHEMA,
            "correction_normalization_scales": list(self.correction_normalization_scales),
            "local_radius_policy": self.local_radius_policy.as_dict(),
            "boundary_challenges": [item.as_dict() for item in self.boundary_challenges],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "FloorDiscoveryPolicyV1":
        if value is None:
            return cls()
        required = set(cls().as_dict())
        if not isinstance(value, Mapping) or set(value) != required:
            raise FloorCoordinatorError("R013 floor discovery policy fields differ")
        boundaries = value["boundary_challenges"]
        if not isinstance(boundaries, Sequence) or isinstance(boundaries, (str, bytes)):
            raise FloorCoordinatorError("R013 floor boundary challenge list is invalid")
        parsed_boundaries = tuple(BoundaryChallengeV1.from_mapping(item) for item in boundaries)
        kwargs = {key: value[key] for key in required if key not in {
            "boundary_challenges", "local_radius_policy", "schema", "version",
            "correction_weight_bounds_schema", "correction_normalization_schema",
        }}
        if value["correction_weight_bounds_schema"] != CORRECTION_WEIGHT_BOUNDS_SCHEMA:
            raise FloorCoordinatorError("R013 correction weight bounds schema differs")
        if value["correction_normalization_schema"] != CORRECTION_NORMALIZATION_SCHEMA:
            raise FloorCoordinatorError("R013 correction normalization schema differs")
        return cls(
            **kwargs,
            local_radius_policy=LocalRadiusPolicyV1.from_mapping(value["local_radius_policy"]),
            boundary_challenges=parsed_boundaries,
            schema=value["schema"],
            version=value["version"],
        )


@dataclass(frozen=True)
class ControllerCoreBlockV1:
    """Exactly the four controller-core GP coordinates."""

    log2_force_p_over_d: float
    log2_force_damping: float
    log2_normal_filter_tau_s: float
    log2_force_i_over_p: float
    schema: str = "step5d.autotune-v4/r013-controller-core-block-v1"
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != "step5d.autotune-v4/r013-controller-core-block-v1" or self.version != 1:
            raise FloorCoordinatorError("R013 controller-core block schema/version differs")
        values = tuple(_finite(getattr(self, name), name) for name in (
            "log2_force_p_over_d", "log2_force_damping", "log2_normal_filter_tau_s", "log2_force_i_over_p",
        ))
        for name, value in zip((
            "log2_force_p_over_d", "log2_force_damping", "log2_normal_filter_tau_s", "log2_force_i_over_p",
        ), values, strict=True):
            object.__setattr__(self, name, value)

    @property
    def coordinates(self) -> tuple[float, float, float, float]:
        return (
            self.log2_force_p_over_d,
            self.log2_force_damping,
            self.log2_normal_filter_tau_s,
            self.log2_force_i_over_p,
        )

    @property
    def log2_p_over_d(self) -> float:
        return self.log2_force_p_over_d

    @property
    def log2_damping(self) -> float:
        return self.log2_force_damping

    @property
    def log2_tau(self) -> float:
        return self.log2_normal_filter_tau_s

    @property
    def log2_i_over_p(self) -> float:
        return self.log2_force_i_over_p

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "log2_force_p_over_d": self.log2_force_p_over_d,
            "log2_force_damping": self.log2_force_damping,
            "log2_normal_filter_tau_s": self.log2_normal_filter_tau_s,
            "log2_force_i_over_p": self.log2_force_i_over_p,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ControllerCoreBlockV1":
        required = {
            "schema", "version", "log2_force_p_over_d", "log2_force_damping",
            "log2_normal_filter_tau_s", "log2_force_i_over_p",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise FloorCoordinatorError("R013 controller-core block fields differ")
        return cls(**dict(value))


ROBUST_CORE_FREEZE_SCHEMA = "step5d.autotune-v4/r013-robust-core-freeze-receipt-v1"
ROBUST_CORE_NOT_QUALIFIED_SCHEMA = (
    "step5d.autotune-v4/r013-robust-core-not-qualified-receipt-v1"
)
CORRECTION_INCUMBENT_SCHEMA = (
    "step5d.autotune-v4/r013-compatible-correction-incumbent-receipt-v1"
)


@dataclass(frozen=True)
class RobustCoreIncumbentReceiptV1:
    """Identity-bound repeated controller-core incumbent freeze."""

    canonical_token: str
    n: int
    mean_n: float
    variance_n2: float
    controller_core: ControllerCoreBlockV1
    orientation_ko: float
    motion_kp: float
    fingerprint: Any
    guardrail_evidence: tuple[Mapping[str, Any], ...]
    schema: str = ROBUST_CORE_FREEZE_SCHEMA
    version: int = 1
    receipt_sha256: str = ""

    def __post_init__(self) -> None:
        if self.schema != ROBUST_CORE_FREEZE_SCHEMA or self.version != 1:
            raise FloorCoordinatorError("R013 robust-core freeze schema/version differs")
        if type(self.n) is not int or self.n < 3:
            raise FloorCoordinatorError("R013 robust-core freeze requires n>=3")
        mean = _finite(self.mean_n, "robust-core mean")
        variance = _finite(self.variance_n2, "robust-core variance")
        if mean < 0.0 or variance < 0.0:
            raise FloorCoordinatorError("R013 robust-core freeze statistics are invalid")
        ko = _finite(self.orientation_ko, "robust-core Ko")
        kp = _finite(self.motion_kp, "robust-core Kp")
        if ko <= 0.0 or kp <= 0.0:
            raise FloorCoordinatorError("R013 robust-core freeze path values are invalid")
        if not isinstance(self.canonical_token, str) or not self.canonical_token:
            raise FloorCoordinatorError("R013 robust-core freeze token is missing")
        if self.fingerprint is None or self.fingerprint == "":
            raise FloorCoordinatorError("R013 robust-core freeze fingerprint is missing")
        evidence = tuple(dict(row) for row in self.guardrail_evidence)
        if len(evidence) != self.n or any(
            row.get("passed") is not True or not row.get("receipt_id")
            for row in evidence
        ):
            raise FloorCoordinatorError("R013 robust-core freeze guardrail evidence is incomplete")
        object.__setattr__(self, "mean_n", mean)
        object.__setattr__(self, "variance_n2", variance)
        object.__setattr__(self, "orientation_ko", ko)
        object.__setattr__(self, "motion_kp", kp)
        object.__setattr__(self, "guardrail_evidence", evidence)
        expected = _token(self._payload())
        if self.receipt_sha256 and self.receipt_sha256 != expected:
            raise FloorCoordinatorError("R013 robust-core freeze receipt hash differs")
        object.__setattr__(self, "receipt_sha256", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "canonical_token": self.canonical_token,
            "n": self.n,
            "mean_n": self.mean_n,
            "variance_n2": self.variance_n2,
            "controller_core": self.controller_core.as_dict(),
            "orientation_ko": self.orientation_ko,
            "motion_kp": self.motion_kp,
            "fingerprint": self.fingerprint,
            "guardrail_evidence": [dict(row) for row in self.guardrail_evidence],
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RobustCoreIncumbentReceiptV1":
        required = {
            "schema", "version", "canonical_token", "n", "mean_n", "variance_n2",
            "controller_core", "orientation_ko", "motion_kp", "fingerprint",
            "guardrail_evidence", "receipt_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise FloorCoordinatorError("R013 robust-core freeze receipt fields differ")
        evidence = value["guardrail_evidence"]
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
            raise FloorCoordinatorError("R013 robust-core freeze guardrail evidence is invalid")
        return cls(
            canonical_token=value["canonical_token"],
            n=value["n"],
            mean_n=value["mean_n"],
            variance_n2=value["variance_n2"],
            controller_core=ControllerCoreBlockV1.from_mapping(value["controller_core"]),
            orientation_ko=value["orientation_ko"],
            motion_kp=value["motion_kp"],
            fingerprint=value["fingerprint"],
            guardrail_evidence=tuple(evidence),
            schema=value["schema"],
            version=value["version"],
            receipt_sha256=value["receipt_sha256"],
        )


@dataclass(frozen=True)
class CompatibleCorrectionIncumbentReceiptV1:
    """Repeated, guardrail-qualified correction incumbent for alternating polish."""

    canonical_token: str
    n: int
    mean_n: float
    variance_n2: float
    controller_core: ControllerCoreBlockV1
    correction: CorrectionBlockV1
    fingerprint: Any
    guardrail_evidence: tuple[Mapping[str, Any], ...]
    schema: str = CORRECTION_INCUMBENT_SCHEMA
    version: int = 1
    receipt_sha256: str = ""

    def __post_init__(self) -> None:
        if self.schema != CORRECTION_INCUMBENT_SCHEMA or self.version != 1:
            raise FloorCoordinatorError(
                "R013 compatible-correction incumbent schema/version differs"
            )
        if type(self.n) is not int or self.n < 1:
            raise FloorCoordinatorError("R013 correction incumbent requires n>=1")
        mean = _finite(self.mean_n, "correction incumbent mean")
        variance = _finite(self.variance_n2, "correction incumbent variance")
        if mean < 0.0 or variance < 0.0:
            raise FloorCoordinatorError("R013 correction incumbent statistics are invalid")
        if not isinstance(self.canonical_token, str) or not self.canonical_token:
            raise FloorCoordinatorError("R013 correction incumbent token is missing")
        if self.fingerprint is None or self.fingerprint == "":
            raise FloorCoordinatorError("R013 correction incumbent fingerprint is missing")
        evidence = tuple(dict(row) for row in self.guardrail_evidence)
        if len(evidence) != self.n or any(
            row.get("passed") is not True
            or row.get("guardrails_passed") is not True
            or not row.get("receipt_id")
            for row in evidence
        ):
            raise FloorCoordinatorError(
                "R013 correction incumbent guardrail evidence is incomplete"
            )
        object.__setattr__(self, "mean_n", mean)
        object.__setattr__(self, "variance_n2", variance)
        object.__setattr__(self, "guardrail_evidence", evidence)
        expected = _token(self._payload())
        if self.receipt_sha256 and self.receipt_sha256 != expected:
            raise FloorCoordinatorError("R013 correction incumbent receipt hash differs")
        object.__setattr__(self, "receipt_sha256", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "canonical_token": self.canonical_token,
            "n": self.n,
            "mean_n": self.mean_n,
            "variance_n2": self.variance_n2,
            "controller_core": self.controller_core.as_dict(),
            "correction": self.correction.as_dict(),
            "fingerprint": self.fingerprint,
            "guardrail_evidence": [dict(row) for row in self.guardrail_evidence],
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CompatibleCorrectionIncumbentReceiptV1":
        required = {
            "schema", "version", "canonical_token", "n", "mean_n", "variance_n2",
            "controller_core", "correction", "fingerprint", "guardrail_evidence",
            "receipt_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise FloorCoordinatorError("R013 correction incumbent receipt fields differ")
        evidence = value["guardrail_evidence"]
        if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
            raise FloorCoordinatorError("R013 correction incumbent guardrail evidence is invalid")
        return cls(
            canonical_token=value["canonical_token"],
            n=value["n"],
            mean_n=value["mean_n"],
            variance_n2=value["variance_n2"],
            controller_core=ControllerCoreBlockV1.from_mapping(value["controller_core"]),
            correction=CorrectionBlockV1.from_mapping(value["correction"]),
            fingerprint=value["fingerprint"],
            guardrail_evidence=tuple(evidence),
            schema=value["schema"],
            version=value["version"],
            receipt_sha256=value["receipt_sha256"],
        )


@dataclass(frozen=True)
class RobustCoreNotQualifiedReceiptV1:
    state: str
    reason: str
    fingerprint: Any
    candidate_group_count: int
    qualified_group_count: int
    required_n: int = 3
    schema: str = ROBUST_CORE_NOT_QUALIFIED_SCHEMA
    version: int = 1
    receipt_sha256: str = ""

    def __post_init__(self) -> None:
        if self.schema != ROBUST_CORE_NOT_QUALIFIED_SCHEMA or self.version != 1:
            raise FloorCoordinatorError("R013 robust-core not-qualified schema/version differs")
        if self.state != "robust_core_not_qualified" or self.reason == "":
            raise FloorCoordinatorError("R013 robust-core not-qualified receipt state differs")
        if type(self.required_n) is not int or self.required_n != 3:
            raise FloorCoordinatorError("R013 robust-core not-qualified requirement differs")
        if type(self.candidate_group_count) is not int or self.candidate_group_count < 0:
            raise FloorCoordinatorError("R013 robust-core group count is invalid")
        if type(self.qualified_group_count) is not int or self.qualified_group_count < 0:
            raise FloorCoordinatorError("R013 robust-core qualified count is invalid")
        if self.fingerprint is None or self.fingerprint == "":
            raise FloorCoordinatorError("R013 robust-core not-qualified fingerprint is missing")
        expected = _token(self._payload())
        if self.receipt_sha256 and self.receipt_sha256 != expected:
            raise FloorCoordinatorError("R013 robust-core not-qualified receipt hash differs")
        object.__setattr__(self, "receipt_sha256", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "state": self.state,
            "reason": self.reason,
            "fingerprint": self.fingerprint,
            "candidate_group_count": self.candidate_group_count,
            "qualified_group_count": self.qualified_group_count,
            "required_n": self.required_n,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RobustCoreNotQualifiedReceiptV1":
        required = {
            "schema", "version", "state", "reason", "fingerprint",
            "candidate_group_count", "qualified_group_count", "required_n", "receipt_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise FloorCoordinatorError("R013 robust-core not-qualified receipt fields differ")
        return cls(**dict(value))


@dataclass(frozen=True)
class CorrectionBlockV1:
    """Six real context-correction weights.

    ``legacy_placeholder_coordinates`` is populated only by the explicit
    v1-to-v2 replay migration.  It keeps an old ledger's canonical identity
    intact without ever treating its unit coordinates as Newton weights.
    """

    weights: tuple[float, float, float, float, float, float]
    legacy_placeholder_coordinates: tuple[float, float, float, float, float, float] | None = None
    schema: str = CORRECTION_BLOCK_SCHEMA_V2
    version: int = CORRECTION_BLOCK_VERSION

    def __post_init__(self) -> None:
        if self.schema != CORRECTION_BLOCK_SCHEMA_V2 or self.version != CORRECTION_BLOCK_VERSION:
            raise FloorCoordinatorError("R013 correction block schema/version differs")
        if len(self.weights) != 6:
            raise FloorCoordinatorError("R013 correction block must have six weights")
        parsed = tuple(_finite(value, "correction weight") for value in self.weights)
        if any(value < -0.5 or value > 0.5 for value in parsed):
            raise FloorCoordinatorError("R013 correction weight exceeds the frozen bounds")
        object.__setattr__(self, "weights", parsed)
        if self.legacy_placeholder_coordinates is not None:
            if len(self.legacy_placeholder_coordinates) != 6:
                raise FloorCoordinatorError("R013 legacy correction migration has six coordinates")
            object.__setattr__(
                self,
                "legacy_placeholder_coordinates",
                tuple(_finite(value, "legacy correction coordinate") for value in self.legacy_placeholder_coordinates),
            )

    @classmethod
    def from_unit_coordinates(
        cls,
        coordinates: Sequence[float],
        *,
        weight_bounds: Sequence[Sequence[float]] = DEFAULT_CORRECTION_WEIGHT_BOUNDS,
    ) -> "CorrectionBlockV1":
        if isinstance(coordinates, (str, bytes)) or len(coordinates) != 6 or len(weight_bounds) != 6:
            raise FloorCoordinatorError("R013 correction Sobol point must have six coordinates/bounds")
        weights: list[float] = []
        for index, (unit, pair) in enumerate(zip(coordinates, weight_bounds, strict=True)):
            value = _finite(unit, f"correction Sobol coordinate {index}")
            if not 0.0 <= value <= 1.0:
                raise FloorCoordinatorError("R013 correction Sobol coordinate is outside [0,1]")
            if len(pair) != 2:
                raise FloorCoordinatorError("R013 correction weight bound is invalid")
            low = _finite(pair[0], "correction weight low")
            high = _finite(pair[1], "correction weight high")
            weights.append(low + value * (high - low))
        return cls(tuple(weights))

    @property
    def is_legacy_migration(self) -> bool:
        return self.legacy_placeholder_coordinates is not None

    def as_dict(self) -> dict[str, Any]:
        if self.is_legacy_migration:
            return {
                "schema": CORRECTION_BLOCK_SCHEMA_V1,
                "version": 1,
                "coordinates": list(self.legacy_placeholder_coordinates or ()),
            }
        return {
            "schema": self.schema,
            "version": self.version,
            "weights": list(self.weights),
        }

    @property
    def vector(self) -> tuple[float, float, float, float, float, float]:
        return self.weights

    @property
    def coordinates(self) -> tuple[float, float, float, float, float, float]:
        """Canonical identity coordinates; legacy values remain identity-only."""
        return self.legacy_placeholder_coordinates or self.weights

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CorrectionBlockV1":
        if not isinstance(value, Mapping):
            raise FloorCoordinatorError("R013 correction block fields differ")
        if value.get("schema") == CORRECTION_BLOCK_SCHEMA_V1 and value.get("version") == 1:
            required = {"schema", "version", "coordinates"}
            if set(value) != required:
                raise FloorCoordinatorError("R013 legacy correction block fields differ")
            coordinates = value["coordinates"]
            if not isinstance(coordinates, Sequence) or isinstance(coordinates, (str, bytes)) or len(coordinates) != 6:
                raise FloorCoordinatorError("R013 legacy correction coordinates are invalid")
            # Explicit migration: old [0,1] placeholders become zero real
            # weights, while their old coordinates remain identity-only.
            return cls((0.0,) * 6, tuple(coordinates))
        required = {"schema", "version", "weights"}
        if set(value) != required:
            raise FloorCoordinatorError("R013 correction block fields differ")
        weights = value["weights"]
        if not isinstance(weights, Sequence) or isinstance(weights, (str, bytes)):
            raise FloorCoordinatorError("R013 correction weights are invalid")
        return cls(tuple(weights), schema=value["schema"], version=value["version"])


def _floor_key_payload(spec: "FloorTrialSpec") -> dict[str, Any]:
    if spec.correction.is_legacy_migration:
        return {
            "schema": FLOOR_KEY_SCHEMA,
            "version": 1,
            "active_block": spec.active_block,
            "controller_core": list(spec.controller_core.coordinates),
            "correction": list(spec.correction.coordinates),
            "orientation_ko": spec.orientation_ko,
            "motion_kp": spec.motion_kp,
            "target_force_n": spec.target_force_n,
            "boundary_name": spec.boundary_name,
        }
    return {
        "schema": FLOOR_KEY_SCHEMA_V2,
        "version": 2,
        "active_block": spec.active_block,
        "controller_core": list(spec.controller_core.coordinates),
        "correction_block_schema": spec.correction.schema,
        "correction_block_version": spec.correction.version,
        "correction_weights": list(spec.correction.weights),
        "orientation_ko": spec.orientation_ko,
        "motion_kp": spec.motion_kp,
        "target_force_n": spec.target_force_n,
        "boundary_name": spec.boundary_name,
    }


@dataclass(frozen=True)
class FloorTrialSpec:
    role: str
    ordinal: int
    active_block: str
    controller_core: ControllerCoreBlockV1
    correction: CorrectionBlockV1
    orientation_ko: float
    motion_kp: float
    target_force_n: float = TARGET_FORCE_N
    boundary_name: str | None = None
    proposal_kind: str = "deterministic"
    schema: str = FLOOR_TRIAL_SCHEMA
    version: int = FLOOR_TRIAL_VERSION

    def __post_init__(self) -> None:
        if self.schema != FLOOR_TRIAL_SCHEMA or self.version != FLOOR_TRIAL_VERSION:
            raise FloorCoordinatorError("R013 floor trial schema/version differs")
        if self.role not in ALL_ROLES:
            raise FloorCoordinatorError(f"R013 unknown floor trial role: {self.role}")
        if self.active_block not in {"controller_core", "correction", "polish_core", "polish_correction", "sentinel"}:
            raise FloorCoordinatorError("R013 floor trial active block differs")
        if type(self.ordinal) is not int or self.ordinal <= 0:
            raise FloorCoordinatorError("R013 floor trial ordinal is invalid")
        object.__setattr__(self, "orientation_ko", _finite(self.orientation_ko, "orientation Ko"))
        object.__setattr__(self, "motion_kp", _finite(self.motion_kp, "motion Kp"))
        object.__setattr__(self, "target_force_n", _finite(self.target_force_n, "target force"))
        if self.orientation_ko <= 0.0 or self.motion_kp <= 0.0 or self.target_force_n != TARGET_FORCE_N:
            raise FloorCoordinatorError("R013 floor trial fixed path values differ")

    @property
    def canonical_key(self) -> tuple[Any, ...]:
        payload = _floor_key_payload(self)
        correction_values = payload.get("correction", payload.get("correction_weights", ()))
        if self.correction.is_legacy_migration:
            return (
                payload["schema"], payload["version"], payload["active_block"],
                *payload["controller_core"], *correction_values,
                payload["orientation_ko"], payload["motion_kp"],
                payload["target_force_n"], payload["boundary_name"],
            )
        return (
            payload["schema"],
            payload["version"],
            payload["active_block"],
            *payload["controller_core"],
            payload.get("correction_block_schema", "legacy_placeholder"),
            payload.get("correction_block_version", 1),
            *correction_values,
            payload["orientation_ko"],
            payload["motion_kp"],
            payload["target_force_n"],
            payload["boundary_name"],
        )

    @property
    def ko(self) -> float:
        return self.orientation_ko

    @property
    def canonical_token(self) -> str:
        return _token(_floor_key_payload(self))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "role": self.role,
            "ordinal": self.ordinal,
            "active_block": self.active_block,
            "controller_core": self.controller_core.as_dict(),
            "correction": self.correction.as_dict(),
            "orientation_ko": self.orientation_ko,
            "motion_kp": self.motion_kp,
            "target_force_n": self.target_force_n,
            "boundary_name": self.boundary_name,
            "proposal_kind": self.proposal_kind,
            "canonical_key": list(self.canonical_key),
            "canonical_token": self.canonical_token,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FloorTrialSpec":
        required = {
            "schema", "version", "role", "ordinal", "active_block", "controller_core",
            "correction", "orientation_ko", "motion_kp", "target_force_n", "boundary_name",
            "proposal_kind", "canonical_key", "canonical_token",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise FloorCoordinatorError("R013 floor trial fields differ")
        parsed = cls(
            role=value["role"],
            ordinal=value["ordinal"],
            active_block=value["active_block"],
            controller_core=ControllerCoreBlockV1.from_mapping(value["controller_core"]),
            correction=CorrectionBlockV1.from_mapping(value["correction"]),
            orientation_ko=value["orientation_ko"],
            motion_kp=value["motion_kp"],
            target_force_n=value["target_force_n"],
            boundary_name=value["boundary_name"],
            proposal_kind=value["proposal_kind"],
            schema=value["schema"],
            version=value["version"],
        )
        if tuple(value["canonical_key"]) != parsed.canonical_key or value["canonical_token"] != parsed.canonical_token:
            raise FloorCoordinatorError("R013 floor trial canonical identity differs")
        return parsed


@dataclass(frozen=True)
class BlockProposalContract:
    block: str
    dimension_count: int
    q: int = 1
    acquisition: str = "qLogNEI"
    schema: str = "step5d.autotune-v4/r013-block-proposal-contract-v1"
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != "step5d.autotune-v4/r013-block-proposal-contract-v1" or self.version != 1:
            raise FloorCoordinatorError("R013 proposal contract schema/version differs")
        if self.block not in {"controller_core", "correction"} or self.dimension_count not in {4, 6} or self.q != 1:
            raise FloorCoordinatorError("R013 block proposal dimensions/q differ")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "block": self.block,
            "dimension_count": self.dimension_count,
            "q": self.q,
            "acquisition": self.acquisition,
        }


CORE_PROPOSAL_CONTRACT = BlockProposalContract("controller_core", 4)
CORRECTION_PROPOSAL_CONTRACT = BlockProposalContract("correction", 6)


@dataclass(frozen=True)
class FloorCandidateProposal:
    candidate: Mapping[str, Any]
    block_input: tuple[float, ...]
    contract: BlockProposalContract
    acquisition_value: float = 0.0
    q: int = 1
    posterior_beating_probability: float = 0.0
    incumbent_threshold_n: float | None = None
    fit_receipt: Mapping[str, Any] | None = None
    local_pool_receipt: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if len(self.block_input) != self.contract.dimension_count or self.q != 1:
            raise FloorCoordinatorError("R013 floor proposal dimension/q differs")
        if self.contract.block == "controller_core":
            physical_candidate_key(self.candidate)
        object.__setattr__(self, "block_input", tuple(_finite(value, "proposal coordinate") for value in self.block_input))
        probability = _finite(self.posterior_beating_probability, "proposal beating probability")
        if not 0.0 <= probability <= 1.0:
            raise FloorCoordinatorError("R013 proposal beating probability is invalid")
        object.__setattr__(self, "posterior_beating_probability", probability)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r013-floor-candidate-proposal-v1",
            "candidate": dict(self.candidate),
            "block_input": list(self.block_input),
            "contract": self.contract.as_dict(),
            "acquisition": self.contract.acquisition,
            "model_dimensions": self.contract.dimension_count,
            "acquisition_value": self.acquisition_value,
            "posterior_beating_probability": self.posterior_beating_probability,
            "incumbent_threshold_n": self.incumbent_threshold_n,
            "fit_receipt": None if self.fit_receipt is None else dict(self.fit_receipt),
            "local_pool_receipt": (
                None if self.local_pool_receipt is None else dict(self.local_pool_receipt)
            ),
            "q": self.q,
        }


@dataclass(frozen=True)
class FloorTrialRequest:
    request_id: str
    role: str
    novel_index: int
    trial: FloorTrialSpec
    runtime_candidate: Mapping[str, Any] | None
    executable: bool
    proposal_contract: BlockProposalContract
    reason: str
    repeat_index: int = 0
    posterior_beating_probability: float = 0.0
    proposal_receipt: Mapping[str, Any] | None = None
    schema: str = "step5d.autotune-v4/r013-floor-trial-request-v1"
    version: int = 1

    def __post_init__(self) -> None:
        if self.role != self.trial.role or self.role not in ALL_ROLES:
            raise FloorCoordinatorError("R013 floor request role differs")
        if type(self.novel_index) is not int or self.novel_index < 0:
            raise FloorCoordinatorError("R013 floor request novel index is invalid")
        if self.runtime_candidate is not None:
            physical_candidate_key(self.runtime_candidate)
        if (
            self.trial.correction.is_legacy_migration
            or any(abs(value) > 1e-12 for value in self.trial.correction.weights)
        ) and (self.runtime_candidate is not None or self.executable):
            raise FloorCoordinatorError(
                "R013 nonzero correction trial cannot be executable without the context runtime"
            )
        probability = _finite(self.posterior_beating_probability, "request beating probability")
        if not 0.0 <= probability <= 1.0:
            raise FloorCoordinatorError("R013 request beating probability is invalid")
        object.__setattr__(self, "posterior_beating_probability", probability)

    @property
    def canonical_token(self) -> str:
        return self.trial.canonical_token

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "request_id": self.request_id,
            "role": self.role,
            "novel_index": self.novel_index,
            "trial": self.trial.as_dict(),
            "runtime_candidate": None if self.runtime_candidate is None else dict(self.runtime_candidate),
            "executable": self.executable,
            "proposal_contract": self.proposal_contract.as_dict(),
            "reason": self.reason,
            "repeat_index": self.repeat_index,
            "posterior_beating_probability": self.posterior_beating_probability,
            "proposal_receipt": None if self.proposal_receipt is None else dict(self.proposal_receipt),
            "canonical_token": self.canonical_token,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FloorTrialRequest":
        required = {
            "schema", "version", "request_id", "role", "novel_index", "trial",
            "runtime_candidate", "executable", "proposal_contract", "reason",
            "repeat_index", "posterior_beating_probability", "proposal_receipt", "canonical_token",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise FloorCoordinatorError("R013 floor request fields differ")
        contract_value = value["proposal_contract"]
        if not isinstance(contract_value, Mapping):
            raise FloorCoordinatorError("R013 floor request proposal contract is invalid")
        contract_required = {"schema", "version", "block", "dimension_count", "q", "acquisition"}
        if set(contract_value) != contract_required:
            raise FloorCoordinatorError("R013 floor request proposal contract fields differ")
        request = cls(
            request_id=value["request_id"],
            role=value["role"],
            novel_index=value["novel_index"],
            trial=FloorTrialSpec.from_mapping(value["trial"]),
            runtime_candidate=value["runtime_candidate"],
            executable=value["executable"],
            proposal_contract=BlockProposalContract(**dict(contract_value)),
            reason=value["reason"],
            repeat_index=value["repeat_index"],
            posterior_beating_probability=value["posterior_beating_probability"],
            proposal_receipt=value["proposal_receipt"],
            schema=value["schema"],
            version=value["version"],
        )
        if value["canonical_token"] != request.canonical_token:
            raise FloorCoordinatorError("R013 floor request canonical identity differs")
        return request


def _base_candidate(policy: FloorDiscoveryPolicyV1) -> dict[str, Any]:
    candidate = {
        "force_p_gain": 0.006727171322157698,
        "force_damping": 56.0,
        "force_i_gain": KI_LATTICE_ANCHOR,
        "i_off": False,
        "normal_filter_tau_s": 0.04375,
        "orientation_ko": policy.fixed_orientation_ko,
        "motion_kp": policy.fixed_motion_kp,
        "target_force_n": policy.target_force_n,
    }
    return snap_candidate_to_live_lattice(candidate)


def _candidate_core(candidate: Mapping[str, Any]) -> ControllerCoreBlockV1:
    features = candidate_to_log_features(candidate)
    return ControllerCoreBlockV1(features[0], features[1], features[2], features[5])


def _candidate_from_core(
    coordinates: Sequence[float],
    *,
    policy: FloorDiscoveryPolicyV1,
) -> dict[str, Any]:
    if len(coordinates) != 4:
        raise FloorCoordinatorError("R013 controller-core proposal must have four coordinates")
    p_over_d = 2.0 ** _finite(coordinates[0], "core log2(P/D)")
    damping = 2.0 ** _finite(coordinates[1], "core log2(damping)")
    tau = 2.0 ** _finite(coordinates[2], "core log2(tau)")
    i_over_p = 2.0 ** _finite(coordinates[3], "core log2(I/P)")
    return snap_candidate_to_live_lattice({
        "force_p_gain": p_over_d * damping,
        "force_damping": damping,
        "force_i_gain": i_over_p * p_over_d * damping,
        "i_off": False,
        "normal_filter_tau_s": tau,
        "orientation_ko": policy.fixed_orientation_ko,
        "motion_kp": policy.fixed_motion_kp,
        "target_force_n": policy.target_force_n,
    })


def _core_pool_with_cursor(
    *,
    policy: FloorDiscoveryPolicyV1,
    cursor: int,
    evaluated_keys: Iterable[tuple[Any, ...]] = (),
    pending_keys: Iterable[tuple[Any, ...]] = (),
) -> tuple[tuple[dict[str, Any], ...], int]:
    excluded = {tuple(key) for key in (*evaluated_keys, *pending_keys)}
    selected: list[dict[str, Any]] = []
    selected_keys = set(excluded)
    generated = 0
    while len(selected) < policy.candidate_pool_size:
        points = _core_sobol_points(int(cursor) + generated, 256)
        generated += len(points)
        for point in points:
            logs = tuple(
                DOMAIN_LOG_BOUNDS[index][0]
                + float(point[position]) * (DOMAIN_LOG_BOUNDS[index][1] - DOMAIN_LOG_BOUNDS[index][0])
                for position, index in enumerate((0, 1, 2, 5))
            )
            try:
                candidate = _candidate_from_core(logs, policy=policy)
                key = physical_candidate_key(candidate)
            except (FloorCoordinatorError, ValueError):
                continue
            if key in selected_keys:
                continue
            selected_keys.add(key)
            selected.append(candidate)
            if len(selected) == policy.candidate_pool_size:
                return tuple(selected), int(cursor) + generated
    raise FloorCoordinatorError("R013 core Sobol pool failed to produce exactly 128 fresh candidates")


def core_candidate_pool(
    *,
    cursor: int = 0,
    evaluated_keys: Iterable[tuple[Any, ...]] = (),
    pending_keys: Iterable[tuple[Any, ...]] = (),
    policy: FloorDiscoveryPolicyV1 | None = None,
) -> tuple[dict[str, Any], ...]:
    parsed = policy or FloorDiscoveryPolicyV1()
    pool, _next_cursor = _core_pool_with_cursor(
        policy=parsed,
        cursor=cursor,
        evaluated_keys=evaluated_keys,
        pending_keys=pending_keys,
    )
    return pool


def _correction_vector_pool(*, cursor: int, policy: FloorDiscoveryPolicyV1, excluded: set[str]) -> tuple[tuple[float, ...], ...]:
    engine = qmc.Sobol(d=6, scramble=True, seed=FLOOR_SOBOL_SEED + 1)
    engine.fast_forward(int(cursor))
    result: list[tuple[float, ...]] = []
    for point in engine.random(256):
        block = CorrectionBlockV1.from_unit_coordinates(
            tuple(float(value) for value in point),
            weight_bounds=policy.correction_weight_bounds,
        )
        vector = block.weights
        token = _token({"schema": CORRECTION_BLOCK_SCHEMA_V2, "version": 2, "weights": list(vector)})
        if token in excluded:
            continue
        excluded.add(token)
        result.append(vector)
        if len(result) == policy.candidate_pool_size:
            return tuple(result)
    raise FloorCoordinatorError("R013 correction Sobol pool failed to produce exactly 128 fresh candidates")


CORE_FEATURE_INDICES = (0, 1, 2, 5)
CORE_LOG_BOUNDS = tuple(DOMAIN_LOG_BOUNDS[index] for index in CORE_FEATURE_INDICES)


def _local_box(
    center: Sequence[float],
    radius: Sequence[float],
    global_bounds: Sequence[Sequence[float]],
) -> tuple[tuple[float, float], ...]:
    if len(center) != len(radius) or len(center) != len(global_bounds):
        raise FloorCoordinatorError("R013 local box dimensions differ")
    bounds: list[tuple[float, float]] = []
    for index, (value, distance, pair) in enumerate(zip(center, radius, global_bounds, strict=True)):
        center_value = _finite(value, f"local center {index}")
        radius_value = _finite(distance, f"local radius {index}")
        low = _finite(pair[0], f"global local bound {index} low")
        high = _finite(pair[1], f"global local bound {index} high")
        clipped_low = max(low, center_value - radius_value)
        clipped_high = min(high, center_value + radius_value)
        if clipped_low >= clipped_high:
            raise FloorCoordinatorError("R013 clipped local box is degenerate")
        bounds.append((clipped_low, clipped_high))
    return tuple(bounds)


def _local_core_candidate_pool(
    *,
    center: Sequence[float],
    policy: FloorDiscoveryPolicyV1,
    cursor: int,
    excluded_keys: Iterable[tuple[Any, ...]],
) -> tuple[tuple[dict[str, Any], ...], tuple[tuple[float, float], ...]]:
    bounds = _local_box(center, policy.local_radius_policy.core_radius, CORE_LOG_BOUNDS)
    excluded = {tuple(key) for key in excluded_keys}
    engine = qmc.Sobol(d=4, scramble=True, seed=FLOOR_SOBOL_SEED + 2)
    engine.fast_forward(int(cursor))
    selected: list[dict[str, Any]] = []
    selected_keys = set(excluded)
    draws = 0
    while len(selected) < policy.candidate_pool_size:
        for point in engine.random(256):
            draws += 1
            if draws > 131072:
                raise FloorCoordinatorError(
                    "R013 local core Sobol pool cannot produce 128 fresh candidates"
                )
            logs = tuple(
                low + float(unit) * (high - low)
                for unit, (low, high) in zip(point, bounds, strict=True)
            )
            try:
                candidate = _candidate_from_core(logs, policy=policy)
            except (FloorCoordinatorError, ValueError):
                if draws >= 131072:
                    raise FloorCoordinatorError(
                        "R013 local core Sobol pool cannot produce 128 valid candidates"
                )
                continue
            actual_logs = _candidate_core(candidate).coordinates
            if any(
                value < low - 1e-12 or value > high + 1e-12
                for value, (low, high) in zip(actual_logs, bounds, strict=True)
            ):
                continue
            key = physical_candidate_key(candidate)
            if key in selected_keys:
                continue
            selected_keys.add(key)
            selected.append(candidate)
            if len(selected) == policy.candidate_pool_size:
                return tuple(selected), bounds
            if draws >= 131072:
                raise FloorCoordinatorError(
                    "R013 local core Sobol pool cannot produce 128 fresh candidates"
                )
    raise FloorCoordinatorError("R013 local core Sobol pool failed to produce exactly 128 candidates")


def _local_correction_weight_pool(
    *,
    center: Sequence[float],
    policy: FloorDiscoveryPolicyV1,
    cursor: int,
    excluded_tokens: set[str],
) -> tuple[tuple[tuple[float, ...], ...], tuple[tuple[float, float], ...]]:
    bounds = _local_box(
        center,
        policy.local_radius_policy.correction_radius,
        policy.correction_weight_bounds,
    )
    excluded = set(excluded_tokens)
    engine = qmc.Sobol(d=6, scramble=True, seed=FLOOR_SOBOL_SEED + 3)
    engine.fast_forward(int(cursor))
    selected: list[tuple[float, ...]] = []
    selected_tokens: set[str] = set(excluded)
    draws = 0
    while len(selected) < policy.candidate_pool_size:
        for point in engine.random(256):
            draws += 1
            if draws > 131072:
                raise FloorCoordinatorError(
                    "R013 local correction Sobol pool cannot produce 128 fresh candidates"
                )
            weights = tuple(
                low + float(unit) * (high - low)
                for unit, (low, high) in zip(point, bounds, strict=True)
            )
            token = _token({
                "schema": CORRECTION_BLOCK_SCHEMA_V2,
                "version": 2,
                "weights": list(weights),
            })
            if token in selected_tokens:
                continue
            selected_tokens.add(token)
            selected.append(weights)
            if len(selected) == policy.candidate_pool_size:
                return tuple(selected), bounds
            if draws >= 131072:
                raise FloorCoordinatorError(
                    "R013 local correction Sobol pool cannot produce 128 fresh candidates"
                )
    raise FloorCoordinatorError(
        "R013 local correction Sobol pool failed to produce exactly 128 candidates"
    )


def _local_pool_receipt(
    *,
    block: str,
    center: Sequence[float],
    bounds: Sequence[Sequence[float]],
    radius: Sequence[float],
    incumbent_receipt: CompatibleCorrectionIncumbentReceiptV1 | None = None,
) -> dict[str, Any]:
    dimension = 4 if block == "controller_core" else 6
    receipt = {
        "schema": "step5d.autotune-v4/r013-local-qlognei-pool-receipt-v1",
        "version": 1,
        "block": block,
        "local_center": list(center),
        "local_bounds": [list(pair) for pair in bounds],
        "radius": list(radius),
        "fresh_candidate_count": 128,
        "pool_size": 128,
        "model_dimensions": dimension,
        "acquisition": "qLogNoisyExpectedImprovement",
        "q": 1,
    }
    if incumbent_receipt is not None:
        receipt["combined_incumbent"] = {
            "controller_core": incumbent_receipt.controller_core.as_dict(),
            "correction": incumbent_receipt.correction.as_dict(),
            "correction_incumbent_receipt": incumbent_receipt.as_dict(),
        }
    return receipt


class FloorDiscoveryCoordinator:
    """Cold-reconstructable state machine for the 200-novel floor campaign."""

    def __init__(
        self,
        policy: FloorDiscoveryPolicyV1 | Mapping[str, Any] | None = None,
        *,
        fingerprint: Mapping[str, Any] | str | None = None,
        seed_candidate: Mapping[str, Any] | None = None,
        frozen_incumbent_n: float | None = None,
        records: Iterable[Mapping[str, Any]] = (),
        core_proposal_provider: Callable[[Sequence[dict[str, Any]]], Mapping[str, Any] | FloorCandidateProposal] | None = None,
        correction_proposal_provider: Callable[[Sequence[tuple[float, ...]]], Mapping[str, Any] | FloorCandidateProposal | Sequence[float]] | None = None,
    ) -> None:
        self.policy = policy if isinstance(policy, FloorDiscoveryPolicyV1) else FloorDiscoveryPolicyV1.from_mapping(policy)
        self.fingerprint = fingerprint
        self.seed_candidate = dict(seed_candidate or _base_candidate(self.policy))
        physical_candidate_key(self.seed_candidate)
        self.frozen_incumbent_n = (
            None if frozen_incumbent_n is None else _finite(frozen_incumbent_n, "frozen incumbent")
        )
        self.core_proposal_provider = core_proposal_provider
        self.correction_proposal_provider = correction_proposal_provider
        self._records: list[dict[str, Any]] = []
        self._new_records: list[dict[str, Any]] = []
        self._reset_state()
        for record in records:
            payload = dict(record.get("payload", record)) if isinstance(record, Mapping) else {}
            self._records.append(payload)
            self._apply_event(payload, replay=True)

    @classmethod
    def from_records(
        cls,
        policy: FloorDiscoveryPolicyV1 | Mapping[str, Any],
        records: Iterable[Mapping[str, Any]],
        *,
        fingerprint: Mapping[str, Any] | str | None = None,
        seed_candidate: Mapping[str, Any] | None = None,
        frozen_incumbent_n: float | None = None,
        core_proposal_provider: Callable[[Sequence[dict[str, Any]]], Mapping[str, Any] | FloorCandidateProposal] | None = None,
        correction_proposal_provider: Callable[[Sequence[tuple[float, ...]]], Mapping[str, Any] | FloorCandidateProposal | Sequence[float]] | None = None,
    ) -> "FloorDiscoveryCoordinator":
        return cls(
            policy,
            fingerprint=fingerprint,
            seed_candidate=seed_candidate,
            frozen_incumbent_n=frozen_incumbent_n,
            records=records,
            core_proposal_provider=core_proposal_provider,
            correction_proposal_provider=correction_proposal_provider,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, Any],
        records: Iterable[Mapping[str, Any]],
        *,
        seed_candidate: Mapping[str, Any] | None = None,
        frozen_incumbent_n: float | None = None,
        core_proposal_provider: Callable[[Sequence[dict[str, Any]]], Mapping[str, Any] | FloorCandidateProposal] | None = None,
        correction_proposal_provider: Callable[[Sequence[tuple[float, ...]]], Mapping[str, Any] | FloorCandidateProposal | Sequence[float]] | None = None,
    ) -> "FloorDiscoveryCoordinator":
        if not isinstance(snapshot, Mapping) or snapshot.get("schema") != FLOOR_COORDINATOR_SCHEMA:
            raise FloorCoordinatorError("R013 floor coordinator snapshot schema differs")
        coordinator = cls.from_records(
            FloorDiscoveryPolicyV1.from_mapping(snapshot.get("policy")),
            records,
            fingerprint=snapshot.get("fingerprint"),
            seed_candidate=seed_candidate,
            frozen_incumbent_n=frozen_incumbent_n,
            core_proposal_provider=core_proposal_provider,
            correction_proposal_provider=correction_proposal_provider,
        )
        if coordinator.snapshot() != dict(snapshot):
            raise FloorCoordinatorError("R013 floor coordinator snapshot does not replay")
        return coordinator

    def _reset_state(self) -> None:
        self.generation = 0
        self.state = "running"
        self.pause_reason: str | None = None
        self.runtime_primitive_state: str | None = None
        self.sobol_cursor = 0
        self.sobol_round = 0
        self.current_request: FloorTrialRequest | None = None
        self.retry_request: FloorTrialRequest | None = None
        self.novel_rows: list[dict[str, Any]] = []
        self.novel_keys: set[tuple[Any, ...]] = set()
        self.stats: dict[str, dict[str, Any]] = {}
        self.spec_catalog: dict[str, FloorTrialSpec] = {}
        self.runtime_catalog: dict[str, Mapping[str, Any] | None] = {}
        self.pool_seen_keys: set[tuple[Any, ...]] = set()
        self.repeat_queue: list[dict[str, Any]] = []
        self.final_queue: list[dict[str, Any]] = []
        self.final_queue_built = False
        self.sentinel_values: list[dict[str, Any]] = []
        self.sentinel_drift_streak = 0
        self.last_sentinel_guardrail_signature: str | None = None
        self.failure_signature: str | None = None
        self.failure_streak = 0
        self.boundary_decisions: list[dict[str, Any]] = []
        self._pending_boundary_decision: dict[str, Any] | None = None
        self.robust_core_freeze: RobustCoreIncumbentReceiptV1 | None = None
        self.robust_core_not_qualified_receipt: RobustCoreNotQualifiedReceiptV1 | None = None
        self.compatible_correction_incumbent: CompatibleCorrectionIncumbentReceiptV1 | None = None

    @property
    def event_log(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._records)

    def drain_events(self) -> tuple[Mapping[str, Any], ...]:
        pending = tuple(self._new_records)
        self._new_records.clear()
        return pending

    def _emit(self, event: Mapping[str, Any]) -> None:
        payload = {
            "schema": FLOOR_COORDINATOR_SCHEMA,
            "version": FLOOR_COORDINATOR_VERSION,
            "generation": self.generation,
            **dict(event),
        }
        self._records.append(payload)
        self._new_records.append(payload)

    def _apply_event(self, event: Mapping[str, Any], *, replay: bool) -> None:
        if event.get("schema") not in {None, FLOOR_COORDINATOR_SCHEMA}:
            raise FloorCoordinatorError("R013 floor coordinator record schema differs")
        if event.get("version", FLOOR_COORDINATOR_VERSION) != FLOOR_COORDINATOR_VERSION:
            raise FloorCoordinatorError("R013 floor coordinator record version differs")
        event_generation = int(event.get("generation", self.generation))
        if event.get("event") == "restart":
            self._reset_state()
            self.generation = event_generation
            self.fingerprint = event.get("fingerprint")
            return
        if event_generation != self.generation:
            raise FloorCoordinatorError("R013 floor coordinator record generation differs")
        kind = event.get("event")
        if kind == "request":
            request = FloorTrialRequest.from_mapping(event["request"])
            if self.current_request is not None:
                raise FloorCoordinatorError("R013 floor coordinator has overlapping requests")
            if request.role == REPEAT:
                if self.retry_request is not None and self.retry_request.canonical_token == request.canonical_token:
                    self.retry_request = None
                else:
                    queue = self.final_queue if self.novel_count >= self.policy.novel_target else self.repeat_queue
                    for index, item in enumerate(queue):
                        if item.get("canonical_token") == request.canonical_token:
                            del queue[index]
                            break
            self.current_request = request
            self.spec_catalog[request.canonical_token] = request.trial
            self.runtime_catalog[request.canonical_token] = request.runtime_candidate
            proposal_receipt = request.proposal_receipt
            if isinstance(proposal_receipt, Mapping):
                local_pool = proposal_receipt.get("local_pool_receipt")
                if isinstance(local_pool, Mapping):
                    incumbent_value = local_pool.get("combined_incumbent", {}).get(
                        "correction_incumbent_receipt"
                    ) if isinstance(local_pool.get("combined_incumbent"), Mapping) else None
                    if incumbent_value is not None:
                        receipt = CompatibleCorrectionIncumbentReceiptV1.from_mapping(
                            incumbent_value
                        )
                        self._validate_compatible_correction_receipt(receipt)
                        self.compatible_correction_incumbent = receipt
            if "sobol_cursor" in event:
                self.sobol_cursor = int(event["sobol_cursor"])
            if "sobol_round" in event:
                self.sobol_round = int(event["sobol_round"])
            return
        if kind == "result":
            self._apply_result(event)
            return
        if kind == "robust_core_freeze":
            receipt_value = event.get("receipt")
            if not isinstance(receipt_value, Mapping):
                raise FloorCoordinatorError("R013 robust-core freeze receipt is missing")
            if event.get("fingerprint", self.fingerprint) != self.fingerprint:
                raise FloorCoordinatorError("R013 robust-core freeze fingerprint differs")
            receipt = RobustCoreIncumbentReceiptV1.from_mapping(receipt_value)
            if receipt.fingerprint != self.fingerprint:
                raise FloorCoordinatorError("R013 robust-core freeze receipt fingerprint differs")
            stat = self.stats.get(receipt.canonical_token)
            if not isinstance(stat, Mapping):
                raise FloorCoordinatorError("R013 robust-core freeze group evidence is missing")
            values = tuple(float(value) for value in stat.get("values", ()))
            if len(values) != receipt.n:
                raise FloorCoordinatorError("R013 robust-core freeze group n differs")
            if not math.isclose(
                math.fsum(values) / len(values), receipt.mean_n, rel_tol=0.0, abs_tol=1e-12
            ):
                raise FloorCoordinatorError("R013 robust-core freeze group mean differs")
            expected_variance = (
                math.fsum(
                    (value - (math.fsum(values) / len(values))) ** 2
                    for value in values
                ) / (len(values) - 1)
                if len(values) >= 2 else 0.0
            )
            if not math.isclose(
                expected_variance, receipt.variance_n2, rel_tol=0.0, abs_tol=1e-12
            ):
                raise FloorCoordinatorError("R013 robust-core freeze group variance differs")
            trial = stat.get("trial")
            if not isinstance(trial, Mapping) or trial.get("active_block") != "controller_core":
                raise FloorCoordinatorError("R013 robust-core freeze group is not controller-core")
            if ControllerCoreBlockV1.from_mapping(trial["controller_core"]) != receipt.controller_core:
                raise FloorCoordinatorError("R013 robust-core freeze controller core differs")
            if not math.isclose(
                float(trial["orientation_ko"]), receipt.orientation_ko,
                rel_tol=0.0, abs_tol=1e-12,
            ) or not math.isclose(
                float(trial["motion_kp"]), receipt.motion_kp,
                rel_tol=0.0, abs_tol=1e-12,
            ):
                raise FloorCoordinatorError("R013 robust-core freeze Ko/Kp differs")
            evidence = stat.get("guardrail_evidence", ())
            if len(evidence) != receipt.n or any(
                row.get("passed") is not True for row in evidence
            ):
                raise FloorCoordinatorError("R013 robust-core freeze guardrails are incomplete")
            if tuple(dict(row) for row in evidence) != receipt.guardrail_evidence:
                raise FloorCoordinatorError("R013 robust-core freeze guardrail evidence differs")
            self.robust_core_freeze = receipt
            return
        if kind == "robust_core_not_qualified":
            receipt_value = event.get("receipt")
            if not isinstance(receipt_value, Mapping):
                raise FloorCoordinatorError("R013 robust-core not-qualified receipt is missing")
            receipt = RobustCoreNotQualifiedReceiptV1.from_mapping(receipt_value)
            if receipt.fingerprint != self.fingerprint:
                raise FloorCoordinatorError("R013 robust-core not-qualified fingerprint differs")
            self.robust_core_not_qualified_receipt = receipt
            self.state = "robust_core_not_qualified"
            return
        if kind == "runtime_primitive_not_installed":
            self.runtime_primitive_state = "runtime_primitive_not_installed"
            self.state = "runtime_primitive_not_installed"
            return
        if kind == "pause":
            self.state = "diagnostic_pause"
            self.pause_reason = str(event.get("reason", "diagnostic_pause"))
            self.runtime_primitive_state = None
            return
        if kind == "boundary_decision":
            decision = dict(event["decision"])
            if not any(
                row.get("canonical_token") == decision.get("canonical_token")
                for row in self.boundary_decisions
            ):
                self.boundary_decisions.append(decision)
            return
        if kind == "pool":
            if event.get("block") != "controller_core" or event.get("pool_size") != self.policy.candidate_pool_size:
                raise FloorCoordinatorError("R013 persisted pool receipt differs")
            self.sobol_cursor = int(event["sobol_cursor"])
            self.sobol_round = int(event["sobol_round"])
            candidate_keys = event.get("candidate_keys", ())
            if (
                len(event.get("candidate_tokens", ())) != self.policy.candidate_pool_size
                or len(candidate_keys) != self.policy.candidate_pool_size
            ):
                raise FloorCoordinatorError("R013 persisted pool is not exactly 128 candidates")
            self.pool_seen_keys.update(tuple(key) for key in candidate_keys)
            return
        raise FloorCoordinatorError(f"R013 unknown floor coordinator event: {kind!r}")

    def _apply_result(self, event: Mapping[str, Any]) -> None:
        request = self.current_request
        token = str(event.get("canonical_token", ""))
        if request is None or request.canonical_token != token:
            raise FloorCoordinatorError("R013 floor result does not match pending request")
        admitted = event.get("admitted") is True
        complete = event.get("complete") is True
        sealed = event.get("sealed") is True
        objective = event.get("sealed_mae_n")
        if objective is not None:
            objective = _finite(objective, "sealed MAE")
        failure_signature = event.get("failure_signature")
        if failure_signature:
            if failure_signature == self.failure_signature:
                self.failure_streak += 1
            else:
                self.failure_signature = str(failure_signature)
                self.failure_streak = 1
            if self.failure_streak >= self.policy.same_failure_pause_after:
                self.state = "diagnostic_pause"
                self.pause_reason = f"same_failure_signature:{self.failure_signature}"
        else:
            self.failure_signature = None
            self.failure_streak = 0
        if not (admitted and complete and sealed and objective is not None):
            self.retry_request = request
            self.current_request = None
            return
        stat = self.stats.setdefault(
            request.canonical_token,
            {
                "trial": request.trial.as_dict(),
                "runtime_candidate": None if request.runtime_candidate is None else dict(request.runtime_candidate),
                "role": request.role,
                "values": [],
                "is_novel": request.role in NOVEL_ROLES,
                "posterior_beating_probability": request.posterior_beating_probability,
                "guardrail_evidence": [],
            },
        )
        stat["posterior_beating_probability"] = max(
            float(stat.get("posterior_beating_probability", 0.0)),
            float(request.posterior_beating_probability),
            float(event.get("posterior_beating_probability", request.posterior_beating_probability) or 0.0),
        )
        stat["guardrail_evidence"].append({
            "passed": event.get("guardrails_passed") is True,
            "guardrails_passed": event.get("guardrails_passed"),
            "receipt_id": event.get("receipt_id"),
        })
        stat["values"].append(objective)
        stat["last_objective_n"] = objective
        if request.role in NOVEL_ROLES and request.canonical_token not in self.novel_keys:
            self.novel_keys.add(request.canonical_token)
            self.novel_rows.append({
                "canonical_token": request.canonical_token,
                "role": request.role,
                "sealed_mae_n": objective,
                "trial": request.trial.as_dict(),
            })
            if request.role == BOUNDARY_NOVEL:
                for repeat_index in (1, 2):
                    self.repeat_queue.append({
                        "canonical_token": request.canonical_token,
                        "target_n": self.policy.adaptive_repeat_target_n,
                        "reason": f"boundary_repeat:{request.trial.boundary_name}",
                        "repeat_index": repeat_index,
                    })
            self._maybe_schedule_adaptive_repeats()
        if request.role == SENTINEL:
            self._record_sentinel(request, objective, event)
        if request.role == REPEAT:
            self._maybe_record_boundary_decision(request, stat, event)
        self.current_request = None
        self.retry_request = None
        if self.state == "running" and self.failure_signature is None:
            self.failure_streak = 0

    def _maybe_schedule_adaptive_repeats(self) -> None:
        """Re-rank every novel candidate after every admitted novel result."""
        rank_limit = max(1, math.ceil(max(1, self.novel_count) * 0.05))
        ranked = sorted(
            (
                item for item in self.stats.values()
                if item.get("is_novel") and item.get("values")
            ),
            key=lambda item: (math.fsum(item["values"]) / len(item["values"]), item["trial"]["canonical_token"]),
        )
        tokens = {item["trial"]["canonical_token"] for item in ranked[:rank_limit]}
        queued_counts = Counter(item["canonical_token"] for item in self.repeat_queue)
        if self.retry_request is not None:
            queued_counts[self.retry_request.canonical_token] += 1
        for token in sorted(tokens):
            stat = self.stats[token]
            probability = _finite(
                stat.get("posterior_beating_probability", 0.0),
                "posterior beating probability",
            )
            if (
                probability >= 0.25
                and len(stat["values"]) < self.policy.adaptive_repeat_target_n
                and stat.get("role") != BOUNDARY_NOVEL
            ):
                missing = self.policy.adaptive_repeat_target_n - len(stat["values"])
                missing = max(0, missing - queued_counts[token])
                for offset in range(missing):
                    self.repeat_queue.append({
                        "canonical_token": token,
                        "target_n": self.policy.adaptive_repeat_target_n,
                        "reason": "adaptive_top5_posterior",
                        "repeat_index": len(stat["values"]) + queued_counts[token] + offset,
                    })
                queued_counts[token] += missing

    def _record_sentinel(self, request: FloorTrialRequest, objective: float, event: Mapping[str, Any]) -> None:
        sigma = max(0.0, _finite(event.get("sigma_pool", 0.0), "pool sigma"))
        guardrail = event.get("guardrail_signature")
        if self.last_sentinel_guardrail_signature is not None and guardrail != self.last_sentinel_guardrail_signature:
            self.state = "diagnostic_pause"
            self.pause_reason = "sentinel_guardrail_signature_changed"
        if self.sentinel_values:
            previous = self.sentinel_values[-1]
            threshold = max(
                self.policy.sentinel_drift_threshold_n,
                self.policy.sentinel_drift_sigma_multiplier * max(sigma, float(previous.get("sigma_pool", 0.0))),
            )
            if abs(objective - float(previous["sealed_mae_n"])) > threshold:
                self.sentinel_drift_streak += 1
            else:
                self.sentinel_drift_streak = 0
            if self.sentinel_drift_streak >= 2:
                self.state = "diagnostic_pause"
                self.pause_reason = "sentinel_drift"
        self.sentinel_values.append({
            "sealed_mae_n": objective,
            "sigma_pool": sigma,
            "guardrail_signature": guardrail,
            "receipt_id": event.get("receipt_id"),
            "canonical_token": request.canonical_token,
            "novel_count": self.novel_count,
        })
        self.last_sentinel_guardrail_signature = None if guardrail is None else str(guardrail)

    def _ensure_robust_core_freeze(self) -> RobustCoreIncumbentReceiptV1:
        if self.robust_core_freeze is not None:
            return self.robust_core_freeze
        if self.robust_core_not_qualified_receipt is not None:
            raise FloorCoordinatorError("R013 robust_core_not_qualified")
        groups: list[tuple[float, str, Mapping[str, Any], tuple[Mapping[str, Any], ...]]] = []
        for token, stat in self.stats.items():
            if stat.get("role") not in {CORE_SOBOL_NOVEL, CORE_BO_NOVEL}:
                continue
            values = tuple(float(value) for value in stat.get("values", ()))
            evidence = tuple(dict(row) for row in stat.get("guardrail_evidence", ()))
            if len(values) < 3 or len(evidence) != len(values):
                continue
            if any(row.get("passed") is not True or not row.get("receipt_id") for row in evidence):
                continue
            groups.append((math.fsum(values) / len(values), token, stat, evidence))
        if not groups:
            receipt = RobustCoreNotQualifiedReceiptV1(
                state="robust_core_not_qualified",
                reason="no_admitted_controller_core_observation_group_with_n3_complete_guardrails",
                fingerprint=self.fingerprint,
                candidate_group_count=sum(
                    1 for stat in self.stats.values()
                    if stat.get("role") in {CORE_SOBOL_NOVEL, CORE_BO_NOVEL}
                ),
                qualified_group_count=0,
            )
            event = {
                "event": "robust_core_not_qualified",
                "fingerprint": self.fingerprint,
                "receipt": receipt.as_dict(),
            }
            self._emit(event)
            self._apply_event(event, replay=True)
            raise FloorCoordinatorError("R013 robust_core_not_qualified")
        _mean, token, stat, evidence = min(groups, key=lambda item: (item[0], item[1]))
        trial = stat.get("trial")
        if not isinstance(trial, Mapping):
            raise FloorCoordinatorError("R013 robust-core freeze group trial evidence is missing")
        values = tuple(float(value) for value in stat["values"])
        mean = math.fsum(values) / len(values)
        variance = (
            math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
            if len(values) >= 2 else 0.0
        )
        receipt = RobustCoreIncumbentReceiptV1(
            canonical_token=token,
            n=len(values),
            mean_n=mean,
            variance_n2=variance,
            controller_core=ControllerCoreBlockV1.from_mapping(trial["controller_core"]),
            orientation_ko=trial["orientation_ko"],
            motion_kp=trial["motion_kp"],
            fingerprint=self.fingerprint,
            guardrail_evidence=evidence,
        )
        event = {
            "event": "robust_core_freeze",
            "fingerprint": self.fingerprint,
            "receipt": receipt.as_dict(),
        }
        self._emit(event)
        self._apply_event(event, replay=True)
        return receipt

    def _frozen_core(self) -> ControllerCoreBlockV1:
        return self._ensure_robust_core_freeze().controller_core

    def _validate_compatible_correction_receipt(
        self,
        receipt: CompatibleCorrectionIncumbentReceiptV1,
    ) -> None:
        if receipt.fingerprint != self.fingerprint:
            raise FloorCoordinatorError("R013 correction incumbent fingerprint differs")
        stat = self.stats.get(receipt.canonical_token)
        if not isinstance(stat, Mapping):
            raise FloorCoordinatorError("R013 correction incumbent group evidence is missing")
        values = tuple(float(value) for value in stat.get("values", ()))
        evidence = tuple(dict(row) for row in stat.get("guardrail_evidence", ()))
        if len(values) != receipt.n or len(evidence) != receipt.n:
            raise FloorCoordinatorError("R013 correction incumbent group n differs")
        if any(
            row.get("passed") is not True
            or row.get("guardrails_passed") is not True
            or not row.get("receipt_id")
            for row in evidence
        ):
            raise FloorCoordinatorError("R013 correction incumbent guardrails are incomplete")
        mean = math.fsum(values) / len(values)
        variance = (
            math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
            if len(values) >= 2 else 0.0
        )
        if not math.isclose(mean, receipt.mean_n, rel_tol=0.0, abs_tol=1e-12):
            raise FloorCoordinatorError("R013 correction incumbent group mean differs")
        if not math.isclose(variance, receipt.variance_n2, rel_tol=0.0, abs_tol=1e-12):
            raise FloorCoordinatorError("R013 correction incumbent group variance differs")
        trial = stat.get("trial")
        if not isinstance(trial, Mapping) or trial.get("active_block") not in {
            "correction", "polish_correction"
        }:
            raise FloorCoordinatorError("R013 correction incumbent group is not correction")
        trial_core = ControllerCoreBlockV1.from_mapping(trial["controller_core"])
        trial_correction = CorrectionBlockV1.from_mapping(trial["correction"])
        if trial_core != receipt.controller_core:
            raise FloorCoordinatorError("R013 correction incumbent controller core differs")
        if trial_correction != receipt.correction or trial_correction.is_legacy_migration:
            raise FloorCoordinatorError("R013 correction incumbent correction block differs")
        if tuple(dict(row) for row in evidence) != receipt.guardrail_evidence:
            raise FloorCoordinatorError("R013 correction incumbent guardrail evidence differs")

    def _best_compatible_correction_receipt(self) -> CompatibleCorrectionIncumbentReceiptV1:
        freeze = self._ensure_robust_core_freeze()
        groups: list[
            tuple[
                float,
                str,
                Mapping[str, Any],
                tuple[Mapping[str, Any], ...],
            ]
        ] = []
        for token, stat in self.stats.items():
            trial = stat.get("trial")
            if not isinstance(trial, Mapping) or trial.get("active_block") not in {
                "correction", "polish_correction"
            }:
                continue
            if ControllerCoreBlockV1.from_mapping(trial["controller_core"]) != freeze.controller_core:
                continue
            values = tuple(float(value) for value in stat.get("values", ()))
            evidence = tuple(dict(row) for row in stat.get("guardrail_evidence", ()))
            if not values or len(evidence) != len(values):
                continue
            if any(
                row.get("passed") is not True
                or row.get("guardrails_passed") is not True
                or not row.get("receipt_id")
                for row in evidence
            ):
                continue
            correction = CorrectionBlockV1.from_mapping(trial["correction"])
            if correction.is_legacy_migration:
                continue
            groups.append((math.fsum(values) / len(values), token, stat, evidence))
        if not groups:
            raise FloorCoordinatorError("R013 correction incumbent group evidence is missing")
        mean, token, stat, evidence = min(groups, key=lambda item: (item[0], item[1]))
        trial = stat["trial"]
        values = tuple(float(value) for value in stat["values"])
        variance = (
            math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
            if len(values) >= 2 else 0.0
        )
        receipt = CompatibleCorrectionIncumbentReceiptV1(
            canonical_token=token,
            n=len(values),
            mean_n=mean,
            variance_n2=variance,
            controller_core=ControllerCoreBlockV1.from_mapping(trial["controller_core"]),
            correction=CorrectionBlockV1.from_mapping(trial["correction"]),
            fingerprint=self.fingerprint,
            guardrail_evidence=evidence,
        )
        self._validate_compatible_correction_receipt(receipt)
        self.compatible_correction_incumbent = receipt
        return receipt

    def _best_compatible_correction(self) -> CorrectionBlockV1:
        return self._best_compatible_correction_receipt().correction

    def _candidate_for_request(self, spec: FloorTrialSpec) -> dict[str, Any] | None:
        if spec.active_block in {"correction", "polish_correction"}:
            return None
        if spec.correction.is_legacy_migration or any(
            abs(value) > 1e-12 for value in spec.correction.weights
        ):
            # The context-correction runtime primitive is deliberately absent
            # from this offline package.  Never project a core-only candidate
            # that silently drops a nonzero correction block.
            return None
        if spec.role == BOUNDARY_NOVEL and spec.boundary_name in {"tau", "I/P", "Ko"}:
            # The outward value is intentionally not projected into the old
            # executable tube.  Until the matching runtime primitive exists,
            # this typed challenge is fail-closed and cannot be dispatched.
            return None
        candidate = _candidate_from_core(spec.controller_core.coordinates, policy=self.policy)
        candidate["orientation_ko"] = self.policy.fixed_orientation_ko
        candidate["motion_kp"] = self.policy.fixed_motion_kp
        return snap_candidate_to_live_lattice(candidate)

    def _core_evaluated_key_set(self) -> set[tuple[Any, ...]]:
        """Include offline/non-executable core polish in fresh-pool exclusion."""

        keys = set(self.runtime_catalog_key_set())
        for spec in self.spec_catalog.values():
            if spec.active_block not in {"controller_core", "polish_core", "sentinel"}:
                continue
            try:
                keys.add(
                    physical_candidate_key(
                        _candidate_from_core(spec.controller_core.coordinates, policy=self.policy)
                    )
                )
            except (FloorCoordinatorError, ValueError):
                raise FloorCoordinatorError("R013 persisted core trial cannot form an exclusion key")
        return keys

    def _new_core_candidate(
        self,
        *,
        force_sobol: bool,
        local_center: Sequence[float] | None = None,
        local_receipt: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], FloorCandidateProposal]:
        evaluated = tuple(self._core_evaluated_key_set())
        if local_center is None:
            pool, next_cursor = _core_pool_with_cursor(
                policy=self.policy,
                cursor=self.sobol_cursor,
                evaluated_keys=evaluated,
                pending_keys=(),
            )
            pool_bounds = None
        else:
            pool, pool_bounds = _local_core_candidate_pool(
                center=local_center,
                policy=self.policy,
                cursor=self.sobol_cursor,
                excluded_keys=evaluated,
            )
            next_cursor = self.sobol_cursor + self.policy.candidate_pool_size
        candidate = pool[0]
        if not force_sobol and self.core_proposal_provider is None:
            raise RuntimePrimitiveNotInstalled(
                "runtime_primitive_not_installed: core qLogNEI proposal provider"
            )
        if not force_sobol:
            supplied = self.core_proposal_provider(pool)
            if isinstance(supplied, FloorCandidateProposal):
                if supplied.contract != CORE_PROPOSAL_CONTRACT:
                    raise FloorCoordinatorError("R013 core proposal provider contract is not four-dimensional")
                proposal = supplied
                candidate = dict(proposal.candidate)
            elif hasattr(supplied, "candidate") and hasattr(supplied, "acquisition_value"):
                candidate = dict(supplied.candidate)
                proposal = FloorCandidateProposal(
                    candidate,
                    _candidate_core(candidate).coordinates,
                    CORE_PROPOSAL_CONTRACT,
                    acquisition_value=float(supplied.acquisition_value),
                    posterior_beating_probability=float(
                        getattr(supplied, "posterior_beating_probability", 0.0)
                    ),
                    incumbent_threshold_n=getattr(supplied, "incumbent_threshold_n", None),
                    fit_receipt=getattr(supplied, "fit_receipt", None),
                )
            elif isinstance(supplied, Mapping):
                candidate = dict(supplied)
                if physical_candidate_key(candidate) in evaluated:
                    raise FloorCoordinatorError("R013 core proposal provider returned an evaluated candidate")
                proposal = FloorCandidateProposal(
                    candidate,
                    _candidate_core(candidate).coordinates,
                    CORE_PROPOSAL_CONTRACT,
                    acquisition_value=0.0,
                )
            else:
                raise FloorCoordinatorError("R013 core proposal provider returned an invalid candidate")
            if physical_candidate_key(candidate) not in {
                physical_candidate_key(item) for item in pool
            }:
                raise FloorCoordinatorError(
                    "R013 core proposal provider returned a point outside the supplied pool"
                )
            if physical_candidate_key(candidate) in evaluated:
                raise FloorCoordinatorError("R013 core proposal provider returned an evaluated candidate")
            if tuple(proposal.block_input) != _candidate_core(candidate).coordinates:
                raise FloorCoordinatorError("R013 core proposal provider block input differs from candidate")
        else:
            proposal = FloorCandidateProposal(
                candidate,
                _candidate_core(candidate).coordinates,
                CORE_PROPOSAL_CONTRACT,
                acquisition_value=0.0,
            )
        if local_receipt is not None:
            proposal = replace(proposal, local_pool_receipt=dict(local_receipt))
        self.sobol_cursor = next_cursor
        self.sobol_round += 1
        return candidate, proposal

    def _new_correction_proposal(
        self,
        *,
        local_center: Sequence[float] | None = None,
        local_receipt: Mapping[str, Any] | None = None,
    ) -> FloorCandidateProposal:
        if self.correction_proposal_provider is None:
            raise RuntimePrimitiveNotInstalled(
                "runtime_primitive_not_installed: correction 6D qLogNEI proposal provider"
            )
        if local_center is None:
            pool = _correction_vector_pool(
                cursor=self.sobol_cursor,
                policy=self.policy,
                excluded=self._correction_excluded_tokens(),
            )
        else:
            pool, _bounds = _local_correction_weight_pool(
                center=local_center,
                policy=self.policy,
                cursor=self.sobol_cursor,
                excluded_tokens=self._correction_excluded_tokens(),
            )
        pool_set = {tuple(item) for item in pool}
        supplied = self.correction_proposal_provider(pool)
        if isinstance(supplied, FloorCandidateProposal):
            if supplied.contract != CORRECTION_PROPOSAL_CONTRACT:
                raise FloorCoordinatorError("R013 correction provider contract is not six-dimensional")
            proposal = supplied
            values = tuple(proposal.block_input)
        elif hasattr(supplied, "weights") and hasattr(supplied, "acquisition_value"):
            values = tuple(supplied.weights)
            proposal = FloorCandidateProposal(
                candidate={},
                block_input=values,
                contract=CORRECTION_PROPOSAL_CONTRACT,
                acquisition_value=float(supplied.acquisition_value),
                posterior_beating_probability=float(
                    getattr(supplied, "posterior_probability_of_improvement", 0.0)
                ),
                incumbent_threshold_n=getattr(supplied, "improvement_threshold_n", None),
                fit_receipt=getattr(supplied, "fit_receipt", None),
            )
        elif isinstance(supplied, Mapping):
            if "coordinates" in supplied:
                raise FloorCoordinatorError("R013 correction provider returned legacy placeholder coordinates")
            values = supplied.get("weights", supplied.get("block_input"))
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise FloorCoordinatorError("R013 correction provider returned invalid weights")
            proposal = FloorCandidateProposal(
                candidate={},
                block_input=tuple(values),
                contract=CORRECTION_PROPOSAL_CONTRACT,
                acquisition_value=float(supplied.get("acquisition_value", 0.0)),
                posterior_beating_probability=float(
                    supplied.get("posterior_probability_of_improvement", supplied.get("posterior_beating_probability", 0.0))
                ),
                fit_receipt=supplied.get("fit_receipt"),
            )
        else:
            values = supplied
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise FloorCoordinatorError("R013 correction provider returned invalid coordinates")
            proposal = FloorCandidateProposal(
                candidate={},
                block_input=tuple(values),
                contract=CORRECTION_PROPOSAL_CONTRACT,
            )
        if tuple(values) not in pool_set:
            raise FloorCoordinatorError(
                "R013 correction provider returned a point outside the supplied pool"
            )
        if local_receipt is not None:
            proposal = replace(proposal, local_pool_receipt=dict(local_receipt))
        self.sobol_cursor += self.policy.candidate_pool_size
        self.sobol_round += 1
        return proposal

    def runtime_catalog_key_set(self) -> set[tuple[Any, ...]]:
        keys: set[tuple[Any, ...]] = set()
        for candidate in self.runtime_catalog.values():
            if candidate is not None:
                keys.add(physical_candidate_key(candidate))
        return keys

    def _boundary_spec(self, index: int, ordinal: int) -> FloorTrialSpec:
        challenge = self.policy.boundary_challenges[index]
        base_core = _candidate_core(_base_candidate(self.policy))
        core_coordinates = list(base_core.coordinates)
        if challenge.axis == "normal_filter_tau_s":
            core_coordinates[2] = math.log2(challenge.outward_value)
        elif challenge.axis == "force_i_gain_over_force_p_gain":
            core_coordinates[3] = math.log2(challenge.outward_value)
        core = ControllerCoreBlockV1(*core_coordinates)
        return FloorTrialSpec(
            role=BOUNDARY_NOVEL,
            ordinal=ordinal,
            active_block="controller_core",
            controller_core=core,
            correction=CorrectionBlockV1((0.0,) * 6),
            orientation_ko=challenge.outward_value if challenge.axis == "orientation_ko" else self.policy.fixed_orientation_ko,
            motion_kp=self.policy.fixed_motion_kp,
            target_force_n=self.policy.target_force_n,
            boundary_name=challenge.name,
            proposal_kind="named_outward_boundary",
        )

    def _make_novel_request(self) -> FloorTrialRequest:
        novel_index = len(self.novel_keys) + 1
        ordinal = len(self._records) + 1
        proposal_result: FloorCandidateProposal | None = None
        if novel_index <= self.policy.core_boundary_probe_count:
            spec = self._boundary_spec(novel_index - 1, ordinal)
            proposal = CORE_PROPOSAL_CONTRACT
            candidate = self._candidate_for_request(spec)
            reason = "core_initial_named_boundary"
        elif novel_index <= self.policy.core_initial_block_size:
            candidate, proposal_result = self._new_core_candidate(force_sobol=True)
            spec = FloorTrialSpec(
                role=CORE_SOBOL_NOVEL,
                ordinal=ordinal,
                active_block="controller_core",
                controller_core=_candidate_core(candidate),
                correction=CorrectionBlockV1((0.0,) * 6),
                orientation_ko=self.policy.fixed_orientation_ko,
                motion_kp=self.policy.fixed_motion_kp,
                target_force_n=self.policy.target_force_n,
                proposal_kind="fresh_global_sobol",
            )
            proposal = CORE_PROPOSAL_CONTRACT
            reason = "core_initial_fresh_sobol"
        elif novel_index <= self.policy.controller_core_target:
            position = novel_index - self.policy.core_initial_block_size
            force_sobol = position % 5 == 0
            candidate, proposal_result = self._new_core_candidate(force_sobol=force_sobol)
            spec = FloorTrialSpec(
                role=CORE_SOBOL_NOVEL if force_sobol else CORE_BO_NOVEL,
                ordinal=ordinal,
                active_block="controller_core",
                controller_core=_candidate_core(candidate),
                correction=CorrectionBlockV1((0.0,) * 6),
                orientation_ko=self.policy.fixed_orientation_ko,
                motion_kp=self.policy.fixed_motion_kp,
                target_force_n=self.policy.target_force_n,
                proposal_kind="fresh_global_sobol" if force_sobol else "qLogNEI",
            )
            proposal = CORE_PROPOSAL_CONTRACT
            reason = "core_remaining_every_fifth_sobol" if force_sobol else "core_remaining_serial_qlognei"
            candidate = self._candidate_for_request(spec)
        elif novel_index <= self.policy.controller_core_target + self.policy.correction_sobol_count:
            frozen_core = self._frozen_core()
            cursor = novel_index - self.policy.controller_core_target - 1
            vector = _correction_vector_pool(
                cursor=cursor,
                policy=self.policy,
                excluded=self._correction_excluded_tokens(),
            )[0]
            spec = FloorTrialSpec(
                role=CORRECTION_SOBOL_NOVEL,
                ordinal=ordinal,
                active_block="correction",
                controller_core=frozen_core,
                correction=CorrectionBlockV1(vector),
                orientation_ko=self.policy.fixed_orientation_ko,
                motion_kp=self.policy.fixed_motion_kp,
                target_force_n=self.policy.target_force_n,
                proposal_kind="fresh_correction_sobol",
            )
            proposal = CORRECTION_PROPOSAL_CONTRACT
            reason = "correction_fresh_sobol"
            candidate = None
        elif novel_index <= self.policy.controller_core_target + self.policy.correction_target:
            frozen_core = self._frozen_core()
            proposal_result = self._new_correction_proposal()
            vector = proposal_result.block_input
            spec = FloorTrialSpec(
                role=CORRECTION_BO_NOVEL,
                ordinal=ordinal,
                active_block="correction",
                controller_core=frozen_core,
                correction=CorrectionBlockV1(vector),
                orientation_ko=self.policy.fixed_orientation_ko,
                motion_kp=self.policy.fixed_motion_kp,
                target_force_n=self.policy.target_force_n,
                proposal_kind="qLogNEI",
            )
            proposal = CORRECTION_PROPOSAL_CONTRACT
            reason = "correction_serial_qlognei"
            candidate = None
        else:
            frozen_core = self._frozen_core()
            polish_index = novel_index - self.policy.controller_core_target - self.policy.correction_target
            active_core = polish_index % 2 == 1
            role = POLISH_CORE_NOVEL if active_core else POLISH_CORRECTION_NOVEL
            if active_core:
                best_correction_receipt = self._best_compatible_correction_receipt()
                local_center = frozen_core.coordinates
                local_bounds = _local_box(
                    local_center,
                    self.policy.local_radius_policy.core_radius,
                    CORE_LOG_BOUNDS,
                )
                local_receipt = _local_pool_receipt(
                    block="controller_core",
                    center=local_center,
                    bounds=local_bounds,
                    radius=self.policy.local_radius_policy.core_radius,
                    incumbent_receipt=best_correction_receipt,
                )
                candidate, proposal_result = self._new_core_candidate(
                    force_sobol=False,
                    local_center=local_center,
                    local_receipt=local_receipt,
                )
                base_core = _candidate_core(candidate)
                base_correction = best_correction_receipt.correction
                active_block = "polish_core"
                proposal = CORE_PROPOSAL_CONTRACT
            else:
                base_core = frozen_core
                best_correction_receipt = self._best_compatible_correction_receipt()
                best_correction = best_correction_receipt.correction
                local_center = best_correction.weights
                local_bounds = _local_box(
                    local_center,
                    self.policy.local_radius_policy.correction_radius,
                    self.policy.correction_weight_bounds,
                )
                local_receipt = _local_pool_receipt(
                    block="correction",
                    center=local_center,
                    bounds=local_bounds,
                    radius=self.policy.local_radius_policy.correction_radius,
                    incumbent_receipt=best_correction_receipt,
                )
                proposal_result = self._new_correction_proposal(
                    local_center=local_center,
                    local_receipt=local_receipt,
                )
                base_correction = CorrectionBlockV1(proposal_result.block_input)
                active_block = "polish_correction"
                proposal = CORRECTION_PROPOSAL_CONTRACT
            spec = FloorTrialSpec(
                role=role,
                ordinal=ordinal,
                active_block=active_block,
                controller_core=base_core,
                correction=base_correction,
                orientation_ko=self.policy.fixed_orientation_ko,
                motion_kp=self.policy.fixed_motion_kp,
                target_force_n=self.policy.target_force_n,
                proposal_kind="local_qlognei",
            )
            reason = "polish_alternating_local_qlognei"
            candidate = self._candidate_for_request(spec)
        request = FloorTrialRequest(
            request_id=f"floor-{self.generation}-{ordinal:04d}-{spec.canonical_token[:12]}",
            role=spec.role,
            novel_index=novel_index,
            trial=spec,
            runtime_candidate=candidate,
            executable=candidate is not None,
            proposal_contract=proposal,
            reason=reason,
            posterior_beating_probability=(
                0.0 if proposal_result is None else proposal_result.posterior_beating_probability
            ),
            proposal_receipt=(
                None if proposal_result is None else proposal_result.as_dict()
            ),
        )
        return request

    def _correction_excluded_tokens(self) -> set[str]:
        excluded: set[str] = set()
        for item in self.stats.values():
            trial = item.get("trial")
            if not isinstance(trial, Mapping):
                continue
            correction = trial.get("correction")
            if not isinstance(correction, Mapping):
                continue
            coordinates = correction.get("coordinates")
            weights = correction.get("weights")
            values = weights if weights is not None else coordinates
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                if weights is not None:
                    excluded.add(_token({
                        "schema": CORRECTION_BLOCK_SCHEMA_V2,
                        "version": 2,
                        "weights": list(values),
                    }))
                else:
                    excluded.add(_token({
                        "schema": CORRECTION_BLOCK_SCHEMA_V1,
                        "version": 1,
                        "coordinates": list(values),
                    }))
        return excluded

    def _repeat_request(self, item: Mapping[str, Any]) -> FloorTrialRequest:
        token = str(item["canonical_token"])
        spec = self.spec_catalog[token]
        candidate = self.runtime_catalog[token]
        contract = CORE_PROPOSAL_CONTRACT if spec.active_block in {"controller_core", "polish_core", "sentinel"} else CORRECTION_PROPOSAL_CONTRACT
        return FloorTrialRequest(
            request_id=f"floor-{self.generation}-repeat-{len(self._records) + 1:04d}-{token[:12]}",
            role=REPEAT,
            novel_index=len(self.novel_keys),
            trial=FloorTrialSpec(
                role=REPEAT,
                ordinal=spec.ordinal,
                active_block=spec.active_block if spec.active_block != "sentinel" else "sentinel",
                controller_core=spec.controller_core,
                correction=spec.correction,
                orientation_ko=spec.orientation_ko,
                motion_kp=spec.motion_kp,
                target_force_n=spec.target_force_n,
                boundary_name=spec.boundary_name,
                proposal_kind="repeat",
            ),
            runtime_candidate=None if candidate is None else dict(candidate),
            executable=candidate is not None,
            proposal_contract=contract,
            reason=str(item.get("reason", "repeat")),
            repeat_index=int(item.get("repeat_index", 0)),
        )

    def _sentinel_request(self) -> FloorTrialRequest:
        ordinal = len(self._records) + 1
        candidate = _base_candidate(self.policy)
        spec = FloorTrialSpec(
            role=SENTINEL,
            ordinal=ordinal,
            active_block="sentinel",
            controller_core=_candidate_core(candidate),
            correction=CorrectionBlockV1((0.0,) * 6),
            orientation_ko=self.policy.fixed_orientation_ko,
            motion_kp=self.policy.fixed_motion_kp,
            target_force_n=self.policy.target_force_n,
            proposal_kind="fixed_sentinel",
        )
        return FloorTrialRequest(
            request_id=f"floor-{self.generation}-sentinel-{len(self.sentinel_values) + 1:04d}",
            role=SENTINEL,
            novel_index=len(self.novel_keys),
            trial=spec,
            runtime_candidate=candidate,
            executable=True,
            proposal_contract=CORE_PROPOSAL_CONTRACT,
            reason="sentinel_before_next_novel",
        )

    def _ensure_final_queue(self) -> None:
        if self.final_queue_built:
            return
        ranked = sorted(
            (
                (math.fsum(item["values"]) / len(item["values"]), token, item)
                for token, item in self.stats.items()
                if item.get("is_novel") and item.get("values")
            ),
            key=lambda value: (value[0], value[1]),
        )
        for _mean, token, item in ranked[: self.policy.final_top_k]:
            item["final_top3"] = True
            remaining = self.policy.final_repeat_target_n - len(item["values"])
            for repeat_index in range(max(0, remaining)):
                self.final_queue.append({
                    "canonical_token": token,
                    "target_n": self.policy.final_repeat_target_n,
                    "reason": "final_top3_n5",
                    "repeat_index": len(item["values"]) + repeat_index,
                })
        self.final_queue_built = True

    @property
    def novel_count(self) -> int:
        return len(self.novel_keys)

    @property
    def target_checkpoint(self) -> bool:
        return any(float(row["sealed_mae_n"]) <= self.policy.target_mae_n for row in self.novel_rows)

    @property
    def final_repeats_complete(self) -> bool:
        if self.novel_count < self.policy.novel_target:
            return False
        self._ensure_final_queue()
        return not self.final_queue and all(
            len(item["values"]) >= self.policy.final_repeat_target_n
            for item in self.stats.values()
            if item.get("is_novel") and item.get("final_top3")
        )

    @property
    def complete(self) -> bool:
        return (
            self.state == "running"
            and self.novel_count >= self.policy.novel_target
            and not self.repeat_queue
            and not self.final_queue
            and not self.sentinel_due
            and self.final_repeats_complete
        )

    @property
    def sentinel_due(self) -> bool:
        return self.novel_count > 0 and self.novel_count % self.policy.sentinel_interval == 0 and not (
            self.current_request is not None and self.current_request.role == SENTINEL
        ) and not any(row.get("novel_count") == self.novel_count for row in self.sentinel_values)

    @property
    def status(self) -> dict[str, Any]:
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        if self.novel_count >= self.policy.novel_target:
            self._ensure_final_queue()
        return {
            "schema": FLOOR_COORDINATOR_SCHEMA,
            "version": FLOOR_COORDINATOR_VERSION,
            "policy": self.policy.as_dict(),
            "fingerprint": self.fingerprint,
            "generation": self.generation,
            "state": self.state,
            "pause_reason": self.pause_reason,
            "runtime_primitive_state": self.runtime_primitive_state,
            "novel_count": self.novel_count,
            "novel_role_counts": {
                role: sum(1 for row in self.novel_rows if row["role"] == role)
                for role in sorted(NOVEL_ROLES)
            },
            "candidate_pool_size": self.policy.candidate_pool_size,
            "sobol_state": {
                "schema": "step5d.autotune-v4/r013-floor-sobol-state-v1",
                "version": 1,
                "seed": FLOOR_SOBOL_SEED,
                "round_index": self.sobol_round,
                "cursor": self.sobol_cursor,
                "pool_size": self.policy.candidate_pool_size,
            },
            "sentinel_due": self.sentinel_due,
            "sentinel_count": len(self.sentinel_values),
            "sentinel_drift_streak": self.sentinel_drift_streak,
            "repeat_queue_count": len(self.repeat_queue),
            "final_queue_count": len(self.final_queue),
            "final_repeats_complete": self.final_repeats_complete,
            "target_checkpoint": self.target_checkpoint,
            "complete": self.complete,
            "boundary_decisions": list(self.boundary_decisions),
            "robust_core_freeze": (
                None if self.robust_core_freeze is None else self.robust_core_freeze.as_dict()
            ),
            "robust_core_not_qualified_receipt": (
                None
                if self.robust_core_not_qualified_receipt is None
                else self.robust_core_not_qualified_receipt.as_dict()
            ),
            "compatible_correction_incumbent": (
                None
                if self.compatible_correction_incumbent is None
                else self.compatible_correction_incumbent.as_dict()
            ),
            "current_request": None if self.current_request is None else self.current_request.as_dict(),
        }

    def candidate_pool(
        self,
        *,
        block: str = "controller_core",
        evaluated_keys: Iterable[tuple[Any, ...]] = (),
        pending_keys: Iterable[tuple[Any, ...]] = (),
    ) -> tuple[Any, ...]:
        if block == "controller_core":
            excluded = set(self._core_evaluated_key_set())
            excluded.update(tuple(key) for key in evaluated_keys)
            excluded.update(tuple(key) for key in pending_keys)
            pool, _next = _core_pool_with_cursor(
                policy=self.policy,
                cursor=self.sobol_cursor,
                evaluated_keys=excluded,
                pending_keys=(),
            )
            return pool
        if block == "correction":
            excluded = self._correction_excluded_tokens()
            return _correction_vector_pool(cursor=self.sobol_cursor, policy=self.policy, excluded=excluded)
        raise FloorCoordinatorError("R013 unknown floor proposal block")

    def next_request(self) -> FloorTrialRequest:
        if self.state == "diagnostic_pause":
            raise FloorCoordinatorError("R013 diagnostic pause requires a new fingerprint restart")
        if self.state == "robust_core_not_qualified":
            raise FloorCoordinatorError("R013 robust_core_not_qualified")
        if self.state == "runtime_primitive_not_installed":
            raise RuntimePrimitiveNotInstalled("runtime_primitive_not_installed")
        if self.current_request is not None:
            return self.current_request
        if self.complete:
            raise FloorCoordinatorError("R013 budgeted floor coordinator is complete")
        if self.retry_request is not None:
            request = self.retry_request
            self.retry_request = None
        elif self.repeat_queue:
            item = self.repeat_queue.pop(0)
            request = self._repeat_request(item)
        elif self.sentinel_due:
            request = self._sentinel_request()
        elif self.novel_count >= self.policy.novel_target:
            self._ensure_final_queue()
            if self.final_queue:
                request = self._repeat_request(self.final_queue.pop(0))
            else:
                raise FloorCoordinatorError("R013 final repeat queue is unexpectedly empty")
        else:
            try:
                request = self._make_novel_request()
            except RuntimePrimitiveNotInstalled as exc:
                self.runtime_primitive_state = "runtime_primitive_not_installed"
                self.state = "runtime_primitive_not_installed"
                self._emit({
                    "event": "runtime_primitive_not_installed",
                    "reason": str(exc),
                    "novel_index": len(self.novel_keys) + 1,
                })
                raise
        self.current_request = request
        self.spec_catalog[request.canonical_token] = request.trial
        self.runtime_catalog[request.canonical_token] = request.runtime_candidate
        self._emit({
            "event": "request",
            "request": request.as_dict(),
            "sobol_cursor": self.sobol_cursor,
            "sobol_round": self.sobol_round,
        })
        return request

    def ask(self) -> FloorTrialRequest:
        return self.next_request()

    def request_next(self) -> FloorTrialRequest:
        return self.next_request()

    def mark_runtime_primitive_not_installed(self, request: FloorTrialRequest | None = None) -> None:
        current = request or self.current_request
        if current is None:
            raise FloorCoordinatorError("R013 runtime primitive state lacks a request")
        self.runtime_primitive_state = "runtime_primitive_not_installed"
        self.state = "runtime_primitive_not_installed"
        self._emit({
            "event": "runtime_primitive_not_installed",
            "canonical_token": current.canonical_token,
            "role": current.role,
        })

    def next_candidate_pool(
        self,
        *,
        block: str = "controller_core",
        evaluated_keys: Iterable[tuple[Any, ...]] = (),
        pending_keys: Iterable[tuple[Any, ...]] = (),
    ) -> tuple[Any, ...]:
        """Consume one persisted Sobol pool round for offline replay tests."""
        if self.state != "running":
            raise FloorCoordinatorError(
                "R013 candidate pool is unavailable until an explicit new-fingerprint restart"
            )
        if block != "controller_core":
            raise FloorCoordinatorError("R013 persisted fresh pool is core-only until correction runtime installation")
        excluded = set(self.runtime_catalog_key_set())
        excluded.update(tuple(key) for key in evaluated_keys)
        excluded.update(tuple(key) for key in pending_keys)
        pool, next_cursor = _core_pool_with_cursor(
            policy=self.policy,
            cursor=self.sobol_cursor,
            evaluated_keys=excluded,
            pending_keys=(),
        )
        start_cursor = self.sobol_cursor
        self.sobol_cursor = next_cursor
        self.sobol_round += 1
        pool_keys = [physical_candidate_key(candidate) for candidate in pool]
        self.pool_seen_keys.update(pool_keys)
        self._emit({
            "event": "pool",
            "block": block,
            "start_cursor": start_cursor,
            "sobol_cursor": self.sobol_cursor,
            "sobol_round": self.sobol_round,
            "pool_size": len(pool),
            "candidate_tokens": [
                _token({"physical_key": list(physical_candidate_key(candidate))})
                for candidate in pool
            ],
            "candidate_keys": [list(key) for key in pool_keys],
        })
        return pool

    def record_result(
        self,
        *,
        admitted: bool,
        sealed_mae_n: float | None = None,
        complete: bool = True,
        sealed: bool = True,
        posterior_beating_probability: float | None = None,
        sigma_pool: float = 0.0,
        guardrail_signature: str | None = None,
        guardrails_passed: bool | None = None,
        failure_signature: str | None = None,
        receipt_id: str | None = None,
    ) -> Mapping[str, Any]:
        if self.current_request is None:
            raise FloorCoordinatorError("R013 floor result requires a pending request")
        event = {
            "event": "result",
            "canonical_token": self.current_request.canonical_token,
            "request_id": self.current_request.request_id,
            "admitted": bool(admitted),
            "complete": bool(complete),
            "sealed": bool(sealed),
            "sealed_mae_n": sealed_mae_n,
            "posterior_beating_probability": (
                self.current_request.posterior_beating_probability
                if posterior_beating_probability is None
                else posterior_beating_probability
            ),
            "sigma_pool": sigma_pool,
            "guardrail_signature": guardrail_signature,
            "guardrails_passed": guardrails_passed,
            "failure_signature": failure_signature,
            "receipt_id": receipt_id,
        }
        self._apply_result(event)
        self._emit(event)
        if self._pending_boundary_decision is not None:
            self._emit({
                "event": "boundary_decision",
                "decision": self._pending_boundary_decision,
            })
            self._pending_boundary_decision = None
        if self.state == "diagnostic_pause" and not any(item.get("event") == "pause" for item in self._new_records):
            self._emit({"event": "pause", "reason": self.pause_reason or "diagnostic_pause"})
        return event

    def tell(self, **kwargs: Any) -> Mapping[str, Any]:
        if "objective_n" in kwargs and "sealed_mae_n" not in kwargs:
            kwargs["sealed_mae_n"] = kwargs.pop("objective_n")
        return self.record_result(**kwargs)

    def record_observation(self, **kwargs: Any) -> Mapping[str, Any]:
        return self.tell(**kwargs)

    def _maybe_record_boundary_decision(
        self,
        request: FloorTrialRequest,
        stat: Mapping[str, Any],
        event: Mapping[str, Any],
    ) -> None:
        boundary_name = request.trial.boundary_name
        if not boundary_name or len(stat.get("values", ())) != self.policy.adaptive_repeat_target_n:
            return
        if any(row.get("boundary_name") == boundary_name for row in self.boundary_decisions):
            return
        values = tuple(float(value) for value in stat["values"])
        mean = math.fsum(values) / len(values)
        incumbent = self.frozen_incumbent_n
        improvement = None if incumbent is None else incumbent - mean
        guardrail_evidence = list(stat.get("guardrail_evidence", ()))
        guardrails_passed = (
            len(guardrail_evidence) == self.policy.adaptive_repeat_target_n
            and all(row.get("passed") is True for row in guardrail_evidence)
        )
        decision = {
            "schema": "step5d.autotune-v4/r013-boundary-decision-v1",
            "version": 1,
            "boundary_name": boundary_name,
            "canonical_token": request.canonical_token,
            "repeat_count": len(values),
            "sealed_mae_n": list(values),
            "repeat_mean_n": mean,
            "frozen_incumbent_n": incumbent,
            "improvement_n": improvement,
            "guardrails_passed": guardrails_passed,
            "guardrail_evidence": guardrail_evidence,
            "guardrails_passed_all_repeats": guardrails_passed,
            "expanded": bool(
                improvement is not None and improvement >= 0.01 and guardrails_passed
            ),
        }
        self.boundary_decisions.append(decision)
        self._pending_boundary_decision = decision

    def restart_with_new_fingerprint(self, fingerprint: Mapping[str, Any] | str) -> Mapping[str, Any]:
        if fingerprint == self.fingerprint:
            raise FloorCoordinatorError("R013 paused coordinator requires a different fingerprint")
        self.generation += 1
        event = {
            "event": "restart",
            "generation": self.generation,
            "fingerprint": fingerprint,
            "reason": "explicit_new_fingerprint_restart",
        }
        self._emit(event)
        self._apply_event(event, replay=True)
        # _apply_event above reset the state and intentionally did not emit a
        # second record.  Preserve the new fingerprint and generation.
        return event

    def restart(self, fingerprint: Mapping[str, Any] | str) -> Mapping[str, Any]:
        return self.restart_with_new_fingerprint(fingerprint)

    def boundary_decision(self, token: str) -> Mapping[str, Any] | None:
        return next((row for row in self.boundary_decisions if row.get("canonical_token") == token), None)


@dataclass(frozen=True)
class HandoffSelectionReceiptV1:
    """Hash-bound receipt for a complete typed State21 -> State25 A/B choice."""

    selected_policy: str
    selected_repeat_count: int
    evidence_sha256: str
    schema: str = HANDOFF_SELECTION_RECEIPT_SCHEMA
    version: int = 1
    receipt_sha256: str = ""

    def __post_init__(self) -> None:
        if self.schema != HANDOFF_SELECTION_RECEIPT_SCHEMA or self.version != 1:
            raise FloorCoordinatorError("R013 handoff selection receipt schema/version differs")
        if self.selected_policy not in {"blind_reset_v0", "freeze_carry_v1"}:
            raise FloorCoordinatorError("R013 handoff selection policy differs")
        if type(self.selected_repeat_count) is not int or self.selected_repeat_count < 5:
            raise FloorCoordinatorError("R013 handoff selection requires n>=5")
        if (
            type(self.evidence_sha256) is not str
            or len(self.evidence_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.evidence_sha256)
        ):
            raise FloorCoordinatorError("R013 handoff selection evidence hash is invalid")
        expected = _token(self._payload())
        if self.receipt_sha256 and self.receipt_sha256 != expected:
            raise FloorCoordinatorError("R013 handoff selection receipt hash differs")
        object.__setattr__(self, "receipt_sha256", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "selected_policy": self.selected_policy,
            "selected_repeat_count": self.selected_repeat_count,
            "evidence_sha256": self.evidence_sha256,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HandoffSelectionReceiptV1":
        required = {
            "schema", "version", "selected_policy", "selected_repeat_count",
            "evidence_sha256", "receipt_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise FloorCoordinatorError("R013 handoff selection receipt fields differ")
        return cls(**dict(value))


@dataclass
class HandoffABPlanV1:
    """Offline A/B evidence coordinator, outside the 200-novel ledger."""

    sequence: tuple[str, ...] = ("A", "B", "B", "A", "A", "B")
    schema: str = HANDOFF_AB_SCHEMA
    version: int = HANDOFF_AB_VERSION
    evidence: list[dict[str, Any]] = field(default_factory=list)
    provisional_policy: str | None = None
    selected_policy: str | None = None
    selected_repeat_count: int = 0

    def __post_init__(self) -> None:
        if self.schema != HANDOFF_AB_SCHEMA or self.version != HANDOFF_AB_VERSION:
            raise FloorCoordinatorError("R013 handoff A/B schema/version differs")
        if self.sequence != ("A", "B", "B", "A", "A", "B"):
            raise FloorCoordinatorError("R013 handoff A/B sequence differs")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HandoffABPlanV1":
        required = {
            "schema", "version", "sequence", "arm_policy", "evidence",
            "provisional_policy", "selected_policy", "selected_repeat_count", "complete_initial",
            "complete", "next_arm", "selection_receipt",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise FloorCoordinatorError("R013 handoff A/B snapshot fields differ")
        plan = cls(schema=value["schema"], version=value["version"])
        if tuple(value["sequence"]) != plan.sequence or value["arm_policy"] != plan.arm_policy:
            raise FloorCoordinatorError("R013 handoff A/B snapshot contract differs")
        for row in value["evidence"]:
            if not isinstance(row, Mapping):
                raise FloorCoordinatorError("R013 handoff A/B snapshot evidence is invalid")
            plan.record_evidence(str(row["arm"]), row)
        if (
            plan.provisional_policy != value["provisional_policy"]
            or plan.selected_policy != value["selected_policy"]
            or plan.selected_repeat_count != value["selected_repeat_count"]
            or plan.complete_initial != value["complete_initial"]
            or plan.complete != value["complete"]
            or plan.next_arm() != value["next_arm"]
        ):
            raise FloorCoordinatorError("R013 handoff A/B snapshot derived state differs")
        expected_receipt = plan.selection_receipt()
        supplied_receipt = value["selection_receipt"]
        if expected_receipt is None:
            if supplied_receipt is not None:
                raise FloorCoordinatorError("R013 incomplete handoff A/B snapshot has a receipt")
        else:
            if not isinstance(supplied_receipt, Mapping):
                raise FloorCoordinatorError("R013 complete handoff A/B snapshot lacks a receipt")
            if HandoffSelectionReceiptV1.from_mapping(supplied_receipt) != expected_receipt:
                raise FloorCoordinatorError("R013 handoff A/B selection receipt differs")
        return plan

    @property
    def arm_policy(self) -> dict[str, str]:
        return {"A": "blind_reset_v0", "B": "freeze_carry_v1"}

    @property
    def complete_initial(self) -> bool:
        return len(self.evidence) >= len(self.sequence)

    @property
    def complete(self) -> bool:
        return self.complete_initial and self.selected_policy is not None and self.selected_repeat_count >= 5

    def next_arm(self) -> str | None:
        if not self.complete_initial:
            return self.sequence[len(self.evidence)]
        arm_policy = self.provisional_policy or self.selected_policy
        if arm_policy is not None and self.selected_repeat_count < 5:
            return "A" if arm_policy == "blind_reset_v0" else "B"
        return None

    def _selection_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "sequence": list(self.sequence),
            "arm_policy": self.arm_policy,
            "evidence": [dict(row) for row in self.evidence],
            "provisional_policy": self.provisional_policy,
            "selected_policy": self.selected_policy,
            "selected_repeat_count": self.selected_repeat_count,
        }

    def selection_receipt(self) -> HandoffSelectionReceiptV1 | None:
        if not self.complete:
            return None
        return HandoffSelectionReceiptV1(
            selected_policy=str(self.selected_policy),
            selected_repeat_count=self.selected_repeat_count,
            evidence_sha256=_token(self._selection_payload()),
        )

    def ask(self) -> str | None:
        return self.next_arm()

    def _validate_evidence(self, arm: str, value: Mapping[str, Any]) -> dict[str, Any]:
        if arm not in self.arm_policy:
            raise FloorCoordinatorError("R013 handoff A/B arm differs")
        required = {
            "sealed", "sealed_mae_n", "gaps", "fmin_n", "fmax_n", "pose_error_m",
            "orientation_error_rad", "carry_reset_receipt_ids", "tmae5_n", "settling_time_s",
            "raw_evidence_refs",
        }
        if not isinstance(value, Mapping) or not required.issubset(value):
            raise FloorCoordinatorError("R013 handoff A/B evidence is incomplete")
        parsed = dict(value)
        if parsed["sealed"] is not True:
            raise FloorCoordinatorError("R013 handoff evidence must be sealed")
        for name in (
            "sealed_mae_n", "fmin_n", "fmax_n", "pose_error_m", "orientation_error_rad", "tmae5_n", "settling_time_s",
        ):
            parsed[name] = _finite(parsed[name], f"handoff {name}")
        if not isinstance(parsed["gaps"], Sequence) or isinstance(parsed["gaps"], (str, bytes)):
            raise FloorCoordinatorError("R013 handoff gaps are invalid")
        if not isinstance(parsed["carry_reset_receipt_ids"], Sequence) or isinstance(parsed["carry_reset_receipt_ids"], (str, bytes)) or not parsed["carry_reset_receipt_ids"]:
            raise FloorCoordinatorError("R013 handoff carry/reset receipts are missing")
        if not isinstance(parsed["raw_evidence_refs"], Sequence) or isinstance(parsed["raw_evidence_refs"], (str, bytes)) or not parsed["raw_evidence_refs"]:
            raise FloorCoordinatorError("R013 handoff raw evidence references are missing")
        parsed["arm"] = arm
        parsed["policy"] = self.arm_policy[arm]
        return parsed

    def record_evidence(self, arm: str, value: Mapping[str, Any]) -> Mapping[str, Any]:
        expected = self.next_arm()
        if expected != arm:
            raise FloorCoordinatorError(f"R013 handoff A/B expected arm {expected!r}")
        parsed = self._validate_evidence(arm, value)
        self.evidence.append(parsed)
        if len(self.evidence) == len(self.sequence):
            self.provisional_policy = self._select_policy()
            self.selected_repeat_count = 3
        elif len(self.evidence) > len(self.sequence):
            recomputed = self._select_policy()
            if recomputed != self.provisional_policy:
                self.provisional_policy = recomputed
                self.selected_policy = None
            self.selected_repeat_count = len(self._arm_rows(
                "A" if self.provisional_policy == "blind_reset_v0" else "B"
            ))
            if self.selected_repeat_count >= 5 and recomputed == self.provisional_policy:
                self.selected_policy = self.provisional_policy
        return parsed

    def tell(self, arm: str, evidence: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.record_evidence(arm, evidence)

    def _arm_rows(self, arm: str) -> list[dict[str, Any]]:
        return [row for row in self.evidence if row["arm"] == arm]

    def _select_policy(self) -> str:
        a = self._arm_rows("A")
        b = self._arm_rows("B")
        if len(a) < 3 or len(b) < 3:
            raise FloorCoordinatorError("R013 handoff A/B cannot select without 3 sealed rows per arm")
        mean_a = math.fsum(row["sealed_mae_n"] for row in a) / len(a)
        mean_b = math.fsum(row["sealed_mae_n"] for row in b) / len(b)
        if mean_b - mean_a > 0.02:
            return "blind_reset_v0"
        if mean_a - mean_b > 0.02:
            return "freeze_carry_v1"
        std_a = _sample_std(a, "sealed_mae_n")
        std_b = _sample_std(b, "sealed_mae_n")
        if not math.isclose(std_a, std_b, rel_tol=0.0, abs_tol=1e-12):
            return "blind_reset_v0" if std_a < std_b else "freeze_carry_v1"
        tmae_a = math.fsum(row["tmae5_n"] for row in a) / len(a)
        tmae_b = math.fsum(row["tmae5_n"] for row in b) / len(b)
        if not math.isclose(tmae_a, tmae_b, rel_tol=0.0, abs_tol=1e-12):
            return "blind_reset_v0" if tmae_a < tmae_b else "freeze_carry_v1"
        settling_a = math.fsum(row["settling_time_s"] for row in a) / len(a)
        settling_b = math.fsum(row["settling_time_s"] for row in b) / len(b)
        return "blind_reset_v0" if settling_a <= settling_b else "freeze_carry_v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "sequence": list(self.sequence),
            "arm_policy": self.arm_policy,
            "evidence": [dict(row) for row in self.evidence],
            "provisional_policy": self.provisional_policy,
            "selected_policy": self.selected_policy,
            "selected_repeat_count": self.selected_repeat_count,
            "complete_initial": self.complete_initial,
            "complete": self.complete,
            "next_arm": self.next_arm(),
            "selection_receipt": (
                None
                if self.selection_receipt() is None
                else self.selection_receipt().as_dict()
            ),
        }

    def materialize_handoff_policy(self) -> str:
        if not self.complete:
            raise FloorCoordinatorError("R013 handoff A/B winner lacks required repeat evidence")
        return str(self.selected_policy)


def _sample_std(rows: Sequence[Mapping[str, Any]], field_name: str) -> float:
    values = [float(row[field_name]) for row in rows]
    mean = math.fsum(values) / len(values)
    return math.sqrt(math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1))


__all__ = [
    "ALL_ROLES", "BOUNDARY_NOVEL", "CORE_BO_NOVEL", "CORE_PROPOSAL_CONTRACT",
    "CORE_SOBOL_NOVEL", "CORRECTION_BO_NOVEL", "CORRECTION_PROPOSAL_CONTRACT",
    "CORRECTION_SOBOL_NOVEL", "ControllerCoreBlockV1", "CorrectionBlockV1",
    "DEFAULT_BOUNDARY_CHALLENGES", "FLOOR_COORDINATOR_SCHEMA", "FLOOR_POLICY_SCHEMA",
    "FLOOR_KEY_SCHEMA", "FLOOR_KEY_SCHEMA_V2", "CORRECTION_BLOCK_SCHEMA_V1",
    "CORRECTION_BLOCK_SCHEMA_V2", "CORRECTION_NORMALIZATION_SCHEMA",
    "CORRECTION_WEIGHT_BOUNDS_SCHEMA",
    "FLOOR_POLICY_VERSION", "FLOOR_TRIAL_SCHEMA", "FloorCandidateProposal",
    "FloorCoordinatorError", "FloorDiscoveryCoordinator", "FloorDiscoveryPolicyV1",
    "FloorTrialRequest", "FloorTrialSpec", "HandoffABPlanV1",
    "HandoffSelectionReceiptV1", "LocalRadiusPolicyV1",
    "CompatibleCorrectionIncumbentReceiptV1", "RobustCoreIncumbentReceiptV1",
    "RobustCoreNotQualifiedReceiptV1",
    "CORRECTION_INCUMBENT_SCHEMA", "HANDOFF_SELECTION_RECEIPT_SCHEMA",
    "ROBUST_CORE_FREEZE_SCHEMA", "ROBUST_CORE_NOT_QUALIFIED_SCHEMA",
    "LOCAL_RADIUS_POLICY_SCHEMA", "LOCAL_RADIUS_POLICY_VERSION",
    "POLISH_CORE_NOVEL",
    "POLISH_CORRECTION_NOVEL", "REPEAT", "RuntimePrimitiveNotInstalled", "SENTINEL",
    "BoundaryChallengeV1", "BlockProposalContract", "core_candidate_pool",
]
