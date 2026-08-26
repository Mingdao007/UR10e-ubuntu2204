"""Typed, offline-only Step6 Figure-eight campaign primitives.

The module deliberately keeps the Step6 identity separate from Step5d/R013
campaign state.  It reuses only the pure Figure-eight path and exact metric
primitives; no controller, RTDE, bridge, or robot operation is imported.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # Tests and the live-facing script put ``tools`` on sys.path.
    from step5d_autotune_v4_r012.censor import (
        ACTIVE_RUN_KINDS as R012_ACTIVE_RUN_KINDS,
        CensoringError,
        NON_ABORT_RUN_KINDS as R012_NON_ABORT_RUN_KINDS,
        prefix_mean,
    )
    from step5d_autotune_v4_r013.handoff import (
        BLIND_RESET_V0,
        FREEZE_CARRY_V1,
        HandoffPolicy,
        validate_handoff_policy,
    )
    from step5d_autotune_v4_r013.metrics import (
        FIGURE8_METRIC,
        GapPreservingMetricAccumulatorV1,
        MetricFingerprintV1,
        MetricResultV1,
    )
    from step5d_autotune_v4_r013.path_context import (
        FIGURE8_BIN_WIDTH_S,
        FIGURE8_DURATION_S,
        FIGURE8_FORMAL_BIN_COUNT,
        FIGURE8_FORMAL_START_S,
        FIGURE8_FULL_BIN_COUNT,
        FIGURE8_MAX_SPEED_M_S,
        FigureEightPathProviderV1,
        PlanarBasisReceiptV1,
        PathContextSampleV1,
    )
except ModuleNotFoundError:  # pragma: no cover - package import from repository root
    from tools.step5d_autotune_v4_r012.censor import (
        ACTIVE_RUN_KINDS as R012_ACTIVE_RUN_KINDS,
        CensoringError,
        NON_ABORT_RUN_KINDS as R012_NON_ABORT_RUN_KINDS,
        prefix_mean,
    )
    from tools.step5d_autotune_v4_r013.handoff import (
        BLIND_RESET_V0,
        FREEZE_CARRY_V1,
        HandoffPolicy,
        validate_handoff_policy,
    )
    from tools.step5d_autotune_v4_r013.metrics import (
        FIGURE8_METRIC,
        GapPreservingMetricAccumulatorV1,
        MetricFingerprintV1,
        MetricResultV1,
    )
    from tools.step5d_autotune_v4_r013.path_context import (
        FIGURE8_BIN_WIDTH_S,
        FIGURE8_DURATION_S,
        FIGURE8_FORMAL_BIN_COUNT,
        FIGURE8_FORMAL_START_S,
        FIGURE8_FULL_BIN_COUNT,
        FIGURE8_MAX_SPEED_M_S,
        FigureEightPathProviderV1,
        PlanarBasisReceiptV1,
        PathContextSampleV1,
    )


FIGURE8_STAGE_ID = "step6_figure8_autotune_v1"
CAMPAIGN_CONFIG_SCHEMA = "step6.autotune/figure8-direct-campaign-config-v1"
CAMPAIGN_CONFIG_VERSION = 1
FINGERPRINT_SCHEMA = "step6.autotune/figure8-campaign-fingerprint-v1"
FINGERPRINT_VERSION = 1
ADMISSION_SCHEMA = "step6.autotune/figure8-trial-admission-v2"
ADMISSION_VERSION = 2
LAUNCH_RECEIPT_SCHEMA = "step6.autotune/figure8-typed-launch-receipt-v1"
LAUNCH_RECEIPT_VERSION = 1
SOBOL_SCHEMA = "step6.autotune/figure8-persisted-sobol-cursor-v1"
SOBOL_VERSION = 1
CORRECTION_SCHEMA = "step6.autotune/figure8-correction-policy-v1"
CORRECTION_VERSION = 1
CORRECTION_STATE_SCHEMA = "step6.autotune/figure8-correction-state-v1"
CORRECTION_FEATURE_NAMES = (
    "bias",
    "speed",
    "signed_acceleration",
    "signed_curvature",
    "sin_phase",
    "cos_phase",
)
CORRECTION_NORMALIZATION_SCALES = (1.0, 0.01, 0.002, 100.0, 1.0, 1.0)
HANDOFF_POLICIES = (BLIND_RESET_V0, FREEZE_CARRY_V1)
HANDOFF_PENDING = "handoff_pending"
HANDOFF_STATES = (HANDOFF_PENDING, *HANDOFF_POLICIES)
MIN_NOVEL = 80
EXACT_NOVEL_TARGET = 200
MAX_NOVEL = EXACT_NOVEL_TARGET
SOBOL_POOL_SIZE = 128
YVAR_MIN_N2 = 1e-4
YVAR_MAX_N2 = 2e-2
YVAR_NU0 = 2
CORRECTION_BOUNDS_N = (-0.5, 0.5)
CORRECTION_L1_MAX_N = 1.25
CORRECTION_CLIP_N = 1.25
CORRECTION_SLEW_N_S = 0.5
TARGET_FORCE_N = 5.0
FRESH_FRAME_WAIT_POLICY = {
    "schema": "step5d.autotune-v5/fresh-frame-wait-policy-v1",
    "version": 1,
    "wait_s": 0.004,
}
FORMAL_BIN_COUNT = FIGURE8_FORMAL_BIN_COUNT
FULL_BIN_COUNT = FIGURE8_FULL_BIN_COUNT
FORMAL_START_BIN = int(round(FIGURE8_FORMAL_START_S / FIGURE8_BIN_WIDTH_S))
FORMAL_END_BIN = int(round(FIGURE8_DURATION_S / FIGURE8_BIN_WIDTH_S))
CENSOR_GUARD_BINS = 25
CENSOR_KAPPA = 2.0
CENSOR_SCHEMA = "step6.autotune/figure8-censored-receipt-v1"
CENSOR_PROTOCOL_SCHEMA = "step6.autotune/figure8-censor-protocol-v1"
CENSOR_ACTIVE_RUN_KINDS = frozenset({"BO_TRIAL", "NOVEL_BO"}) & R012_ACTIVE_RUN_KINDS
CENSOR_FORBIDDEN_RUN_KINDS = frozenset(
    {"HANDOFF_AB", "SENTINEL", "BOUNDARY_PROBE", "BOUNDARY_CONFIRMATION", "REPEAT", "TOP3_CONFIRMATION"}
) | (R012_NON_ABORT_RUN_KINDS - {"QUALIFICATION"})
CONTACT_SEARCH_BOUNDARY_M = 0.011029311
CONTACT_SEARCH_FAR_SPEED_M_S = 0.005
CONTACT_SEARCH_NEAR_SPEED_M_S = 0.0002
CONTACT_SEARCH_FAR_ACCEL_M_S2 = 0.01
CONTACT_SEARCH_NEAR_ACCEL_M_S2 = 0.005
CONTACT_SEARCH_FUSE_N = 50.0
CONTACT_SEARCH_MAX_TRAVEL_M = 0.025
CONTACT_SEARCH_TIMEOUT_S = 90.0
COMPLETE_CANDIDATE_SCHEMA = "step6.autotune/figure8-complete-candidate-v1"
COMPLETE_CANDIDATE_VERSION = 1
PROPOSAL_RECEIPT_SCHEMA = "step6.autotune/figure8-proposal-receipt-v1"
PROPOSAL_RECEIPT_VERSION = 1
CAMPAIGN_STATE_SCHEMA = "step6.autotune/figure8-campaign-state-v1"
CAMPAIGN_STATE_VERSION = 2
LAUNCH_RECEIPT_V2_SCHEMA = "step6.autotune/figure8-launch-receipt-v2"
LAUNCH_RECEIPT_V2_VERSION = 2
EVIDENCE_REFERENCE_SCHEMA = "step6.autotune/figure8-evidence-reference-v1"
SAFETY_FAULT_SCHEMA = "step6.autotune/figure8-safety-fault-v1"
SENTINEL_PROPOSAL_SCHEMA = "step6.autotune/figure8-convergence-proposal-batch-v1"
SAFETY_FAULT_KINDS = frozenset({
    "protective_stop", "emergency_stop", "safety_fault", "force_hard_fault",
    "torque_hard_fault", "sensor_hard_fault",
})


class FigureEightError(ValueError):
    """A Step6 typed contract or persisted state is invalid."""


@dataclass
class OperationalSegmentBudgetV1:
    """Injectable 320-attempt/10-hour owner-segment boundary."""

    max_attempts: int = 320
    max_duration_s: float = 10.0 * 60.0 * 60.0
    now: Callable[[], float] = time.monotonic
    attempts: int = 0
    started_at_s: float | None = None

    def start(self, now_s: float | None = None) -> None:
        self.attempts = 0
        self.started_at_s = float(self.now() if now_s is None else now_s)

    def record_attempt(self) -> None:
        self.attempts += 1

    def due(self, now_s: float | None = None) -> bool:
        if self.started_at_s is None:
            return False
        current = float(self.now() if now_s is None else now_s)
        return self.attempts >= self.max_attempts or current - self.started_at_s >= self.max_duration_s

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "max_duration_s": self.max_duration_s,
            "attempts": self.attempts,
            "started_at_s": self.started_at_s,
        }


@dataclass(frozen=True)
class SafetyFaultReceiptV1:
    kind: str
    evidence: Mapping[str, Any]
    latched: bool = True
    automatic_retry_allowed: bool = False
    automatic_home_allowed: bool = False
    schema: str = SAFETY_FAULT_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.kind not in SAFETY_FAULT_KINDS or not self.latched:
            raise FigureEightError("Step6 safety fault must be latched and typed")
        if self.automatic_retry_allowed or self.automatic_home_allowed:
            raise FigureEightError("Step6 safety fault permits automatic recovery")
        if not isinstance(self.evidence, Mapping) or not self.evidence:
            raise FigureEightError("Step6 safety fault lacks evidence")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "kind": self.kind,
            "evidence": dict(self.evidence),
            "latched": True,
            "automatic_retry_allowed": False,
            "automatic_home_allowed": False,
        }


class LatchedSafetyFaultError(RuntimeError):
    def __init__(self, receipt: SafetyFaultReceiptV1):
        self.receipt = receipt
        super().__init__(f"latched Step6 safety fault: {receipt.kind}")


class HandoffPolicyEnum(str, Enum):
    HANDOFF_PENDING = HANDOFF_PENDING
    BLIND_RESET_V0 = BLIND_RESET_V0
    FREEZE_CARRY_V1 = FREEZE_CARRY_V1


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise FigureEightError(f"{name} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FigureEightError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise FigureEightError(f"{name} must be finite")
    return parsed


def _sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require_sha(value: Any, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise FigureEightError(f"{name} must be a lowercase SHA-256")
    return value


def _canonical_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _clip(value: float, low: float, high: float) -> tuple[float, bool]:
    clipped = min(high, max(low, value))
    return clipped, not math.isclose(clipped, value, rel_tol=0.0, abs_tol=0.0)


@dataclass(frozen=True)
class CampaignConfigV1:
    """Immutable direct-campaign contract loaded from the Step6 JSON."""

    raw: Mapping[str, Any]
    schema: str = CAMPAIGN_CONFIG_SCHEMA
    version: int = CAMPAIGN_CONFIG_VERSION

    def __post_init__(self) -> None:
        if self.schema != CAMPAIGN_CONFIG_SCHEMA or self.version != CAMPAIGN_CONFIG_VERSION:
            raise FigureEightError("Step6 campaign config schema/version differs")
        if not isinstance(self.raw, Mapping):
            raise FigureEightError("Step6 campaign config must be an object")
        if self.raw.get("immutable") is not True:
            raise FigureEightError("Step6 direct campaign config must be immutable")
        if self.raw.get("launch_ready") is not False:
            raise FigureEightError("Step6 direct campaign config must remain offline-only")
        if self.raw.get("source_mode") != "provisional_cycloid_n3":
            raise FigureEightError("Step6 source mode differs")
        if self.raw.get("cycloid_empirical_floor_available") is not False:
            raise FigureEightError("Step6 cycloid empirical-floor flag must remain false")
        if self.raw.get("correction_seed") != "zero":
            raise FigureEightError("Step6 correction seed must be zero")
        physical_writer = self.raw.get("physical_writer")
        if (
            not isinstance(physical_writer, Mapping)
            or physical_writer.get("fresh_frame_wait_policy") != FRESH_FRAME_WAIT_POLICY
        ):
            raise FigureEightError("Step6 fresh-frame wait policy differs")
        handoff = self.raw.get("handoff")
        if not isinstance(handoff, Mapping) or handoff.get("selected") != HANDOFF_PENDING:
            raise FigureEightError("Step6 direct campaign must remain handoff_pending")
        if handoff.get("trainable") is not False or handoff.get("launchable") is not False:
            raise FigureEightError("Step6 handoff-pending campaign is trainable/launchable")
        metric = MetricFingerprintV1.from_mapping(self.raw["metric_fingerprint"])
        if metric != MetricFingerprintV1.figure8():
            raise FigureEightError("Step6 Figure-eight metric fingerprint differs")
        budget = self.raw.get("budget", {})
        if budget.get("min_novel") != MIN_NOVEL or budget.get("max_novel") != MAX_NOVEL:
            raise FigureEightError("Step6 novel budget differs")
        if (
            budget.get("exact_novel_target") != EXACT_NOVEL_TARGET
            or budget.get("campaign_target_stop") is not False
            or budget.get("warm_start_count") != 24
        ):
            raise FigureEightError("Step6 warm-start/stop policy differs")
        censor = self.raw.get("trial_censor")
        if not isinstance(censor, Mapping):
            raise FigureEightError("Step6 trial-censor contract is missing")
        if (
            censor.get("active") is not True
            or censor.get("denominator_bins") != FORMAL_BIN_COUNT
            or censor.get("guard_bins") != CENSOR_GUARD_BINS
            or censor.get("kappa") != CENSOR_KAPPA
            or censor.get("trigger")
            != "prefix_mean_mae_n > 2.0 * repeat_confirmed_incumbent_mean_n"
            or tuple(censor.get("eligible_run_kinds", ()))
            != tuple(sorted(CENSOR_ACTIVE_RUN_KINDS))
            or set(censor.get("forbidden_run_kinds", ())) != CENSOR_FORBIDDEN_RUN_KINDS
        ):
            raise FigureEightError("Step6 trial-censor policy differs")
        segment = self.raw.get("operational_segment")
        if not isinstance(segment, Mapping) or segment.get("max_attempts") != 320 or segment.get("max_duration_h") != 10:
            raise FigureEightError("Step6 operational segment bounds differ")
        scheduler = self.raw.get("scheduler")
        if (
            not isinstance(scheduler, Mapping)
            or scheduler.get("serial_q") != 1
            or scheduler.get("async_policy")
            != "safe_return_only_proposal_prefetch_v1"
            or scheduler.get("async_max_workers") != 1
            or tuple(scheduler.get("async_allowed_work", ()))
            != (
                "next_candidate_materialization",
                "fresh_128_pool_scoring",
                "optimizer_ledger_persistence",
            )
            or scheduler.get("async_forbidden_before_safe_return") is not True
            or scheduler.get(
                "prefetched_proposal_treats_current_candidate_as_pending"
            )
            is not True
            or scheduler.get("arm_requires_current_home_admission") is not True
        ):
            raise FigureEightError("Step6 scheduler must remain serial and Home-admission bound")
        home_rule = self.raw.get("home_rule")
        if (
            not isinstance(home_rule, Mapping)
            or home_rule.get("calibration_home_z_m") != 0.0345
            or home_rule.get("final_home_offset_m") != 0.013525311
            or home_rule.get("calibration_acquisitions") != 3
            or home_rule.get("final_home_rule") != "max(contact_confirm_z_m)+0.013525311 m"
            or home_rule.get("final_home_status") != "pending_contact_confirmation"
            or home_rule.get("launchable") is not False
            or home_rule.get("contact_confirmation_run_kind") != "NON_BO"
            or home_rule.get("contact_confirmation_acquisitions") != 3
        ):
            raise FigureEightError("Step6 contact/Home rule differs")
        path = self.raw.get("path")
        if not isinstance(path, Mapping) or path.get("theoretical_max_speed_m_s") != FIGURE8_MAX_SPEED_M_S:
            raise FigureEightError("Step6 Figure-eight path speed contract differs")
        contact_search = self.raw.get("contact_search")
        if not isinstance(contact_search, Mapping):
            raise FigureEightError("Step6 Figure-eight contact-search contract is missing")
        expected_contact_search = {
            "boundary_m": CONTACT_SEARCH_BOUNDARY_M,
            "far_speed_m_s": CONTACT_SEARCH_FAR_SPEED_M_S,
            "near_speed_m_s": CONTACT_SEARCH_NEAR_SPEED_M_S,
            "far_acceleration_m_s2": CONTACT_SEARCH_FAR_ACCEL_M_S2,
            "near_acceleration_m_s2": CONTACT_SEARCH_NEAR_ACCEL_M_S2,
            "fuse_n": CONTACT_SEARCH_FUSE_N,
            "max_travel_m": CONTACT_SEARCH_MAX_TRAVEL_M,
            "timeout_s": CONTACT_SEARCH_TIMEOUT_S,
        }
        for key, expected in expected_contact_search.items():
            if contact_search.get(key) != expected:
                raise FigureEightError(f"Step6 contact-search contract differs for {key}")
        if contact_search.get("schema") != "step6.autotune/figure8-contact-search-v4-equivalent-v1":
            raise FigureEightError("Step6 contact-search schema differs")
        correction = self.raw.get("correction", {})
        if tuple(correction.get("feature_names", ())) != CORRECTION_FEATURE_NAMES:
            raise FigureEightError("Step6 correction feature names differ")
        if tuple(correction.get("normalization_scales", ())) != CORRECTION_NORMALIZATION_SCALES:
            raise FigureEightError("Step6 correction normalization scales differ")
        if correction.get("weights_default") != [0.0] * 6:
            raise FigureEightError("Step6 correction default is not zero")
        if correction.get("coefficient_bounds_n") != [-0.5, 0.5]:
            raise FigureEightError("Step6 correction coefficient bounds differ")
        if correction.get("l1_norm_max_n") != CORRECTION_L1_MAX_N:
            raise FigureEightError("Step6 correction L1 bound differs")
        if correction.get("output_clip_n") != CORRECTION_CLIP_N:
            raise FigureEightError("Step6 correction output clip differs")
        if correction.get("slew_limit_n_s") != CORRECTION_SLEW_N_S:
            raise FigureEightError("Step6 correction slew bound differs")

    @property
    def metric(self) -> MetricFingerprintV1:
        return MetricFingerprintV1.from_mapping(self.raw["metric_fingerprint"])

    @property
    def correction(self) -> Mapping[str, Any]:
        return self.raw["correction"]

    @property
    def scheduler(self) -> Mapping[str, Any]:
        return self.raw["scheduler"]

    @property
    def handoff_policy(self) -> str:
        return str(self.raw["handoff"]["selected"])

    @property
    def trainable(self) -> bool:
        return bool(self.raw["handoff"].get("trainable", False))

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, **dict(self.raw)}


def load_campaign_config(path: Path | None = None) -> CampaignConfigV1:
    root = Path(__file__).resolve().parents[2]
    target = path or root / "config" / "step6" / "r013_figure8_direct_campaign_v1.json"
    payload = json.loads(Path(target).read_text(encoding="utf-8"))
    if payload.get("schema") != CAMPAIGN_CONFIG_SCHEMA:
        raise FigureEightError("Step6 campaign config schema differs")
    version = payload.pop("version", None)
    schema = payload.pop("schema", None)
    return CampaignConfigV1(payload, schema=schema, version=version)


def _validate_correction_weights(value: Sequence[Any]) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or len(value) != 6:
        raise FigureEightError("Step6 complete candidate needs six correction weights")
    weights = tuple(_finite(item, "correction weight") for item in value)
    if any(abs(item) > CORRECTION_BOUNDS_N[1] + 1e-12 for item in weights):
        raise FigureEightError("Step6 correction coefficient exceeds +/-0.5 N")
    if math.fsum(abs(item) for item in weights) > CORRECTION_L1_MAX_N + 1e-12:
        raise FigureEightError("Step6 correction coefficient L1 bound exceeded")
    return weights


@dataclass(frozen=True)
class CompleteCandidateV1(Mapping[str, Any]):
    """Executable Figure-eight candidate with both frozen blocks present."""

    controller_path: Mapping[str, Any]
    correction_weights: tuple[float, ...] = (0.0,) * 6
    target_force_n: float = TARGET_FORCE_N
    schema: str = COMPLETE_CANDIDATE_SCHEMA
    version: int = COMPLETE_CANDIDATE_VERSION

    def __post_init__(self) -> None:
        if self.schema != COMPLETE_CANDIDATE_SCHEMA or self.version != COMPLETE_CANDIDATE_VERSION:
            raise FigureEightError("Step6 complete candidate schema/version differs")
        required = (
            "force_p_gain", "force_damping", "force_i_gain", "i_off",
            "normal_filter_tau_s", "orientation_ko", "motion_kp",
        )
        if not isinstance(self.controller_path, Mapping) or any(key not in self.controller_path for key in required):
            raise FigureEightError("Step6 complete candidate controller/path block is incomplete")
        if self.controller_path.get("i_off") is not False:
            raise FigureEightError("Step6 complete candidate requires i_off=false")
        for key in required:
            if key != "i_off":
                _finite(self.controller_path[key], key)
        if any(float(self.controller_path[key]) <= 0.0 for key in required if key != "i_off"):
            raise FigureEightError("Step6 complete candidate controller/path values must be positive")
        if _finite(self.target_force_n, "target_force_n") != TARGET_FORCE_N:
            raise FigureEightError("Step6 complete candidate target must be 5 N")
        weights = _validate_correction_weights(self.correction_weights)
        object.__setattr__(self, "correction_weights", weights)
        object.__setattr__(self, "controller_path", dict(self.controller_path))

    @property
    def correction_block(self) -> dict[str, Any]:
        return {
            "schema": CORRECTION_SCHEMA,
            "version": CORRECTION_VERSION,
            "feature_names": list(CORRECTION_FEATURE_NAMES),
            "normalization_scales": list(CORRECTION_NORMALIZATION_SCALES),
            "weights": list(self.correction_weights),
            "coefficient_bounds_n": list(CORRECTION_BOUNDS_N),
            "l1_norm_max_n": CORRECTION_L1_MAX_N,
            "output_clip_n": CORRECTION_CLIP_N,
            "slew_limit_n_s": CORRECTION_SLEW_N_S,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            **dict(self.controller_path),
            "target_force_n": self.target_force_n,
            "controller_path": dict(self.controller_path),
            "correction": self.correction_block,
            "correction_weights": list(self.correction_weights),
        }

    def as_physical_candidate(self) -> dict[str, Any]:
        return {**dict(self.controller_path), "target_force_n": self.target_force_n,
                "correction_weights": list(self.correction_weights),
                "correction": self.correction_block}

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self):
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())

    @property
    def candidate_key(self) -> str:
        return _canonical_key(self.as_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CompleteCandidateV1":
        if isinstance(value, CompleteCandidateV1):
            return value
        if not isinstance(value, Mapping):
            raise FigureEightError("Step6 candidate must be a typed mapping")
        controller = value.get("controller_path")
        correction = value.get("correction")
        if not isinstance(controller, Mapping) or not isinstance(correction, Mapping):
            raise FigureEightError("Step6 candidate must contain controller/path and correction blocks")
        if (
            correction.get("schema") != CORRECTION_SCHEMA
            or correction.get("version") != CORRECTION_VERSION
            or tuple(correction.get("feature_names", ())) != CORRECTION_FEATURE_NAMES
            or correction.get("coefficient_bounds_n") != list(CORRECTION_BOUNDS_N)
            or correction.get("l1_norm_max_n") != CORRECTION_L1_MAX_N
            or correction.get("output_clip_n") != CORRECTION_CLIP_N
            or correction.get("slew_limit_n_s") != CORRECTION_SLEW_N_S
        ):
            raise FigureEightError("Step6 candidate correction contract differs")
        if tuple(correction.get("normalization_scales", ())) != CORRECTION_NORMALIZATION_SCALES:
            raise FigureEightError("Step6 candidate correction normalization differs")
        weights = correction.get("weights", value.get("correction_weights"))
        return cls(
            controller_path=controller,
            correction_weights=_validate_correction_weights(weights),
            target_force_n=value.get("target_force_n", TARGET_FORCE_N),
            schema=value.get("schema", COMPLETE_CANDIDATE_SCHEMA),
            version=value.get("version", COMPLETE_CANDIDATE_VERSION),
        )


@dataclass(frozen=True)
class FigureEightCampaignFingerprintV1:
    """Complete identity for the isolated Step6 campaign."""

    path_identity_sha256: str
    metric_fingerprint_sha256: str
    handoff_policy: str
    correction_schema: str
    correction_normalization_scales: tuple[float, ...]
    source_sha256: str
    controller_triplet_sha256: Mapping[str, str]
    home_pose: tuple[float, ...]
    home_frame_sha256: str
    eoat_tcp_payload: Mapping[str, Any]
    admission_policy: str
    lineage: str
    home_calibration_receipt_sha256: str | None = None
    schema: str = FINGERPRINT_SCHEMA
    version: int = FINGERPRINT_VERSION

    def __post_init__(self) -> None:
        if self.schema != FINGERPRINT_SCHEMA or self.version != FINGERPRINT_VERSION:
            raise FigureEightError("Step6 fingerprint schema/version differs")
        for value, name in (
            (self.path_identity_sha256, "path identity"),
            (self.metric_fingerprint_sha256, "metric fingerprint"),
            (self.source_sha256, "source"),
            (self.home_frame_sha256, "Home/frame"),
        ):
            _require_sha(value, name)
        if self.handoff_policy not in HANDOFF_STATES:
            raise FigureEightError("Step6 fingerprint handoff policy differs")
        if self.handoff_policy != HANDOFF_PENDING:
            validate_handoff_policy(HandoffPolicy(policy=self.handoff_policy))
        if not self.correction_schema or len(self.correction_normalization_scales) != 6:
            raise FigureEightError("Step6 fingerprint correction binding is incomplete")
        if any(_finite(value, "correction normalization scale") <= 0.0 for value in self.correction_normalization_scales):
            raise FigureEightError("Step6 fingerprint correction scale is invalid")
        if set(self.controller_triplet_sha256) != {"script", "txt", "urp"}:
            raise FigureEightError("Step6 controller triplet binding is incomplete")
        for role, value in self.controller_triplet_sha256.items():
            _require_sha(value, f"controller {role}")
        if len(self.home_pose) != 6 or any(not math.isfinite(float(value)) for value in self.home_pose):
            raise FigureEightError("Step6 Home pose binding is invalid")
        if not isinstance(self.eoat_tcp_payload, Mapping) or not self.eoat_tcp_payload:
            raise FigureEightError("Step6 EOAT/TCP/payload binding is incomplete")
        if not self.admission_policy or not self.lineage:
            raise FigureEightError("Step6 admission/lineage binding is incomplete")
        if self.handoff_policy != HANDOFF_PENDING:
            if self.home_calibration_receipt_sha256 is None:
                raise FigureEightError(
                    "Step6 trainable fingerprint lacks contact-derived Home calibration"
                )
            _require_sha(
                self.home_calibration_receipt_sha256,
                "Home calibration receipt",
            )
        elif self.home_calibration_receipt_sha256 is not None:
            _require_sha(
                self.home_calibration_receipt_sha256,
                "pending Home calibration receipt",
            )

    @property
    def sha256(self) -> str:
        return _sha256(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "path_identity_sha256": self.path_identity_sha256,
            "metric_fingerprint_sha256": self.metric_fingerprint_sha256,
            "handoff_policy": self.handoff_policy,
            "correction_schema": self.correction_schema,
            "correction_normalization_scales": list(self.correction_normalization_scales),
            "source_sha256": self.source_sha256,
            "controller_triplet_sha256": dict(self.controller_triplet_sha256),
            "home_pose": list(self.home_pose),
            "home_frame_sha256": self.home_frame_sha256,
            "eoat_tcp_payload": dict(self.eoat_tcp_payload),
            "admission_policy": self.admission_policy,
            "lineage": self.lineage,
            "home_calibration_receipt_sha256": self.home_calibration_receipt_sha256,
            "trainable": (
                self.handoff_policy != HANDOFF_PENDING
                and self.home_calibration_receipt_sha256 is not None
            ),
        }


def build_campaign_fingerprint(
    *,
    config: CampaignConfigV1,
    source_sha256: str,
    controller_triplet_sha256: Mapping[str, str],
    handoff_policy: str = HANDOFF_PENDING,
    home_materialization: Mapping[str, Any] | None = None,
    _allow_materialized_handoff: bool = False,
) -> FigureEightCampaignFingerprintV1:
    if handoff_policy != HANDOFF_PENDING and not _allow_materialized_handoff:
        raise FigureEightError(
            "Step6 direct campaign handoff remains pending until matched A/B n=5"
        )
    frame = dict(config.raw["home_frame"])
    home_receipt_sha: str | None = None
    if home_materialization is not None:
        if (
            home_materialization.get("schema")
            != "step6.autotune/figure8-home-calibration-receipt-v1"
            or home_materialization.get("passed") is not True
            or home_materialization.get("home_profile_id")
            != "step6.autotune/figure8-contact-derived-home-v1"
            or home_materialization.get("final_home_rule")
            != "max(contact_confirm_z_m)+0.013525311 m"
        ):
            raise FigureEightError("Step6 Home materialization receipt differs")
        home_pose = tuple(
            float(value) for value in home_materialization["final_home_pose"]
        )
        home_receipt_sha = _require_sha(
            home_materialization.get("receipt_sha256"),
            "Home calibration receipt",
        )
        frame.update(
            {
                "home_pose": list(home_pose),
                "no_contact_z_m": home_pose[2],
                "frame_status": "contact_derived_final_home",
                "home_calibration_receipt_sha256": home_receipt_sha,
            }
        )
    else:
        home_pose = tuple(float(value) for value in frame["home_pose"])
    provider = FigureEightPathProviderV1(
        anchor_pose_base=home_pose,
        basis_receipt=PlanarBasisReceiptV1(
            along_base=(*tuple(float(value) for value in frame["along_xy"]), 0.0),
            lateral_base=(*tuple(float(value) for value in frame["lateral_xy"]), 0.0),
        ),
    )
    if handoff_policy != HANDOFF_PENDING and home_receipt_sha is None:
        raise FigureEightError(
            "Step6 materialized handoff requires contact-derived final Home"
        )
    return FigureEightCampaignFingerprintV1(
        path_identity_sha256=provider.identity_receipt.sha256,
        metric_fingerprint_sha256=_sha256(config.metric.as_dict()),
        handoff_policy=handoff_policy,
        correction_schema=str(config.correction["schema"]),
        correction_normalization_scales=tuple(config.correction["normalization_scales"]),
        source_sha256=_require_sha(source_sha256, "source"),
        controller_triplet_sha256=dict(controller_triplet_sha256),
        home_pose=home_pose,
        home_frame_sha256=_sha256(frame),
        eoat_tcp_payload=dict(config.raw["eoat_tcp_payload"]),
        admission_policy=str(config.raw["admission_policy"]["schema"]),
        lineage=str(config.raw["lineage"]),
        home_calibration_receipt_sha256=home_receipt_sha,
    )


def build_frozen_campaign_fingerprint(
    *,
    config: CampaignConfigV1,
    source_sha256: str,
    controller_triplet_sha256: Mapping[str, str],
    matched_ab: Mapping[str, Any],
    home_materialization: Mapping[str, Any],
) -> FigureEightCampaignFingerprintV1:
    """Issue a trainable fingerprint only from a typed matched A/B winner."""

    if not isinstance(matched_ab, Mapping):
        raise FigureEightError("Step6 matched A/B result is not typed evidence")
    policy = str(matched_ab.get("winner_handoff_policy", ""))
    if policy not in HANDOFF_POLICIES:
        raise FigureEightError("Step6 matched A/B winner lacks a handoff policy")
    if matched_ab.get("winner") is not True or int(matched_ab.get("winner_n", 0)) < 5:
        raise FigureEightError("Step6 frozen fingerprint requires the winning A/B arm at n=5")
    if matched_ab.get("same_function_fingerprint") is not True:
        raise FigureEightError("Step6 matched A/B function fingerprint is not qualified")
    arm_fingerprints = matched_ab.get("arm_fingerprints")
    if (
        not isinstance(arm_fingerprints, Mapping)
        or set(arm_fingerprints) != {"A", "B"}
        or any(
            type(value) is not str or len(value) != 64
            for value in arm_fingerprints.values()
        )
        or arm_fingerprints["A"] == arm_fingerprints["B"]
        or matched_ab.get("winner_fingerprint_sha256") not in set(arm_fingerprints.values())
    ):
        raise FigureEightError("Step6 matched A/B artifacts need distinct arm fingerprints")
    return build_campaign_fingerprint(
        config=config,
        source_sha256=source_sha256,
        controller_triplet_sha256=controller_triplet_sha256,
        handoff_policy=policy,
        home_materialization=home_materialization,
        _allow_materialized_handoff=True,
    )


@dataclass(frozen=True)
class CorrectionStateV1:
    fingerprint_sha256: str
    generation: int = 0
    last_path_time_s: float | None = None
    last_output_n: float = 0.0
    schema: str = CORRECTION_STATE_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        _require_sha(self.fingerprint_sha256, "correction fingerprint")
        if type(self.generation) is not int or self.generation < 0:
            raise FigureEightError("correction generation is invalid")
        if self.last_path_time_s is not None and _finite(self.last_path_time_s, "path time") < 0.0:
            raise FigureEightError("correction path time is invalid")
        output = _finite(self.last_output_n, "correction output")
        if abs(output) > CORRECTION_CLIP_N + 1e-12:
            raise FigureEightError("correction output is out of bounds")
        object.__setattr__(self, "last_output_n", output)


@dataclass(frozen=True)
class CorrectionReceiptV1:
    raw_features: tuple[float, ...]
    normalized_features: tuple[float, ...]
    weights: tuple[float, ...]
    unclipped_n: float
    clipped_n: float
    applied_n: float
    effective_target_n: float
    slew_delta_n: float
    clip_applied: bool
    slew_limited: bool
    fingerprint_sha256: str
    schema: str = CORRECTION_SCHEMA
    version: int = CORRECTION_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "feature_names": list(CORRECTION_FEATURE_NAMES),
            "raw_features": list(self.raw_features),
            "normalized_features": list(self.normalized_features),
            "weights": list(self.weights),
            "unclipped_n": self.unclipped_n,
            "clipped_n": self.clipped_n,
            "applied_n": self.applied_n,
            "effective_target_n": self.effective_target_n,
            "slew_delta_n": self.slew_delta_n,
            "clip_applied": self.clip_applied,
            "slew_limited": self.slew_limited,
            "fingerprint_sha256": self.fingerprint_sha256,
        }


@dataclass(frozen=True)
class CorrectionPolicyV1:
    normalization_scales: tuple[float, ...]
    weights: tuple[float, ...] = (0.0,) * 6
    base_target_n: float = TARGET_FORCE_N
    coefficient_bounds_n: tuple[float, float] = CORRECTION_BOUNDS_N
    l1_norm_max_n: float = CORRECTION_L1_MAX_N
    output_clip_n: float = CORRECTION_CLIP_N
    slew_limit_n_s: float = CORRECTION_SLEW_N_S
    schema: str = CORRECTION_SCHEMA
    version: int = CORRECTION_VERSION

    def __post_init__(self) -> None:
        if self.schema != CORRECTION_SCHEMA or self.version != CORRECTION_VERSION:
            raise FigureEightError("correction schema/version differs")
        scales = tuple(_finite(value, "normalization scale") for value in self.normalization_scales)
        weights = tuple(_finite(value, "correction weight") for value in self.weights)
        if len(scales) != 6 or scales != CORRECTION_NORMALIZATION_SCALES or len(weights) != 6 or any(value <= 0.0 for value in scales):
            raise FigureEightError("correction dimensions/scales differ")
        low, high = self.coefficient_bounds_n
        if (low, high) != CORRECTION_BOUNDS_N or any(value < low or value > high for value in weights):
            raise FigureEightError("correction coefficient bounds differ")
        if math.fsum(abs(value) for value in weights) > self.l1_norm_max_n + 1e-12:
            raise FigureEightError("correction coefficient L1 bound exceeded")
        if self.base_target_n != TARGET_FORCE_N or self.output_clip_n != CORRECTION_CLIP_N or self.slew_limit_n_s != CORRECTION_SLEW_N_S:
            raise FigureEightError("correction target/clip/slew contract differs")
        object.__setattr__(self, "normalization_scales", scales)
        object.__setattr__(self, "weights", weights)

    @classmethod
    def zero(cls, scales: Sequence[float]) -> "CorrectionPolicyV1":
        return cls(normalization_scales=tuple(scales))

    def features(
        self, context: PathContextSampleV1
    ) -> tuple[tuple[float, ...], tuple[float, ...]]:
        if not isinstance(context, PathContextSampleV1):
            raise FigureEightError("correction requires typed Figure-eight path context")
        raw = (
            1.0,
            context.scalar_speed_m_s,
            context.signed_tangential_acceleration_m_s2,
            context.signed_planar_curvature_m_inv,
            math.sin(context.phase_rad),
            math.cos(context.phase_rad),
        )
        normalized = tuple(
            _clip(value / scale, -1.0, 1.0)[0]
            for value, scale in zip(raw, self.normalization_scales, strict=True)
        )
        return raw, normalized

    def apply(
        self,
        state: CorrectionStateV1,
        context: PathContextSampleV1,
        *,
        fingerprint_sha256: str,
        allow_equal_endpoint_hold: bool = False,
    ) -> tuple[CorrectionStateV1, CorrectionReceiptV1]:
        if state.fingerprint_sha256 != fingerprint_sha256:
            raise FigureEightError("correction state fingerprint differs")
        if type(allow_equal_endpoint_hold) is not bool:
            raise FigureEightError("equal endpoint hold flag is not boolean")
        raw, normalized = self.features(context)
        if state.last_path_time_s is not None:
            regressed = context.path_time_s < state.last_path_time_s
            equal_without_typed_hold = (
                context.path_time_s == state.last_path_time_s
                and not allow_equal_endpoint_hold
            )
            if regressed or equal_without_typed_hold:
                raise FigureEightError("correction path time is non-monotonic")
        dt = context.path_time_s if state.last_path_time_s is None else context.path_time_s - state.last_path_time_s
        unclipped = math.fsum(weight * feature for weight, feature in zip(self.weights, normalized, strict=True))
        clipped, clip_applied = _clip(unclipped, -self.output_clip_n, self.output_clip_n)
        slew_limit = self.slew_limit_n_s * dt
        delta = clipped - state.last_output_n
        applied_delta = min(slew_limit, max(-slew_limit, delta))
        applied = state.last_output_n + applied_delta
        if abs(applied) > self.output_clip_n + 1e-12 or abs(applied_delta) > slew_limit + 1e-12:
            raise FigureEightError("correction output/slew invariant violated")
        next_state = CorrectionStateV1(
            fingerprint_sha256=fingerprint_sha256,
            generation=state.generation,
            last_path_time_s=context.path_time_s,
            last_output_n=applied,
        )
        return next_state, CorrectionReceiptV1(
            raw_features=raw,
            normalized_features=normalized,
            weights=self.weights,
            unclipped_n=unclipped,
            clipped_n=clipped,
            applied_n=applied,
            effective_target_n=self.base_target_n - applied,
            slew_delta_n=applied_delta,
            clip_applied=clip_applied,
            slew_limited=not math.isclose(applied, clipped, rel_tol=0.0, abs_tol=0.0),
            fingerprint_sha256=fingerprint_sha256,
        )


@dataclass(frozen=True)
class TrialEvidenceV1:
    epoch_id: str
    trial_id: str
    fingerprint_sha256: str
    motion_gate: bool
    timing_gate: bool
    metric_result: MetricResultV1 | None
    raw_samples: tuple[Mapping[str, Any], ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    schema: str = "step6.autotune/figure8-trial-evidence-v1"
    version: int = 1


@dataclass(frozen=True)
class FigureEightCensorDecisionV1:
    """Metric-adapted decision receipt using the mature constant-kappa rule."""

    active: bool
    triggered: bool
    run_kind: str
    closed_bin_count: int
    prefix_mean_n: float | None
    causal_lower_bound_n: float | None
    confirmed_incumbent_mean_n: float | None
    incumbent_threshold_n: float | None
    kappa: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step6.autotune/figure8-censor-decision-v1",
            "active": self.active,
            "triggered": self.triggered,
            "run_kind": self.run_kind,
            "closed_bin_count": self.closed_bin_count,
            "prefix_mean_n": self.prefix_mean_n,
            "causal_lower_bound_n": self.causal_lower_bound_n,
            "confirmed_incumbent_mean_n": self.confirmed_incumbent_mean_n,
            "incumbent_threshold_n": self.incumbent_threshold_n,
            "kappa": self.kappa,
            "reason": self.reason,
        }


def evaluate_figure8_censor_prefix(
    closed_absolute_errors: Sequence[float],
    *,
    run_kind: str,
    confirmed_incumbent_mean_n: float | None,
) -> FigureEightCensorDecisionV1:
    """Evaluate one raw 550-bin Figure-eight prefix without ending a campaign."""

    values: list[float] = []
    for value in closed_absolute_errors:
        error = _finite(value, "closed bin absolute error")
        if error < 0.0:
            raise CensoringError("Figure-eight closed bin absolute errors must be non-negative")
        values.append(error)
    count = len(values)
    prefix = None if count == 0 else prefix_mean(values)
    lower = None if count == 0 else math.fsum(values) / FORMAL_BIN_COUNT
    active = run_kind in CENSOR_ACTIVE_RUN_KINDS and confirmed_incumbent_mean_n is not None
    threshold = None if confirmed_incumbent_mean_n is None else CENSOR_KAPPA * _finite(
        confirmed_incumbent_mean_n, "confirmed incumbent mean"
    )
    if count < CENSOR_GUARD_BINS:
        reason = "guard_not_reached"
    elif run_kind not in CENSOR_ACTIVE_RUN_KINDS:
        reason = "run_kind_excluded"
    elif confirmed_incumbent_mean_n is None:
        reason = "no_confirmed_incumbent"
    else:
        triggered = bool(prefix is not None and prefix > float(threshold))
        return FigureEightCensorDecisionV1(
            True,
            triggered,
            run_kind,
            count,
            prefix,
            lower,
            float(confirmed_incumbent_mean_n),
            float(threshold),
            CENSOR_KAPPA,
            "prefix_mean_exceeded" if triggered else "prefix_mean_within_bound",
        )
    return FigureEightCensorDecisionV1(
        active,
        False,
        run_kind,
        count,
        prefix,
        lower,
        None if confirmed_incumbent_mean_n is None else float(confirmed_incumbent_mean_n),
        threshold,
        CENSOR_KAPPA,
        reason,
    )


def _figure8_raw_prefix_contract(
    raw_samples: Sequence[Mapping[str, Any]],
    *,
    closed_bin_count: int,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    observed: set[int] = set()
    for sample in raw_samples:
        if not isinstance(sample, Mapping):
            continue
        raw_time = sample.get("time_s", sample.get("path_time_s"))
        try:
            time_s = _finite(raw_time, "raw prefix time")
        except FigureEightError:
            continue
        index = int(math.floor((time_s - FIGURE8_FORMAL_START_S + 1e-12) / FIGURE8_BIN_WIDTH_S))
        if 0 <= index < closed_bin_count:
            observed.add(index)
    expected = set(range(closed_bin_count))
    gaps = tuple(
        {
            "bin_index": index,
            "start_s": 5.0 + index * 0.1,
            "end_s": 5.0 + (index + 1) * 0.1,
        }
        for index in sorted(expected - observed)
    )
    coverage = {
        "formal_window_s": [FIGURE8_FORMAL_START_S, FIGURE8_DURATION_S],
        "bin_width_s": FIGURE8_BIN_WIDTH_S,
        "expected_bin_indices": list(range(closed_bin_count)),
        "observed_bin_indices": sorted(observed),
        "observed_bin_count": len(observed),
        "closed_bin_count": closed_bin_count,
    }
    return coverage, gaps


@dataclass(frozen=True)
class FigureEightCensoredReceiptV1:
    """Sealed non-GP receipt for one gracefully ended ordinary BO trial."""

    candidate: Mapping[str, Any]
    candidate_key: str
    campaign_fingerprint_sha256: str
    campaign_id: str
    run_id: str
    attempt_id: str
    trial_id: str
    run_kind: str
    raw_prefix_coverage: Mapping[str, Any]
    raw_gaps: tuple[Mapping[str, Any], ...]
    watermark_s: float
    closed_bin_count: int
    prefix_mean_n: float
    causal_lower_bound_n: float
    incumbent_threshold_n: float
    request_sequence: int
    ack_sequence: int
    attempt_sequence: int = 1
    terminal_reason: str = "normal"
    terminal_reason_code: int = 0
    terminal_reason_subtype: int = 0
    return_guard_value: int = 123
    return_guard_closed: bool = True
    home_closed: bool = True
    safe_return_closed: bool = True
    home_calibrated: bool = True
    home_profile_id: str = "step6.autotune/figure8-contact-derived-home-v1"
    home_calibration_receipt_sha256: str = ""
    home_observation: Mapping[str, Any] = field(default_factory=dict)
    completed: bool = True
    sealed: bool = True
    nontrainable: bool = True
    noncontrol: bool = True
    schema: str = CENSOR_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != CENSOR_SCHEMA or self.version != 1:
            raise FigureEightError("Figure-eight censor receipt schema/version differs")
        if self.run_kind not in CENSOR_ACTIVE_RUN_KINDS or self.run_kind in CENSOR_FORBIDDEN_RUN_KINDS:
            raise FigureEightError("Figure-eight censoring is limited to ordinary novel BO trials")
        if not all(isinstance(value, str) and value for value in (
            self.campaign_id, self.run_id, self.attempt_id, self.trial_id,
        )):
            raise FigureEightError("Figure-eight censor receipt run identity is incomplete")
        _require_sha(self.campaign_fingerprint_sha256, "Figure-eight censor fingerprint")
        if not isinstance(self.candidate, Mapping) or self.candidate_key != _candidate_key(self.candidate):
            raise FigureEightError("Figure-eight censor candidate key differs")
        if not isinstance(self.raw_prefix_coverage, Mapping) or not isinstance(self.raw_gaps, tuple):
            raise FigureEightError("Figure-eight censor raw prefix evidence is not typed")
        if isinstance(self.closed_bin_count, bool) or not CENSOR_GUARD_BINS <= self.closed_bin_count <= FORMAL_BIN_COUNT:
            raise FigureEightError("Figure-eight censor closed-bin count is outside the guard/formal range")
        for value, name in (
            (self.watermark_s, "watermark_s"),
            (self.prefix_mean_n, "prefix_mean_n"),
            (self.causal_lower_bound_n, "causal_lower_bound_n"),
            (self.incumbent_threshold_n, "incumbent_threshold_n"),
        ):
            if _finite(value, name) < 0.0:
                raise FigureEightError(f"Figure-eight censor {name} is negative")
        if not math.isclose(self.causal_lower_bound_n, self.prefix_mean_n * self.closed_bin_count / FORMAL_BIN_COUNT, rel_tol=0.0, abs_tol=1e-12):
            raise FigureEightError("Figure-eight censor lower bound is not the fixed 550-bin causal bound")
        if self.request_sequence <= 0 or self.ack_sequence <= 0 or self.request_sequence != self.ack_sequence:
            raise FigureEightError("Figure-eight censor request/ack sequence is not matched")
        if self.attempt_sequence <= 0 or self.return_guard_value != 123:
            raise FigureEightError("Figure-eight censor attempt/return guard identity is invalid")
        if self.terminal_reason != "normal" or self.terminal_reason_code != 0 or self.terminal_reason_subtype != 0 or not all(value is True for value in (
            self.completed, self.sealed, self.nontrainable, self.noncontrol,
            self.return_guard_closed, self.home_closed, self.safe_return_closed,
            self.home_calibrated,
        )):
            raise FigureEightError("Figure-eight censor closure is not normal and complete")
        if self.home_profile_id != "step6.autotune/figure8-contact-derived-home-v1":
            raise FigureEightError("Figure-eight censor did not close at contact-derived Home")
        _require_sha(
            self.home_calibration_receipt_sha256,
            "Figure-eight censor Home calibration receipt",
        )
        if (
            not isinstance(self.home_observation, Mapping)
            or self.home_observation.get("fresh") is not True
            or self.home_observation.get("safety_normal") is not True
            or self.home_observation.get("stationary") is not True
            or self.home_observation.get("pose_closed") is not True
        ):
            raise FigureEightError("Figure-eight censor lacks a fresh Home observation")

    @property
    def censored(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "candidate": dict(self.candidate),
            "candidate_key": self.candidate_key,
            "campaign_fingerprint_sha256": self.campaign_fingerprint_sha256,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "trial_id": self.trial_id,
            "run_kind": self.run_kind,
            "raw_prefix_coverage": dict(self.raw_prefix_coverage),
            "raw_gaps": [dict(item) for item in self.raw_gaps],
            "watermark_s": self.watermark_s,
            "closed_bin_count": self.closed_bin_count,
            "prefix_mean_n": self.prefix_mean_n,
            "causal_lower_bound_n": self.causal_lower_bound_n,
            "incumbent_threshold_n": self.incumbent_threshold_n,
            "kappa": CENSOR_KAPPA,
            "denominator_bins": FORMAL_BIN_COUNT,
            "guard_bins": CENSOR_GUARD_BINS,
            "request_sequence": self.request_sequence,
            "ack_sequence": self.ack_sequence,
            "attempt_sequence": self.attempt_sequence,
            "terminal_reason": self.terminal_reason,
            "terminal_reason_code": self.terminal_reason_code,
            "terminal_reason_subtype": self.terminal_reason_subtype,
            "return_guard_value": self.return_guard_value,
            "return_guard_closed": True,
            "home_closed": True,
            "safe_return_closed": True,
            "home_calibrated": True,
            "home_profile_id": self.home_profile_id,
            "home_calibration_receipt_sha256": self.home_calibration_receipt_sha256,
            "home_observation": dict(self.home_observation),
            "completed": True,
            "sealed": True,
            "nontrainable": True,
            "noncontrol": True,
            "censored": True,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FigureEightCensoredReceiptV1":
        if not isinstance(value, Mapping) or value.get("schema") != CENSOR_SCHEMA:
            raise FigureEightError("Figure-eight censor receipt schema differs")
        if value.get("kappa") != CENSOR_KAPPA or value.get("denominator_bins") != FORMAL_BIN_COUNT or value.get("guard_bins") != CENSOR_GUARD_BINS or value.get("censored") is not True:
            raise FigureEightError("Figure-eight censor receipt protocol differs")
        return cls(
            candidate=dict(value["candidate"]),
            candidate_key=str(value["candidate_key"]),
            campaign_fingerprint_sha256=str(value["campaign_fingerprint_sha256"]),
            campaign_id=str(value["campaign_id"]),
            run_id=str(value["run_id"]),
            attempt_id=str(value["attempt_id"]),
            trial_id=str(value["trial_id"]),
            run_kind=str(value["run_kind"]),
            raw_prefix_coverage=dict(value["raw_prefix_coverage"]),
            raw_gaps=tuple(dict(item) for item in value["raw_gaps"]),
            watermark_s=float(value["watermark_s"]),
            closed_bin_count=int(value["closed_bin_count"]),
            prefix_mean_n=float(value["prefix_mean_n"]),
            causal_lower_bound_n=float(value["causal_lower_bound_n"]),
            incumbent_threshold_n=float(value["incumbent_threshold_n"]),
            request_sequence=int(value["request_sequence"]),
            ack_sequence=int(value["ack_sequence"]),
            attempt_sequence=int(value.get("attempt_sequence", 1)),
            terminal_reason=str(value["terminal_reason"]),
            terminal_reason_code=int(value.get("terminal_reason_code", 0)),
            terminal_reason_subtype=int(value.get("terminal_reason_subtype", 0)),
            return_guard_value=int(value.get("return_guard_value", 123)),
            return_guard_closed=value.get("return_guard_closed") is True,
            home_closed=value.get("home_closed") is True,
            safe_return_closed=value.get("safe_return_closed") is True,
            home_calibrated=value.get("home_calibrated", True) is True,
            home_profile_id=str(value.get("home_profile_id", "")),
            home_calibration_receipt_sha256=str(
                value.get("home_calibration_receipt_sha256", "")
            ),
            home_observation=dict(value.get("home_observation", {})),
            completed=value.get("completed") is True,
            sealed=value.get("sealed") is True,
            nontrainable=value.get("nontrainable") is True,
            noncontrol=value.get("noncontrol") is True,
            version=int(value.get("version", 1)),
        )


def make_figure8_censored_receipt(
    *,
    candidate: Mapping[str, Any],
    campaign_fingerprint_sha256: str,
    campaign_id: str,
    run_id: str,
    attempt_id: str,
    trial_id: str,
    run_kind: str,
    closed_absolute_errors: Sequence[float],
    raw_samples: Sequence[Mapping[str, Any]],
    watermark_s: float,
    confirmed_incumbent_mean_n: float,
    request_sequence: int,
    ack_sequence: int,
    attempt_sequence: int = 1,
    terminal_reason_code: int = 0,
    terminal_reason_subtype: int = 0,
    return_guard_value: int = 123,
    home_calibrated: bool = True,
    home_profile_id: str = "step6.autotune/figure8-contact-derived-home-v1",
    home_calibration_receipt_sha256: str = "",
    home_observation: Mapping[str, Any] | None = None,
) -> FigureEightCensoredReceiptV1:
    decision = evaluate_figure8_censor_prefix(
        closed_absolute_errors,
        run_kind=run_kind,
        confirmed_incumbent_mean_n=confirmed_incumbent_mean_n,
    )
    if not decision.triggered:
        raise FigureEightError("Figure-eight censor receipt requires a triggered confirmed-incumbent prefix")
    coverage, gaps = _figure8_raw_prefix_contract(
        raw_samples,
        closed_bin_count=decision.closed_bin_count,
    )
    return FigureEightCensoredReceiptV1(
        candidate=dict(candidate),
        candidate_key=_candidate_key(candidate),
        campaign_fingerprint_sha256=campaign_fingerprint_sha256,
        campaign_id=campaign_id,
        run_id=run_id,
        attempt_id=attempt_id,
        trial_id=trial_id,
        run_kind=run_kind,
        raw_prefix_coverage=coverage,
        raw_gaps=gaps,
        watermark_s=watermark_s,
        closed_bin_count=decision.closed_bin_count,
        prefix_mean_n=float(decision.prefix_mean_n),
        causal_lower_bound_n=float(decision.causal_lower_bound_n),
        incumbent_threshold_n=float(decision.incumbent_threshold_n),
        request_sequence=request_sequence,
        ack_sequence=ack_sequence,
        attempt_sequence=attempt_sequence,
        terminal_reason_code=terminal_reason_code,
        terminal_reason_subtype=terminal_reason_subtype,
        return_guard_value=return_guard_value,
        home_calibrated=home_calibrated,
        home_profile_id=home_profile_id,
        home_calibration_receipt_sha256=home_calibration_receipt_sha256,
        home_observation=dict(home_observation or {}),
    )


@dataclass(frozen=True)
class AdmissionReceiptV1:
    trial_id: str
    epoch_id: str
    fingerprint_sha256: str
    trial_admission_passed: bool
    motion_gate: bool
    timing_gate: bool
    sealed: bool
    exact_550_bin_seal: bool
    accepted: bool
    reasons: tuple[str, ...]
    sealed_mae_n: float | None
    raw_gap_count: int
    schema: str = ADMISSION_SCHEMA
    version: int = ADMISSION_VERSION

    @property
    def strict_passed(self) -> bool:
        return self.accepted and self.trial_admission_passed and self.motion_gate and self.timing_gate and self.exact_550_bin_seal

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "trial_id": self.trial_id,
            "epoch_id": self.epoch_id,
            "fingerprint_sha256": self.fingerprint_sha256,
            "trial_admission_passed": self.trial_admission_passed,
            "motion_gate": self.motion_gate,
            "timing_gate": self.timing_gate,
            "sealed": self.sealed,
            "exact_550_bin_seal": self.exact_550_bin_seal,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "sealed_mae_n": self.sealed_mae_n,
            "raw_gap_count": self.raw_gap_count,
        }


class StrictAdmissionV1:
    """Exact per-trial admission before any optimizer ``tell_exact``."""

    def __init__(self, fingerprint: FigureEightCampaignFingerprintV1, metric: MetricFingerprintV1) -> None:
        if metric != MetricFingerprintV1.figure8():
            raise FigureEightError("Step6 admission metric is not Figure-eight 550-bin metric")
        self.fingerprint = fingerprint
        self.metric = metric

    def evaluate(self, trial: TrialEvidenceV1) -> AdmissionReceiptV1:
        reasons: list[str] = []
        trial_fp_ok = trial.fingerprint_sha256 == self.fingerprint.sha256
        if not trial_fp_ok:
            reasons.append("campaign_fingerprint_mismatch")
        if not trial.motion_gate:
            reasons.append("motion_gate_failed")
        if not trial.timing_gate:
            reasons.append("timing_gate_failed")
        metric_result = trial.metric_result
        observed_raw_bins: set[int] = set()
        for sample in trial.raw_samples:
            try:
                time_s = _finite(sample.get("time_s"), "raw sample time")
            except FigureEightError:
                continue
            if self.metric.formal_start_s <= time_s < self.metric.formal_end_s:
                observed_raw_bins.add(int(math.floor((time_s + 1e-12) / self.metric.bin_width_s)))
        raw_gap_count = FORMAL_BIN_COUNT - len(
            {index for index in observed_raw_bins if FORMAL_START_BIN <= index < FORMAL_END_BIN}
        )
        raw_complete = raw_gap_count == 0
        if not raw_complete:
            reasons.append("raw_formal_gap_present")
        sealed = bool(metric_result is not None and metric_result.sealed)
        exact = bool(
            metric_result is not None
            and metric_result.exact
            and metric_result.metric_fingerprint == self.metric
            and metric_result.observed_formal_bin_count == FORMAL_BIN_COUNT
            and metric_result.required_formal_bin_count == FORMAL_BIN_COUNT
            and metric_result.formal_coverage == 1.0
            and raw_complete
        )
        if not sealed:
            reasons.append("metric_not_sealed")
        if not exact:
            reasons.append("exact_550_bin_seal_failed")
        trial_ok = trial_fp_ok and trial.motion_gate and trial.timing_gate and sealed and exact
        if not trial_ok:
            reasons.append("trial_admission_failed")
        accepted = trial_ok
        return AdmissionReceiptV1(
            trial_id=trial.trial_id,
            epoch_id=trial.epoch_id,
            fingerprint_sha256=trial.fingerprint_sha256,
            trial_admission_passed=trial_ok,
            motion_gate=trial.motion_gate,
            timing_gate=trial.timing_gate,
            sealed=sealed,
            exact_550_bin_seal=exact,
            accepted=accepted,
            reasons=tuple(dict.fromkeys(reasons)),
            sealed_mae_n=None if metric_result is None else float(metric_result.formal_mae_n),
            raw_gap_count=raw_gap_count,
        )


@dataclass(frozen=True)
class ObservationRecordV1:
    fingerprint_sha256: str
    candidate_key: str
    mae_n: float


def repeat_aware_yvar(
    observations: Sequence[Mapping[str, Any] | ObservationRecordV1],
    *,
    fallback_n2: float = 0.01,
) -> tuple[dict[str, Any], ...]:
    """Pool same-fingerprint variance, shrink repeats with nu0=2, then divide by n."""

    groups: dict[tuple[str, str], list[float]] = {}
    for row in observations:
        if isinstance(row, ObservationRecordV1):
            fp, key, value = row.fingerprint_sha256, row.candidate_key, row.mae_n
        else:
            fp = str(row["fingerprint_sha256"])
            key = str(row["candidate_key"])
            value = row["mae_n"]
        groups.setdefault((fp, key), []).append(_finite(value, "observation MAE"))
    if not groups:
        return ()
    by_fp: dict[str, list[float]] = {}
    for (fp, _key), values in groups.items():
        by_fp.setdefault(fp, []).extend(values)
    pooled: dict[str, float] = {}
    for fp, values in by_fp.items():
        pooled[fp] = statistics.variance(values) if len(values) >= 2 else _finite(fallback_n2, "fallback variance")
        pooled[fp] = min(YVAR_MAX_N2, max(YVAR_MIN_N2, pooled[fp]))
    result: list[dict[str, Any]] = []
    for (fp, key), values in sorted(groups.items()):
        n = len(values)
        sample = statistics.variance(values) if n >= 2 else pooled[fp]
        shrunk = sample if n == 1 else ((n - 1) * sample + YVAR_NU0 * pooled[fp]) / (n - 1 + YVAR_NU0)
        yvar = min(YVAR_MAX_N2, max(YVAR_MIN_N2, shrunk / n))
        result.append({
            "schema": "step6.autotune/figure8-observation-noise-group-v1",
            "fingerprint_sha256": fp,
            "candidate_key": key,
            "n": n,
            "mean_n": math.fsum(values) / n,
            "sample_variance_n2": None if n == 1 else sample,
            "pooled_same_fingerprint_variance_n2": pooled[fp],
            "shrinkage_nu0": YVAR_NU0,
            "yvar_n2": yvar,
        })
    return tuple(result)


@dataclass(frozen=True)
class ReportEvidenceContractV1:
    """Shared report labels for cycloid and Figure-eight metric windows."""

    metric_fingerprint: Mapping[str, Any]
    single_trial_minimum: float | None
    repeated_incumbent: Mapping[str, Any] | None
    posterior_incumbent: Mapping[str, Any] | None
    raw_gap_policy: str = "preserve_raw_gaps_no_interpolation"
    schema: str = "step5d.autotune-v4/r013-report-evidence-contract-v1"
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "metric_fingerprint": dict(self.metric_fingerprint),
            "single_trial_minimum": self.single_trial_minimum,
            "repeated_incumbent": None if self.repeated_incumbent is None else dict(self.repeated_incumbent),
            "posterior_incumbent": None if self.posterior_incumbent is None else dict(self.posterior_incumbent),
            "raw_gap_policy": self.raw_gap_policy,
        }


def build_report_evidence_contract(
    observations: Sequence[Mapping[str, Any]],
    *,
    metric: MetricFingerprintV1,
    posterior_incumbent: Mapping[str, Any] | None = None,
) -> ReportEvidenceContractV1:
    """Build labels from raw admitted rows without joining their gaps."""

    if not isinstance(metric, MetricFingerprintV1):
        raise FigureEightError("Step6 report metric fingerprint is not typed")
    rows = tuple(observations)
    single = min((float(row["mae_n"]) for row in rows), default=None)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["candidate_key"]), []).append(row)
    repeated = []
    for key, values in grouped.items():
        if len(values) >= 3:
            repeated.append((math.fsum(float(value["mae_n"]) for value in values) / len(values), key, values))
    repeated_row = None
    if repeated:
        mean, key, values = min(repeated, key=lambda item: (item[0], item[1]))
        repeated_row = {"candidate_key": key, "n": len(values), "mean_n": mean}
    return ReportEvidenceContractV1(
        metric_fingerprint=metric.as_dict(),
        single_trial_minimum=single,
        repeated_incumbent=repeated_row,
        posterior_incumbent=posterior_incumbent,
    )


def _candidate_key(candidate: Mapping[str, Any]) -> str:
    return _canonical_key(dict(candidate))


def _base_controller_candidate() -> dict[str, Any]:
    """Return the provisional cycloid n=3 core used only as a warm-start center."""

    return {
        "force_p_gain": 0.019027313840405524,
        "force_damping": 188.36079701683204,
        "force_i_gain": 0.008610779292198037,
        "i_off": False,
        "normal_filter_tau_s": 0.04375,
        "orientation_ko": 0.05,
        "motion_kp": 2.5226892457611436,
        "target_force_n": TARGET_FORCE_N,
    }


def _log_interpolate(low: float, high: float, unit: float) -> float:
    value = _finite(unit, "normalized controller coordinate")
    if not 0.0 <= value <= 1.0:
        raise FigureEightError("Step6 controller coordinate is outside [0,1]")
    return 2.0 ** (math.log2(low) + value * (math.log2(high) - math.log2(low)))


def _controller_candidate_from_unit(
    unit: Sequence[float],
    *,
    probe_bounds_open: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Map the six transformed coordinates into the Figure-eight physical box.

    The three outward limits become available only after their matched n=3
    challenge has passed.  This is intentionally continuous: the historical
    R013 quarter-octave graph is an optimizer policy, not a controller seam.
    """

    if len(unit) != 6:
        raise FigureEightError("Step6 controller point must have six coordinates")
    opened = dict(probe_bounds_open or {})
    ratio = _log_interpolate(1.25e-5, 4.0e-4, unit[0])
    damping = _log_interpolate(7.0, 224.0, unit[1])
    tau = _log_interpolate(
        0.03094 if opened.get("normal_filter_tau_s") else 0.04375,
        0.0735784,
        unit[2],
    )
    ko = _log_interpolate(
        0.03536 if opened.get("orientation_ko") else 0.05,
        0.8,
        unit[3],
    )
    motion_kp = _log_interpolate(1.5, 6.0, unit[4])
    i_over_p = _log_interpolate(
        0.05,
        0.7071 if opened.get("force_i_gain") else 0.5,
        unit[5],
    )
    p_gain = ratio * damping
    return {
        "force_p_gain": p_gain,
        "force_damping": damping,
        "force_i_gain": p_gain * i_over_p,
        "i_off": False,
        "normal_filter_tau_s": tau,
        "orientation_ko": ko,
        "motion_kp": motion_kp,
        "target_force_n": TARGET_FORCE_N,
    }


def _controller_candidate_to_unit(
    candidate: Mapping[str, Any],
    *,
    probe_bounds_open: Mapping[str, bool] | None = None,
) -> tuple[float, ...]:
    opened = dict(probe_bounds_open or {})
    bounds = (
        (1.25e-5, 4.0e-4),
        (7.0, 224.0),
        (0.03094 if opened.get("normal_filter_tau_s") else 0.04375, 0.0735784),
        (0.03536 if opened.get("orientation_ko") else 0.05, 0.8),
        (1.5, 6.0),
        (0.05, 0.7071 if opened.get("force_i_gain") else 0.5),
    )
    values = (
        float(candidate["force_p_gain"]) / float(candidate["force_damping"]),
        float(candidate["force_damping"]),
        float(candidate["normal_filter_tau_s"]),
        float(candidate["orientation_ko"]),
        float(candidate["motion_kp"]),
        float(candidate["force_i_gain"]) / float(candidate["force_p_gain"]),
    )
    result = []
    for value, (low, high) in zip(values, bounds, strict=True):
        normalized = (math.log2(value) - math.log2(low)) / (
            math.log2(high) - math.log2(low)
        )
        result.append(min(1.0, max(0.0, normalized)))
    return tuple(result)


def _complete_candidate(controller: Mapping[str, Any], weights: Sequence[float] = (0.0,) * 6) -> CompleteCandidateV1:
    return CompleteCandidateV1(
        controller_path=dict(controller),
        correction_weights=_validate_correction_weights(weights),
    )


class PersistedSobolCursorV1:
    """Restart-safe Sobol cursor whose pool contract is exactly 128 fresh rows."""

    def __init__(self, state_path: Path, *, seed: int = 6016) -> None:
        self.state_path = Path(state_path)
        self.seed = int(seed)
        self.cursor_index = 0
        self.issued_keys: set[str] = set()
        if self.state_path.is_file():
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if payload.get("schema") != SOBOL_SCHEMA or payload.get("version") != SOBOL_VERSION:
                raise FigureEightError("Step6 Sobol state schema/version differs")
            self.seed = int(payload["seed"])
            self.cursor_index = int(payload["cursor_index"])
            self.issued_keys = {str(value) for value in payload.get("issued_keys", [])}
        if self.cursor_index < 0:
            raise FigureEightError("Step6 Sobol cursor is negative")

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": SOBOL_SCHEMA,
            "version": SOBOL_VERSION,
            "seed": self.seed,
            "cursor_index": self.cursor_index,
            "issued_keys": sorted(self.issued_keys),
        }
        self.state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def _unit(index: int, seed: int) -> tuple[float, ...]:
        try:
            from scipy.stats import qmc
        except ImportError as exc:  # pragma: no cover - project optimizer environment provides scipy
            raise FigureEightError("Step6 Sobol cursor requires scipy.stats.qmc") from exc
        engine = qmc.Sobol(d=6, scramble=True, seed=seed)
        # scipy 1.15's Sobol implementation rejects ``fast_forward(0)`` even
        # though zero is the valid genesis cursor.  Skipping that no-op keeps
        # the persisted sequence identical across the control/optimizer envs.
        if index:
            engine.fast_forward(index)
        return tuple(float(value) for value in engine.random(1)[0])

    def _candidate(
        self,
        unit: Sequence[float],
        domain: str,
        *,
        fixed_other_block: CompleteCandidateV1 | None = None,
        probe_bounds_open: Mapping[str, bool] | None = None,
        local: bool = False,
    ) -> dict[str, Any]:
        if domain == "controller_path":
            mapped = tuple(float(value) for value in unit)
            if local:
                if fixed_other_block is None:
                    raise FigureEightError("Step6 local controller pool lacks incumbent")
                center = _controller_candidate_to_unit(
                    fixed_other_block.controller_path,
                    probe_bounds_open=probe_bounds_open,
                )
                mapped = tuple(
                    min(1.0, max(0.0, center[index] + (mapped[index] - 0.5) * 0.25))
                    for index in range(6)
                )
            weights = (
                fixed_other_block.correction_weights
                if fixed_other_block is not None
                else (0.0,) * 6
            )
            return _complete_candidate(
                _controller_candidate_from_unit(
                    mapped, probe_bounds_open=probe_bounds_open
                ),
                weights,
            ).as_dict()
        if domain == "correction":
            centered = [2.0 * float(value) - 1.0 for value in unit]
            if local:
                if fixed_other_block is None:
                    raise FigureEightError("Step6 local correction pool lacks incumbent")
                weights = [
                    fixed_other_block.correction_weights[index] + centered[index] * 0.25
                    for index in range(6)
                ]
            else:
                norm = math.fsum(abs(value) for value in centered)
                scale = 0.0 if norm == 0.0 else CORRECTION_L1_MAX_N / norm
                weights = [value * scale for value in centered]
            weights = [
                max(CORRECTION_BOUNDS_N[0], min(CORRECTION_BOUNDS_N[1], value))
                for value in weights
            ]
            clipped_l1 = math.fsum(abs(value) for value in weights)
            if clipped_l1 > CORRECTION_L1_MAX_N:
                weights = [value * CORRECTION_L1_MAX_N / clipped_l1 for value in weights]
            weights = [round(value, 12) for value in weights]
            rounded_l1 = math.fsum(abs(value) for value in weights)
            if rounded_l1 > CORRECTION_L1_MAX_N:
                adjustment = rounded_l1 - CORRECTION_L1_MAX_N + 1e-12
                index = max(range(len(weights)), key=lambda item: abs(weights[item]))
                weights[index] -= math.copysign(adjustment, weights[index])
            controller = (
                fixed_other_block.controller_path
                if fixed_other_block is not None
                else _base_controller_candidate()
            )
            return _complete_candidate(controller, weights).as_dict()
        raise FigureEightError(f"unknown Step6 Sobol domain: {domain}")

    def fresh_pool(
        self,
        *,
        evaluated_keys: Iterable[str] = (),
        pending_keys: Iterable[str] = (),
        domain: str = "controller_path",
        count: int = SOBOL_POOL_SIZE,
        fixed_other_block: CompleteCandidateV1 | None = None,
        probe_bounds_open: Mapping[str, bool] | None = None,
        local: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        if count != SOBOL_POOL_SIZE:
            raise FigureEightError("Step6 Sobol ask must request exactly 128 candidates")
        excluded = set(evaluated_keys) | set(pending_keys) | self.issued_keys
        selected: list[dict[str, Any]] = []
        while len(selected) < SOBOL_POOL_SIZE:
            unit = self._unit(self.cursor_index, self.seed)
            self.cursor_index += 1
            candidate = self._candidate(
                unit,
                domain,
                fixed_other_block=fixed_other_block,
                probe_bounds_open=probe_bounds_open,
                local=local,
            )
            key = _candidate_key(candidate)
            if key in excluded:
                continue
            self.issued_keys.add(key)
            excluded.add(key)
            selected.append(candidate)
        self._save()
        return tuple(selected)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": SOBOL_SCHEMA,
            "version": SOBOL_VERSION,
            "seed": self.seed,
            "cursor_index": self.cursor_index,
            "issued_count": len(self.issued_keys),
            "issued_keys": sorted(self.issued_keys),
        }


@dataclass(frozen=True)
class ProposalReceiptV1:
    """Evidence that one 128-point pool was selected by the named method."""

    acquisition: str
    block: str
    pool_size: int
    fresh_candidate_count: int
    selected_candidate_key: str
    pool_sha256: str
    production_provider: str
    fit_receipt: Mapping[str, Any] = field(default_factory=dict)
    async_prefetched: bool = False
    pending_candidate_keys: tuple[str, ...] = ()
    overlap_phase: str | None = None
    schema: str = PROPOSAL_RECEIPT_SCHEMA
    version: int = PROPOSAL_RECEIPT_VERSION

    def __post_init__(self) -> None:
        if self.schema != PROPOSAL_RECEIPT_SCHEMA or self.version != PROPOSAL_RECEIPT_VERSION:
            raise FigureEightError("Step6 proposal receipt schema/version differs")
        if self.acquisition not in {"designed_probe", "sobol", "forced_global_sobol", "qlognei", "qlognei_local"}:
            raise FigureEightError("Step6 proposal acquisition is unknown")
        if self.acquisition == "designed_probe":
            if self.pool_size != 0 or self.fresh_candidate_count != 0:
                raise FigureEightError("Step6 designed probe cannot claim a Sobol pool")
        elif self.pool_size != SOBOL_POOL_SIZE or self.fresh_candidate_count != SOBOL_POOL_SIZE:
            raise FigureEightError("Step6 proposal receipt pool is not exactly 128 fresh candidates")
        if self.acquisition in {"qlognei", "qlognei_local"} and not self.production_provider:
            raise FigureEightError("Step6 qLogNEI receipt lacks production provider")
        pending = tuple(str(value) for value in self.pending_candidate_keys)
        if self.async_prefetched:
            if self.overlap_phase != "safe_return" or len(pending) != 1:
                raise FigureEightError(
                    "Step6 async proposal is not bound to one safe-return pending candidate"
                )
            if not pending[0] or pending[0] == self.selected_candidate_key:
                raise FigureEightError(
                    "Step6 async pending candidate identity is invalid"
                )
        elif pending or self.overlap_phase is not None:
            raise FigureEightError("Step6 synchronous proposal claims async evidence")
        object.__setattr__(self, "pending_candidate_keys", pending)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "acquisition": self.acquisition,
            "block": self.block,
            "pool_size": self.pool_size,
            "fresh_candidate_count": self.fresh_candidate_count,
            "selected_candidate_key": self.selected_candidate_key,
            "pool_sha256": self.pool_sha256,
            "production_provider": self.production_provider,
            "fit_receipt": dict(self.fit_receipt),
            "async_prefetched": self.async_prefetched,
            "pending_candidate_keys": list(self.pending_candidate_keys),
            "overlap_phase": self.overlap_phase,
        }


class ProductionProposalUnavailable(FigureEightError):
    """The mature R013 GP/proposal path is unavailable; no Sobol fallback is allowed."""


class R013ProductionProposalProviderV1:
    """Adapter over the mature R013 production GP and qLogNEI functions."""

    def __init__(self, delegate: Callable[..., Any] | None = None) -> None:
        self.delegate = delegate
        self.invocations: list[dict[str, Any]] = []

    @staticmethod
    def _correction_groups(observations: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
        try:
            from step5d_autotune_v4_r013.identity import CampaignFingerprint
        except ModuleNotFoundError:  # pragma: no cover - repository-root import
            from tools.step5d_autotune_v4_r013.identity import CampaignFingerprint
        fingerprint = CampaignFingerprint.legacy_default(
            handoff_policy=HANDOFF_PENDING,
            runtime_strategy_identity="step6_figure8_correction_pending",
        )
        grouped: dict[tuple[float, ...], list[float]] = {}
        for observation in observations:
            candidate = CompleteCandidateV1.from_mapping(observation["candidate"])
            grouped.setdefault(candidate.correction_weights, []).append(float(observation["mae_n"]))
        return tuple(
            {
                "schema": "step5d.autotune-v4/r013-correction-observation-group-v1",
                "version": 1,
                "campaign_fingerprint": fingerprint.as_dict(),
                "weights": list(weights),
                "n": len(values),
                "mean_n": math.fsum(values) / len(values),
                "yvar_n2": repeat_aware_yvar([
                    {"fingerprint_sha256": fingerprint.sha256, "candidate_key": _canonical_key(list(weights)), "mae_n": value}
                    for value in values
                ])[0]["yvar_n2"],
            }
            for weights, values in grouped.items()
        )

    @staticmethod
    def _exact_rows(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        try:
            from step5d_autotune_v4_r013.domain import physical_candidate_key
        except ModuleNotFoundError:  # pragma: no cover - repository-root import
            from tools.step5d_autotune_v4_r013.domain import physical_candidate_key
        rows: list[dict[str, Any]] = []
        for observation in observations:
            candidate = CompleteCandidateV1.from_mapping(observation["candidate"]).as_physical_candidate()
            # The three fixed outward challenges are evidence for the bound
            # decision, not members of the mature R013 executable GP domain.
            # Keep them in the Step6 ledger while excluding only those rows
            # from the mature fit.
            try:
                physical_candidate_key(candidate)
            except Exception:
                continue
            rows.append({
                "schema": "step5d.autotune-v4/r013-exact-observation-v1",
                "completed": True,
                "sealed": True,
                "full_observation": True,
                "eligible": True,
                "censored": False,
                "candidate": candidate,
                "objective_n": float(observation["mae_n"]),
                "observation_variance_n2": float(observation.get("yvar_n2", 0.01)),
                "campaign_fingerprint": observation.get("fingerprint"),
                "anti_windup": {
                    "schema": "step5d.autotune-v4/r013-trial-anti-windup-v1",
                    "policy": "conditional-double-clamp-v1",
                    "max_abs_integral_n_s": 0.0,
                    "max_abs_i_term": 0.0,
                    "invariant_violation_count": 0,
                    "path_gain_hot_switch": False,
                },
            })
        return rows

    def __call__(
        self,
        pool: Sequence[Mapping[str, Any]],
        *,
        observations: Sequence[Mapping[str, Any]],
        block: str,
        evaluated_keys: Iterable[str] = (),
        pending_keys: Iterable[str] = (),
        pending_candidates: Sequence[Mapping[str, Any]] = (),
    ) -> Mapping[str, Any]:
        if len(pool) != SOBOL_POOL_SIZE:
            raise ProductionProposalUnavailable("R013 production proposal requires exactly 128 candidates")
        parsed_pool = tuple(CompleteCandidateV1.from_mapping(item) for item in pool)
        keys = tuple(item.candidate_key for item in parsed_pool)
        evaluated_key_set = {str(item) for item in evaluated_keys}
        pending_key_set = {str(item) for item in pending_keys}
        if len(set(keys)) != SOBOL_POOL_SIZE or set(keys) & (
            evaluated_key_set | pending_key_set
        ):
            raise ProductionProposalUnavailable("R013 production proposal pool is not fresh")
        parsed_pending = tuple(
            CompleteCandidateV1.from_mapping(item) for item in pending_candidates
        )
        if {item.candidate_key for item in parsed_pending} != pending_key_set:
            raise ProductionProposalUnavailable(
                "R013 pending candidates do not match pending keys"
            )
        invocation = {"block": block, "pool_size": len(pool), "fresh_candidate_count": len(pool)}
        self.invocations.append(invocation)
        if self.delegate is not None:
            result = self.delegate(
                pool,
                observations=observations,
                block=block,
                evaluated_keys=tuple(sorted(evaluated_key_set)),
                pending_keys=tuple(sorted(pending_key_set)),
                pending_candidates=tuple(item.as_dict() for item in parsed_pending),
            )
            if not isinstance(result, Mapping):
                result = {
                    "candidate": getattr(result, "candidate", None),
                    "acquisition_value": getattr(result, "acquisition_value", None),
                    "posterior_beating_probability": getattr(result, "posterior_beating_probability", 0.0),
                    "fit_receipt": getattr(result, "fit_receipt", {}),
                    "acquisition": "qLogNEI",
                    "production_provider": type(self.delegate).__name__,
                }
            return dict(result)
        try:
            from step5d_autotune_v4_r013.gp import (
                ProductionGPConfig, ask_qlognei, fit_production_gp,
            )
        except (ImportError, ModuleNotFoundError) as exc:
            raise ProductionProposalUnavailable("R013 production GP modules are unavailable") from exc
        if not observations:
            raise ProductionProposalUnavailable("R013 production GP requires admitted observations")
        snapshot = {
            "schema": "step5d.autotune-v4/r013-optimizer-snapshot-v1",
            "noise_floor_n2": 0.01,
        }
        snapshot_sha = _sha256(snapshot)
        try:
            if block == "correction":
                from step5d_autotune_v4_r013.gp import (
                    CorrectionGPConfigV1,
                    ask_correction_qlognei,
                    fit_correction_production_gp,
                )
                groups = self._correction_groups(observations)
                if not groups:
                    raise ProductionProposalUnavailable("R013 correction GP has no grouped observations")
                fit = fit_correction_production_gp(
                    groups,
                    robust_incumbent_n=min(float(row["mae_n"]) for row in observations),
                    config=CorrectionGPConfigV1(),
                )
                proposal = ask_correction_qlognei(
                    fit,
                    [CompleteCandidateV1.from_mapping(item).correction_weights for item in parsed_pool],
                    evaluated_weights=(),
                    pending_weights=(),
                )
                selected = next(
                    item for item in parsed_pool if item.correction_weights == tuple(proposal.weights)
                )
                return {
                    "candidate": selected.as_dict(),
                    "acquisition_value": proposal.acquisition_value,
                    "posterior_beating_probability": proposal.posterior_probability_of_improvement,
                    "fit_receipt": dict(proposal.fit_receipt),
                    "acquisition": "qLogNEI",
                    "production_provider": "step5d_autotune_v4_r013.gp.ask_correction_qlognei",
                }
            exact_rows = self._exact_rows(observations)
            if not exact_rows:
                raise ProductionProposalUnavailable("R013 production GP has no mature-domain exact rows")
            fit = fit_production_gp(
                exact_rows,
                config=ProductionGPConfig(noise_floor_n2=0.01, noise_snapshot_sha256=snapshot_sha),
            )
            proposal = ask_qlognei(
                fit,
                [item.as_physical_candidate() for item in parsed_pool],
                evaluated_keys=(),
                pending_keys=(),
            )
        except Exception as exc:
            raise ProductionProposalUnavailable(
                f"R013 production GP/qLogNEI unavailable: {type(exc).__name__}: {exc}"
            ) from exc
        return {
            "candidate": _complete_candidate(proposal.candidate, parsed_pool[0].correction_weights).as_dict(),
            "acquisition_value": proposal.acquisition_value,
            "posterior_beating_probability": 0.0,
            "fit_receipt": dict(getattr(proposal, "as_dict", lambda: {})()),
            "acquisition": "qLogNEI",
            "production_provider": "step5d_autotune_v4_r013.gp.ask_qlognei",
        }
@dataclass(frozen=True)
class TrialPlanV1:
    novel_ordinal: int
    phase: str
    acquisition: str
    block: str
    candidate: Mapping[str, Any]
    kind: str = "novel"
    repeat_of: str | None = None
    probe_axis: str | None = None
    proposal_receipt: Mapping[str, Any] | None = None

    @property
    def candidate_key(self) -> str:
        return _candidate_key(self.candidate)


def _probe_anchor() -> dict[str, Any]:
    anchor = _base_controller_candidate()
    anchor["normal_filter_tau_s"] = 0.04375
    anchor["orientation_ko"] = 0.05
    anchor["force_i_gain"] = anchor["force_p_gain"] * 0.5
    return anchor


def _outward_probe(ordinal: int) -> tuple[str, CompleteCandidateV1]:
    anchor = _probe_anchor()
    axis_values = (
        ("normal_filter_tau_s", 0.03094),
        ("orientation_ko", 0.03536),
        ("force_i_gain", anchor["force_p_gain"] * 0.7071),
    )
    axis, value = axis_values[ordinal - 1]
    candidate = dict(anchor)
    candidate[axis] = value
    return axis, _complete_candidate(candidate)


class FigureEightSchedulerV1:
    """Serial q=1 scheduler backed by persisted state and real proposal seams."""

    def __init__(
        self,
        cursor: PersistedSobolCursorV1,
        *,
        state_path: Path | None = None,
        proposal_provider: Callable[..., Any] | None = None,
        campaign_fingerprint_sha256: str | None = None,
        fixed_sentinel_candidate: Mapping[str, Any] | None = None,
    ) -> None:
        self.cursor = cursor
        self.state_path = None if state_path is None else Path(state_path)
        self.proposal_provider = proposal_provider
        if campaign_fingerprint_sha256 is not None:
            _require_sha(campaign_fingerprint_sha256, "Step6 scheduler fingerprint")
        self.campaign_fingerprint_sha256 = campaign_fingerprint_sha256
        self.fixed_sentinel_candidate = (
            None
            if fixed_sentinel_candidate is None
            else CompleteCandidateV1.from_mapping(fixed_sentinel_candidate).as_dict()
        )
        self.novel_count = 0
        self.novel_dispatch_count = 0
        self.in_flight: TrialPlanV1 | None = None
        self.prefetched_plan: TrialPlanV1 | None = None
        self.repeat_queue: list[TrialPlanV1] = []
        self.completed: list[dict[str, Any]] = []
        self.accepted_observations: list[dict[str, Any]] = []
        self.observation_groups: dict[str, dict[str, Any]] = {}
        self.sentinels: list[dict[str, Any]] = []
        self.drift_state: dict[str, Any] = {}
        self.top_candidates: list[dict[str, Any]] = []
        self.convergence_checks: list[dict[str, Any]] = []
        self._consecutive_convergence_checks = 0
        self.failure_signatures: list[str] = []
        self.consecutive_failure_signature: str | None = None
        self.consecutive_failure_count = 0
        self.frozen_controller_path: dict[str, Any] | None = None
        self.polish_incumbent: dict[str, Any] | None = None
        self.probe_results: dict[str, list[dict[str, Any]]] = {"normal_filter_tau_s": [], "orientation_ko": [], "force_i_gain": []}
        self.probe_bounds_open: dict[str, bool] = {key: False for key in self.probe_results}
        if self.state_path is not None and self.state_path.is_file():
            self._restore(json.loads(self.state_path.read_text(encoding="utf-8")))

    @staticmethod
    def phase_for_novel(ordinal: int) -> tuple[str, str, str]:
        if 1 <= ordinal <= 24:
            return "novel_warm_start", "designed_probe" if ordinal <= 3 else "sobol", "controller_path"
        if 25 <= ordinal <= 100:
            return "controller_path_bo", "global_sobol" if ordinal % 5 == 0 else "qlognei", "controller_path"
        if 101 <= ordinal <= 112:
            return "correction_sobol", "sobol", "correction"
        if 113 <= ordinal <= 160:
            return "correction_bo", "qlognei", "correction"
        if 161 <= ordinal <= EXACT_NOVEL_TARGET:
            return "alternating_local", "qlognei_local", "controller_path" if ordinal % 2 else "correction"
        raise FigureEightError("Step6 novel ordinal is outside [1,200]")

    def _persist(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.snapshot(), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")

    @staticmethod
    def _plan_from_mapping(value: Mapping[str, Any]) -> TrialPlanV1:
        return TrialPlanV1(
            novel_ordinal=int(value["novel_ordinal"]),
            phase=str(value["phase"]),
            acquisition=str(value["acquisition"]),
            block=str(value["block"]),
            candidate=CompleteCandidateV1.from_mapping(value["candidate"]).as_dict(),
            kind=str(value.get("kind", "novel")),
            repeat_of=value.get("repeat_of"),
            probe_axis=value.get("probe_axis"),
            proposal_receipt=value.get("proposal_receipt"),
        )

    @staticmethod
    def _plan_dict(plan: TrialPlanV1 | None) -> dict[str, Any] | None:
        if plan is None:
            return None
        return {
            "novel_ordinal": plan.novel_ordinal,
            "phase": plan.phase,
            "acquisition": plan.acquisition,
            "block": plan.block,
            "candidate": CompleteCandidateV1.from_mapping(plan.candidate).as_dict(),
            "kind": plan.kind,
            "repeat_of": plan.repeat_of,
            "probe_axis": plan.probe_axis,
            "proposal_receipt": None if plan.proposal_receipt is None else dict(plan.proposal_receipt),
        }

    def _restore(self, value: Mapping[str, Any]) -> None:
        if value.get("schema") != CAMPAIGN_STATE_SCHEMA or value.get("version") != CAMPAIGN_STATE_VERSION:
            raise FigureEightError("Step6 campaign state schema/version differs")
        self.novel_count = int(value.get("novel_count", 0))
        self.novel_dispatch_count = int(
            value.get(
                "novel_dispatch_count",
                max(
                    (
                        int(item["novel_dispatch_count"])
                        for item in value.get("completed", ())
                        if item.get("novel_dispatch_count") is not None
                    ),
                    default=self.novel_count,
                ),
            )
        )
        if self.novel_dispatch_count < self.novel_count:
            raise FigureEightError("Step6 novel dispatch count cannot trail exact novel count")
        persisted_fingerprint = value.get("campaign_fingerprint_sha256")
        if persisted_fingerprint is not None:
            _require_sha(persisted_fingerprint, "Step6 persisted scheduler fingerprint")
        if (
            self.campaign_fingerprint_sha256 is not None
            and persisted_fingerprint is not None
            and self.campaign_fingerprint_sha256 != persisted_fingerprint
        ):
            raise FigureEightError("Step6 scheduler fingerprint changed across resume")
        self.campaign_fingerprint_sha256 = (
            self.campaign_fingerprint_sha256 or persisted_fingerprint
        )
        persisted_sentinel = value.get("fixed_sentinel_candidate")
        if persisted_sentinel is not None:
            parsed_sentinel = CompleteCandidateV1.from_mapping(persisted_sentinel).as_dict()
            if (
                self.fixed_sentinel_candidate is not None
                and _candidate_key(self.fixed_sentinel_candidate)
                != _candidate_key(parsed_sentinel)
            ):
                raise FigureEightError("Step6 fixed sentinel changed across resume")
            self.fixed_sentinel_candidate = parsed_sentinel
        self.in_flight = self._plan_from_mapping(value["in_flight"]) if value.get("in_flight") else None
        self.prefetched_plan = (
            self._plan_from_mapping(value["prefetched_plan"])
            if value.get("prefetched_plan")
            else None
        )
        self.repeat_queue = [self._plan_from_mapping(item) for item in value.get("repeat_queue", [])]
        self.completed = [dict(item) for item in value.get("completed", [])]
        self.accepted_observations = [dict(item) for item in value.get("accepted_observations", [])]
        self.observation_groups = {str(key): dict(item) for key, item in value.get("observation_groups", {}).items()}
        self.sentinels = [dict(item) for item in value.get("sentinels", [])]
        self.drift_state = dict(value.get("drift_state", {}))
        self.top_candidates = [dict(item) for item in value.get("top_candidates", [])]
        self.convergence_checks = [dict(item) for item in value.get("convergence_checks", [])]
        self._consecutive_convergence_checks = int(
            value.get("consecutive_convergence_checks", len(self.convergence_checks))
        )
        if self._consecutive_convergence_checks < 0:
            raise FigureEightError("Step6 convergence window is negative")
        self.failure_signatures = [str(item) for item in value.get("failure_signatures", [])]
        self.consecutive_failure_signature = value.get("consecutive_failure_signature")
        self.consecutive_failure_count = int(value.get("consecutive_failure_count", 0))
        if self.consecutive_failure_count < 0:
            raise FigureEightError("Step6 consecutive failure count is negative")
        frozen = value.get("frozen_controller_path")
        self.frozen_controller_path = None if frozen is None else dict(frozen)
        polish = value.get("polish_incumbent")
        self.polish_incumbent = (
            None if polish is None else CompleteCandidateV1.from_mapping(polish).as_dict()
        )
        self.probe_results = {key: [dict(item) for item in rows] for key, rows in value.get("probe_results", self.probe_results).items()}
        self.probe_bounds_open = {key: bool(item) for key, item in value.get("probe_bounds_open", self.probe_bounds_open).items()}
        cursor_state = value.get("sobol", {})
        if cursor_state.get("cursor_index") is not None and int(cursor_state["cursor_index"]) != self.cursor.cursor_index:
            raise FigureEightError("Step6 campaign and Sobol cursor state differ")

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": CAMPAIGN_STATE_SCHEMA,
            "version": CAMPAIGN_STATE_VERSION,
            "campaign_fingerprint_sha256": self.campaign_fingerprint_sha256,
            "fixed_sentinel_candidate": self.fixed_sentinel_candidate,
            "novel_count": self.novel_count,
            "exact_novel_count": self.novel_count,
            "novel_dispatch_count": self.novel_dispatch_count,
            "phase": None if self.in_flight is None else self.in_flight.phase,
            "in_flight": self._plan_dict(self.in_flight),
            "prefetched_plan": self._plan_dict(self.prefetched_plan),
            "repeat_queue": [self._plan_dict(item) for item in self.repeat_queue],
            "completed": list(self.completed),
            "accepted_observations": list(self.accepted_observations),
            "observation_groups": dict(self.observation_groups),
            "sentinels": list(self.sentinels),
            "drift_state": dict(self.drift_state),
            "top_candidates": list(self.top_candidates),
            "convergence_checks": list(self.convergence_checks),
            "consecutive_convergence_checks": self._consecutive_convergence_checks,
            "failure_signatures": list(self.failure_signatures),
            "consecutive_failure_signature": self.consecutive_failure_signature,
            "consecutive_failure_count": self.consecutive_failure_count,
            "frozen_controller_path": self.frozen_controller_path,
            "polish_incumbent": self.polish_incumbent,
            "probe_results": dict(self.probe_results),
            "probe_bounds_open": dict(self.probe_bounds_open),
            "sobol": self.cursor.snapshot(),
        }

    @property
    def exact_novel_count(self) -> int:
        """Strict exact novel observations only; censor/repeat rows are excluded."""

        return self.novel_count

    @property
    def evaluated_candidate_keys(self) -> frozenset[str]:
        """Canonical keys consumed by exact, censored, or rejected trial receipts."""

        return frozenset(
            str(item["candidate_key"])
            for item in self.completed
            if item.get("candidate_key")
        )

    def _grouped_proposal_observations(
        self,
        *,
        domain: str,
        fixed_other_block: CompleteCandidateV1 | None,
    ) -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = []
        for key, group in self.observation_groups.items():
            candidate = CompleteCandidateV1.from_mapping(group["candidate"])
            if fixed_other_block is not None:
                if (
                    domain == "correction"
                    and candidate.controller_path != fixed_other_block.controller_path
                ):
                    continue
                if (
                    domain == "controller_path"
                    and candidate.correction_weights
                    != fixed_other_block.correction_weights
                ):
                    continue
            rows.append(
                {
                    "candidate": candidate.as_dict(),
                    "candidate_key": key,
                    "mae_n": float(group["mean_n"]),
                    "fingerprint": group["fingerprint_sha256"],
                    "fingerprint_sha256": group["fingerprint_sha256"],
                    "n": int(group["n"]),
                    "yvar_n2": float(group["yvar_n2"]),
                    "sample_variance_n2": group.get("sample_variance_n2"),
                }
            )
        return tuple(sorted(rows, key=lambda item: item["candidate_key"]))

    def _robust_incumbent(self) -> CompleteCandidateV1:
        if not self.observation_groups:
            return _complete_candidate(_base_controller_candidate())
        key = min(
            self.observation_groups,
            key=lambda item: (float(self.observation_groups[item]["mean_n"]), item),
        )
        return CompleteCandidateV1.from_mapping(self.observation_groups[key]["candidate"])

    def confirmed_incumbent_for_censor(self) -> tuple[float, int] | None:
        """Return the lowest mean among repeat-confirmed exact groups only."""

        confirmed = [
            (float(group["mean_n"]), int(group["n"]))
            for group in self.observation_groups.values()
            if int(group.get("n", 0)) >= 3
        ]
        return min(confirmed, key=lambda item: (item[0], item[1])) if confirmed else None

    def _frozen_robust_incumbent(self) -> CompleteCandidateV1:
        if self.frozen_controller_path is None:
            raise FigureEightError("Step6 polish lacks a frozen controller/path block")
        ranked = [
            (float(group["mean_n"]), key, group)
            for key, group in self.observation_groups.items()
            if int(group.get("n", 0)) >= 3
            and CompleteCandidateV1.from_mapping(group["candidate"]).controller_path
            == self.frozen_controller_path
        ]
        if not ranked:
            raise FigureEightError("Step6 polish lacks a repeat-qualified frozen incumbent")
        return CompleteCandidateV1.from_mapping(
            min(ranked, key=lambda item: (item[0], item[1]))[2]["candidate"]
        )

    def _freeze_blocks_for_ordinal(self, ordinal: int) -> None:
        if ordinal >= 101 and self.frozen_controller_path is None:
            eligible_keys = {
                str(row["candidate_key"])
                for row in self.accepted_observations
                if int(row.get("novel_ordinal", 0)) <= 100
            }
            ranked = [
                (float(group["mean_n"]), key, group)
                for key, group in self.observation_groups.items()
                if key in eligible_keys and int(group.get("n", 0)) >= 3
            ]
            if not ranked:
                raise FigureEightError("Step6 correction phase lacks a robust controller")
            _mean, _key, group = min(ranked, key=lambda item: (item[0], item[1]))
            self.frozen_controller_path = dict(
                CompleteCandidateV1.from_mapping(group["candidate"]).controller_path
            )
        if ordinal >= 161 and self.polish_incumbent is None:
            self.polish_incumbent = self._frozen_robust_incumbent().as_dict()

    def _pool_and_receipt(
        self,
        ordinal: int,
        phase: str,
        acquisition: str,
        domain: str,
        *,
        async_prefetched: bool = False,
    ) -> tuple[CompleteCandidateV1, ProposalReceiptV1]:
        evaluated = set(self.evaluated_candidate_keys)
        evaluated.update(str(item["candidate_key"]) for item in self.accepted_observations)
        pending = () if self.in_flight is None else (self.in_flight.candidate_key,)
        pending_candidates = (
            ()
            if self.in_flight is None
            else (CompleteCandidateV1.from_mapping(self.in_flight.candidate).as_dict(),)
        )
        fixed_other_block: CompleteCandidateV1 | None = None
        if domain == "correction" and self.frozen_controller_path is not None:
            fixed_other_block = CompleteCandidateV1(
                controller_path=self.frozen_controller_path,
                correction_weights=(0.0,) * 6,
            )
        if ordinal >= 161:
            if self.polish_incumbent is None:
                raise FigureEightError("Step6 alternating polish lacks incumbent")
            fixed_other_block = CompleteCandidateV1.from_mapping(self.polish_incumbent)
        local = acquisition == "qlognei_local"
        pool = self.cursor.fresh_pool(
            evaluated_keys=evaluated,
            pending_keys=pending,
            domain=domain,
            count=SOBOL_POOL_SIZE,
            fixed_other_block=fixed_other_block,
            probe_bounds_open=self.probe_bounds_open,
            local=local,
        )
        pool_keys = tuple(CompleteCandidateV1.from_mapping(item).candidate_key for item in pool)
        pool_sha = _sha256(pool_keys)
        if acquisition in {"qlognei", "qlognei_local"}:
            if self.proposal_provider is None:
                raise ProductionProposalUnavailable("Step6 qLogNEI proposal provider is unavailable")
            result = self.proposal_provider(
                pool,
                observations=self._grouped_proposal_observations(
                    domain=domain,
                    fixed_other_block=fixed_other_block,
                ),
                block=domain,
                evaluated_keys=evaluated,
                pending_keys=pending,
                pending_candidates=pending_candidates,
            )
            if not isinstance(result, Mapping):
                raise ProductionProposalUnavailable("Step6 qLogNEI provider returned no typed proposal")
            if str(result.get("acquisition", "")).lower().replace(" ", "") not in {"qlognei", "qlognoisyexpectedimprovement"}:
                raise ProductionProposalUnavailable("Step6 qLogNEI phase received a non-qLogNEI proposal")
            candidate = CompleteCandidateV1.from_mapping(result.get("candidate"))
            if candidate.candidate_key not in set(pool_keys):
                raise ProductionProposalUnavailable("Step6 qLogNEI provider selected outside its fresh pool")
            provider_name = str(result.get("production_provider", type(self.proposal_provider).__name__))
            receipt = ProposalReceiptV1(
                acquisition=acquisition,
                block=domain,
                pool_size=SOBOL_POOL_SIZE,
                fresh_candidate_count=SOBOL_POOL_SIZE,
                selected_candidate_key=candidate.candidate_key,
                pool_sha256=pool_sha,
                production_provider=provider_name,
                fit_receipt=result.get("fit_receipt", {}),
                async_prefetched=async_prefetched,
                pending_candidate_keys=pending if async_prefetched else (),
                overlap_phase="safe_return" if async_prefetched else None,
            )
            return candidate, receipt
        selected = CompleteCandidateV1.from_mapping(pool[(ordinal - 1) % len(pool)])
        receipt_acquisition = "forced_global_sobol" if acquisition == "global_sobol" else acquisition
        receipt = ProposalReceiptV1(
            acquisition=receipt_acquisition,
            block=domain,
            pool_size=SOBOL_POOL_SIZE,
            fresh_candidate_count=SOBOL_POOL_SIZE,
            selected_candidate_key=selected.candidate_key,
            pool_sha256=pool_sha,
            production_provider="persisted_sobol_cursor",
            async_prefetched=async_prefetched,
            pending_candidate_keys=pending if async_prefetched else (),
            overlap_phase="safe_return" if async_prefetched else None,
        )
        return selected, receipt

    def _candidate(
        self,
        ordinal: int,
        domain: str,
        acquisition: str,
        *,
        async_prefetched: bool = False,
    ) -> tuple[CompleteCandidateV1, ProposalReceiptV1, str | None]:
        if ordinal <= 3:
            axis, candidate = _outward_probe(ordinal)
            return candidate, ProposalReceiptV1("designed_probe", domain, 0, 0, candidate.candidate_key, _sha256([candidate.candidate_key]), "fixed_outward_probe"), axis
        candidate, receipt = self._pool_and_receipt(
            ordinal,
            "",
            acquisition,
            domain,
            async_prefetched=async_prefetched,
        )
        return candidate, receipt, None

    def prefetch_next_for_safe_return(self) -> TrialPlanV1 | None:
        """Score one next proposal while the current physical trial returns Home.

        The proposal treats the current candidate as pending.  It is reusable
        after either Exact admission or censor/refill only when both ordinals
        share the same frozen phase/acquisition/block contract.  This method
        never changes ``in_flight`` and therefore cannot authorize ARM.
        """

        current = self.in_flight
        if current is None or current.kind != "novel" or current.novel_ordinal >= MAX_NOVEL:
            return None
        if self.prefetched_plan is not None:
            return self.prefetched_plan
        current_contract = self.phase_for_novel(current.novel_ordinal)
        next_ordinal = current.novel_ordinal + 1
        next_contract = self.phase_for_novel(next_ordinal)
        if current_contract != next_contract or current.novel_ordinal <= 3:
            return None
        phase, acquisition, domain = next_contract
        candidate, receipt, probe_axis = self._candidate(
            next_ordinal,
            domain,
            acquisition,
            async_prefetched=True,
        )
        if probe_axis is not None:
            raise FigureEightError("Step6 async prefetch cannot materialize a probe")
        self.prefetched_plan = TrialPlanV1(
            next_ordinal,
            phase,
            acquisition,
            domain,
            candidate.as_dict(),
            proposal_receipt=receipt.as_dict(),
        )
        self._persist()
        return self.prefetched_plan

    def ask(self) -> TrialPlanV1 | None:
        if self.in_flight is not None:
            raise FigureEightError("Step6 serial q=1 dispatch already in flight")
        if self.repeat_queue:
            plan = self.repeat_queue.pop(0)
            self.in_flight = plan
            self._persist()
            return plan
        if self.prefetched_plan is not None:
            expected_ordinal = self.novel_count + 1
            phase, acquisition, domain = self.phase_for_novel(expected_ordinal)
            prefetched = self.prefetched_plan
            if (prefetched.phase, prefetched.acquisition, prefetched.block) != (
                phase,
                acquisition,
                domain,
            ):
                raise FigureEightError(
                    "Step6 async prefetched proposal crossed a phase boundary"
                )
            plan = replace(prefetched, novel_ordinal=expected_ordinal)
            self.prefetched_plan = None
            self.in_flight = plan
            self._persist()
            return plan
        if self.novel_count >= MAX_NOVEL:
            return None
        ordinal = self.novel_count + 1
        self._freeze_blocks_for_ordinal(ordinal)
        phase, acquisition, domain = self.phase_for_novel(ordinal)
        candidate, proposal_receipt, probe_axis = self._candidate(ordinal, domain, acquisition)
        plan = TrialPlanV1(
            ordinal, phase, acquisition, domain, candidate.as_dict(),
            probe_axis=probe_axis, proposal_receipt=proposal_receipt.as_dict(),
        )
        self.in_flight = plan
        self._persist()
        return plan

    def _refresh_observation_groups(self) -> None:
        if not self.accepted_observations:
            self.observation_groups = {}
            return
        noise = repeat_aware_yvar(
            [
                {
                    "fingerprint_sha256": row["fingerprint_sha256"],
                    "candidate_key": row["candidate_key"],
                    "mae_n": row["mae_n"],
                }
                for row in self.accepted_observations
            ]
        )
        by_key = {(row["fingerprint_sha256"], row["candidate_key"]): row for row in noise}
        groups: dict[str, dict[str, Any]] = {}
        for observation in self.accepted_observations:
            key = str(observation["candidate_key"])
            group = groups.setdefault(
                key,
                {
                    "candidate": observation["candidate"],
                    "fingerprint_sha256": observation["fingerprint_sha256"],
                    "values": [],
                },
            )
            if group["fingerprint_sha256"] != observation["fingerprint_sha256"]:
                raise FigureEightError("Step6 observation group mixes fingerprints")
            group["values"].append(float(observation["mae_n"]))
        for key, group in groups.items():
            receipt = by_key[(group["fingerprint_sha256"], key)]
            group.update(
                {
                    "n": int(receipt["n"]),
                    "mean_n": float(receipt["mean_n"]),
                    "sample_variance_n2": receipt["sample_variance_n2"],
                    "pooled_same_fingerprint_variance_n2": float(
                        receipt["pooled_same_fingerprint_variance_n2"]
                    ),
                    "yvar_n2": float(receipt["yvar_n2"]),
                }
            )
        self.observation_groups = groups
        if self.top_candidates:
            ranked = sorted(
                self.observation_groups,
                key=lambda item: (float(self.observation_groups[item]["mean_n"]), item),
            )[:3]
            self.top_candidates = [
                {
                    "candidate_key": key,
                    "n": int(self.observation_groups[key]["n"]),
                    "mean_n": float(self.observation_groups[key]["mean_n"]),
                }
                for key in ranked
            ]

    def complete(
        self,
        plan: TrialPlanV1,
        *,
        accepted: bool,
        mae_n: float | None = None,
        failure_signature: str | None = None,
        fingerprint_sha256: str | None = None,
        censored_receipt: FigureEightCensoredReceiptV1 | Mapping[str, Any] | None = None,
    ) -> None:
        if self.in_flight is None and any(item.get("candidate_key") == plan.candidate_key for item in self.completed):
            return
        if self.in_flight != plan:
            raise FigureEightError("Step6 completion does not match in-flight q=1 plan")
        censored = None
        if censored_receipt is not None:
            censored = (
                censored_receipt
                if isinstance(censored_receipt, FigureEightCensoredReceiptV1)
                else FigureEightCensoredReceiptV1.from_mapping(censored_receipt)
            )
            if accepted or plan.kind != "novel" or censored.candidate_key != plan.candidate_key:
                raise FigureEightError("Step6 censored receipt is not an ordinary in-flight novel candidate")
            if (
                self.campaign_fingerprint_sha256 is not None
                and censored.campaign_fingerprint_sha256 != self.campaign_fingerprint_sha256
            ):
                raise FigureEightError("Step6 censored receipt fingerprint differs")
            self.campaign_fingerprint_sha256 = (
                self.campaign_fingerprint_sha256 or censored.campaign_fingerprint_sha256
            )
        if accepted and mae_n is None:
            raise FigureEightError("Step6 accepted completion lacks sealed MAE")
        counted_dispatch = bool(
            plan.kind == "novel" and (accepted or censored is not None)
        )
        if counted_dispatch:
            self.novel_dispatch_count += 1
        bound_fingerprint = fingerprint_sha256 or self.campaign_fingerprint_sha256
        if accepted:
            if bound_fingerprint is None:
                raise FigureEightError("Step6 accepted completion lacks campaign fingerprint")
            _require_sha(bound_fingerprint, "Step6 accepted observation fingerprint")
            if (
                self.campaign_fingerprint_sha256 is not None
                and bound_fingerprint != self.campaign_fingerprint_sha256
            ):
                raise FigureEightError("Step6 accepted observation fingerprint differs")
            self.campaign_fingerprint_sha256 = bound_fingerprint
        self.completed.append({
            "novel_ordinal": plan.novel_ordinal,
            "novel_dispatch_count": (
                self.novel_dispatch_count if counted_dispatch else None
            ),
            "phase": plan.phase,
            "candidate_key": plan.candidate_key,
            "accepted": bool(accepted),
            "censored": censored is not None,
            "exact_budget_counted": bool(accepted and plan.kind == "novel"),
            "mae_n": mae_n,
            "kind": plan.kind,
            "candidate": CompleteCandidateV1.from_mapping(plan.candidate).as_dict(),
            "probe_axis": plan.probe_axis,
            "proposal_receipt": plan.proposal_receipt,
            "censored_receipt": None if censored is None else censored.as_dict(),
        })
        if censored is not None:
            self.consecutive_failure_signature = None
            self.consecutive_failure_count = 0
        else:
            if failure_signature:
                self.failure_signatures.append(str(failure_signature))
        if censored is None and not accepted:
            signature = str(failure_signature or "untyped_failure")
            if signature == self.consecutive_failure_signature:
                self.consecutive_failure_count += 1
            else:
                self.consecutive_failure_signature = signature
                self.consecutive_failure_count = 1
        elif censored is None:
            self.consecutive_failure_signature = None
            self.consecutive_failure_count = 0
        if accepted and mae_n is not None:
            observation = {
                "candidate": CompleteCandidateV1.from_mapping(plan.candidate).as_dict(),
                "candidate_key": plan.candidate_key,
                "mae_n": float(mae_n),
                "fingerprint": bound_fingerprint,
                "fingerprint_sha256": bound_fingerprint,
                "n": 1,
                "novel_ordinal": plan.novel_ordinal,
                "kind": plan.kind,
            }
            self.accepted_observations.append(observation)
            self._refresh_observation_groups()
            if plan.kind == "novel":
                if plan.novel_ordinal != self.novel_count + 1:
                    raise FigureEightError("Step6 admitted novel ordinal is not contiguous")
                self.novel_count = plan.novel_ordinal
            if plan.probe_axis is not None:
                self.probe_results.setdefault(plan.probe_axis, []).append({"mae_n": float(mae_n), "strict_gates": True})
        if plan.probe_axis is not None and (accepted or plan.kind == "repeat"):
            repeats = len(self.probe_results.get(plan.probe_axis, ()))
            if repeats < 3 and not any(
                item.repeat_of == (plan.repeat_of or plan.candidate_key)
                for item in self.repeat_queue
            ):
                self.queue_probe_repeat(plan, repeat_index=repeats + 1)
        if plan.kind == "novel" and self.prefetched_plan is not None:
            desired_ordinal = self.novel_count + 1
            phase, acquisition, domain = self.phase_for_novel(desired_ordinal)
            if (
                self.prefetched_plan.phase,
                self.prefetched_plan.acquisition,
                self.prefetched_plan.block,
            ) != (phase, acquisition, domain):
                # This should already have been prevented by the prefetch
                # contract.  Discarding is safer than dispatching across a
                # changed function block; the persisted Sobol cursor remains
                # monotonic and the next synchronous ask will be fresh.
                self.prefetched_plan = None
            else:
                self.prefetched_plan = replace(
                    self.prefetched_plan,
                    novel_ordinal=desired_ordinal,
                )
        self.in_flight = None
        self._persist()

    def complete_censored(
        self,
        plan: TrialPlanV1,
        receipt: FigureEightCensoredReceiptV1 | Mapping[str, Any],
    ) -> None:
        self.complete(plan, accepted=False, censored_receipt=receipt)

    @property
    def failure_pause_required(self) -> bool:
        return self.consecutive_failure_count >= 3

    def queue_probe_repeat(self, plan: TrialPlanV1, *, repeat_index: int) -> TrialPlanV1:
        if plan.kind not in {"novel", "repeat"} or plan.novel_ordinal > 24 or repeat_index not in {2, 3}:
            raise FigureEightError("Step6 probe repeat is outside the fixed warm-start rule")
        base_key = plan.repeat_of or plan.candidate_key
        repeat = TrialPlanV1(
            plan.novel_ordinal, "probe_repeat", "repeat", plan.block, plan.candidate,
            "repeat", base_key, plan.probe_axis, plan.proposal_receipt,
        )
        if not any(item.repeat_of == base_key for item in self.repeat_queue):
            self.repeat_queue.append(repeat)
            self._persist()
        return repeat

    def open_probe_bound(self, axis: str, *, handoff_anchor_mean_n: float, strict_gates: bool) -> bool:
        rows = self.probe_results.get(axis)
        if rows is None or len(rows) < 3:
            return False
        values = [float(row["mae_n"]) for row in rows[:3]]
        opened = self.repeat_opens_bound(
            baseline_mean_n=handoff_anchor_mean_n,
            repeated_mean_n=math.fsum(values) / len(values),
            strict_gates=strict_gates and all(row.get("strict_gates") is True for row in rows[:3]),
        )
        self.probe_bounds_open[axis] = opened
        self._persist()
        return opened

    @staticmethod
    def repeat_opens_bound(*, baseline_mean_n: float, repeated_mean_n: float, strict_gates: bool) -> bool:
        return strict_gates and _finite(baseline_mean_n, "baseline mean") - _finite(repeated_mean_n, "repeat mean") >= 0.01

    @staticmethod
    def sentinel_due(novel_dispatch_count: int) -> bool:
        return novel_dispatch_count > 0 and novel_dispatch_count % 10 == 0

    @staticmethod
    def sentinel_selection(observations: Sequence[Mapping[str, Any]], *, posterior_beating_probability: Mapping[str, float]) -> tuple[str, ...]:
        if not observations:
            return ()
        ranked = sorted(observations, key=lambda row: float(row["mae_n"]))
        top_count = max(1, math.ceil(len(ranked) * 0.05))
        selected = []
        for row in ranked[:top_count]:
            key = str(row["candidate_key"])
            if float(posterior_beating_probability.get(key, 0.0)) >= 0.25 and int(row.get("n", 1)) < 3:
                selected.append(key)
        return tuple(selected)

    @staticmethod
    def terminal_top_three(observations: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        ranked = sorted(observations, key=lambda row: float(row["mae_n"]))
        return tuple(str(row["candidate_key"]) for row in ranked[:3] if int(row.get("n", 1)) < 5)

    def queue_repeat_candidate(self, candidate_key: str, *, target_n: int, kind: str) -> int:
        """Queue sentinel/A-B/top repeats without incrementing novel dispatches."""

        if kind not in {
            "sentinel",
            "phase_boundary_repeat",
            "terminal_repeat",
            "matched_ab",
        } or target_n < 2:
            raise FigureEightError("Step6 repeat kind/target is invalid")
        group = self.observation_groups.get(str(candidate_key))
        if group is None:
            raise FigureEightError("Step6 repeat candidate is not an admitted group")
        current_n = int(group.get("n", len(group.get("values", ()))))
        if current_n >= target_n:
            return current_n
        if any(item.repeat_of == str(candidate_key) for item in self.repeat_queue):
            return current_n
        repeat = TrialPlanV1(
            novel_ordinal=self.novel_dispatch_count,
            phase=kind,
            acquisition="repeat",
            block="complete_candidate",
            candidate=CompleteCandidateV1.from_mapping(group["candidate"]).as_dict(),
            kind=kind,
            repeat_of=str(candidate_key),
        )
        self.repeat_queue.append(repeat)
        self._persist()
        return current_n

    def schedule_controller_freeze_repeats(self) -> str:
        """Make the phase-100 controller winner repeat-qualified before freeze."""

        eligible_keys = {
            str(row["candidate_key"])
            for row in self.accepted_observations
            if int(row.get("novel_ordinal", 0)) <= 100
        }
        ranked = sorted(
            (
                (float(group["mean_n"]), key)
                for key, group in self.observation_groups.items()
                if key in eligible_keys
            ),
            key=lambda item: (item[0], item[1]),
        )
        if not ranked:
            raise FigureEightError(
                "Step6 controller freeze has no admitted controller candidate"
            )
        key = ranked[0][1]
        self.queue_repeat_candidate(
            key,
            target_n=3,
            kind="phase_boundary_repeat",
        )
        return key

    def queue_fixed_sentinel(self) -> str:
        if self.fixed_sentinel_candidate is None:
            raise FigureEightError("Step6 fixed sentinel candidate is missing")
        candidate = CompleteCandidateV1.from_mapping(self.fixed_sentinel_candidate)
        key = candidate.candidate_key
        if any(
            item.kind == "sentinel" and item.repeat_of == key
            for item in self.repeat_queue
        ):
            return key
        self.repeat_queue.append(
            TrialPlanV1(
                novel_ordinal=self.novel_dispatch_count,
                phase="fixed_sentinel",
                acquisition="repeat",
                block="complete_candidate",
                candidate=candidate.as_dict(),
                kind="sentinel",
                repeat_of=key,
            )
        )
        self._persist()
        return key

    def schedule_top_repeats(
        self, posterior_beating_probability: Mapping[str, float]
    ) -> tuple[str, ...]:
        selected = self.sentinel_selection(
            tuple(
                {
                    "candidate_key": key,
                    "mae_n": group["mean_n"],
                    "n": group.get("n", 0),
                }
                for key, group in self.observation_groups.items()
            ),
            posterior_beating_probability=posterior_beating_probability,
        )
        for key in selected:
            self.queue_repeat_candidate(key, target_n=3, kind="sentinel")
        return selected

    def schedule_sentinel_repeats(self, posterior_beating_probability: Mapping[str, float]) -> tuple[str, ...]:
        if not self.sentinel_due(self.novel_dispatch_count):
            return ()
        if any(int(row.get("novel_dispatch_count", -1)) == self.novel_dispatch_count for row in self.sentinels):
            return ()
        fixed = self.queue_fixed_sentinel()
        selected = self.schedule_top_repeats(posterior_beating_probability)
        self.sentinels.append({
            "novel_dispatch_count": self.novel_dispatch_count,
            "fixed_sentinel_candidate_key": fixed,
            "candidate_keys": list(selected),
            "posterior_beating_probability": dict(posterior_beating_probability),
        })
        self._persist()
        return (fixed, *selected)

    def schedule_terminal_repeats(self) -> tuple[str, ...]:
        if self.novel_count < EXACT_NOVEL_TARGET:
            return ()
        selected = tuple(
            sorted(self.observation_groups, key=lambda key: float(self.observation_groups[key]["mean_n"]))[:3]
        )
        for key in selected:
            self.queue_repeat_candidate(key, target_n=5, kind="terminal_repeat")
        self.top_candidates = [
            {"candidate_key": key, "n": int(self.observation_groups[key].get("n", 0)), "mean_n": self.observation_groups[key]["mean_n"]}
            for key in selected
        ]
        self._persist()
        return selected

    def record_convergence_proposal_batch(self, proposals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Persist a complete fresh proposal batch, never a caller scalar."""

        if len(proposals) != SOBOL_POOL_SIZE:
            raise FigureEightError("Step6 convergence receipt requires exactly 128 proposals")
        keys: list[str] = []
        normalized: list[dict[str, Any]] = []
        for proposal in proposals:
            if not isinstance(proposal, Mapping) or proposal.get("fresh") is not True:
                raise FigureEightError("Step6 convergence receipt lacks fresh candidate evidence")
            key = str(proposal.get("candidate_key", ""))
            if not key or key in keys:
                raise FigureEightError("Step6 convergence receipt contains duplicate candidate keys")
            probability = _finite(
                proposal.get("posterior_probability_of_robust_improvement"),
                "robust improvement probability",
            )
            if not 0.0 <= probability <= 1.0:
                raise FigureEightError("Step6 robust improvement probability is outside [0,1]")
            keys.append(key)
            normalized.append({
                "candidate_key": key,
                "posterior_probability_of_robust_improvement": probability,
                "fresh": True,
            })
        batch = {
            "schema": SENTINEL_PROPOSAL_SCHEMA,
            "version": 1,
            "batch_size": SOBOL_POOL_SIZE,
            "proposals": normalized,
            "max_probability_of_robust_improvement": max(
                item["posterior_probability_of_robust_improvement"] for item in normalized
            ),
            "batch_sha256": _sha256(normalized),
        }
        self.convergence_checks.append(batch)
        if batch["max_probability_of_robust_improvement"] < 0.05:
            self._consecutive_convergence_checks += 1
        else:
            self._consecutive_convergence_checks = 0
        self._persist()
        return batch

    def should_stop(self, *, novel_count: int | None = None, top_three_n: Sequence[int] | None = None) -> bool:
        """Apply the frozen stop rule from persisted batch receipts only."""

        count = self.novel_count if novel_count is None else int(novel_count)
        repeats = (
            tuple(int(item.get("n", 0)) for item in self.top_candidates[:3])
            if top_three_n is None
            else tuple(int(value) for value in top_three_n)
        )
        top_three_complete = len(repeats) == 3 and all(value >= 5 for value in repeats)
        if count < EXACT_NOVEL_TARGET:
            return False
        return top_three_complete and not self.repeat_queue and self.in_flight is None


@dataclass(frozen=True)
class CampaignStateV1:
    """Cold-readable state envelope for a scheduler snapshot."""

    payload: Mapping[str, Any]
    schema: str = CAMPAIGN_STATE_SCHEMA
    version: int = CAMPAIGN_STATE_VERSION

    def __post_init__(self) -> None:
        if self.schema != CAMPAIGN_STATE_SCHEMA or self.version != CAMPAIGN_STATE_VERSION:
            raise FigureEightError("Step6 campaign state schema/version differs")
        if not isinstance(self.payload, Mapping):
            raise FigureEightError("Step6 campaign state payload is not an object")
        for field_name in (
            "novel_count", "exact_novel_count", "novel_dispatch_count", "accepted_observations", "observation_groups", "in_flight", "prefetched_plan",
            "repeat_queue", "sentinels", "drift_state", "phase", "top_candidates",
            "convergence_checks", "failure_signatures", "sobol",
        ):
            if field_name not in self.payload:
                raise FigureEightError(f"Step6 campaign state lacks {field_name}")

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, **dict(self.payload)}

    def save(self, path: Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CampaignStateV1":
        if not isinstance(value, Mapping):
            raise FigureEightError("Step6 campaign state must be an object")
        payload = dict(value)
        schema = payload.pop("schema", None)
        version = payload.pop("version", None)
        payload.setdefault("exact_novel_count", payload.get("novel_count", 0))
        payload.setdefault("prefetched_plan", None)
        payload.setdefault(
            "novel_dispatch_count",
            max(
                (
                    int(item["novel_dispatch_count"])
                    for item in payload.get("completed", ())
                    if item.get("novel_dispatch_count") is not None
                ),
                default=int(payload.get("novel_count", 0)),
            ),
        )
        return cls(payload=payload, schema=schema, version=version)


@dataclass(frozen=True)
class EvidenceReferenceV1:
    role: str
    path: str
    content_sha256: str
    schema: str = EVIDENCE_REFERENCE_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != EVIDENCE_REFERENCE_SCHEMA or self.version != 1:
            raise FigureEightError("Step6 evidence reference schema/version differs")
        if not self.role or not self.path:
            raise FigureEightError("Step6 evidence reference role/path is missing")
        _require_sha(self.content_sha256, f"evidence {self.role}")

    def verify(self) -> None:
        path = Path(self.path)
        if not path.is_file():
            raise FigureEightError(f"Step6 evidence artifact is missing: {self.role}")
        actual = _sha256_file(path)
        if actual != self.content_sha256:
            raise FigureEightError(f"Step6 evidence artifact hash differs: {self.role}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "role": self.role,
            "path": self.path,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvidenceReferenceV1":
        if not isinstance(value, Mapping):
            raise FigureEightError("Step6 evidence reference must be an object")
        return cls(
            role=str(value["role"]),
            path=str(value["path"]),
            content_sha256=str(value["content_sha256"]),
            schema=str(value.get("schema", EVIDENCE_REFERENCE_SCHEMA)),
            version=int(value.get("version", 1)),
        )


@dataclass(frozen=True)
class FigureEightLaunchReceiptV1:
    """Verifier-derived live gate receipt; booleans are never an input."""

    evidence: tuple[EvidenceReferenceV1, ...]
    final_campaign_fingerprint_sha256: str
    package_triplet_sha256: Mapping[str, str]
    source_sha256: str
    home_frame_sha256: str
    controller_identity_sha256: str
    eoat_tcp_payload_sha256: str
    admission_policy_sha256: str
    schema: str = LAUNCH_RECEIPT_V2_SCHEMA
    version: int = LAUNCH_RECEIPT_V2_VERSION

    REQUIRED_ROLES = frozenset({
        "package_readback", "no_contact_canary",
        "home_frame", "controller_identity", "source_identity",
        "eoat_tcp_payload", "admission_policy", "final_campaign_fingerprint",
    })

    def __post_init__(self) -> None:
        if self.schema != LAUNCH_RECEIPT_V2_SCHEMA or self.version != LAUNCH_RECEIPT_V2_VERSION:
            raise FigureEightError("Step6 launch receipt schema/version differs")
        refs = tuple(self.evidence)
        roles = {ref.role for ref in refs}
        if roles != self.REQUIRED_ROLES or len(refs) != len(roles):
            raise FigureEightError("Step6 launch receipt evidence roles are incomplete")
        for ref in refs:
            ref.verify()
        _require_sha(self.final_campaign_fingerprint_sha256, "final campaign fingerprint")
        _require_sha(self.source_sha256, "launch source")
        _require_sha(self.home_frame_sha256, "launch Home/frame")
        _require_sha(self.controller_identity_sha256, "launch controller identity")
        _require_sha(self.eoat_tcp_payload_sha256, "launch EOAT/TCP/payload")
        _require_sha(self.admission_policy_sha256, "launch admission policy")
        if set(self.package_triplet_sha256) != {"script", "txt", "urp"}:
            raise FigureEightError("Step6 launch package triplet is incomplete")
        for role, value in self.package_triplet_sha256.items():
            _require_sha(value, f"launch package {role}")

    @property
    def live_ready(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "evidence": [ref.as_dict() for ref in self.evidence],
            "final_campaign_fingerprint_sha256": self.final_campaign_fingerprint_sha256,
            "package_triplet_sha256": dict(self.package_triplet_sha256),
            "source_sha256": self.source_sha256,
            "home_frame_sha256": self.home_frame_sha256,
            "controller_identity_sha256": self.controller_identity_sha256,
            "eoat_tcp_payload_sha256": self.eoat_tcp_payload_sha256,
            "admission_policy_sha256": self.admission_policy_sha256,
            "live_ready": True,
            "derived_by_verifier": True,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FigureEightLaunchReceiptV1":
        # A JSON object is evidence to verify, never an authorization source.
        raise FigureEightError("Step6 launch receipt must be derived by the evidence verifier")

    @classmethod
    def derive(
        cls,
        *,
        evidence_paths: Mapping[str, Path],
        final_fingerprint: FigureEightCampaignFingerprintV1,
        package_paths: Mapping[str, Path],
        expected_source_sha256: str,
        expected_home_frame_sha256: str,
        expected_controller_identity_sha256: str,
        expected_eoat_tcp_payload_sha256: str,
        expected_admission_policy_sha256: str,
    ) -> "FigureEightLaunchReceiptV1":
        if final_fingerprint.handoff_policy == HANDOFF_PENDING or not final_fingerprint.as_dict()["trainable"]:
            raise FigureEightError("Step6 launch receipt cannot derive from handoff_pending fingerprint")
        if set(package_paths) != {"script", "txt", "urp"}:
            raise FigureEightError("Step6 launch package paths are incomplete")
        triplet = {role: _sha256_file(Path(path)) for role, path in package_paths.items()}
        refs = tuple(
            EvidenceReferenceV1(role=role, path=str(Path(path)), content_sha256=_sha256_file(Path(path)))
            for role, path in sorted(evidence_paths.items())
        )
        by_role = {ref.role: ref for ref in refs}
        if set(by_role) != cls.REQUIRED_ROLES:
            raise FigureEightError("Step6 launch evidence roles are incomplete")

        def artifact(role: str) -> Mapping[str, Any]:
            try:
                payload = json.loads(Path(by_role[role].path).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise FigureEightError(f"Step6 {role} evidence is not a readable typed artifact") from exc
            if not isinstance(payload, Mapping) or payload.get("passed") is not True:
                raise FigureEightError(f"Step6 {role} evidence is not passed typed evidence")
            return payload

        readback = artifact("package_readback")
        readback_triplet = readback.get("package_triplet_sha256", readback.get("triplet_sha256"))
        if readback_triplet != triplet:
            raise FigureEightError("Step6 package read-back hashes differ from local package bytes")
        if artifact("no_contact_canary").get("contact_detected") is True:
            raise FigureEightError("Step6 no-contact canary evidence reports contact")
        if artifact("home_frame").get("home_frame_sha256") != expected_home_frame_sha256:
            raise FigureEightError("Step6 Home/frame evidence differs")
        if artifact("controller_identity").get("controller_identity_sha256") != expected_controller_identity_sha256:
            raise FigureEightError("Step6 controller identity evidence differs")
        if artifact("source_identity").get("source_sha256") != expected_source_sha256:
            raise FigureEightError("Step6 source identity evidence differs")
        if artifact("eoat_tcp_payload").get("eoat_tcp_payload_sha256") != expected_eoat_tcp_payload_sha256:
            raise FigureEightError("Step6 EOAT/TCP/payload evidence differs")
        if artifact("admission_policy").get("admission_policy_sha256") != expected_admission_policy_sha256:
            raise FigureEightError("Step6 admission policy evidence differs")
        final = artifact("final_campaign_fingerprint")
        if final.get("fingerprint_sha256") != final_fingerprint.sha256:
            raise FigureEightError("Step6 final campaign fingerprint evidence differs")
        return cls(
            evidence=refs,
            final_campaign_fingerprint_sha256=final_fingerprint.sha256,
            package_triplet_sha256=triplet,
            source_sha256=expected_source_sha256,
            home_frame_sha256=expected_home_frame_sha256,
            controller_identity_sha256=expected_controller_identity_sha256,
            eoat_tcp_payload_sha256=expected_eoat_tcp_payload_sha256,
            admission_policy_sha256=expected_admission_policy_sha256,
        )


LaunchReceiptV1 = FigureEightLaunchReceiptV1


class OfflineCampaignV1:
    """Receipt store and exact-admission seam used by the runner and fake writer."""

    def __init__(self, admission: StrictAdmissionV1, receipt_path: Path, optimizer: Any | None = None) -> None:
        self.admission = admission
        self.receipt_path = Path(receipt_path)
        self.optimizer = optimizer
        self.receipts: list[dict[str, Any]] = []
        self.observations: list[ObservationRecordV1] = []
        self.censored_receipts: list[FigureEightCensoredReceiptV1] = []
        self.evaluated_candidate_keys: set[str] = set()
        self._receipt_by_trial: dict[str, AdmissionReceiptV1] = {}
        if self.receipt_path.is_file():
            self.receipts = [json.loads(line) for line in self.receipt_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            for row in self.receipts:
                if row.get("status") == "censored_nontrainable":
                    parsed_censored = FigureEightCensoredReceiptV1.from_mapping(row["censored_receipt"])
                    self.censored_receipts.append(parsed_censored)
                    self.evaluated_candidate_keys.add(parsed_censored.candidate_key)
                    continue
                receipt = row.get("receipt")
                if isinstance(receipt, Mapping) and receipt.get("trial_id"):
                    self._receipt_by_trial[str(receipt["trial_id"])] = AdmissionReceiptV1(
                        trial_id=str(receipt["trial_id"]),
                        epoch_id=str(receipt["epoch_id"]),
                        fingerprint_sha256=str(receipt["fingerprint_sha256"]),
                        trial_admission_passed=bool(receipt["trial_admission_passed"]),
                        motion_gate=bool(receipt["motion_gate"]),
                        timing_gate=bool(receipt["timing_gate"]),
                        sealed=bool(receipt["sealed"]),
                        exact_550_bin_seal=bool(receipt["exact_550_bin_seal"]),
                        accepted=bool(receipt["accepted"]),
                        reasons=tuple(receipt.get("reasons", ())),
                        sealed_mae_n=receipt.get("sealed_mae_n"),
                        raw_gap_count=int(receipt.get("raw_gap_count", FORMAL_BIN_COUNT)),
                    )
                    candidate = row.get("candidate")
                    if isinstance(candidate, Mapping):
                        self.evaluated_candidate_keys.add(_candidate_key(candidate))

    def _append(self, value: Mapping[str, Any]) -> None:
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with self.receipt_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(value), sort_keys=True, allow_nan=False) + "\n")
        self.receipts.append(dict(value))

    def tell_exact(self, *, candidate: Mapping[str, Any], trial: TrialEvidenceV1) -> AdmissionReceiptV1:
        if trial.trial_id in self._receipt_by_trial:
            return self._receipt_by_trial[trial.trial_id]
        receipt = self.admission.evaluate(trial)
        try:
            parsed_candidate = CompleteCandidateV1.from_mapping(candidate)
            canonical_candidate: Mapping[str, Any] = parsed_candidate.as_dict()
        except FigureEightError:
            canonical_candidate = dict(candidate)
            if receipt.strict_passed:
                receipt = replace(
                    receipt,
                    accepted=False,
                    trial_admission_passed=False,
                    reasons=tuple((*receipt.reasons, "complete_candidate_contract_failed")),
                )
        row = {"kind": "admission", "candidate": dict(canonical_candidate), "receipt": receipt.as_dict(), "raw_samples": [dict(value) for value in trial.raw_samples]}
        if not receipt.strict_passed:
            self._append({**row, "status": "rejected_incomplete_or_ineligible"})
            self._receipt_by_trial[trial.trial_id] = receipt
            return receipt
        assert receipt.sealed_mae_n is not None
        record = ObservationRecordV1(receipt.fingerprint_sha256, _candidate_key(canonical_candidate), receipt.sealed_mae_n)
        self.observations.append(record)
        yvar = repeat_aware_yvar([{"fingerprint_sha256": item.fingerprint_sha256, "candidate_key": item.candidate_key, "mae_n": item.mae_n} for item in self.observations])[-1]["yvar_n2"]
        self._append({**row, "status": "admitted_exact", "yvar_n2": yvar})
        # The direct Figure-eight fingerprint is deliberately pending and is
        # evidence-only.  A production tell_exact becomes legal only after the
        # typed matched-A/B winner issues a frozen fingerprint.
        if self.optimizer is not None and self.admission.fingerprint.handoff_policy != HANDOFF_PENDING:
            self.optimizer.tell_exact(dict(canonical_candidate), receipt.sealed_mae_n, yvar)
        self._receipt_by_trial[trial.trial_id] = receipt
        return receipt

    def record_censored(
        self,
        receipt: FigureEightCensoredReceiptV1 | Mapping[str, Any],
    ) -> FigureEightCensoredReceiptV1:
        """Persist a typed censor receipt without invoking exact GP admission."""

        parsed = receipt if isinstance(receipt, FigureEightCensoredReceiptV1) else FigureEightCensoredReceiptV1.from_mapping(receipt)
        if parsed.campaign_fingerprint_sha256 != self.admission.fingerprint.sha256:
            raise FigureEightError("Figure-eight censored receipt fingerprint differs")
        if parsed.candidate_key in self.evaluated_candidate_keys:
            for existing in self.censored_receipts:
                if existing.candidate_key == parsed.candidate_key:
                    return existing
            raise FigureEightError("Figure-eight candidate key was already evaluated")
        row = {
            "kind": "censored_observation",
            "candidate": dict(parsed.candidate),
            "censored_receipt": parsed.as_dict(),
            "status": "censored_nontrainable",
            "exact_novel_counted": False,
            "tell_exact_called": False,
        }
        self._append(row)
        self.censored_receipts.append(parsed)
        self.evaluated_candidate_keys.add(parsed.candidate_key)
        return parsed


def make_metric_result(samples: Iterable[Mapping[str, Any]]) -> MetricResultV1:
    accumulator = GapPreservingMetricAccumulatorV1(MetricFingerprintV1.figure8())
    accumulator.add_samples(samples)
    return accumulator.seal()


__all__ = [
    "ADMISSION_SCHEMA", "CAMPAIGN_CONFIG_SCHEMA", "CORRECTION_FEATURE_NAMES", "CORRECTION_NORMALIZATION_SCALES", "FIGURE8_STAGE_ID", "FRESH_FRAME_WAIT_POLICY",
    "CENSOR_GUARD_BINS", "CENSOR_KAPPA", "CENSOR_SCHEMA", "EXACT_NOVEL_TARGET",
    "HANDOFF_POLICIES", "MAX_NOVEL", "MIN_NOVEL", "SOBOL_POOL_SIZE", "AdmissionReceiptV1",
    "CampaignConfigV1", "CampaignStateV1", "CompleteCandidateV1", "CorrectionPolicyV1", "CorrectionReceiptV1", "CorrectionStateV1",
    "FigureEightCampaignFingerprintV1", "FigureEightCensorDecisionV1", "FigureEightCensoredReceiptV1", "FigureEightError",
    "FigureEightSchedulerV1", "FigureEightLaunchReceiptV1", "EvidenceReferenceV1", "HandoffPolicyEnum", "LaunchReceiptV1", "OfflineCampaignV1",
    "ObservationRecordV1", "PersistedSobolCursorV1", "StrictAdmissionV1", "TrialEvidenceV1",
    "TrialPlanV1", "ProposalReceiptV1", "ProductionProposalUnavailable", "R013ProductionProposalProviderV1",
    "ReportEvidenceContractV1", "SafetyFaultReceiptV1", "LatchedSafetyFaultError",
    "build_campaign_fingerprint", "build_frozen_campaign_fingerprint", "build_report_evidence_contract",
    "load_campaign_config", "evaluate_figure8_censor_prefix", "make_figure8_censored_receipt", "make_metric_result", "repeat_aware_yvar",
]
