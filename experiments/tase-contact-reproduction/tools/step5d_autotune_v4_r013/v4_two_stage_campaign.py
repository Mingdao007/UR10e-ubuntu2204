"""Typed two-stage V4 FF/I campaign scheduler.

This module is deliberately independent from the historical R013 200-row
campaign.  It provides the identity, proposal schedule, exact/censored
observation boundary, and milestone semantics for the two user-requested
100-attempt campaigns:

* ``V4_FF_IOFF_100``: five controller dimensions with I disabled;
* ``V4_FF_ION_6D_100``: the complete six-dimensional I-on controller block.

The module does not open a robot transport or decide a live safety gate.  A
live owner may consume the typed dispatch and return a sealed lifecycle
receipt, while the scheduler remains the single campaign/ledger writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence


V4_TWO_STAGE_SCHEMA = "step5d.autotune-v4/v4-two-stage-campaign-v1"
V4_TWO_STAGE_VERSION = 2
FF_BUNDLE_SCHEMA = "step5d.autotune-v4/combined-ff-bundle-v1"
ATTEMPT_SCHEMA = "step5d.autotune-v4/v4-stage-attempt-v1"
MILESTONE_SCHEMA = "step5d.autotune-v4/v4-mae-milestones-v1"
LEDGER_SCHEMA = "step5d.autotune-v4/v4-stage-ledger-v1"
CROSS_STAGE_BASELINE_SCHEMA = "step5d.autotune-v4/v4-cross-stage-baseline-v1"
RECOVERY_SCHEMA = "step5d.autotune-v4/v4-recovery-receipt-v1"

ATTEMPT_BUDGET = 100
WINNER_TOTAL_N = 3
CONFIRMED_REPEATS = 3
CENSOR_KAPPA = 2.0
CENSOR_MIN_CLOSED_BINS = 25
MILESTONE_THRESHOLDS_N = (0.3, 0.2, 0.1)
TARGET_FORCE_N = 5.0
METRIC_WINDOW_S = (5.0, 60.0)

P_OVER_D_BOUNDS = (1.25e-5, 4.0e-4)
DAMPING_BOUNDS = (7.0, 224.0)
TAU_BOUNDS = (0.04375, 0.0735784)
KO_BOUNDS = (0.05, 0.8)
MOTION_KP_BOUNDS = (1.5, 6.0)
I_OVER_P_BOUNDS = (0.05, 0.5)
I_GAIN_MAX = 0.008610779292198037

# Native R006 executable quarter-octave anchors.  V4 exposes different
# physical coordinates for tau/Ko/Kp, but the live DTO validates these
# anchors exactly.
R006_P_ANCHOR = 0.0003535533906
R006_I_ANCHOR = 0.00001
R006_D_ANCHOR = 28.0
R006_TAU_ANCHOR = 0.35
R006_KO_ANCHOR = 0.1
R006_KP_ANCHOR = 1.5

STAGE_A_FEATURES = (
    "log2_p_over_d",
    "log2_damping",
    "log2_tau_s",
    "log2_orientation_ko",
    "log2_motion_kp",
)
STAGE_B_FEATURES = STAGE_A_FEATURES + ("log2_i_over_p",)
STAGE_B2_FEATURES = STAGE_B_FEATURES + ("log2_integral_state_limit_n_s",)

# Stage B2 exposes the soft state-limit as a performance coordinate.  The
# authority clamp and normal-velocity bound remain independent hard policy
# invariants.  Candidate validation also rejects points for which the
# authority clamp would make two raw limits physically identical.
INTEGRAL_STATE_LIMIT_BOUNDS = (0.5, 5.0)
FIXED_INTEGRAL_STATE_LIMIT = 1.0
FIXED_INTEGRAL_STATE_LIMIT_BOUNDS = (1.0, 1.0)
INTEGRAL_STATE_HARD_LIMIT = 5.0
INTEGRAL_AUTHORITY_ERROR_N = 0.5


class V4StageError(ValueError):
    """A typed V4 stage contract or ledger transition is invalid."""


class V4Stage(str, Enum):
    FF_IOFF_100 = "V4_FF_IOFF_100"
    FF_ION_6D_100 = "V4_FF_ION_6D_100"
    FF_ION_LIMIT_7D_100 = "V4_FF_ION_LIMIT_7D_100"


class ProposalMethod(str, Enum):
    ANCHOR = "anchor"
    WARM_LOCAL = "warm_local"
    WARM_GLOBAL = "warm_global"
    QLOGNEI_LOCAL = "qlognei_local"
    QLOGNEI_GLOBAL = "qlognei_global"
    LOCAL_POLISH = "local_polish"


class AttemptOutcome(str, Enum):
    IN_FLIGHT = "in_flight"
    EXACT = "exact"
    CENSORED = "censored"
    INELIGIBLE = "ineligible"
    FAILURE = "failure"


class V4FailureClass(str, Enum):
    """The only failure classes eligible for the recovery seam."""

    TIMING_BOUNDARY = "timing_boundary"
    TRANSPORT = "transport"
    HOST_BOUNDARY = "host_boundary"
    FORCE_INVARIANT = "force_invariant"
    RAW_SENSOR_ANOMALY = "raw_sensor_anomaly"
    JOINT_ANOMALY = "joint_anomaly"
    PROTECTIVE_STOP = "protective_stop"
    EMERGENCY_STOP = "emergency_stop"
    HOME_UNSAFE = "home_unsafe"
    SOURCE_IDENTITY_MISMATCH = "source_identity_mismatch"
    UNKNOWN = "unknown"


class V4FailureDisposition(str, Enum):
    RECOVERABLE_RESUME = "recoverable_resume"
    HARD_TERMINAL = "hard_terminal"


class V4HomeStatus(str, Enum):
    VERIFIED = "verified"
    NOT_ATTEMPTED = "not_attempted"
    NOT_PERMITTED = "not_permitted"
    FAILED = "failed"
    BLOCKED = "home_blocked"


class V4RecoveryStatus(str, Enum):
    RESUME_READY = "resume_ready"
    STALE_EPOCH = "stale_epoch"
    READINESS_MISSING = "readiness_missing"
    IN_FLIGHT = "in_flight"
    DUPLICATE_DISPATCH = "duplicate_dispatch"
    HOME_BLOCKED = "home_blocked"
    HOME_NOT_VERIFIED = "home_not_verified"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class V4FailureEvidenceV1:
    """Typed owner evidence consumed by the fail-closed recovery decision."""

    failure_class: V4FailureClass
    reason: str
    home_permitted: bool
    home_verified: bool = False
    safety_fault: bool = False
    force_fault: bool = False
    sensor_fault: bool = False
    joint_fault: bool = False
    protective_stop: bool = False
    emergency_stop: bool = False
    source_identity_matches: bool = True

    def __post_init__(self) -> None:
        try:
            failure_class = (
                self.failure_class
                if isinstance(self.failure_class, V4FailureClass)
                else V4FailureClass(self.failure_class)
            )
        except (TypeError, ValueError) as exc:
            raise V4StageError("V4 failure class is not typed") from exc
        if not isinstance(self.reason, str) or not self.reason:
            raise V4StageError("V4 failure reason is missing")
        for name in (
            "home_permitted",
            "home_verified",
            "safety_fault",
            "force_fault",
            "sensor_fault",
            "joint_fault",
            "protective_stop",
            "emergency_stop",
            "source_identity_matches",
        ):
            if type(getattr(self, name)) is not bool:
                raise V4StageError(f"V4 failure evidence {name} must be bool")
        object.__setattr__(self, "failure_class", failure_class)


@dataclass(frozen=True)
class V4RecoveryReceiptV1:
    """Immutable decision/status receipt; it never authorizes hard dispatch."""

    disposition: V4FailureDisposition
    status: V4RecoveryStatus
    failure_class: V4FailureClass
    reason: str
    current_attempt_ordinal: int
    attempt_count: int
    home_status: V4HomeStatus
    home_attempted: bool
    home_receipt: Mapping[str, Any] | None
    prior_epoch: int
    fresh_epoch: int | None
    fresh_readiness: bool
    next_attempt_ordinal: int | None
    auto_dispatch_permitted: bool
    dispatch_id: str | None = None
    schema: str = RECOVERY_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        try:
            disposition = (
                self.disposition
                if isinstance(self.disposition, V4FailureDisposition)
                else V4FailureDisposition(self.disposition)
            )
            status = (
                self.status
                if isinstance(self.status, V4RecoveryStatus)
                else V4RecoveryStatus(self.status)
            )
            failure_class = (
                self.failure_class
                if isinstance(self.failure_class, V4FailureClass)
                else V4FailureClass(self.failure_class)
            )
            home_status = (
                self.home_status
                if isinstance(self.home_status, V4HomeStatus)
                else V4HomeStatus(self.home_status)
            )
        except (TypeError, ValueError) as exc:
            raise V4StageError("V4 recovery receipt enum is invalid") from exc
        if self.schema != RECOVERY_SCHEMA or self.version != 1:
            raise V4StageError("V4 recovery receipt schema/version differs")
        if not isinstance(self.reason, str) or not self.reason:
            raise V4StageError("V4 recovery receipt reason is missing")
        for name in ("current_attempt_ordinal", "attempt_count", "prior_epoch"):
            _positive_int(getattr(self, name), f"recovery {name}")
        if self.fresh_epoch is not None:
            _positive_int(self.fresh_epoch, "recovery fresh epoch")
        if self.next_attempt_ordinal is not None:
            _positive_int(self.next_attempt_ordinal, "recovery next attempt ordinal")
        if type(self.home_attempted) is not bool or type(self.fresh_readiness) is not bool:
            raise V4StageError("V4 recovery receipt boolean fields are invalid")
        if type(self.auto_dispatch_permitted) is not bool:
            raise V4StageError("V4 recovery auto-dispatch field is invalid")
        if self.home_receipt is not None and not isinstance(self.home_receipt, Mapping):
            raise V4StageError("V4 recovery Home receipt is not an object")
        if disposition is V4FailureDisposition.HARD_TERMINAL and self.auto_dispatch_permitted:
            raise V4StageError("hard-terminal recovery cannot permit dispatch")
        if self.auto_dispatch_permitted and status is not V4RecoveryStatus.RESUME_READY:
            raise V4StageError("only resume-ready recovery can permit dispatch")
        if status is V4RecoveryStatus.RESUME_READY and disposition is not V4FailureDisposition.RECOVERABLE_RESUME:
            raise V4StageError("resume-ready recovery must be recoverable")
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "failure_class", failure_class)
        object.__setattr__(self, "home_status", home_status)
        if self.home_receipt is not None:
            object.__setattr__(self, "home_receipt", MappingProxyType(dict(self.home_receipt)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "disposition": self.disposition.value,
            "status": self.status.value,
            "failure_class": self.failure_class.value,
            "reason": self.reason,
            "current_attempt_ordinal": self.current_attempt_ordinal,
            "attempt_count": self.attempt_count,
            "home_status": self.home_status.value,
            "home_attempted": self.home_attempted,
            "home_receipt": None if self.home_receipt is None else dict(self.home_receipt),
            "prior_epoch": self.prior_epoch,
            "fresh_epoch": self.fresh_epoch,
            "fresh_readiness": self.fresh_readiness,
            "next_attempt_ordinal": self.next_attempt_ordinal,
            "auto_dispatch_permitted": self.auto_dispatch_permitted,
            "dispatch_id": self.dispatch_id,
        }


@dataclass(frozen=True)
class V4CrossStageBaselineV1:
    """A confirmed sealed incumbent imported only for causal censoring.

    This is deliberately not an optimizer observation.  It supplies a typed
    threshold to a later Step5 curve until that curve has its own confirmed
    n=3 incumbent; it never enters GP/tell/ranking and it cannot censor warm,
    anchor, confirmation, or qualification attempts.
    """

    target_stage: str
    source_stage: str
    candidate_token: str
    mean_n: float
    n: int
    source_fingerprint_sha256: str
    source_identity: str
    confirmation_receipt_sha256: str
    schema: str = CROSS_STAGE_BASELINE_SCHEMA
    version: int = 1
    baseline_kind: str = "cross_stage_confirmed"

    def __post_init__(self) -> None:
        if self.schema != CROSS_STAGE_BASELINE_SCHEMA or self.version != 1:
            raise V4StageError("cross-stage baseline schema/version differs")
        if self.baseline_kind != "cross_stage_confirmed":
            raise V4StageError("cross-stage baseline kind differs")
        if not isinstance(self.target_stage, str) or not self.target_stage:
            raise V4StageError("cross-stage baseline target stage is missing")
        if not isinstance(self.source_stage, str) or not self.source_stage:
            raise V4StageError("cross-stage baseline source stage is missing")
        if self.source_stage == self.target_stage:
            raise V4StageError("cross-stage baseline must come from another stage")
        _require_sha(self.candidate_token, "cross-stage baseline candidate token")
        _require_sha(self.source_fingerprint_sha256, "cross-stage baseline source fingerprint")
        _require_sha(self.source_identity, "cross-stage baseline source identity")
        _require_sha(self.confirmation_receipt_sha256, "cross-stage baseline confirmation receipt")
        mean = _finite(self.mean_n, "cross-stage baseline mean")
        if mean <= 0.0:
            raise V4StageError("cross-stage baseline mean must be positive")
        if type(self.n) is not int or self.n < WINNER_TOTAL_N:
            raise V4StageError("cross-stage baseline requires confirmed n=3")
        object.__setattr__(self, "mean_n", mean)

    @classmethod
    def from_confirmation_report(
        cls,
        path: Path,
        *,
        target_stage: V4Stage | str,
    ) -> "V4CrossStageBaselineV1":
        """Load and validate a sealed V4 confirmation report as a threshold."""

        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise V4StageError("cross-stage confirmation report is unreadable") from exc
        if not isinstance(raw, Mapping):
            raise V4StageError("cross-stage confirmation report is not an object")
        source_stage = raw.get("stage")
        selected_target = _stage(target_stage).value
        if source_stage == selected_target or not isinstance(source_stage, str):
            raise V4StageError("cross-stage confirmation source/target stage differs")
        if not str(raw.get("status", "")).startswith("PASS"):
            raise V4StageError("cross-stage confirmation report is not PASS")
        # A confirmation root can be useful as a historical checkpoint while
        # still being explicitly forbidden from promoting the next stage.  A
        # cross-stage censor baseline is an admission artifact, not a general
        # performance claim, so never import a report that declined that
        # promotion.
        if raw.get("promotion_to_stage_b") is not True:
            raise V4StageError(
                "cross-stage confirmation report is not authorized for stage promotion"
            )
        try:
            n = int(raw["confirmation_eligible_n"])
            summary = raw["summary"]
            rows = raw["eligible_rows"]
            receipt_sha = str(raw["receipt_sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise V4StageError("cross-stage confirmation report lacks summary") from exc
        if not isinstance(summary, Mapping) or not isinstance(rows, list) or not rows:
            raise V4StageError("cross-stage confirmation report rows are incomplete")
        unsigned = {key: value for key, value in raw.items() if key != "receipt_sha256"}
        if _sha(unsigned) != receipt_sha:
            raise V4StageError("cross-stage confirmation report receipt hash differs")
        if n < WINNER_TOTAL_N or len(rows) != n:
            raise V4StageError("cross-stage confirmation report has insufficient eligible n")
        candidate_tokens = {str(row.get("candidate_token")) for row in rows if isinstance(row, Mapping)}
        fingerprints = {str(row.get("campaign_fingerprint_sha256")) for row in rows if isinstance(row, Mapping)}
        source_identities = {str(row.get("source_identity")) for row in rows if isinstance(row, Mapping)}
        if len(candidate_tokens) != 1 or len(fingerprints) != 1 or len(source_identities) != 1:
            raise V4StageError("cross-stage confirmation rows do not share one identity")
        if any(
            not isinstance(row, Mapping)
            or row.get("physical_eligible") is not True
            or row.get("home") is not True
            or row.get("timing_gate") is not True
            or not isinstance(row.get("mae_n"), (int, float))
            or not math.isfinite(float(row.get("mae_n")))
        for row in rows
        ):
            raise V4StageError("cross-stage confirmation contains an ineligible row")
        report_root = Path(path).resolve().parent.parent
        for row in rows:
            correction = row.get("serialization_correction")
            if correction is None:
                continue
            if not isinstance(correction, str) or not correction:
                raise V4StageError("cross-stage serialization correction path is invalid")
            correction_path = (
                Path(correction)
                if Path(correction).is_absolute()
                else report_root / correction
            )
            try:
                correction_raw = json.loads(correction_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise V4StageError("cross-stage serialization correction is unreadable") from exc
            if not isinstance(correction_raw, Mapping):
                raise V4StageError("cross-stage serialization correction is not an object")
            correction_sha = correction_raw.get("receipt_sha256")
            correction_unsigned = {
                key: value for key, value in correction_raw.items() if key != "receipt_sha256"
            }
            if (
                not isinstance(correction_sha, str)
                or _sha(correction_unsigned) != correction_sha
                or correction_raw.get("repair_type") != "serialization_only"
                or correction_raw.get("new_trial") is not False
                or correction_raw.get("original_confirmation_state") != "failed"
                or correction_raw.get("candidate_token") != row.get("candidate_token")
                or correction_raw.get("physical_eligible") is not True
                or correction_raw.get("timing_gate") is not True
                or correction_raw.get("home_verified") is not True
            ):
                raise V4StageError("cross-stage serialization correction is not a sealed repair")
        return cls(
            target_stage=selected_target,
            source_stage=source_stage,
            candidate_token=next(iter(candidate_tokens)),
            mean_n=_finite(summary.get("mean_n"), "cross-stage report mean"),
            n=n,
            source_fingerprint_sha256=next(iter(fingerprints)),
            source_identity=next(iter(source_identities)),
            confirmation_receipt_sha256=receipt_sha,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "baseline_kind": self.baseline_kind,
            "target_stage": self.target_stage,
            "source_stage": self.source_stage,
            "candidate_token": self.candidate_token,
            "mean_n": self.mean_n,
            "n": self.n,
            "source_fingerprint_sha256": self.source_fingerprint_sha256,
            "source_identity": self.source_identity,
            "confirmation_receipt_sha256": self.confirmation_receipt_sha256,
        }


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool):
        raise V4StageError(f"{role} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise V4StageError(f"{role} must be finite") from exc
    if not math.isfinite(parsed):
        raise V4StageError(f"{role} must be finite")
    return parsed


def _sha(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise V4StageError("value is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _positive_int(value: Any, role: str) -> int:
    if type(value) is not int or value <= 0:
        raise V4StageError(f"{role} must be a positive integer")
    return value


def _snap_quarter_lattice(
    value: float,
    *,
    anchor: float,
    bounds: tuple[float, float],
    role: str,
) -> float:
    """Return the nearest executable quarter-octave value inside bounds."""

    if not all(math.isfinite(float(item)) and float(item) > 0.0 for item in (value, anchor)):
        raise V4StageError(f"{role} lattice inputs are invalid")
    try:
        step = int(round(math.log2(float(value) / float(anchor)) / 0.25))
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        raise V4StageError(f"{role} lattice projection failed") from exc
    lower, upper = (float(bounds[0]), float(bounds[1]))
    for _ in range(512):
        projected = float(anchor) * (2.0 ** (0.25 * step))
        if lower <= projected <= upper:
            return projected
        step += 1 if projected < lower else -1
    raise V4StageError(f"{role} has no executable lattice value in bounds")


def _require_sha(value: Any, role: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise V4StageError(f"{role} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True)
class CombinedFFBundleV1:
    """The exact existing FF bundle used by the historical 0.31 branch."""

    velocity_xy: bool = True
    phase_force_reference: bool = True
    curvature: bool = False
    acceleration: bool = False
    history_residual: bool = False
    phase_force_reference_profile_id: str = "r013_goal035_path_phase_target_profile_v1"
    schema: str = FF_BUNDLE_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != FF_BUNDLE_SCHEMA or self.version != 1:
            raise V4StageError("combined FF bundle schema/version differs")
        values = (
            self.velocity_xy,
            self.phase_force_reference,
            self.curvature,
            self.acceleration,
            self.history_residual,
        )
        if any(type(value) is not bool for value in values):
            raise V4StageError("FF bundle terms must be boolean")
        if not self.velocity_xy or not self.phase_force_reference:
            raise V4StageError("the selected combined FF bundle is incomplete")
        if self.curvature or self.acceleration or self.history_residual:
            raise V4StageError("new curvature/acceleration/history FF is deferred")
        if self.phase_force_reference_profile_id != "r013_goal035_path_phase_target_profile_v1":
            raise V4StageError("phase force-reference profile identity differs")

    @property
    def identity(self) -> str:
        return _sha(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "velocity_xy": self.velocity_xy,
            "phase_force_reference": self.phase_force_reference,
            "curvature": self.curvature,
            "acceleration": self.acceleration,
            "history_residual": self.history_residual,
            "phase_force_reference_profile_id": self.phase_force_reference_profile_id,
            "comparison_role": "historical_goal035_combined_ff_only",
        }


def _stage(value: V4Stage | str) -> V4Stage:
    try:
        return value if isinstance(value, V4Stage) else V4Stage(value)
    except (TypeError, ValueError) as exc:
        raise V4StageError(f"unknown V4 stage {value!r}") from exc


@dataclass(frozen=True)
class V4StageConfigV1:
    stage: V4Stage
    ff_bundle: CombinedFFBundleV1 = field(default_factory=CombinedFFBundleV1)
    attempt_budget: int = ATTEMPT_BUDGET
    winner_total_n: int = WINNER_TOTAL_N
    censor_kappa: float = CENSOR_KAPPA
    censor_min_closed_bins: int = CENSOR_MIN_CLOSED_BINS
    stretch_target_n: float = 0.1
    target_force_n: float = TARGET_FORCE_N
    metric_window_s: tuple[float, float] = METRIC_WINDOW_S
    kernel: str = "matern52_ard"
    i_policy: str = "off"
    integral_state_limit_n_s: float = 1.0
    integral_state_limit_bounds: tuple[float, float] = FIXED_INTEGRAL_STATE_LIMIT_BOUNDS
    authority_error_n: float = 0.5
    normal_velocity_bound_m_s: float = 0.003
    schema: str = V4_TWO_STAGE_SCHEMA
    version: int = V4_TWO_STAGE_VERSION

    def __post_init__(self) -> None:
        stage = _stage(self.stage)
        if self.schema != V4_TWO_STAGE_SCHEMA or self.version != V4_TWO_STAGE_VERSION:
            raise V4StageError("V4 stage config schema/version differs")
        if not isinstance(self.ff_bundle, CombinedFFBundleV1):
            raise V4StageError("V4 stage FF bundle is not typed")
        if self.attempt_budget != ATTEMPT_BUDGET or self.winner_total_n != WINNER_TOTAL_N:
            raise V4StageError("V4 stage budget/repeat contract differs")
        if _finite(self.censor_kappa, "censor kappa") != CENSOR_KAPPA:
            raise V4StageError("V4 stage censor kappa differs")
        if self.censor_min_closed_bins != CENSOR_MIN_CLOSED_BINS:
            raise V4StageError("V4 stage censor warmup differs")
        if _finite(self.stretch_target_n, "stretch target") != 0.1:
            raise V4StageError("V4 stretch target differs")
        if _finite(self.target_force_n, "target force") != TARGET_FORCE_N:
            raise V4StageError("V4 target force is not fixed at 5 N")
        window = tuple(_finite(value, "metric window") for value in self.metric_window_s)
        if window != METRIC_WINDOW_S:
            raise V4StageError("V4 metric window differs")
        if self.kernel != "matern52_ard":
            raise V4StageError("V4 stage kernel must remain Matérn-5/2 ARD")
        limits = tuple(_finite(value, "integral state limit bound") for value in self.integral_state_limit_bounds)
        if len(limits) != 2 or limits[0] <= 0.0 or limits[1] < limits[0]:
            raise V4StageError("integral state limit bounds are invalid")
        if stage is V4Stage.FF_IOFF_100:
            if self.i_policy != "off":
                raise V4StageError("Stage A must keep I off")
            if _finite(self.integral_state_limit_n_s, "integral state limit") != FIXED_INTEGRAL_STATE_LIMIT or limits != FIXED_INTEGRAL_STATE_LIMIT_BOUNDS:
                raise V4StageError("Stage A integral state limit must remain fixed")
        elif stage is V4Stage.FF_ION_6D_100:
            if self.i_policy != "conditional-double-clamp-v1":
                raise V4StageError("Stage B anti-windup policy differs")
            if _finite(self.integral_state_limit_n_s, "integral state limit") != FIXED_INTEGRAL_STATE_LIMIT or limits != FIXED_INTEGRAL_STATE_LIMIT_BOUNDS:
                raise V4StageError("Stage B1 integral state limit must remain fixed")
        else:
            if self.i_policy != "conditional-double-clamp-v1":
                raise V4StageError("Stage B2 anti-windup policy differs")
            if limits != INTEGRAL_STATE_LIMIT_BOUNDS:
                raise V4StageError("Stage B2 integral state limit domain differs")
            for value, role in (
                (self.integral_state_limit_n_s, "integral state limit"),
                (self.authority_error_n, "authority error"),
                (self.normal_velocity_bound_m_s, "normal velocity bound"),
            ):
                if _finite(value, role) <= 0.0:
                    raise V4StageError(f"{role} must be positive")
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "metric_window_s", window)
        object.__setattr__(self, "integral_state_limit_bounds", limits)

    @property
    def feature_names(self) -> tuple[str, ...]:
        if self.stage is V4Stage.FF_IOFF_100:
            return STAGE_A_FEATURES
        if self.stage is V4Stage.FF_ION_6D_100:
            return STAGE_B_FEATURES
        return STAGE_B2_FEATURES

    @property
    def fingerprint_payload(self) -> dict[str, Any]:
        return {
            "schema": V4_TWO_STAGE_SCHEMA,
            "version": V4_TWO_STAGE_VERSION,
            "stage": self.stage.value,
            "features": list(self.feature_names),
            "ff_bundle": self.ff_bundle.as_dict(),
            "attempt_budget": self.attempt_budget,
            "winner_total_n": self.winner_total_n,
            "censor": {
                "kappa": self.censor_kappa,
                "min_closed_bins": self.censor_min_closed_bins,
                "counts_attempt": True,
                "trainable": False,
            },
            "metric_window_s": list(self.metric_window_s),
            "target_force_n": self.target_force_n,
            "kernel": self.kernel,
            "i_policy": self.i_policy,
            "integral_state_limit_n_s": self.integral_state_limit_n_s,
            "integral_state_limit_bounds": list(self.integral_state_limit_bounds),
            "authority_error_n": self.authority_error_n,
            "normal_velocity_bound_m_s": self.normal_velocity_bound_m_s,
        }

    @property
    def fingerprint_sha256(self) -> str:
        return _sha(self.fingerprint_payload)

    def as_dict(self) -> dict[str, Any]:
        return self.fingerprint_payload | {
            "stretch_target_n": self.stretch_target_n,
            "feature_names": list(self.feature_names),
            "fingerprint_sha256": self.fingerprint_sha256,
        }


def validate_candidate(candidate: Mapping[str, Any], stage: V4Stage | str) -> dict[str, Any]:
    """Validate the shared physical candidate with stage-specific I rules."""

    selected = _stage(stage)
    required = {
        "force_p_gain",
        "force_damping",
        "force_i_gain",
        "i_off",
        "normal_filter_tau_s",
        "orientation_ko",
        "motion_kp",
        "target_force_n",
    }
    if not isinstance(candidate, Mapping) or not required.issubset(candidate):
        raise V4StageError("candidate fields are incomplete")
    result = {key: candidate[key] for key in required}
    p = _finite(result["force_p_gain"], "force P")
    damping = _finite(result["force_damping"], "force damping")
    i_gain = _finite(result["force_i_gain"], "force I")
    tau = _finite(result["normal_filter_tau_s"], "filter tau")
    ko = _finite(result["orientation_ko"], "orientation Ko")
    kp = _finite(result["motion_kp"], "motion Kp")
    target = _finite(result["target_force_n"], "target force")
    ratio = p / damping
    i_ratio = i_gain / p if p else math.inf
    limit = _finite(candidate.get("integral_state_limit_n_s", FIXED_INTEGRAL_STATE_LIMIT), "integral state limit")
    if not P_OVER_D_BOUNDS[0] <= ratio <= P_OVER_D_BOUNDS[1]:
        raise V4StageError("candidate P/D is outside the fixed domain")
    if not DAMPING_BOUNDS[0] <= damping <= DAMPING_BOUNDS[1]:
        raise V4StageError("candidate damping is outside the fixed domain")
    if not TAU_BOUNDS[0] <= tau <= TAU_BOUNDS[1]:
        raise V4StageError("candidate tau is outside the fixed domain")
    if not KO_BOUNDS[0] <= ko <= KO_BOUNDS[1]:
        raise V4StageError("candidate Ko is outside the fixed domain")
    if not MOTION_KP_BOUNDS[0] <= kp <= MOTION_KP_BOUNDS[1]:
        raise V4StageError("candidate motion Kp is outside the fixed domain")
    if target != TARGET_FORCE_N:
        raise V4StageError("candidate target force must remain 5 N")
    if selected is V4Stage.FF_IOFF_100:
        if result["i_off"] is not True or i_gain != 0.0:
            raise V4StageError("Stage A requires i_off=true and force_i_gain=0")
        if limit != FIXED_INTEGRAL_STATE_LIMIT:
            raise V4StageError("Stage A integral state limit must be fixed")
    else:
        if result["i_off"] is not False or not I_OVER_P_BOUNDS[0] <= i_ratio <= I_OVER_P_BOUNDS[1]:
            raise V4StageError("Stage B requires a bounded positive I/P")
        if i_gain > I_GAIN_MAX + 1e-15:
            raise V4StageError("Stage B force I exceeds the reviewed bound")
        if selected is V4Stage.FF_ION_6D_100:
            if limit != FIXED_INTEGRAL_STATE_LIMIT:
                raise V4StageError("Stage B1 integral state limit must be fixed")
        else:
            if not INTEGRAL_STATE_LIMIT_BOUNDS[0] <= limit <= INTEGRAL_STATE_LIMIT_BOUNDS[1]:
                raise V4StageError("Stage B2 integral state limit is outside the fixed domain")
            authority_limit = INTEGRAL_AUTHORITY_ERROR_N / i_ratio
            feasible_upper = min(INTEGRAL_STATE_HARD_LIMIT, authority_limit)
            if limit > feasible_upper + 1e-12:
                raise V4StageError("Stage B2 integral state limit is masked by the authority clamp")
    result.update(
        {
            "force_p_gain": p,
            "force_damping": damping,
            "force_i_gain": i_gain,
            "normal_filter_tau_s": tau,
            "orientation_ko": ko,
            "motion_kp": kp,
            "target_force_n": target,
            "i_off": bool(result["i_off"]),
            "integral_state_limit_n_s": limit,
        }
    )
    return {key: result[key] for key in (
        "force_p_gain", "force_damping", "force_i_gain", "i_off",
        "normal_filter_tau_s", "orientation_ko", "motion_kp", "target_force_n", "integral_state_limit_n_s",
    )}


def candidate_key(candidate: Mapping[str, Any], stage: V4Stage | str) -> str:
    return _sha({"stage": _stage(stage).value, "candidate": validate_candidate(candidate, stage)})


def candidate_features(candidate: Mapping[str, Any], stage: V4Stage | str) -> tuple[float, ...]:
    selected = _stage(stage)
    parsed = validate_candidate(candidate, selected)
    p = parsed["force_p_gain"]
    damping = parsed["force_damping"]
    values = [
        math.log2(p / damping),
        math.log2(damping),
        math.log2(parsed["normal_filter_tau_s"]),
        math.log2(parsed["orientation_ko"]),
        math.log2(parsed["motion_kp"]),
    ]
    if selected in {V4Stage.FF_ION_6D_100, V4Stage.FF_ION_LIMIT_7D_100}:
        values.append(math.log2(parsed["force_i_gain"] / p))
    if selected is V4Stage.FF_ION_LIMIT_7D_100:
        values.append(math.log2(parsed["integral_state_limit_n_s"]))
    return tuple(values)


def candidate_token(candidate: Mapping[str, Any], stage: V4Stage | str) -> str:
    return candidate_key(candidate, stage)


@dataclass(frozen=True)
class V4StageAttemptV1:
    ordinal: int
    candidate: Mapping[str, Any]
    candidate_token: str
    method: ProposalMethod
    outcome: AttemptOutcome = AttemptOutcome.IN_FLIGHT
    mae_n: float | None = None
    prefix_mae_n: float | None = None
    closed_bin_count: int = 0
    reason: str = ""
    trainable: bool = False
    milestone_thresholds_n: tuple[float, ...] = ()
    partial_receipt_path: str | None = None
    home_receipt_path: str | None = None
    trigger_watermark_s: float | None = None
    censor_baseline: Mapping[str, Any] | None = None
    timing_diagnostics: Mapping[str, Any] | None = None
    diagnostic_mae_n: float | None = None
    failure_disposition: V4FailureDisposition | None = None
    schema: str = ATTEMPT_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        _positive_int(self.ordinal, "attempt ordinal")
        if self.schema != ATTEMPT_SCHEMA or self.version != 1:
            raise V4StageError("attempt schema/version differs")
        _require_sha(self.candidate_token, "candidate token")
        if not isinstance(self.method, ProposalMethod) or not isinstance(self.outcome, AttemptOutcome):
            raise V4StageError("attempt method/outcome must be typed")
        if self.mae_n is not None and _finite(self.mae_n, "attempt MAE") < 0.0:
            raise V4StageError("attempt MAE cannot be negative")
        if self.prefix_mae_n is not None and _finite(self.prefix_mae_n, "prefix MAE") < 0.0:
            raise V4StageError("prefix MAE cannot be negative")
        if type(self.closed_bin_count) is not int or self.closed_bin_count < 0:
            raise V4StageError("closed bin count must be nonnegative")
        if self.outcome is AttemptOutcome.EXACT and (self.mae_n is None or not self.trainable):
            raise V4StageError("exact attempt must be trainable")
        if self.outcome is AttemptOutcome.CENSORED and (self.mae_n is not None or self.trainable):
            raise V4StageError("censored attempt cannot be an exact GP row")
        if self.trigger_watermark_s is not None:
            _finite(self.trigger_watermark_s, "censor trigger watermark")
        if self.censor_baseline is not None and not isinstance(self.censor_baseline, Mapping):
            raise V4StageError("censor baseline receipt must be an object")
        if self.diagnostic_mae_n is not None and _finite(self.diagnostic_mae_n, "diagnostic MAE") < 0.0:
            raise V4StageError("diagnostic MAE cannot be negative")
        if self.timing_diagnostics is not None and not isinstance(self.timing_diagnostics, Mapping):
            raise V4StageError("timing diagnostics receipt must be an object")
        if self.failure_disposition is not None:
            try:
                disposition = (
                    self.failure_disposition
                    if isinstance(self.failure_disposition, V4FailureDisposition)
                    else V4FailureDisposition(self.failure_disposition)
                )
            except (TypeError, ValueError) as exc:
                raise V4StageError("attempt failure disposition is not typed") from exc
            object.__setattr__(self, "failure_disposition", disposition)
        object.__setattr__(self, "candidate", dict(self.candidate))
        object.__setattr__(self, "milestone_thresholds_n", tuple(float(value) for value in self.milestone_thresholds_n))
        if self.censor_baseline is not None:
            object.__setattr__(self, "censor_baseline", dict(self.censor_baseline))
        if self.timing_diagnostics is not None:
            object.__setattr__(self, "timing_diagnostics", dict(self.timing_diagnostics))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "ordinal": self.ordinal,
            "candidate": dict(self.candidate),
            "candidate_token": self.candidate_token,
            "method": self.method.value,
            "outcome": self.outcome.value,
            "mae_n": self.mae_n,
            "prefix_mae_n": self.prefix_mae_n,
            "closed_bin_count": self.closed_bin_count,
            "reason": self.reason,
            "trainable": self.trainable,
            "milestone_thresholds_n": list(self.milestone_thresholds_n),
            "partial_receipt_path": self.partial_receipt_path,
            "home_receipt_path": self.home_receipt_path,
            "trigger_watermark_s": self.trigger_watermark_s,
            "censor_baseline": None if self.censor_baseline is None else dict(self.censor_baseline),
            "timing_diagnostics": (
                None if self.timing_diagnostics is None else dict(self.timing_diagnostics)
            ),
            "diagnostic_mae_n": self.diagnostic_mae_n,
            "failure_disposition": (
                None
                if self.failure_disposition is None
                else self.failure_disposition.value
            ),
        }


@dataclass
class V4StageCampaignV1:
    config: V4StageConfigV1
    seed_candidate: Mapping[str, Any]
    fingerprint_sha256: str
    ledger_path: Path | None = None
    attempts: list[V4StageAttemptV1] = field(default_factory=list)
    retired_tokens: set[str] = field(default_factory=set)
    announced_milestones: set[float] = field(default_factory=set)
    in_flight: V4StageAttemptV1 | None = None
    confirmation_mode: bool = False
    terminal_failure: str | None = None
    cross_stage_baseline: V4CrossStageBaselineV1 | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.config, V4StageConfigV1):
            raise V4StageError("V4 campaign config is not typed")
        self.fingerprint_sha256 = _require_sha(self.fingerprint_sha256, "stage fingerprint")
        self.seed_candidate = validate_candidate(self.seed_candidate, self.config.stage)
        if self.cross_stage_baseline is not None:
            if not isinstance(self.cross_stage_baseline, V4CrossStageBaselineV1):
                raise V4StageError("cross-stage baseline is not typed")
            if self.cross_stage_baseline.target_stage != self.config.stage.value:
                raise V4StageError("cross-stage baseline target stage differs")
        if self.config.stage is V4Stage.FF_IOFF_100:
            if self.seed_candidate["i_off"] is not True:
                raise V4StageError("Stage A seed must be I-off")
        elif self.seed_candidate["i_off"] is not False:
            raise V4StageError("Stage B seed must be I-on")
        if self.ledger_path is not None:
            self.ledger_path = Path(self.ledger_path)
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.ledger_path.exists():
                self._append_ledger({
                    "event": "header",
                    "config": self.config.as_dict(),
                    "fingerprint_sha256": self.fingerprint_sha256,
                    "cross_stage_baseline": (
                        None
                        if self.cross_stage_baseline is None
                        else self.cross_stage_baseline.as_dict()
                    ),
                })

    @classmethod
    def resume(
        cls,
        *,
        config: V4StageConfigV1,
        seed_candidate: Mapping[str, Any],
        fingerprint_sha256: str,
        ledger_path: Path,
        cross_stage_baseline: V4CrossStageBaselineV1 | None = None,
    ) -> "V4StageCampaignV1":
        """Restore closed attempts from one append-only stage ledger."""

        path = Path(ledger_path)
        if not path.is_file():
            raise V4StageError("V4 stage resume ledger is missing")
        campaign = cls(
            config=config,
            seed_candidate=seed_candidate,
            fingerprint_sha256=fingerprint_sha256,
            ledger_path=path,
            cross_stage_baseline=cross_stage_baseline,
        )
        try:
            first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        except (IndexError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise V4StageError("V4 stage ledger header is unreadable") from exc
        header_baseline_raw = first.get("cross_stage_baseline") if isinstance(first, Mapping) else None
        if header_baseline_raw is not None:
            try:
                header_baseline = V4CrossStageBaselineV1(**dict(header_baseline_raw))
            except (TypeError, ValueError) as exc:
                raise V4StageError("V4 stage cross-stage baseline header is malformed") from exc
            if campaign.cross_stage_baseline is None:
                campaign.cross_stage_baseline = header_baseline
            elif campaign.cross_stage_baseline.as_dict() != header_baseline.as_dict():
                raise V4StageError("V4 stage cross-stage baseline differs on resume")
        pending: dict[str, dict[str, Any]] = {}
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise V4StageError(f"V4 stage ledger line {line_number} is invalid JSON") from exc
            if not isinstance(row, Mapping) or row.get("fingerprint_sha256") != fingerprint_sha256:
                raise V4StageError("V4 stage ledger fingerprint differs")
            event = row.get("event")
            attempt_raw = row.get("attempt")
            if event == "dispatch":
                if not isinstance(attempt_raw, Mapping):
                    raise V4StageError("V4 stage dispatch record is incomplete")
                pending[str(attempt_raw.get("candidate_token"))] = dict(attempt_raw)
            elif event in {"result", "confirmation_result"}:
                if not isinstance(attempt_raw, Mapping):
                    raise V4StageError("V4 stage result record is incomplete")
                token = str(attempt_raw.get("candidate_token"))
                pending.pop(token, None)
                try:
                    attempt = V4StageAttemptV1(
                        ordinal=int(attempt_raw["ordinal"]),
                        candidate=attempt_raw["candidate"],
                        candidate_token=token,
                        method=ProposalMethod(str(attempt_raw["method"])),
                        outcome=AttemptOutcome(str(attempt_raw["outcome"])),
                        mae_n=attempt_raw.get("mae_n"),
                        prefix_mae_n=attempt_raw.get("prefix_mae_n"),
                        closed_bin_count=int(attempt_raw.get("closed_bin_count", 0)),
                        reason=str(attempt_raw.get("reason", "")),
                        trainable=bool(attempt_raw.get("trainable", False)),
                        milestone_thresholds_n=tuple(attempt_raw.get("milestone_thresholds_n", ())),
                        partial_receipt_path=attempt_raw.get("partial_receipt_path"),
                        home_receipt_path=attempt_raw.get("home_receipt_path"),
                        trigger_watermark_s=attempt_raw.get("trigger_watermark_s"),
                        censor_baseline=attempt_raw.get("censor_baseline"),
                        timing_diagnostics=attempt_raw.get("timing_diagnostics"),
                        diagnostic_mae_n=attempt_raw.get("diagnostic_mae_n"),
                        failure_disposition=(
                            None
                            if attempt_raw.get("failure_disposition") is None
                            else V4FailureDisposition(
                                str(attempt_raw["failure_disposition"])
                            )
                        ),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise V4StageError(f"V4 stage result line {line_number} is malformed") from exc
                campaign.attempts.append(attempt)
                campaign.announced_milestones.update(attempt.milestone_thresholds_n)
                if attempt.outcome is AttemptOutcome.CENSORED:
                    campaign.retired_tokens.add(token)
                if (
                    attempt.outcome is AttemptOutcome.FAILURE
                    and attempt.failure_disposition is not V4FailureDisposition.RECOVERABLE_RESUME
                ):
                    campaign.terminal_failure = attempt.reason or "stage_failure"
        if pending:
            raise V4StageError("V4 stage resume has an unresolved in-flight dispatch")
        return campaign

    @property
    def exact_observations(self) -> tuple[V4StageAttemptV1, ...]:
        return tuple(attempt for attempt in self.attempts if attempt.outcome is AttemptOutcome.EXACT)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def search_attempt_count(self) -> int:
        return sum(attempt.ordinal <= self.config.attempt_budget for attempt in self.attempts)

    @property
    def confirmation_attempt_count(self) -> int:
        return sum(attempt.ordinal > self.config.attempt_budget for attempt in self.attempts)

    @property
    def complete(self) -> bool:
        return self.attempt_count >= self.config.attempt_budget

    @property
    def confirmed_incumbent(self) -> dict[str, Any] | None:
        groups: dict[str, list[V4StageAttemptV1]] = {}
        for attempt in self.exact_observations:
            groups.setdefault(attempt.candidate_token, []).append(attempt)
        confirmed: list[dict[str, Any]] = []
        for token, rows in groups.items():
            if len(rows) < CONFIRMED_REPEATS:
                continue
            mean = math.fsum(float(row.mae_n) for row in rows) / len(rows)
            confirmed.append({"candidate": dict(rows[0].candidate), "candidate_token": token, "n": len(rows), "mean_n": mean})
        return min(confirmed, key=lambda row: (row["mean_n"], row["candidate_token"])) if confirmed else None

    @property
    def censor_incumbent(self) -> dict[str, Any] | None:
        """Return the threshold source for the current novel-candidate censor.

        A branch-local confirmed incumbent always supersedes a cross-stage
        seed.  Until local n=3 exists, a validated previous-stage confirmation
        is usable as a threshold only; it is never an optimizer observation.
        """

        local = self.confirmed_incumbent
        cross = None if self.cross_stage_baseline is None else self.cross_stage_baseline.as_dict()
        if local is not None and cross is not None:
            # A later branch is not allowed to weaken a previously confirmed
            # cross-stage threshold merely because its own n=3 mean is worse.
            selected = local if float(local["mean_n"]) <= float(cross["mean_n"]) else cross
            return {
                **selected,
                "baseline_kind": "minimum_of_confirmed",
                "baseline_candidates": [local, cross],
            }
        if local is not None:
            return {**local, "baseline_kind": "branch_local_confirmed"}
        if cross is not None:
            return cross
        return None

    @property
    def best_exact(self) -> V4StageAttemptV1 | None:
        return min(self.exact_observations, key=lambda row: (float(row.mae_n), row.candidate_token), default=None)

    def _append_ledger(self, value: Mapping[str, Any]) -> None:
        if self.ledger_path is None:
            return
        with self.ledger_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(value), sort_keys=True, allow_nan=False) + "\n")
            stream.flush()

    def _next_method(self, ordinal: int) -> ProposalMethod:
        if ordinal <= 3:
            return ProposalMethod.ANCHOR
        if ordinal <= 12:
            return ProposalMethod.WARM_GLOBAL if ordinal % 5 == 0 else ProposalMethod.WARM_LOCAL
        if ordinal <= 80:
            return ProposalMethod.QLOGNEI_GLOBAL if ordinal % 5 == 0 else ProposalMethod.QLOGNEI_LOCAL
        return ProposalMethod.QLOGNEI_GLOBAL if ordinal % 5 == 0 else ProposalMethod.LOCAL_POLISH

    def _candidate_variant(self, ordinal: int, method: ProposalMethod) -> dict[str, Any]:
        if ordinal <= 3:
            return dict(self.seed_candidate)
        seed = dict(self.seed_candidate)
        # Quarter-octave perturbations keep the warm and local proposal surface
        # on the same executable lattice as the historical R013 domain.
        step = ((ordinal - 4) % 9) - 4
        # The mature R006 candidate adapter is quarter-octave typed.  Keep
        # local warm points on that executable lattice; a half-step here
        # creates candidates that can never reach the physical writer.
        scale = 2.0 ** (0.25 * step)
        if method in {ProposalMethod.WARM_GLOBAL, ProposalMethod.QLOGNEI_GLOBAL}:
            scale = 2.0 ** (0.25 * (((ordinal * 7) % 9) - 4))
        seed["force_damping"] = min(DAMPING_BOUNDS[1], max(DAMPING_BOUNDS[0], seed["force_damping"] * scale))
        seed["force_p_gain"] = min(
            P_OVER_D_BOUNDS[1] * seed["force_damping"],
            max(P_OVER_D_BOUNDS[0] * seed["force_damping"], seed["force_p_gain"] * scale),
        )
        seed["normal_filter_tau_s"] = min(TAU_BOUNDS[1], max(TAU_BOUNDS[0], seed["normal_filter_tau_s"] * scale))
        seed["orientation_ko"] = min(KO_BOUNDS[1], max(KO_BOUNDS[0], seed["orientation_ko"] * scale))
        seed["motion_kp"] = min(MOTION_KP_BOUNDS[1], max(MOTION_KP_BOUNDS[0], seed["motion_kp"] * scale))
        if self.config.stage in {V4Stage.FF_ION_6D_100, V4Stage.FF_ION_LIMIT_7D_100}:
            seed["force_i_gain"] = min(I_GAIN_MAX, max(I_OVER_P_BOUNDS[0] * seed["force_p_gain"], seed["force_i_gain"] * scale))
            seed["i_off"] = False
        # B2 opens with three explicit feasible limit sentinels.  Lower I/P
        # for the 2 and 5 N s points so the fixed 0.5 N authority clamp does
        # not collapse them onto the same effective limit as 1 N s.
        if self.config.stage is V4Stage.FF_ION_LIMIT_7D_100 and ordinal in {4, 5, 6}:
            sentinel_limit = {4: 1.0, 5: 2.0, 6: 5.0}[ordinal]
            if sentinel_limit > 1.0:
                seed["force_i_gain"] = 0.05 * seed["force_p_gain"]
            seed["integral_state_limit_n_s"] = sentinel_limit
        # The physical R006 DTO accepts only quarter-octave lattice points.
        # Clipping a floating domain bound (notably tau=0.0735784) can create
        # an off-lattice value even when the proposal scale is valid; snap all
        # dimensions back to the nearest executable point before admission.
        for key, bounds in (
            ("force_p_gain", (P_OVER_D_BOUNDS[0] * seed["force_damping"], P_OVER_D_BOUNDS[1] * seed["force_damping"])),
            ("force_damping", DAMPING_BOUNDS),
            ("normal_filter_tau_s", TAU_BOUNDS),
            ("orientation_ko", KO_BOUNDS),
            ("motion_kp", MOTION_KP_BOUNDS),
        ):
            seed[key] = _snap_quarter_lattice(
                float(seed[key]),
                anchor={
                    "force_p_gain": R006_P_ANCHOR,
                    "force_damping": R006_D_ANCHOR,
                    "normal_filter_tau_s": R006_TAU_ANCHOR,
                    "orientation_ko": R006_KO_ANCHOR,
                    "motion_kp": R006_KP_ANCHOR,
                }[key],
                bounds=(float(bounds[0]), float(bounds[1])),
                role=key,
            )
        if self.config.stage in {V4Stage.FF_ION_6D_100, V4Stage.FF_ION_LIMIT_7D_100}:
            seed["force_i_gain"] = _snap_quarter_lattice(
                float(seed["force_i_gain"]),
                anchor=R006_I_ANCHOR,
                bounds=(I_OVER_P_BOUNDS[0] * seed["force_p_gain"], I_GAIN_MAX),
                role="force_i_gain",
            )
        if self.config.stage is V4Stage.FF_ION_LIMIT_7D_100:
            i_ratio = float(seed["force_i_gain"]) / float(seed["force_p_gain"])
            feasible_upper = min(INTEGRAL_STATE_HARD_LIMIT, INTEGRAL_AUTHORITY_ERROR_N / i_ratio)
            if ordinal not in {4, 5, 6}:
                raw_limit = 2.0 ** (0.25 * (((ordinal * 11) % 9) - 4))
                seed["integral_state_limit_n_s"] = min(
                    feasible_upper,
                    max(INTEGRAL_STATE_LIMIT_BOUNDS[0], raw_limit),
                )
            if seed["integral_state_limit_n_s"] > feasible_upper + 1e-12:
                raise V4StageError("B2 sentinel is not feasible under authority clamp")
        return validate_candidate(seed, self.config.stage)

    def ask(self, candidate: Mapping[str, Any] | None = None) -> V4StageAttemptV1:
        if self.complete:
            raise V4StageError("V4 stage attempt budget is complete")
        if self.in_flight is not None:
            raise V4StageError("V4 stage has an in-flight attempt")
        ordinal = self.attempt_count + 1
        method = self._next_method(ordinal)
        chosen = validate_candidate(candidate, self.config.stage) if candidate is not None else self._candidate_variant(ordinal, method)
        token = candidate_token(chosen, self.config.stage)
        repeated_exact = any(
            row.candidate_token == token and row.outcome is not AttemptOutcome.CENSORED
            for row in self.attempts
        )
        if token in self.retired_tokens or (repeated_exact and method is not ProposalMethod.ANCHOR):
            # Deterministic collision escape; a live optimizer may still pass
            # a fresh candidate explicitly on the next call.
            for offset in range(1, 128):
                chosen = self._candidate_variant(ordinal + offset, method)
                # The compact warm surface can hit a clipped boundary.  Use
                # bounded *lattice* secondary perturbations; continuous
                # Ko/Kp escape values cannot reach the native R006 writer.
                chosen["orientation_ko"] = _snap_quarter_lattice(
                    KO_BOUNDS[0] + (KO_BOUNDS[1] - KO_BOUNDS[0])
                    * ((ordinal + offset) % 127) / 126.0,
                    anchor=R006_KO_ANCHOR,
                    bounds=KO_BOUNDS,
                    role="orientation_ko",
                )
                chosen["motion_kp"] = _snap_quarter_lattice(
                    MOTION_KP_BOUNDS[0] + (MOTION_KP_BOUNDS[1] - MOTION_KP_BOUNDS[0])
                    * ((ordinal + 3 * offset) % 127) / 126.0,
                    anchor=R006_KP_ANCHOR,
                    bounds=MOTION_KP_BOUNDS,
                    role="motion_kp",
                )
                chosen = validate_candidate(chosen, self.config.stage)
                token = candidate_token(chosen, self.config.stage)
                if token not in self.retired_tokens and not any(row.candidate_token == token for row in self.attempts):
                    break
            else:
                raise V4StageError("V4 stage candidate pool exhausted")
        attempt = V4StageAttemptV1(
            ordinal=ordinal,
            candidate=chosen,
            candidate_token=token,
            method=method,
        )
        self.in_flight = attempt
        self._append_ledger({"event": "dispatch", "attempt": attempt.as_dict(), "fingerprint_sha256": self.fingerprint_sha256})
        return attempt

    def ask_confirmation(self, candidate: Mapping[str, Any] | None = None) -> V4StageAttemptV1:
        """Open one winner confirmation outside the 100-attempt search budget."""

        if not self.complete:
            raise V4StageError("winner confirmation requires a closed search budget")
        if self.terminal_failure is not None:
            raise V4StageError(
                "V4 stage confirmation is terminally failed: "
                + self.terminal_failure
            )
        if self.in_flight is not None:
            raise V4StageError("V4 stage has an in-flight attempt")
        winner = self.best_exact if candidate is None else None
        chosen = (
            dict(winner.candidate)
            if winner is not None
            else validate_candidate(candidate or {}, self.config.stage)
        )
        token = candidate_token(chosen, self.config.stage)
        if winner is None and token != candidate_token(self.best_exact.candidate if self.best_exact else chosen, self.config.stage):
            raise V4StageError("confirmation candidate is not the discovered winner")
        attempt = V4StageAttemptV1(
            ordinal=self.config.attempt_budget + self.confirmation_attempt_count + 1,
            candidate=chosen,
            candidate_token=token,
            method=ProposalMethod.LOCAL_POLISH,
        )
        self.in_flight = attempt
        self.confirmation_mode = True
        self._append_ledger({"event": "confirmation_dispatch", "attempt": attempt.as_dict(), "fingerprint_sha256": self.fingerprint_sha256})
        return attempt

    def _finish(self, attempt: V4StageAttemptV1) -> V4StageAttemptV1:
        self.attempts.append(attempt)
        self.in_flight = None
        if attempt.outcome is AttemptOutcome.CENSORED:
            self.retired_tokens.add(attempt.candidate_token)
        self._append_ledger({"event": "result", "attempt": attempt.as_dict(), "fingerprint_sha256": self.fingerprint_sha256})
        return attempt

    def record_exact(self, mae_n: float, *, reason: str = "") -> V4StageAttemptV1:
        if self.in_flight is None:
            raise V4StageError("no V4 attempt is in flight")
        value = _finite(mae_n, "sealed MAE")
        if value < 0.0:
            raise V4StageError("sealed MAE cannot be negative")
        newly_announced = tuple(
            threshold for threshold in MILESTONE_THRESHOLDS_N
            if value < threshold and threshold not in self.announced_milestones
        )
        self.announced_milestones.update(newly_announced)
        return self._finish(V4StageAttemptV1(
            ordinal=self.in_flight.ordinal,
            candidate=self.in_flight.candidate,
            candidate_token=self.in_flight.candidate_token,
            method=self.in_flight.method,
            outcome=AttemptOutcome.EXACT,
            mae_n=value,
            closed_bin_count=550,
            reason=reason,
            trainable=True,
            milestone_thresholds_n=newly_announced,
        ))

    def record_censored(
        self,
        prefix_mae_n: float,
        *,
        closed_bin_count: int,
        incumbent_mean_n: float | None = None,
        censor_baseline: Mapping[str, Any] | None = None,
        reason: str = "prefix_mae_exceeds_confirmed_incumbent",
        partial_receipt_path: str | None = None,
        home_receipt_path: str | None = None,
        trigger_watermark_s: float | None = None,
    ) -> V4StageAttemptV1:
        if self.in_flight is None:
            raise V4StageError("no V4 attempt is in flight")
        if self.in_flight.method in {ProposalMethod.ANCHOR, ProposalMethod.WARM_LOCAL, ProposalMethod.WARM_GLOBAL}:
            raise V4StageError("warm/anchor attempts cannot be censored")
        if closed_bin_count < self.config.censor_min_closed_bins:
            raise V4StageError("censor requires the declared closed-bin warmup")
        incumbent = self.censor_incumbent if incumbent_mean_n is None else {
            "mean_n": _finite(incumbent_mean_n, "incumbent mean"),
            "baseline_kind": "explicit",
        }
        if incumbent is None:
            raise V4StageError("censor requires a local or cross-stage confirmed incumbent")
        baseline = dict(censor_baseline or incumbent)
        baseline_mean = _finite(baseline.get("mean_n"), "censor baseline mean")
        if not math.isclose(baseline_mean, float(incumbent["mean_n"]), rel_tol=0.0, abs_tol=1e-12):
            raise V4StageError("censor baseline mean differs from its threshold")
        prefix = _finite(prefix_mae_n, "prefix MAE")
        if prefix <= self.config.censor_kappa * baseline_mean:
            raise V4StageError("prefix MAE does not cross the censor threshold")
        return self._finish(V4StageAttemptV1(
            ordinal=self.in_flight.ordinal,
            candidate=self.in_flight.candidate,
            candidate_token=self.in_flight.candidate_token,
            method=self.in_flight.method,
            outcome=AttemptOutcome.CENSORED,
            prefix_mae_n=prefix,
            closed_bin_count=closed_bin_count,
            reason=reason,
            trainable=False,
            partial_receipt_path=partial_receipt_path,
            home_receipt_path=home_receipt_path,
            trigger_watermark_s=trigger_watermark_s,
            censor_baseline=baseline,
        ))

    def record_ineligible(
        self,
        reason: str,
        *,
        partial_receipt_path: str | None = None,
        home_receipt_path: str | None = None,
        closed_bin_count: int = 0,
        timing_diagnostics: Mapping[str, Any] | None = None,
        diagnostic_mae_n: float | None = None,
    ) -> V4StageAttemptV1:
        if self.in_flight is None:
            raise V4StageError("no V4 attempt is in flight")
        if not reason:
            raise V4StageError("ineligible attempt needs a reason")
        if type(closed_bin_count) is not int or closed_bin_count < 0:
            raise V4StageError("ineligible closed-bin count must be nonnegative")
        return self._finish(V4StageAttemptV1(
            ordinal=self.in_flight.ordinal,
            candidate=self.in_flight.candidate,
            candidate_token=self.in_flight.candidate_token,
            method=self.in_flight.method,
            outcome=AttemptOutcome.INELIGIBLE,
            closed_bin_count=closed_bin_count,
            reason=reason,
            trainable=False,
            partial_receipt_path=partial_receipt_path,
            home_receipt_path=home_receipt_path,
            timing_diagnostics=timing_diagnostics,
            diagnostic_mae_n=diagnostic_mae_n,
        ))

    def record_failure(
        self,
        reason: str,
        *,
        partial_receipt_path: str | None = None,
        home_receipt_path: str | None = None,
    ) -> V4StageAttemptV1:
        """Close a live safety/terminal failure without scheduling a retry."""

        if self.in_flight is None:
            raise V4StageError("no V4 attempt is in flight")
        if not reason:
            raise V4StageError("terminal failure needs a reason")
        self.terminal_failure = str(reason)
        return self._finish(V4StageAttemptV1(
            ordinal=self.in_flight.ordinal,
            candidate=self.in_flight.candidate,
            candidate_token=self.in_flight.candidate_token,
            method=self.in_flight.method,
            outcome=AttemptOutcome.FAILURE,
            reason=str(reason),
            trainable=False,
            partial_receipt_path=partial_receipt_path,
            home_receipt_path=home_receipt_path,
            failure_disposition=V4FailureDisposition.HARD_TERMINAL,
        ))

    def record_recoverable_failure(
        self,
        recovery: V4RecoveryReceiptV1,
        *,
        partial_receipt_path: str | None = None,
        home_receipt_path: str | None = None,
    ) -> V4StageAttemptV1:
        """Close one recoverable attempt without poisoning the stage."""

        if self.in_flight is None:
            raise V4StageError("no V4 attempt is in flight")
        if recovery.disposition is not V4FailureDisposition.RECOVERABLE_RESUME:
            raise V4StageError("recovery receipt is not recoverable")
        if recovery.current_attempt_ordinal != self.in_flight.ordinal:
            raise V4StageError("recovery receipt attempt ordinal differs")
        if recovery.attempt_count != self.attempt_count + 1:
            raise V4StageError("recovery receipt attempt count differs")
        result = self._finish(V4StageAttemptV1(
            ordinal=self.in_flight.ordinal,
            candidate=self.in_flight.candidate,
            candidate_token=self.in_flight.candidate_token,
            method=self.in_flight.method,
            outcome=AttemptOutcome.FAILURE,
            reason=recovery.reason,
            trainable=False,
            partial_receipt_path=partial_receipt_path,
            home_receipt_path=home_receipt_path,
            failure_disposition=V4FailureDisposition.RECOVERABLE_RESUME,
        ))
        self._append_ledger({
            "event": "recovery",
            "receipt": recovery.as_dict(),
            "fingerprint_sha256": self.fingerprint_sha256,
        })
        return result

    def resume_after_recovery(
        self,
        recovery: V4RecoveryReceiptV1,
        *,
        candidate: Mapping[str, Any] | None = None,
    ) -> V4StageAttemptV1:
        """Dispatch exactly the next stage ordinal after a fresh owner epoch."""

        if recovery.disposition is not V4FailureDisposition.RECOVERABLE_RESUME:
            raise V4StageError("hard-terminal recovery cannot dispatch")
        if recovery.status is not V4RecoveryStatus.RESUME_READY:
            raise V4StageError("recovery is not ready for dispatch")
        if not recovery.auto_dispatch_permitted:
            raise V4StageError("recovery receipt does not permit dispatch")
        if self.in_flight is not None:
            raise V4StageError("V4 stage has an in-flight attempt")
        if not self.attempts or self.attempts[-1].ordinal != recovery.current_attempt_ordinal:
            raise V4StageError("recovery receipt is stale or duplicated")
        if self.attempts[-1].failure_disposition is not V4FailureDisposition.RECOVERABLE_RESUME:
            raise V4StageError("last stage attempt is not recoverable")
        if recovery.attempt_count != self.attempt_count:
            raise V4StageError("recovery receipt ledger count is stale")
        expected = self.attempt_count + 1
        if recovery.next_attempt_ordinal != expected:
            raise V4StageError("recovery next ordinal is not append-only")
        return self.ask(candidate=candidate)

    def status(self) -> dict[str, Any]:
        return {
            "schema": V4_TWO_STAGE_SCHEMA,
            "version": V4_TWO_STAGE_VERSION,
            "stage": self.config.stage.value,
            "fingerprint_sha256": self.fingerprint_sha256,
            "attempt_budget": self.config.attempt_budget,
            "attempt_count": self.attempt_count,
            "exact_count": len(self.exact_observations),
            "censored_count": sum(row.outcome is AttemptOutcome.CENSORED for row in self.attempts),
            "ineligible_count": sum(row.outcome is AttemptOutcome.INELIGIBLE for row in self.attempts),
            "confirmed_incumbent": self.confirmed_incumbent,
            "cross_stage_baseline": (
                None if self.cross_stage_baseline is None else self.cross_stage_baseline.as_dict()
            ),
            "censor_incumbent": self.censor_incumbent,
            "best_exact": None if self.best_exact is None else self.best_exact.as_dict(),
            "announced_milestones_n": sorted(self.announced_milestones, reverse=True),
            "complete": self.complete,
            "terminal_failure": self.terminal_failure,
            "recoverable_failure_count": sum(
                row.failure_disposition is V4FailureDisposition.RECOVERABLE_RESUME
                for row in self.attempts
            ),
            "kernel": self.config.kernel,
            "feature_names": list(self.config.feature_names),
            "ff_bundle_identity": self.config.ff_bundle.identity,
        }


def load_two_stage_config(raw: Mapping[str, Any]) -> tuple[V4StageConfigV1, V4StageConfigV1]:
    if not isinstance(raw, Mapping) or raw.get("schema") != V4_TWO_STAGE_SCHEMA or raw.get("version") != V4_TWO_STAGE_VERSION:
        raise V4StageError("two-stage config schema/version differs")
    bundle = CombinedFFBundleV1(**dict(raw.get("ff_bundle", {})))
    stages = raw.get("stages")
    if not isinstance(stages, Mapping):
        raise V4StageError("two-stage config stages are missing")
    configs: list[V4StageConfigV1] = []
    for stage in (V4Stage.FF_IOFF_100, V4Stage.FF_ION_6D_100):
        value = stages.get(stage.value)
        if not isinstance(value, Mapping):
            raise V4StageError(f"missing config for {stage.value}")
        configs.append(V4StageConfigV1(stage=stage, ff_bundle=bundle, **dict(value)))
    return configs[0], configs[1]


def load_three_stage_config(
    raw: Mapping[str, Any],
) -> tuple[V4StageConfigV1, V4StageConfigV1, V4StageConfigV1]:
    """Load Stage A, fixed-limit B1, and limit-aware B2 in order."""

    if not isinstance(raw, Mapping) or raw.get("schema") != V4_TWO_STAGE_SCHEMA or raw.get("version") != V4_TWO_STAGE_VERSION:
        raise V4StageError("three-stage config schema/version differs")
    bundle = CombinedFFBundleV1(**dict(raw.get("ff_bundle", {})))
    stages = raw.get("stages")
    if not isinstance(stages, Mapping):
        raise V4StageError("three-stage config stages are missing")
    configs: list[V4StageConfigV1] = []
    for stage in V4Stage:
        value = stages.get(stage.value)
        if not isinstance(value, Mapping):
            raise V4StageError(f"missing config for {stage.value}")
        configs.append(V4StageConfigV1(stage=stage, ff_bundle=bundle, **dict(value)))
    return configs[0], configs[1], configs[2]


__all__ = [
    "ATTEMPT_BUDGET",
    "AttemptOutcome",
    "CENSOR_KAPPA",
    "CENSOR_MIN_CLOSED_BINS",
    "CombinedFFBundleV1",
    "CROSS_STAGE_BASELINE_SCHEMA",
    "MILESTONE_THRESHOLDS_N",
    "ProposalMethod",
    "RECOVERY_SCHEMA",
    "V4FailureClass",
    "V4FailureDisposition",
    "V4FailureEvidenceV1",
    "V4HomeStatus",
    "V4RecoveryReceiptV1",
    "V4RecoveryStatus",
    "V4Stage",
    "V4StageAttemptV1",
    "V4StageCampaignV1",
    "V4StageConfigV1",
    "V4StageError",
    "V4CrossStageBaselineV1",
    "candidate_features",
    "candidate_key",
    "candidate_token",
    "INTEGRAL_STATE_LIMIT_BOUNDS",
    "INTEGRAL_STATE_HARD_LIMIT",
    "STAGE_B2_FEATURES",
    "load_three_stage_config",
    "load_two_stage_config",
    "validate_candidate",
]
