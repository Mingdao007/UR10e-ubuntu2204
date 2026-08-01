"""Fail-closed force MAE v2 and audit-only v1 shadow primitives."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Final

from .contracts import (
    FORCE_MAE_V2_SPEC,
    STEP6_R001_PROFILE,
    AutotuneTaskProfile,
    ForceMaeSpec,
    require_finite_float,
    validate_path_time,
)
from .errors import (
    ContractInvariantError,
    DuplicateIdentityConflictError,
    ForceEvidenceError,
    IncompleteCoverageError,
    InvalidForceSampleError,
)


FORMAL_WINDOW_START_S: Final[float] = FORCE_MAE_V2_SPEC.window_start_s
FORMAL_WINDOW_END_S: Final[float] = FORCE_MAE_V2_SPEC.window_end_s
FORMAL_BIN_WIDTH_S: Final[float] = FORCE_MAE_V2_SPEC.bin_width_s
FORMAL_BIN_COUNT: Final[int] = FORCE_MAE_V2_SPEC.bin_count
CENTER_CROSSING_WINDOW_START_S: Final[float] = 30.0
CENTER_CROSSING_WINDOW_END_S: Final[float] = 35.0
SEGMENT_COUNT: Final[int] = 11
SEGMENT_WIDTH_S: Final[float] = 5.0
LOBE_CLASSIFICATION_RULE: Final[str] = (
    "classify each complete formal bin from its full phase interval: right when "
    "phase_end <= pi (local along is non-negative), left when phase_start >= pi "
    "(local along is non-positive), and exclude the one bin straddling phase pi "
    "as an ambiguous center bin"
)

class MetricRole(str, Enum):
    OPTIMIZER_OBJECTIVE = "optimizer_objective"
    AUDIT_ONLY = "audit_only"


@dataclass(frozen=True, slots=True, order=True)
class ForceSampleIdentity:
    """Stable source plus sequence identity for one fresh aligned sample."""

    source_id: str
    sequence: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise InvalidForceSampleError("sample identity source_id must be non-empty")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise InvalidForceSampleError("sample identity sequence must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ForceSample:
    """One typed force sample with a required stable fresh-evidence identity."""

    path_time_s: float
    filtered_normal_n: float
    sample_identity: ForceSampleIdentity

    def __post_init__(self) -> None:
        try:
            path_time = validate_path_time(self.path_time_s)
        except ValueError as exc:
            raise InvalidForceSampleError(str(exc)) from exc
        try:
            force = require_finite_float(self.filtered_normal_n, "filtered_normal_n")
        except ContractInvariantError as exc:
            raise InvalidForceSampleError(str(exc)) from exc
        if not isinstance(self.sample_identity, ForceSampleIdentity):
            raise InvalidForceSampleError("sample_identity must be a ForceSampleIdentity")
        object.__setattr__(self, "path_time_s", path_time)
        object.__setattr__(self, "filtered_normal_n", force)


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start_s: float
    end_s: float

    def __post_init__(self) -> None:
        start = require_finite_float(self.start_s, "window.start_s")
        end = require_finite_float(self.end_s, "window.end_s")
        if start >= end:
            raise ContractInvariantError(f"time window must satisfy start < end, got [{start}, {end})")
        object.__setattr__(self, "start_s", start)
        object.__setattr__(self, "end_s", end)


@dataclass(frozen=True, slots=True)
class FormalBinMetrics:
    index: int
    window: TimeWindow
    distinct_sample_count: int
    v2_abs_error_mean_n: float
    v1_signed_force_mean_n: float
    v1_compat_abs_error_n: float


@dataclass(frozen=True, slots=True)
class ForceCoverage:
    """Audit metadata proving the exact formal evidence population."""

    formal_window: TimeWindow
    bin_width_s: float
    bin_count: int
    total_input_sample_count: int
    distinct_sample_count: int
    exact_replay_count: int
    in_window_distinct_sample_count: int
    pre_window_distinct_sample_count: int
    end_boundary_distinct_sample_count: int
    per_bin_distinct_sample_counts: tuple[int, ...]
    source_ids: tuple[str, ...]
    observed_path_time_min_s: float
    observed_path_time_max_s: float
    complete: bool

    def __post_init__(self) -> None:
        spec = STEP6_R001_PROFILE.force_mae_spec
        if self.formal_window != TimeWindow(spec.window_start_s, spec.window_end_s):
            raise ContractInvariantError("ForceCoverage formal window is not [5.0, 60.0)")
        if self.bin_width_s != spec.bin_width_s or self.bin_count != spec.bin_count:
            raise ContractInvariantError("ForceCoverage bin geometry is not the locked 550 x 0.1 s grid")
        if len(self.per_bin_distinct_sample_counts) != spec.bin_count:
            raise ContractInvariantError("ForceCoverage must report exactly 550 bin counts")
        if spec.requires_complete_coverage is not True:
            raise ContractInvariantError("locked ForceMaeSpec must require complete coverage")
        if spec.requires_distinct_sample_identities is not True:
            raise ContractInvariantError("locked ForceMaeSpec must require distinct identities")
        if not self.complete or any(count < 1 for count in self.per_bin_distinct_sample_counts):
            raise ContractInvariantError("complete ForceCoverage requires one distinct sample in every bin")

    @property
    def mae_spec_id(self) -> str:
        return STEP6_R001_PROFILE.mae_spec_id

    @property
    def semantic_fingerprint(self) -> str:
        return STEP6_R001_PROFILE.force_mae_spec.semantic_fingerprint

    @property
    def spec_fingerprint(self) -> str:
        return self.semantic_fingerprint

    @property
    def mae_spec_fingerprint(self) -> str:
        return self.semantic_fingerprint


@dataclass(frozen=True, slots=True)
class ForceDiagnostics:
    """Diagnostic-only aggregates; none is an eligibility or objective scalar."""

    right_lobe_mae_n: float
    left_lobe_mae_n: float
    center_crossing_window: TimeWindow
    center_crossing_mae_n: float
    segment_windows: tuple[TimeWindow, ...]
    segment_maes_n: tuple[float, ...]
    worst_segment_index: int
    worst_segment_window: TimeWindow
    worst_segment_mae_n: float
    lobe_imbalance_n: float
    right_lobe_bin_indices: tuple[int, ...]
    left_lobe_bin_indices: tuple[int, ...]
    ambiguous_center_bin_indices: tuple[int, ...]
    lobe_classification_rule: str = LOBE_CLASSIFICATION_RULE

    def __post_init__(self) -> None:
        if self.center_crossing_window != TimeWindow(
            CENTER_CROSSING_WINDOW_START_S,
            CENTER_CROSSING_WINDOW_END_S,
        ):
            raise ContractInvariantError("center crossing diagnostic window is not [30.0, 35.0)")
        if len(self.segment_windows) != SEGMENT_COUNT or len(self.segment_maes_n) != SEGMENT_COUNT:
            raise ContractInvariantError("Step6 diagnostics require exactly eleven 5 s segments")
        if self.worst_segment_index not in range(SEGMENT_COUNT):
            raise ContractInvariantError("worst_segment_index must be a zero-based segment index")


@dataclass(frozen=True, slots=True)
class ForceMetrics:
    """Successful force evidence result with explicit v2/v1 metric roles."""

    force_mae_v2_n: float
    force_mae_v1_compat_shadow_n: float
    v2_role: MetricRole
    v1_role: MetricRole
    coverage: ForceCoverage
    bins: tuple[FormalBinMetrics, ...]
    diagnostics: ForceDiagnostics

    def __post_init__(self) -> None:
        for label in ("force_mae_v2_n", "force_mae_v1_compat_shadow_n"):
            require_finite_float(getattr(self, label), label)
        if self.v2_role is not MetricRole.OPTIMIZER_OBJECTIVE:
            raise ContractInvariantError("v2 must be the optimizer objective role")
        if self.v1_role is not MetricRole.AUDIT_ONLY:
            raise ContractInvariantError("v1 compatibility shadow must remain audit-only")
        if len(self.bins) != STEP6_R001_PROFILE.force_mae_spec.bin_count:
            raise ContractInvariantError("ForceMetrics must expose all 550 complete formal bins")
        if self.coverage.semantic_fingerprint != STEP6_R001_PROFILE.force_mae_spec.semantic_fingerprint:
            raise ContractInvariantError("ForceMetrics coverage fingerprint is not the bound force_mae_v2 fingerprint")

    @property
    def force_mae_v1_compat_n(self) -> float:
        """Explicit value alias; the role remains audit-only via ``v1_role``."""

        return self.force_mae_v1_compat_shadow_n

    @property
    def mae_spec_id(self) -> str:
        return STEP6_R001_PROFILE.mae_spec_id

    @property
    def semantic_fingerprint(self) -> str:
        return STEP6_R001_PROFILE.force_mae_spec.semantic_fingerprint

    @property
    def spec_fingerprint(self) -> str:
        return self.semantic_fingerprint

    @property
    def mae_spec_fingerprint(self) -> str:
        return self.semantic_fingerprint


def _formal_bin_index(path_time_s: float, spec: ForceMaeSpec) -> int | None:
    decimal_time = Decimal(str(path_time_s))
    decimal_start = Decimal(str(spec.window_start_s))
    decimal_end = Decimal(str(spec.window_end_s))
    decimal_width = Decimal(str(spec.bin_width_s))
    if decimal_time < decimal_start or decimal_time >= decimal_end:
        return None
    index = int((decimal_time - decimal_start) // decimal_width)
    if index < 0 or index >= spec.bin_count:
        raise ForceEvidenceError(f"formal bin index out of range for path_time_s={path_time_s!r}")
    return index


def _formal_bin_window(index: int, spec: ForceMaeSpec) -> TimeWindow:
    decimal_start = Decimal(str(spec.window_start_s))
    decimal_width = Decimal(str(spec.bin_width_s))
    start = float(decimal_start + decimal_width * index)
    end = float(decimal_start + decimal_width * (index + 1))
    return TimeWindow(start, end)


def _mean(values: Iterable[float], *, label: str) -> float:
    values_tuple = tuple(values)
    if not values_tuple:
        raise ForceEvidenceError(f"cannot average empty evidence for {label}")
    result = math.fsum(values_tuple) / len(values_tuple)
    if not math.isfinite(result):
        raise ForceEvidenceError(f"non-finite aggregate for {label}")
    return result


def _build_diagnostics(
    bins: tuple[FormalBinMetrics, ...],
    profile: AutotuneTaskProfile,
) -> ForceDiagnostics:
    right_indices: list[int] = []
    left_indices: list[int] = []
    ambiguous_indices: list[int] = []
    for formal_bin in bins:
        phase_start = profile.along_frequency_rad_s * formal_bin.window.start_s
        phase_end = profile.along_frequency_rad_s * formal_bin.window.end_s
        if phase_end <= math.pi:
            right_indices.append(formal_bin.index)
        elif phase_start >= math.pi:
            left_indices.append(formal_bin.index)
        else:
            ambiguous_indices.append(formal_bin.index)

    right_mae = _mean((bins[index].v2_abs_error_mean_n for index in right_indices), label="right lobe MAE")
    left_mae = _mean((bins[index].v2_abs_error_mean_n for index in left_indices), label="left lobe MAE")

    center_bins = tuple(
        formal_bin
        for formal_bin in bins
        if formal_bin.window.start_s >= CENTER_CROSSING_WINDOW_START_S
        and formal_bin.window.end_s <= CENTER_CROSSING_WINDOW_END_S
    )
    if len(center_bins) != 50:
        raise ForceEvidenceError("center-crossing diagnostic must contain exactly fifty complete bins")
    center_mae = _mean((formal_bin.v2_abs_error_mean_n for formal_bin in center_bins), label="center crossing MAE")

    segment_windows = tuple(
        TimeWindow(5.0 + SEGMENT_WIDTH_S * index, 5.0 + SEGMENT_WIDTH_S * (index + 1))
        for index in range(SEGMENT_COUNT)
    )
    segment_maes: list[float] = []
    for segment in segment_windows:
        segment_bins = tuple(
            formal_bin
            for formal_bin in bins
            if formal_bin.window.start_s >= segment.start_s
            and formal_bin.window.end_s <= segment.end_s
        )
        if len(segment_bins) != 50:
            raise ForceEvidenceError(f"segment {segment!r} does not contain fifty complete bins")
        segment_maes.append(
            _mean((formal_bin.v2_abs_error_mean_n for formal_bin in segment_bins), label="segment MAE")
        )
    segment_maes_tuple = tuple(segment_maes)
    worst_index = max(range(SEGMENT_COUNT), key=segment_maes_tuple.__getitem__)
    return ForceDiagnostics(
        right_lobe_mae_n=right_mae,
        left_lobe_mae_n=left_mae,
        center_crossing_window=TimeWindow(
            CENTER_CROSSING_WINDOW_START_S,
            CENTER_CROSSING_WINDOW_END_S,
        ),
        center_crossing_mae_n=center_mae,
        segment_windows=segment_windows,
        segment_maes_n=segment_maes_tuple,
        worst_segment_index=worst_index,
        worst_segment_window=segment_windows[worst_index],
        worst_segment_mae_n=segment_maes_tuple[worst_index],
        lobe_imbalance_n=abs(right_mae - left_mae),
        right_lobe_bin_indices=tuple(right_indices),
        left_lobe_bin_indices=tuple(left_indices),
        ambiguous_center_bin_indices=tuple(ambiguous_indices),
    )


def compute_force_metrics(samples: Iterable[ForceSample]) -> ForceMetrics:
    """Compute v2 and the audit-only v1 shadow after strict evidence validation.

    The input is first reduced to distinct stable identities. A replay with the
    same identity and exact path-time/force payload is ignored; any conflicting
    reuse fails before an objective can be produced. Every complete formal bin
    then receives equal weight, independent of its number of samples.
    """

    spec = STEP6_R001_PROFILE.force_mae_spec
    if spec != FORCE_MAE_V2_SPEC:
        raise ForceEvidenceError("Step6 r001 profile is not bound to the locked force_mae_v2 spec")
    try:
        sample_tuple = tuple(samples)
    except TypeError as exc:
        raise InvalidForceSampleError("force evidence must be an iterable of ForceSample values") from exc
    by_identity: dict[ForceSampleIdentity, ForceSample] = {}
    duplicate_replay_count = 0
    for sample in sample_tuple:
        if not isinstance(sample, ForceSample):
            raise InvalidForceSampleError(f"expected ForceSample, got {sample!r}")
        prior = by_identity.get(sample.sample_identity)
        if prior is None:
            by_identity[sample.sample_identity] = sample
            continue
        if (
            prior.path_time_s != sample.path_time_s
            or prior.filtered_normal_n != sample.filtered_normal_n
        ):
            raise DuplicateIdentityConflictError(
                sample_identity=sample.sample_identity,
                prior=prior,
                conflicting=sample,
            )
        duplicate_replay_count += 1

    bin_evidence: list[list[tuple[float, float]]] = [[] for _ in range(spec.bin_count)]
    pre_window_count = 0
    end_boundary_count = 0
    in_window_times: list[float] = []
    for sample in by_identity.values():
        bin_index = _formal_bin_index(sample.path_time_s, spec)
        if bin_index is None:
            if sample.path_time_s < spec.window_start_s:
                pre_window_count += 1
            elif sample.path_time_s == spec.window_end_s:
                end_boundary_count += 1
            else:
                raise ForceEvidenceError(f"invalid formal timing for sample={sample!r}")
            continue
        error = abs(sample.filtered_normal_n - spec.target_force_n)
        if not math.isfinite(error):
            raise InvalidForceSampleError(f"non-finite absolute force error for sample={sample!r}")
        bin_evidence[bin_index].append((sample.filtered_normal_n, error))
        in_window_times.append(sample.path_time_s)

    per_bin_counts = tuple(len(values) for values in bin_evidence)
    missing_bin_indices = tuple(index for index, count in enumerate(per_bin_counts) if count == 0)
    if missing_bin_indices:
        raise IncompleteCoverageError(
            missing_bin_indices=missing_bin_indices,
            observed_bin_sample_counts=per_bin_counts,
            distinct_sample_count=len(by_identity),
            duplicate_replay_count=duplicate_replay_count,
        )

    formal_bins = tuple(
        FormalBinMetrics(
            index=index,
            window=_formal_bin_window(index, spec),
            distinct_sample_count=len(values),
            v2_abs_error_mean_n=_mean((error for _, error in values), label="v2 bin absolute error"),
            v1_signed_force_mean_n=_mean((force for force, _ in values), label="v1 bin signed force"),
            v1_compat_abs_error_n=0.0,
        )
        for index, values in enumerate(bin_evidence)
    )
    formal_bins = tuple(
        FormalBinMetrics(
            index=formal_bin.index,
            window=formal_bin.window,
            distinct_sample_count=formal_bin.distinct_sample_count,
            v2_abs_error_mean_n=formal_bin.v2_abs_error_mean_n,
            v1_signed_force_mean_n=formal_bin.v1_signed_force_mean_n,
            v1_compat_abs_error_n=abs(
                formal_bin.v1_signed_force_mean_n - spec.target_force_n
            ),
        )
        for formal_bin in formal_bins
    )
    force_mae_v2_n = _mean((formal_bin.v2_abs_error_mean_n for formal_bin in formal_bins), label="force_mae_v2")
    force_mae_v1 = _mean((formal_bin.v1_compat_abs_error_n for formal_bin in formal_bins), label="force_mae_v1")
    diagnostics = _build_diagnostics(formal_bins, STEP6_R001_PROFILE)
    coverage = ForceCoverage(
        formal_window=TimeWindow(spec.window_start_s, spec.window_end_s),
        bin_width_s=spec.bin_width_s,
        bin_count=spec.bin_count,
        total_input_sample_count=len(sample_tuple),
        distinct_sample_count=len(by_identity),
        exact_replay_count=duplicate_replay_count,
        in_window_distinct_sample_count=sum(per_bin_counts),
        pre_window_distinct_sample_count=pre_window_count,
        end_boundary_distinct_sample_count=end_boundary_count,
        per_bin_distinct_sample_counts=per_bin_counts,
        source_ids=tuple(sorted({identity.source_id for identity in by_identity})),
        observed_path_time_min_s=min(in_window_times),
        observed_path_time_max_s=max(in_window_times),
        complete=True,
    )
    return ForceMetrics(
        force_mae_v2_n=force_mae_v2_n,
        force_mae_v1_compat_shadow_n=force_mae_v1,
        v2_role=MetricRole.OPTIMIZER_OBJECTIVE,
        v1_role=MetricRole.AUDIT_ONLY,
        coverage=coverage,
        bins=formal_bins,
        diagnostics=diagnostics,
    )
