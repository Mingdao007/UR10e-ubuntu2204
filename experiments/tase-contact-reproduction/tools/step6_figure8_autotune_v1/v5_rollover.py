"""Pure Step6 Autotuner V5 entry, tail, and candidate-rollover primitives.

The module is intentionally offline-only.  It contains the deterministic
entry/phase policy, the exact analytic Figure-eight continuation, and an
immutable two-slot rollover state machine.  It does not import transport,
sensor, filesystem, controller, or motion code.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from typing import Any, ClassVar, Mapping, Sequence

try:  # Tests and the live-facing script put ``tools`` on sys.path.
    from step5d_autotune_v4_r013.path_context import (
        FIGURE8_ALONG_AMPLITUDE_M,
        FIGURE8_ALONG_OMEGA_RAD_S,
        FIGURE8_DURATION_S,
        FIGURE8_LATERAL_AMPLITUDE_M,
        FIGURE8_LATERAL_OMEGA_RAD_S,
    )
    from step6_figure8_autotune_v1.v5_composition_contract import (
        V5AttemptKind,
        V5RolloverInput,
        RolloverCommand,
    )
except ModuleNotFoundError:  # pragma: no cover - package import from repository root
    from tools.step5d_autotune_v4_r013.path_context import (
        FIGURE8_ALONG_AMPLITUDE_M,
        FIGURE8_ALONG_OMEGA_RAD_S,
        FIGURE8_DURATION_S,
        FIGURE8_LATERAL_AMPLITUDE_M,
        FIGURE8_LATERAL_OMEGA_RAD_S,
    )
    from tools.step6_figure8_autotune_v1.v5_composition_contract import (
        V5AttemptKind,
        V5RolloverInput,
        RolloverCommand,
    )


V5_ROLLOVER_SCHEMA = "step6.autotune/figure8-v5-rollover-primitives-v1"
V5_ROLLOVER_VERSION = 1
V5_PHASE_SCHEMA = "step6.autotune/figure8-v5-phase-receipt-v1"
V5_ENTRY_SCHEMA = "step6.autotune/figure8-v5-entry-tick-v1"
V5_PATH_SCHEMA = "step6.autotune/figure8-v5-analytic-sample-v1"
V5_GATE_SCHEMA = "step6.autotune/figure8-v5-switch-gates-v1"
V5_RECEIPT_SCHEMA = "step6.autotune/figure8-v5-rollover-receipt-v1"
V5_STATE_V2_SCHEMA = "step6.autotune/figure8-v5-rollover-state-v2"

ENTRY_LATCH_N = 1.0
ENTRY_RAMP_END_S = 4.0
FORMAL_METRIC_START_S = 5.0
PATH_END_S = FIGURE8_DURATION_S
TAIL_END_S = 20.0 * math.pi
MAX_COMMITTED_ROLLOVERS = 4


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{role} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{role} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{role} must be finite")
    return result


def _typed_count(value: Any, role: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{role} must be a {qualifier} typed integer")
    return value


def _finite_tuple(value: Sequence[Any], length: int | None, role: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{role} must be a typed sequence")
    if length is not None and len(value) != length:
        raise ValueError(f"{role} must contain {length} values")
    return tuple(_finite(item, f"{role}[{index}]") for index, item in enumerate(value))


class V5Phase(str, Enum):
    ENTRY_PATH = "entry_path"
    FORMAL_METRIC_PATH = "formal_metric_path"
    ANALYTIC_CLOSURE_TAIL = "analytic_closure_tail"
    COMPLETE_ENDPOINT = "complete_endpoint"


class CandidateAuthority(str, Enum):
    ACTIVE_CANDIDATE = "active_candidate"
    OLD_CANDIDATE = "old_candidate"
    NONE = "none"


class RolloverOperation(str, Enum):
    PREPARE = "prepare"
    COMMIT = "commit"
    CANCEL = "cancel"


class RolloverReason(str, Enum):
    ACCEPTED = "accepted"
    COMMAND_MISMATCH = "command_mismatch"
    TYPED_STATE_REQUIRED = "typed_state_required"
    TYPED_REQUEST_REQUIRED = "typed_request_required"
    SAME_EPOCH_REQUIRED = "same_epoch_required"
    ORDINAL_NOT_INCREASING = "ordinal_not_strictly_increasing"
    PREPARED_SLOT_OCCUPIED = "prepared_slot_occupied"
    STALE_GENERATION = "stale_generation"
    PREPARED_SLOT_MISSING = "prepared_slot_missing"
    PREPARED_IDENTITY_MISMATCH = "prepared_identity_mismatch"
    PREPARED_GENERATION_MISMATCH = "prepared_generation_mismatch"
    GENERATION_FENCE_MISMATCH = "generation_fence_mismatch"
    QDOT_GENERATION_MISMATCH = "qdot_generation_mismatch"
    PERIODIC_SEAM_REQUIRED = "periodic_seam_required"
    TYPED_SEAM_REQUIRED = "typed_periodic_seam_required"
    MISSING_BOOLEAN_GATE = "missing_boolean_gate"
    INVALID_BOOLEAN_GATE = "invalid_boolean_gate"
    GATE_FAMILY_FAILED = "gate_family_failed"
    CHAIN_CAP_REACHED = "chain_rollover_cap_reached_home_fallback_required"


class V5RolloverError(ValueError):
    """Deterministic, typed rejection of one pure rollover transition."""

    def __init__(self, reason: RolloverReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        message = reason.value if not detail else f"{reason.value}: {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class V5EntryTickV1:
    path_time_s: float
    target_force_n: float
    correction_enabled: bool
    formal_metric_enabled: bool
    schema: str = V5_ENTRY_SCHEMA
    version: int = V5_ROLLOVER_VERSION

    def __post_init__(self) -> None:
        time_s = _finite(self.path_time_s, "entry path time")
        if not 0.0 <= time_s <= TAIL_END_S:
            raise ValueError("entry path time is outside the analytic V5 interval")
        target = _finite(self.target_force_n, "entry target force")
        if not ENTRY_LATCH_N <= target <= 5.0:
            raise ValueError("entry target is invalid")
        if type(self.correction_enabled) is not bool or type(self.formal_metric_enabled) is not bool:
            raise TypeError("entry enable flags must be boolean")
        if self.schema != V5_ENTRY_SCHEMA or self.version != V5_ROLLOVER_VERSION:
            raise ValueError("entry tick schema/version differs")
        object.__setattr__(self, "path_time_s", time_s)
        object.__setattr__(self, "target_force_n", target)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "path_time_s": self.path_time_s,
            "target_force_n": self.target_force_n,
            "correction_enabled": self.correction_enabled,
            "formal_metric_enabled": self.formal_metric_enabled,
        }


def entry_target_n(path_time_s: float) -> float:
    """Return the fixed 5 N target after pre-trial contact acquisition."""

    time_s = _finite(path_time_s, "entry path time")
    if not 0.0 <= time_s <= TAIL_END_S:
        raise ValueError("entry path time is outside the analytic V5 interval")
    return 5.0


def entry_tick(path_time_s: float) -> V5EntryTickV1:
    time_s = _finite(path_time_s, "entry path time")
    target = entry_target_n(time_s)
    correction_enabled = FORMAL_METRIC_START_S <= time_s < TAIL_END_S
    formal_metric_enabled = FORMAL_METRIC_START_S <= time_s < PATH_END_S
    return V5EntryTickV1(
        path_time_s=time_s,
        target_force_n=target,
        correction_enabled=correction_enabled,
        formal_metric_enabled=formal_metric_enabled,
    )


@dataclass(frozen=True)
class V5PhaseReceiptV1:
    phase: V5Phase
    metric_evidence_path: bool
    formal_metric: bool
    tail_old_candidate_authority: bool
    included_in_mae: bool
    included_in_censor: bool
    included_in_gp: bool
    included_in_tell_exact: bool
    counts_exact_novel_budget: bool
    schema: str = V5_PHASE_SCHEMA
    version: int = V5_ROLLOVER_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.phase, V5Phase):
            raise TypeError("phase must be a typed V5Phase")
        flags = (
            self.metric_evidence_path,
            self.formal_metric,
            self.tail_old_candidate_authority,
            self.included_in_mae,
            self.included_in_censor,
            self.included_in_gp,
            self.included_in_tell_exact,
            self.counts_exact_novel_budget,
        )
        if any(type(flag) is not bool for flag in flags):
            raise TypeError("phase inclusion fields must be boolean")
        if self.schema != V5_PHASE_SCHEMA or self.version != V5_ROLLOVER_VERSION:
            raise ValueError("phase receipt schema/version differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "phase": self.phase.value,
            "metric_evidence_path": self.metric_evidence_path,
            "formal_metric": self.formal_metric,
            "tail_old_candidate_authority": self.tail_old_candidate_authority,
            "included_in_mae": self.included_in_mae,
            "included_in_censor": self.included_in_censor,
            "included_in_gp": self.included_in_gp,
            "included_in_tell_exact": self.included_in_tell_exact,
            "counts_exact_novel_budget": self.counts_exact_novel_budget,
        }


def phase_at(path_time_s: float) -> V5Phase:
    time_s = _finite(path_time_s, "phase path time")
    if not 0.0 <= time_s <= TAIL_END_S:
        raise ValueError("phase path time is outside the analytic V5 interval")
    if time_s < FORMAL_METRIC_START_S:
        return V5Phase.ENTRY_PATH
    if time_s < PATH_END_S:
        return V5Phase.FORMAL_METRIC_PATH
    if time_s < TAIL_END_S:
        return V5Phase.ANALYTIC_CLOSURE_TAIL
    return V5Phase.COMPLETE_ENDPOINT


def phase_for_time(path_time_s: float) -> V5PhaseReceiptV1:
    phase = phase_at(path_time_s)
    if phase is V5Phase.ENTRY_PATH:
        return V5PhaseReceiptV1(phase, True, False, False, False, False, False, False, False)
    if phase is V5Phase.FORMAL_METRIC_PATH:
        return V5PhaseReceiptV1(phase, True, True, False, True, True, True, True, True)
    if phase is V5Phase.ANALYTIC_CLOSURE_TAIL:
        return V5PhaseReceiptV1(phase, False, False, True, False, False, False, False, False)
    return V5PhaseReceiptV1(phase, False, False, False, False, False, False, False, False)


@dataclass(frozen=True)
class FigureEightAnalyticSampleV1:
    """Exact Figure-eight position and derivatives, including the closure tail."""

    path_time_s: float
    phase_rad: float
    along_m: float
    lateral_m: float
    along_velocity_m_s: float
    lateral_velocity_m_s: float
    along_acceleration_m_s2: float
    lateral_acceleration_m_s2: float
    schema: str = V5_PATH_SCHEMA
    version: int = V5_ROLLOVER_VERSION

    def __post_init__(self) -> None:
        time_s = _finite(self.path_time_s, "analytic path time")
        if not 0.0 <= time_s <= TAIL_END_S:
            raise ValueError("analytic path time is outside the V5 interval")
        for role in (
            "phase_rad",
            "along_m",
            "lateral_m",
            "along_velocity_m_s",
            "lateral_velocity_m_s",
            "along_acceleration_m_s2",
            "lateral_acceleration_m_s2",
        ):
            _finite(getattr(self, role), role)
        if self.schema != V5_PATH_SCHEMA or self.version != V5_ROLLOVER_VERSION:
            raise ValueError("analytic sample schema/version differs")
        object.__setattr__(self, "path_time_s", time_s)
        for role in (
            "phase_rad",
            "along_m",
            "lateral_m",
            "along_velocity_m_s",
            "lateral_velocity_m_s",
            "along_acceleration_m_s2",
            "lateral_acceleration_m_s2",
        ):
            object.__setattr__(self, role, _finite(getattr(self, role), role))

    @property
    def phase(self) -> V5Phase:
        return phase_at(self.path_time_s)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "path_time_s": self.path_time_s,
            "phase_rad": self.phase_rad,
            "along_m": self.along_m,
            "lateral_m": self.lateral_m,
            "along_velocity_m_s": self.along_velocity_m_s,
            "lateral_velocity_m_s": self.lateral_velocity_m_s,
            "along_acceleration_m_s2": self.along_acceleration_m_s2,
            "lateral_acceleration_m_s2": self.lateral_acceleration_m_s2,
        }


def figure8_analytic_sample(path_time_s: float) -> FigureEightAnalyticSampleV1:
    """Evaluate the mature analytic formula without interpolation or gap filling."""

    time_s = _finite(path_time_s, "analytic path time")
    if not 0.0 <= time_s <= TAIL_END_S:
        raise ValueError("analytic path time is outside the V5 interval")
    along_phase = FIGURE8_ALONG_OMEGA_RAD_S * time_s
    lateral_phase = FIGURE8_LATERAL_OMEGA_RAD_S * time_s
    return FigureEightAnalyticSampleV1(
        path_time_s=time_s,
        phase_rad=along_phase,
        along_m=FIGURE8_ALONG_AMPLITUDE_M * math.sin(along_phase),
        lateral_m=FIGURE8_LATERAL_AMPLITUDE_M * math.sin(lateral_phase),
        along_velocity_m_s=FIGURE8_ALONG_AMPLITUDE_M * FIGURE8_ALONG_OMEGA_RAD_S * math.cos(along_phase),
        lateral_velocity_m_s=FIGURE8_LATERAL_AMPLITUDE_M * FIGURE8_LATERAL_OMEGA_RAD_S * math.cos(lateral_phase),
        along_acceleration_m_s2=-FIGURE8_ALONG_AMPLITUDE_M * FIGURE8_ALONG_OMEGA_RAD_S**2 * math.sin(along_phase),
        lateral_acceleration_m_s2=-FIGURE8_LATERAL_AMPLITUDE_M * FIGURE8_LATERAL_OMEGA_RAD_S**2 * math.sin(lateral_phase),
    )


@dataclass(frozen=True)
class CandidateIdentityV1:
    epoch: int
    ordinal: int
    attempt_kind: V5AttemptKind
    candidate_token: int
    schema: str = "step6.autotune/figure8-v5-candidate-identity-v1"
    version: int = V5_ROLLOVER_VERSION

    def __post_init__(self) -> None:
        _typed_count(self.epoch, "candidate epoch")
        _typed_count(self.ordinal, "candidate ordinal", positive=True)
        if not isinstance(self.attempt_kind, V5AttemptKind):
            raise TypeError("candidate attempt_kind must be a typed V5AttemptKind")
        _typed_count(self.candidate_token, "candidate token", positive=True)
        if self.schema != "step6.autotune/figure8-v5-candidate-identity-v1" or self.version != V5_ROLLOVER_VERSION:
            raise ValueError("candidate identity schema/version differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "epoch": self.epoch,
            "ordinal": self.ordinal,
            "attempt_kind": int(self.attempt_kind),
            "candidate_token": self.candidate_token,
        }


@dataclass(frozen=True)
class CandidateSlotV1:
    identity: CandidateIdentityV1
    active: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.identity, CandidateIdentityV1) or self.active is not True:
            raise TypeError("active candidate slot must contain one typed active identity")


@dataclass(frozen=True)
class PreparedCandidateSlotV1:
    identity: CandidateIdentityV1
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.identity, CandidateIdentityV1):
            raise TypeError("prepared slot identity must be typed")
        _typed_count(self.generation, "prepared generation", positive=True)


@dataclass(frozen=True)
class LogicalRuntimeStateV1:
    metric_sample_count: int = 0
    censor_sample_count: int = 0
    path_clock_s: float = 0.0
    correction_generation: int = 0
    correction_output_n: float = 0.0
    force_integral_n_s: float = 0.0

    def __post_init__(self) -> None:
        _typed_count(self.metric_sample_count, "metric sample count")
        _typed_count(self.censor_sample_count, "censor sample count")
        path_clock = _finite(self.path_clock_s, "logical path clock")
        _typed_count(self.correction_generation, "correction generation")
        correction_output = _finite(self.correction_output_n, "correction output")
        force_integral = _finite(self.force_integral_n_s, "force integral")
        object.__setattr__(self, "path_clock_s", path_clock)
        object.__setattr__(self, "correction_output_n", correction_output)
        object.__setattr__(self, "force_integral_n_s", force_integral)

    @classmethod
    def fresh(cls) -> "LogicalRuntimeStateV1":
        return cls()

    @property
    def is_fresh(self) -> bool:
        return self == type(self).fresh()


@dataclass(frozen=True)
class CurrentObservationV1:
    pose: tuple[float, ...]
    q: tuple[float, ...]
    qd: tuple[float, ...]
    tcp_velocity: tuple[float, ...]
    controller_timestamp: float
    reaction_normal: float

    schema: ClassVar[str] = "step6.autotune/figure8-v5-current-observation-v1"
    version: ClassVar[int] = V5_ROLLOVER_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "pose", _finite_tuple(self.pose, 6, "observation pose"))
        object.__setattr__(self, "q", _finite_tuple(self.q, 6, "observation q"))
        object.__setattr__(self, "qd", _finite_tuple(self.qd, 6, "observation qd"))
        object.__setattr__(self, "tcp_velocity", _finite_tuple(self.tcp_velocity, 6, "observation TCP velocity"))
        timestamp = _finite(self.controller_timestamp, "controller timestamp")
        if timestamp < 0.0:
            raise ValueError("controller timestamp must be non-negative")
        object.__setattr__(self, "controller_timestamp", timestamp)
        object.__setattr__(self, "reaction_normal", _finite(self.reaction_normal, "reaction normal"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "pose": list(self.pose),
            "q": list(self.q),
            "qd": list(self.qd),
            "tcp_velocity": list(self.tcp_velocity),
            "controller_timestamp": self.controller_timestamp,
            "reaction_normal": self.reaction_normal,
        }


@dataclass(frozen=True)
class PhysicalContinuitySeedsV1:
    last_applied_cartesian_twist: tuple[float, ...] = (0.0,) * 6
    previous_qdot: tuple[float, ...] = (0.0,) * 6
    current_observations: CurrentObservationV1 = CurrentObservationV1(
        pose=(0.0,) * 6,
        q=(0.0,) * 6,
        qd=(0.0,) * 6,
        tcp_velocity=(0.0,) * 6,
        controller_timestamp=0.0,
        reaction_normal=0.0,
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "last_applied_cartesian_twist",
            _finite_tuple(self.last_applied_cartesian_twist, 6, "last applied Cartesian twist"),
        )
        object.__setattr__(self, "previous_qdot", _finite_tuple(self.previous_qdot, 6, "previous qdot"))
        if not isinstance(self.current_observations, CurrentObservationV1):
            raise TypeError("current observations must be a typed observation value")


@dataclass(frozen=True)
class V5RolloverStateV2:
    """Complete typed state map carried across a V5 candidate activation.

    Logical trial accumulators are intentionally separate from the physical
    continuity values.  A successor may carry the latter directly, while
    force-integral mapping is accepted only when its discrete update law can
    reproduce the last applied normal velocity without clipping.
    """

    candidate: Mapping[str, Any]
    filtered_force_n: float
    filter_initialized: bool
    force_integral_n_s: float
    xdot_p_prev_m_s: tuple[float, float, float]
    theta_dot_state: tuple[float, ...]
    lambda_state: tuple[float, ...]
    last_cartesian_twist: tuple[float, ...]
    previous_qdot: tuple[float, ...]
    pose: tuple[float, ...]
    q: tuple[float, ...]
    qd: tuple[float, ...]
    jacobian_6x6: tuple[float, ...]
    reaction_normal: tuple[float, float, float]
    approach_normal: tuple[float, float, float]
    target_force_n: float
    actual_dt_s: float
    controller_timestamp: float
    generation: int
    schema: str = V5_STATE_V2_SCHEMA
    version: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Mapping) or not self.candidate:
            raise TypeError("V5 rollover candidate state must be a mapping")
        object.__setattr__(self, "candidate", dict(self.candidate))
        for name in (
            "filtered_force_n",
            "force_integral_n_s",
            "target_force_n",
            "actual_dt_s",
            "controller_timestamp",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if type(self.filter_initialized) is not bool:
            raise TypeError("V5 rollover filter initialization must be boolean")
        object.__setattr__(self, "xdot_p_prev_m_s", _finite_tuple(self.xdot_p_prev_m_s, 3, "xdot_p_prev_m_s"))
        for name in (
            "theta_dot_state",
            "lambda_state",
            "last_cartesian_twist",
            "previous_qdot",
            "pose",
            "q",
            "qd",
        ):
            object.__setattr__(self, name, _finite_tuple(getattr(self, name), 6, name))
        object.__setattr__(self, "jacobian_6x6", _finite_tuple(self.jacobian_6x6, 36, "jacobian_6x6"))
        object.__setattr__(self, "reaction_normal", _finite_tuple(self.reaction_normal, 3, "reaction_normal"))
        object.__setattr__(self, "approach_normal", _finite_tuple(self.approach_normal, 3, "approach_normal"))
        _typed_count(self.generation, "V5 rollover generation")
        if self.target_force_n != 5.0:
            raise ValueError("V5 rollover target force must remain 5 N")
        if not 0.0 < self.actual_dt_s < 0.08:
            raise ValueError("V5 rollover actual dt is outside (0,80ms)")
        if self.controller_timestamp < 0.0:
            raise ValueError("V5 controller timestamp must be non-negative")
        if self.schema != V5_STATE_V2_SCHEMA or self.version != 2:
            raise ValueError("V5 rollover state-v2 schema/version differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "candidate": dict(self.candidate),
            "filtered_force_n": self.filtered_force_n,
            "filter_initialized": self.filter_initialized,
            "force_integral_n_s": self.force_integral_n_s,
            "xdot_p_prev_m_s": list(self.xdot_p_prev_m_s),
            "theta_dot_state": list(self.theta_dot_state),
            "lambda_state": list(self.lambda_state),
            "last_cartesian_twist": list(self.last_cartesian_twist),
            "previous_qdot": list(self.previous_qdot),
            "pose": list(self.pose),
            "q": list(self.q),
            "qd": list(self.qd),
            "jacobian_6x6": list(self.jacobian_6x6),
            "reaction_normal": list(self.reaction_normal),
            "approach_normal": list(self.approach_normal),
            "target_force_n": self.target_force_n,
            "actual_dt_s": self.actual_dt_s,
            "controller_timestamp": self.controller_timestamp,
            "generation": self.generation,
        }


@dataclass(frozen=True)
class RolloverStateV1:
    active_slot: CandidateSlotV1
    qdot_generation: int = 0
    rollover_generation_fence: int = 0
    committed_rollovers: int = 0
    prepared_slot: PreparedCandidateSlotV1 | None = None
    logical_state: LogicalRuntimeStateV1 = LogicalRuntimeStateV1()
    continuity_seeds: PhysicalContinuitySeedsV1 = PhysicalContinuitySeedsV1()
    schema: str = V5_ROLLOVER_SCHEMA
    version: int = V5_ROLLOVER_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.active_slot, CandidateSlotV1):
            raise TypeError("rollover state requires an active candidate slot")
        _typed_count(self.qdot_generation, "state qdot generation")
        _typed_count(self.rollover_generation_fence, "rollover generation fence")
        if self.rollover_generation_fence < self.qdot_generation:
            raise ValueError("rollover generation fence cannot trail qdot generation")
        _typed_count(self.committed_rollovers, "committed rollover count")
        if self.committed_rollovers > MAX_COMMITTED_ROLLOVERS:
            raise ValueError("rollover state exceeds the chain cap")
        if self.prepared_slot is not None:
            if not isinstance(self.prepared_slot, PreparedCandidateSlotV1):
                raise TypeError("prepared slot must be typed")
            if self.prepared_slot.identity.epoch != self.epoch:
                raise ValueError("prepared candidate epoch differs from active epoch")
        if not isinstance(self.logical_state, LogicalRuntimeStateV1):
            raise TypeError("logical runtime state must be typed")
        if not isinstance(self.continuity_seeds, PhysicalContinuitySeedsV1):
            raise TypeError("continuity seeds must be typed")
        if self.schema != V5_ROLLOVER_SCHEMA or self.version != V5_ROLLOVER_VERSION:
            raise ValueError("rollover state schema/version differs")

    @property
    def epoch(self) -> int:
        return self.active_slot.identity.epoch

    @property
    def active_identity(self) -> CandidateIdentityV1:
        return self.active_slot.identity


def initial_rollover_state(
    active_identity: CandidateIdentityV1,
    *,
    qdot_generation: int = 0,
    rollover_generation_fence: int | None = None,
    committed_rollovers: int = 0,
    logical_state: LogicalRuntimeStateV1 | None = None,
    continuity_seeds: PhysicalContinuitySeedsV1 | None = None,
) -> RolloverStateV1:
    return RolloverStateV1(
        active_slot=CandidateSlotV1(active_identity),
        qdot_generation=qdot_generation,
        rollover_generation_fence=(qdot_generation if rollover_generation_fence is None else rollover_generation_fence),
        committed_rollovers=committed_rollovers,
        logical_state=LogicalRuntimeStateV1.fresh() if logical_state is None else logical_state,
        continuity_seeds=PhysicalContinuitySeedsV1() if continuity_seeds is None else continuity_seeds,
    )


@dataclass(frozen=True)
class SwitchGateFamiliesV1:
    """Already-evaluated boolean gate families; no thresholds live here."""

    hard_safety: bool
    timing_freshness: bool
    tube_cbf: bool
    identity_metric_closure: bool
    command_envelope: bool
    schema: str = V5_GATE_SCHEMA
    version: int = V5_ROLLOVER_VERSION

    def __post_init__(self) -> None:
        values = (
            self.hard_safety,
            self.timing_freshness,
            self.tube_cbf,
            self.identity_metric_closure,
            self.command_envelope,
        )
        if any(type(value) is not bool for value in values):
            raise TypeError("switch gate families must be evaluated booleans")
        if self.schema != V5_GATE_SCHEMA or self.version != V5_ROLLOVER_VERSION:
            raise ValueError("switch gate schema/version differs")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SwitchGateFamiliesV1":
        if not isinstance(value, Mapping):
            raise V5RolloverError(RolloverReason.MISSING_BOOLEAN_GATE, "gate family mapping")
        required = (
            "hard_safety",
            "timing_freshness",
            "tube_cbf",
            "identity_metric_closure",
            "command_envelope",
        )
        missing = tuple(name for name in required if name not in value)
        if missing:
            raise V5RolloverError(RolloverReason.MISSING_BOOLEAN_GATE, ",".join(missing))
        if any(type(value[name]) is not bool for name in required):
            raise V5RolloverError(RolloverReason.INVALID_BOOLEAN_GATE)
        return cls(**{name: value[name] for name in required})

    @property
    def all_passed(self) -> bool:
        return all(
            (
                self.hard_safety,
                self.timing_freshness,
                self.tube_cbf,
                self.identity_metric_closure,
                self.command_envelope,
            )
        )

    @property
    def failed_families(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, passed in (
                ("hard_safety", self.hard_safety),
                ("timing_freshness", self.timing_freshness),
                ("tube_cbf", self.tube_cbf),
                ("identity_metric_closure", self.identity_metric_closure),
                ("command_envelope", self.command_envelope),
            )
            if not passed
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "hard_safety": self.hard_safety,
            "timing_freshness": self.timing_freshness,
            "tube_cbf": self.tube_cbf,
            "identity_metric_closure": self.identity_metric_closure,
            "command_envelope": self.command_envelope,
        }


@dataclass(frozen=True)
class RolloverReceiptV1:
    operation: RolloverOperation
    reason: RolloverReason
    epoch: int
    generation: int
    committed_rollovers: int
    active_before: CandidateIdentityV1
    active_after: CandidateIdentityV1
    prepared_before: CandidateIdentityV1 | None
    prepared_after: CandidateIdentityV1 | None
    qdot_generation_before: int
    qdot_generation_after: int
    rollover_generation_fence_before: int
    rollover_generation_fence_after: int
    accepted: bool = True
    schema: str = V5_RECEIPT_SCHEMA
    version: int = V5_ROLLOVER_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.operation, RolloverOperation) or not isinstance(self.reason, RolloverReason):
            raise TypeError("rollover receipt operation/reason must be typed")
        _typed_count(self.epoch, "receipt epoch")
        _typed_count(self.generation, "receipt generation")
        _typed_count(self.committed_rollovers, "receipt rollover count")
        _typed_count(self.qdot_generation_before, "receipt prior qdot generation")
        _typed_count(self.qdot_generation_after, "receipt qdot generation")
        _typed_count(self.rollover_generation_fence_before, "receipt prior generation fence")
        _typed_count(self.rollover_generation_fence_after, "receipt generation fence")
        if self.rollover_generation_fence_after < self.rollover_generation_fence_before:
            raise ValueError("rollover generation fence regressed")
        if not isinstance(self.active_before, CandidateIdentityV1) or not isinstance(self.active_after, CandidateIdentityV1):
            raise TypeError("rollover receipt active identities must be typed")
        if self.accepted is not True:
            raise ValueError("rollover receipt must describe an accepted transition")
        if self.prepared_before is not None and not isinstance(self.prepared_before, CandidateIdentityV1):
            raise TypeError("rollover receipt prepared_before must be typed")
        if self.prepared_after is not None and not isinstance(self.prepared_after, CandidateIdentityV1):
            raise TypeError("rollover receipt prepared_after must be typed")
        if self.schema != V5_RECEIPT_SCHEMA or self.version != V5_ROLLOVER_VERSION:
            raise ValueError("rollover receipt schema/version differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "operation": self.operation.value,
            "reason": self.reason.value,
            "epoch": self.epoch,
            "generation": self.generation,
            "committed_rollovers": self.committed_rollovers,
            "active_before": self.active_before.as_dict(),
            "active_after": self.active_after.as_dict(),
            "prepared_before": None if self.prepared_before is None else self.prepared_before.as_dict(),
            "prepared_after": None if self.prepared_after is None else self.prepared_after.as_dict(),
            "qdot_generation_before": self.qdot_generation_before,
            "qdot_generation_after": self.qdot_generation_after,
            "rollover_generation_fence_before": self.rollover_generation_fence_before,
            "rollover_generation_fence_after": self.rollover_generation_fence_after,
            "accepted": self.accepted,
        }


def _require_state(value: Any) -> RolloverStateV1:
    if not isinstance(value, RolloverStateV1):
        raise V5RolloverError(RolloverReason.TYPED_STATE_REQUIRED)
    return value


def _require_request(value: Any, command: RolloverCommand) -> V5RolloverInput:
    if not isinstance(value, V5RolloverInput):
        raise V5RolloverError(RolloverReason.TYPED_REQUEST_REQUIRED)
    if value.command is not command:
        raise V5RolloverError(RolloverReason.COMMAND_MISMATCH, command.name.lower())
    return value


def _require_epoch(value: Any) -> int:
    try:
        return _typed_count(value, "request epoch")
    except ValueError as exc:
        raise V5RolloverError(RolloverReason.SAME_EPOCH_REQUIRED, str(exc)) from exc


def _request_identity(request: V5RolloverInput, epoch: int) -> CandidateIdentityV1:
    return CandidateIdentityV1(
        epoch=epoch,
        ordinal=request.next_attempt_ordinal,
        attempt_kind=request.next_attempt_kind,  # type: ignore[arg-type]
        candidate_token=request.next_candidate_token,
    )


def _receipt(
    operation: RolloverOperation,
    reason: RolloverReason,
    before: RolloverStateV1,
    after: RolloverStateV1,
    generation: int,
) -> RolloverReceiptV1:
    return RolloverReceiptV1(
        operation=operation,
        reason=reason,
        epoch=before.epoch,
        generation=generation,
        committed_rollovers=after.committed_rollovers,
        active_before=before.active_identity,
        active_after=after.active_identity,
        prepared_before=None if before.prepared_slot is None else before.prepared_slot.identity,
        prepared_after=None if after.prepared_slot is None else after.prepared_slot.identity,
        qdot_generation_before=before.qdot_generation,
        qdot_generation_after=after.qdot_generation,
        rollover_generation_fence_before=before.rollover_generation_fence,
        rollover_generation_fence_after=after.rollover_generation_fence,
    )


def prepare_rollover(
    state: RolloverStateV1,
    request: V5RolloverInput,
    *,
    epoch: int,
) -> tuple[RolloverStateV1, RolloverReceiptV1]:
    current = _require_state(state)
    typed_request = _require_request(request, RolloverCommand.PREPARE)
    request_epoch = _require_epoch(epoch)
    if request_epoch != current.epoch:
        raise V5RolloverError(RolloverReason.SAME_EPOCH_REQUIRED)
    if current.committed_rollovers >= MAX_COMMITTED_ROLLOVERS:
        raise V5RolloverError(RolloverReason.CHAIN_CAP_REACHED)
    if current.prepared_slot is not None:
        raise V5RolloverError(RolloverReason.PREPARED_SLOT_OCCUPIED)
    identity = _request_identity(typed_request, request_epoch)
    if identity.ordinal <= current.active_identity.ordinal:
        raise V5RolloverError(RolloverReason.ORDINAL_NOT_INCREASING)
    if typed_request.generation <= current.rollover_generation_fence:
        raise V5RolloverError(RolloverReason.STALE_GENERATION)
    next_state = replace(
        current,
        prepared_slot=PreparedCandidateSlotV1(identity=identity, generation=typed_request.generation),
        rollover_generation_fence=typed_request.generation,
    )
    return next_state, _receipt(
        RolloverOperation.PREPARE,
        RolloverReason.ACCEPTED,
        current,
        next_state,
        typed_request.generation,
    )


def commit_rollover(
    state: RolloverStateV1,
    request: V5RolloverInput,
    gates: SwitchGateFamiliesV1 | Mapping[str, Any],
    *,
    epoch: int,
    at_periodic_seam: bool,
) -> tuple[RolloverStateV1, RolloverReceiptV1]:
    current, typed_request = validate_commit_request(state, request, epoch=epoch)
    if type(at_periodic_seam) is not bool:
        raise V5RolloverError(RolloverReason.TYPED_SEAM_REQUIRED)
    if not at_periodic_seam:
        raise V5RolloverError(RolloverReason.PERIODIC_SEAM_REQUIRED)
    prepared = current.prepared_slot
    assert prepared is not None  # established by validate_commit_request
    if isinstance(gates, Mapping):
        evaluated = SwitchGateFamiliesV1.from_mapping(gates)
    elif isinstance(gates, SwitchGateFamiliesV1):
        evaluated = gates
    else:
        raise V5RolloverError(RolloverReason.MISSING_BOOLEAN_GATE, "gate family receipt")
    if not evaluated.all_passed:
        raise V5RolloverError(RolloverReason.GATE_FAMILY_FAILED, ",".join(evaluated.failed_families))
    next_state = replace(
        current,
        active_slot=CandidateSlotV1(prepared.identity),
        qdot_generation=typed_request.qdot_generation,
        committed_rollovers=current.committed_rollovers + 1,
        prepared_slot=None,
        logical_state=LogicalRuntimeStateV1.fresh(),
        continuity_seeds=current.continuity_seeds,
    )
    return next_state, _receipt(
        RolloverOperation.COMMIT,
        RolloverReason.ACCEPTED,
        current,
        next_state,
        typed_request.generation,
    )


def validate_commit_request(
    state: RolloverStateV1,
    request: V5RolloverInput,
    *,
    epoch: int,
) -> tuple[RolloverStateV1, V5RolloverInput]:
    """Validate a COMMIT envelope without claiming the periodic seam.

    The resident controller uses this to arm a future atomic switch.  It does
    not evaluate or cache live switch gates; those remain seam-time evidence.
    """

    current = _require_state(state)
    typed_request = _require_request(request, RolloverCommand.COMMIT)
    request_epoch = _require_epoch(epoch)
    if request_epoch != current.epoch:
        raise V5RolloverError(RolloverReason.SAME_EPOCH_REQUIRED)
    if current.committed_rollovers >= MAX_COMMITTED_ROLLOVERS:
        raise V5RolloverError(RolloverReason.CHAIN_CAP_REACHED)
    prepared = current.prepared_slot
    if prepared is None:
        raise V5RolloverError(RolloverReason.PREPARED_SLOT_MISSING)
    identity = _request_identity(typed_request, request_epoch)
    if identity != prepared.identity:
        raise V5RolloverError(RolloverReason.PREPARED_IDENTITY_MISMATCH)
    if typed_request.generation != prepared.generation:
        raise V5RolloverError(RolloverReason.PREPARED_GENERATION_MISMATCH)
    if typed_request.generation != current.rollover_generation_fence:
        raise V5RolloverError(RolloverReason.GENERATION_FENCE_MISMATCH)
    if typed_request.qdot_generation != prepared.generation:
        raise V5RolloverError(RolloverReason.QDOT_GENERATION_MISMATCH)
    if typed_request.qdot_generation <= current.qdot_generation:
        raise V5RolloverError(RolloverReason.QDOT_GENERATION_MISMATCH)
    return current, typed_request


def cancel_rollover(
    state: RolloverStateV1,
    request: V5RolloverInput,
    *,
    epoch: int,
) -> tuple[RolloverStateV1, RolloverReceiptV1]:
    current = _require_state(state)
    typed_request = _require_request(request, RolloverCommand.CANCEL)
    request_epoch = _require_epoch(epoch)
    if request_epoch != current.epoch:
        raise V5RolloverError(RolloverReason.SAME_EPOCH_REQUIRED)
    prepared = current.prepared_slot
    if prepared is None:
        raise V5RolloverError(RolloverReason.PREPARED_SLOT_MISSING)
    if typed_request.generation != prepared.generation:
        raise V5RolloverError(RolloverReason.PREPARED_GENERATION_MISMATCH)
    next_state = replace(current, prepared_slot=None)
    return next_state, _receipt(
        RolloverOperation.CANCEL,
        RolloverReason.ACCEPTED,
        current,
        next_state,
        typed_request.generation,
    )


__all__ = [
    "CandidateAuthority",
    "CandidateIdentityV1",
    "CandidateSlotV1",
    "CurrentObservationV1",
    "ENTRY_LATCH_N",
    "ENTRY_RAMP_END_S",
    "FORMAL_METRIC_START_S",
    "FigureEightAnalyticSampleV1",
    "LogicalRuntimeStateV1",
    "MAX_COMMITTED_ROLLOVERS",
    "PATH_END_S",
    "PhysicalContinuitySeedsV1",
    "PreparedCandidateSlotV1",
    "RolloverOperation",
    "RolloverReason",
    "RolloverReceiptV1",
    "RolloverStateV1",
    "SwitchGateFamiliesV1",
    "TAIL_END_S",
    "V5EntryTickV1",
    "V5Phase",
    "V5PhaseReceiptV1",
    "V5RolloverStateV2",
    "V5RolloverError",
    "entry_target_n",
    "entry_tick",
    "figure8_analytic_sample",
    "initial_rollover_state",
    "phase_at",
    "phase_for_time",
    "prepare_rollover",
    "validate_commit_request",
    "commit_rollover",
    "cancel_rollover",
]
