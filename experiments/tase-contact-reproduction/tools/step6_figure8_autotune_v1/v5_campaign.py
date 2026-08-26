"""Offline, append-only campaign orchestration for Autotuner V5.

This module is deliberately a campaign layer above the sealed physical V5
ledger.  It does not open a live transport, construct a sensor reader, call an
optimizer, or decide any safety/performance threshold.  A physical result is
accepted only after the caller supplies the real G3 record and a cold-verified
G3 ledger containing the required durable tell state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import os
import statistics
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # Tests may put ``tools`` directly on sys.path.
    from step6_figure8_autotune_v1.core import (
        CompleteCandidateV1,
        PersistedSobolCursorV1,
        SOBOL_POOL_SIZE,
    )
    from step6_figure8_autotune_v1.v5_composition_contract import V5AttemptKind
    from step6_figure8_autotune_v1.v5_lifecycle_ledger import (
        BoundaryMode,
        FigureEightPhysicalRecordV2,
        LedgerRole,
        OptimizerReceiptV2,
        TellState,
        V5PhysicalAdmissionLedgerV2,
        V5LifecycleLedgerError,
        canonical_sha256,
    )
except ModuleNotFoundError:  # pragma: no cover - repository-root imports
    from tools.step6_figure8_autotune_v1.core import (
        CompleteCandidateV1,
        PersistedSobolCursorV1,
        SOBOL_POOL_SIZE,
    )
    from tools.step6_figure8_autotune_v1.v5_composition_contract import V5AttemptKind
    from tools.step6_figure8_autotune_v1.v5_lifecycle_ledger import (
        BoundaryMode,
        FigureEightPhysicalRecordV2,
        LedgerRole,
        OptimizerReceiptV2,
        TellState,
        V5PhysicalAdmissionLedgerV2,
        V5LifecycleLedgerError,
        canonical_sha256,
    )


CAMPAIGN_SCHEMA = "step6.autotune/figure8-v5-campaign-v2"
CAMPAIGN_VERSION = 2
CONFIG_SCHEMA = "step6.autotune/autotuner-v5-campaign-config-v2"
CONFIG_VERSION = 2
IDENTITY_SCHEMA = "step6.autotune/figure8-v5-campaign-identity-v2"
IDENTITY_VERSION = 2
PLAN_SCHEMA = "step6.autotune/figure8-v5-trial-plan-v2"
PLAN_VERSION = 2
PROPOSAL_SCHEMA = "step6.autotune/figure8-v5-proposal-receipt-v2"
PROPOSAL_VERSION = 2
LEDGER_SCHEMA = "step6.autotune/figure8-v5-campaign-decision-ledger-v2"
LEDGER_VERSION = 2
LEDGER_EVENT_SCHEMA = "step6.autotune/figure8-v5-campaign-decision-event-v2"
CHECKPOINT_SCHEMA = "step6.autotune/figure8-v5-campaign-checkpoint-v2"
CHECKPOINT_VERSION = 2
REPORT_SCHEMA = "step6.autotune/figure8-v5-campaign-report-v2"
REPORT_VERSION = 2
GENESIS_SHA256 = "0" * 64
PRIMARY_NOVEL_TARGET = 200
CORRECTION_NOVEL_TARGET = 60
TOP_COUNT = 3
CONFIRMATIONS_PER_TOP = 4
MATCHED_PAIRS = 5
MATCHED_ORDER = ("Z", "C", "C", "Z", "Z", "C", "C", "Z", "Z", "C")
# Offline tests use one positive transport-session placeholder.  Live owners
# bind the fresh READY ``session_epoch`` explicitly.  It is a wire freshness
# identity only: V5 has no campaign/epoch qualification stage or budget.
V5_WIRE_EPOCH = 1
V5_OBSERVATION_GROUP_SCHEMA = "step6.autotune/figure8-v5-observation-group-v2"
V5_OBSERVATION_NOISE_MIN_N2 = 1e-4
V5_OBSERVATION_NOISE_MAX_N2 = 2e-2
V5_OBSERVATION_NOISE_FALLBACK_N2 = 0.01
V5_OBSERVATION_NOISE_PRIOR_DOF = 2
ENTRY_MODE_ROLLOVER_CHAIN_V1 = "ROLLOVER_CHAIN_V1"
ENTRY_MODE_HOME_ONLY_V1 = "HOME_ONLY_V1"


class V5CampaignError(RuntimeError):
    """Deterministic fail-closed campaign error."""


class CampaignRoleV2(str, Enum):
    PRIMARY = "PRIMARY"
    CORRECTION = "CORRECTION"


class TrialStageV2(str, Enum):
    PRIMARY_NOVEL = "PRIMARY_NOVEL"
    PRIMARY_CONFIRM = "PRIMARY_CONFIRM"
    CORRECTION_NOVEL = "CORRECTION_NOVEL"
    MATCHED_ZERO = "MATCHED_ZERO"
    MATCHED_CORRECTED = "MATCHED_CORRECTED"


class OutcomeV2(str, Enum):
    PHYSICALLY_ELIGIBLE = "PHYSICALLY_ELIGIBLE"
    CENSORED = "CENSORED"
    INELIGIBLE = "INELIGIBLE"
    FAILURE = "FAILURE"
    NEXT_NOT_READY = "NEXT_NOT_READY"


class ProposalMethodV2(str, Enum):
    DETERMINISTIC_SOBOL = "deterministic_sobol"
    GLOBAL_SOBOL = "global_sobol"
    QLOGNEI = "qlognei"
    FIXED_REPLAY = "fixed_replay"


@dataclass(frozen=True)
class V5ObservationGroupV2:
    """One identity-bound candidate group consumed by the V5 optimizer."""

    fingerprint_sha256: str
    candidate_key: str
    candidate: Mapping[str, Any]
    n: int
    mean_n: float
    sample_variance_n2: float | None
    pooled_within_fingerprint_variance_n2: float
    yvar_n2: float
    schema: str = V5_OBSERVATION_GROUP_SCHEMA
    version: int = 2

    def __post_init__(self) -> None:
        _require_sha(self.fingerprint_sha256, "V5 observation fingerprint")
        if self.schema != V5_OBSERVATION_GROUP_SCHEMA or self.version != 2:
            raise V5CampaignError("V5 observation group schema differs")
        _positive_int(self.n, "V5 observation repeat count")
        if not self.candidate_key or not isinstance(self.candidate, Mapping):
            raise V5CampaignError("V5 observation candidate identity is incomplete")
        _finite(self.mean_n, "V5 observation mean")
        if self.sample_variance_n2 is not None:
            _finite(self.sample_variance_n2, "V5 observation sample variance")
            if self.sample_variance_n2 < 0.0:
                raise V5CampaignError("V5 observation sample variance is negative")
        pooled = _finite(
            self.pooled_within_fingerprint_variance_n2,
            "V5 pooled observation variance",
        )
        yvar = _finite(self.yvar_n2, "V5 observation Yvar")
        if not V5_OBSERVATION_NOISE_MIN_N2 <= pooled <= V5_OBSERVATION_NOISE_MAX_N2:
            raise V5CampaignError("V5 pooled observation variance is outside bounds")
        if not V5_OBSERVATION_NOISE_MIN_N2 <= yvar <= V5_OBSERVATION_NOISE_MAX_N2:
            raise V5CampaignError("V5 observation Yvar is outside bounds")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "fingerprint_sha256": self.fingerprint_sha256,
            "candidate_key": self.candidate_key,
            "candidate": dict(self.candidate),
            "n": self.n,
            "mean_n": self.mean_n,
            "mae_n": self.mean_n,
            "sample_variance_n2": self.sample_variance_n2,
            "pooled_within_fingerprint_variance_n2": self.pooled_within_fingerprint_variance_n2,
            "pooled_same_fingerprint_variance_n2": self.pooled_within_fingerprint_variance_n2,
            "yvar_n2": self.yvar_n2,
        }


def _clip_v5_observation_noise(value: float) -> float:
    parsed = _finite(value, "V5 observation noise")
    if parsed < 0.0:
        raise V5CampaignError("V5 observation noise is negative")
    return min(
        V5_OBSERVATION_NOISE_MAX_N2,
        max(V5_OBSERVATION_NOISE_MIN_N2, parsed),
    )


def build_v5_observation_groups(
    observations: Sequence[Mapping[str, Any]],
    *,
    fingerprint_sha256: str,
) -> tuple[V5ObservationGroupV2, ...]:
    """Group repeats without treating between-candidate signal as noise."""

    fingerprint = _require_sha(fingerprint_sha256, "V5 observation fingerprint")
    groups: dict[str, dict[str, Any]] = {}
    for row in observations:
        if not isinstance(row, Mapping):
            raise V5CampaignError("V5 observation row is not a mapping")
        candidate = row.get("candidate")
        if not isinstance(candidate, Mapping):
            raise V5CampaignError("V5 observation candidate is missing")
        parsed = _candidate(candidate)
        candidate_key = parsed.candidate_key
        if row.get("candidate_key") not in (None, candidate_key):
            raise V5CampaignError("V5 observation candidate key differs")
        value = _finite(row.get("mae_n"), "V5 observation MAE")
        if value < 0.0:
            raise V5CampaignError("V5 observation MAE is negative")
        group = groups.setdefault(
            candidate_key,
            {"candidate": parsed.as_dict(), "values": []},
        )
        group["values"].append(value)

    if not groups:
        return ()

    within: dict[str, tuple[int, float]] = {}
    for key, group in groups.items():
        values = tuple(float(value) for value in group["values"])
        if len(values) >= 2:
            within[key] = (len(values) - 1, statistics.variance(values))

    degrees = sum(item[0] for item in within.values())
    if degrees:
        pooled_raw = math.fsum(dof * variance for dof, variance in within.values()) / degrees
    else:
        pooled_raw = V5_OBSERVATION_NOISE_FALLBACK_N2
    pooled = _clip_v5_observation_noise(pooled_raw)

    result: list[V5ObservationGroupV2] = []
    for key, group in sorted(groups.items()):
        values = tuple(float(value) for value in group["values"])
        n = len(values)
        sample = None if n < 2 else statistics.variance(values)
        if n < 2:
            shrunk = pooled
        else:
            shrunk = (
                (n - 1) * float(sample)
                + V5_OBSERVATION_NOISE_PRIOR_DOF * pooled
            ) / (n - 1 + V5_OBSERVATION_NOISE_PRIOR_DOF)
        result.append(
            V5ObservationGroupV2(
                fingerprint_sha256=fingerprint,
                candidate_key=key,
                candidate=group["candidate"],
                n=n,
                mean_n=math.fsum(values) / n,
                sample_variance_n2=sample,
                pooled_within_fingerprint_variance_n2=pooled,
                yvar_n2=_clip_v5_observation_noise(shrunk / n),
            )
        )
    return tuple(result)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise V5CampaignError("campaign value is not canonical JSON") from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _bytes_sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha(value: Any, name: str) -> str:
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise V5CampaignError(f"{name} must be a lowercase SHA-256")
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise V5CampaignError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise V5CampaignError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise V5CampaignError(f"{name} must be finite")
    return result


def _positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0 or value > 2**31 - 1:
        raise V5CampaignError(f"{name} must be a positive signed integer")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0 or value > 2**31 - 1:
        raise V5CampaignError(f"{name} must be a non-negative signed integer")
    return value


def _resolved_root(value: Path | str) -> str:
    path = Path(value)
    if path.is_symlink():
        raise V5CampaignError("campaign state root must not be a symlink")
    return str(path.resolve())


def _role(value: CampaignRoleV2 | str) -> CampaignRoleV2:
    try:
        return value if isinstance(value, CampaignRoleV2) else CampaignRoleV2(value)
    except (TypeError, ValueError) as exc:
        raise V5CampaignError("campaign role is unknown") from exc


def _ledger_role(value: CampaignRoleV2) -> LedgerRole:
    return LedgerRole.PRIMARY if value is CampaignRoleV2.PRIMARY else LedgerRole.CORRECTION


def _attempt_kind(stage: TrialStageV2) -> V5AttemptKind:
    return {
        TrialStageV2.PRIMARY_NOVEL: V5AttemptKind.PRIMARY_NOVEL,
        TrialStageV2.PRIMARY_CONFIRM: V5AttemptKind.PRIMARY_CONFIRM,
        TrialStageV2.CORRECTION_NOVEL: V5AttemptKind.CORRECTION_NOVEL,
        TrialStageV2.MATCHED_ZERO: V5AttemptKind.MATCHED_ZERO,
        TrialStageV2.MATCHED_CORRECTED: V5AttemptKind.MATCHED_CORRECTED,
    }[stage]


def _candidate(value: CompleteCandidateV1 | Mapping[str, Any]) -> CompleteCandidateV1:
    try:
        return value if isinstance(value, CompleteCandidateV1) else CompleteCandidateV1.from_mapping(value)
    except Exception as exc:
        raise V5CampaignError("candidate is not a CompleteCandidateV1") from exc


def _candidate_token(candidate_key: str) -> int:
    # 31 bits keep the value valid for the signed integer wire contract.
    token = int(hashlib.sha256(candidate_key.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF
    return token or 1


def _controller_hash(controller: Mapping[str, Any]) -> str:
    return _sha(dict(controller))


def _student_t_ci90(differences: Sequence[float]) -> tuple[float, tuple[float, float], str]:
    values = tuple(_finite(value, "paired difference") for value in differences)
    if len(values) != MATCHED_PAIRS:
        raise V5CampaignError("paired report requires five differences")
    mean = math.fsum(values) / MATCHED_PAIRS
    try:
        from scipy.stats import t
        critical = float(t.ppf(0.95, 4))
    except Exception as exc:  # no statistical fallback is allowed
        raise V5CampaignError("Student-t CI provider is unavailable") from exc
    variance = math.fsum((value - mean) ** 2 for value in values) / 4.0
    half = critical * math.sqrt(variance / MATCHED_PAIRS)
    ci = (mean - half, mean + half)
    decision = "improvement" if ci[1] < 0.0 else "worse" if ci[0] > 0.0 else "inconclusive"
    return mean, ci, decision


def _strict_keys(
    value: Mapping[str, Any],
    allowed: set[str],
    name: str,
    *,
    optional: set[str] = frozenset(),
) -> None:
    if not isinstance(value, Mapping) or not set(value).issubset(allowed) or set(value) | optional != allowed:
        raise V5CampaignError(f"{name} schema keys differ")


@dataclass(frozen=True)
class CampaignConfigV2:
    raw: Mapping[str, Any]
    schema: str = CONFIG_SCHEMA
    version: int = CONFIG_VERSION

    def __post_init__(self) -> None:
        if self.schema != CONFIG_SCHEMA or self.version != CONFIG_VERSION or not isinstance(self.raw, Mapping):
            raise V5CampaignError("V5 campaign config schema/version differs")
        wire = self.raw.get("wire")
        if not isinstance(wire, Mapping) or tuple(wire.get("input_integer_registers", ())) != tuple(range(24, 40)) or tuple(wire.get("output_integer_registers", ())) != tuple(range(24, 35)) or tuple(wire.get("forbidden_output_integer_registers", ())) != tuple(range(35, 40)):
            raise V5CampaignError("V5 campaign config does not bind integer input 24..39")
        primary = self.raw.get("primary_campaign")
        correction = self.raw.get("correction_campaign")
        entry_mode = self.raw.get("entry_mode", ENTRY_MODE_ROLLOVER_CHAIN_V1)
        if entry_mode not in {ENTRY_MODE_ROLLOVER_CHAIN_V1, ENTRY_MODE_HOME_ONLY_V1}:
            raise V5CampaignError("V5 config entry mode is unknown")
        if not isinstance(primary, Mapping) or not isinstance(correction, Mapping):
            raise V5CampaignError("V5 campaign config campaign blocks are incomplete")
        if primary.get("exact_novel_target") != PRIMARY_NOVEL_TARGET:
            raise V5CampaignError("V5 primary exact target differs")
        if correction.get("exact_novel_target") != CORRECTION_NOVEL_TARGET:
            raise V5CampaignError("V5 correction exact target differs")
        rollover = self.raw.get("rollover")
        switch = self.raw.get("switch_admission")
        if not isinstance(rollover, Mapping):
            raise V5CampaignError("V5 config rollover block is missing")
        if entry_mode == ENTRY_MODE_ROLLOVER_CHAIN_V1 and rollover.get("one_step_prefetch") is not True:
            raise V5CampaignError("V5 rollover config requires one-step prefetch")
        if entry_mode == ENTRY_MODE_HOME_ONLY_V1 and rollover.get("one_step_prefetch") is not False:
            raise V5CampaignError("V5 Home-only config must disable chain prefetch")
        if not isinstance(switch, Mapping) or switch.get("performance_force_windows_blocking") is not False or switch.get("new_safety_thresholds_introduced") is not False:
            raise V5CampaignError("V5 config changes performance/safety admission")
        if tuple(primary.get("correction_weights", ())) != (0.0,) * 6 or primary.get("controller_dimensions") != 6 or primary.get("terminal_top_count") != TOP_COUNT or primary.get("terminal_total_repeats_per_candidate") != 5:
            raise V5CampaignError("V5 PRIMARY accounting contract differs")
        if correction.get("controller_candidate") != "fixed_primary_winner" or correction.get("optimized_dimensions") != 6 or correction.get("optimized_block") != "correction_only" or tuple(correction.get("matched_order", ())) != MATCHED_ORDER or correction.get("matched_zero_n") != MATCHED_PAIRS or correction.get("matched_corrected_n") != MATCHED_PAIRS or correction.get("home_after_each_trial") is not True or correction.get("paired_confidence_interval") != 0.9 or correction.get("automatic_promotion") is not False:
            raise V5CampaignError("V5 CORRECTION accounting contract differs")
        entry = self.raw.get("entry")
        if (
            not isinstance(entry, Mapping)
            or entry.get("path_starts_on_stable_five_newton_target_same_tick") is not True
            or entry.get("base_target_formula_0_to_4_s") != "5.0"
            or entry.get("base_target_after_4_s_n") != 5.0
            or tuple(entry.get("primary_metric_window_s", ())) != (0.0, 60.0)
            or tuple(entry.get("secondary_metric_window_s", ())) != (5.0, 60.0)
        ):
            raise V5CampaignError("V5 entry/primary metric contract differs")
        metric = self.raw.get("metric")
        if (
            not isinstance(metric, Mapping)
            or metric.get("primary_metric") != "evidence_0_60"
            or metric.get("secondary_metric") != "formal_5_60"
            or tuple(metric.get("evidence_window_s", ())) != (0.0, 60.0)
            or tuple(metric.get("formal_window_s", ())) != (5.0, 60.0)
        ):
            raise V5CampaignError("V5 metric windows differ")
        optimizer = self.raw.get("optimizer")
        challenger_kernels = tuple(optimizer.get("challenger_kernels", ())) if isinstance(optimizer, Mapping) else ()
        calibration_after = optimizer.get("kernel_calibration_after_exact_global_observations") if isinstance(optimizer, Mapping) else None
        if (
            not isinstance(optimizer, Mapping)
            or optimizer.get("feature_dimension") != 6
            or optimizer.get("normalization")
            != "fixed_design_bounds_not_observation_minmax"
            or optimizer.get("initial_kernel") != "matern52_ard"
            or (
                entry_mode == ENTRY_MODE_ROLLOVER_CHAIN_V1
                and (challenger_kernels != ("rbf_ard", "matern32_ard") or calibration_after != 24)
            )
            or (
                entry_mode == ENTRY_MODE_HOME_ONLY_V1
                and (challenger_kernels or calibration_after not in (None, 0))
            )
        ):
            raise V5CampaignError("V5 optimizer feature/kernel contract differs")
        freeze_gate = optimizer.get("kernel_freeze_gate")
        if (
            not isinstance(freeze_gate, Mapping)
            or freeze_gate.get("nlpd") != "strictly_lower"
            or freeze_gate.get("coverage") != "no_worse"
            or freeze_gate.get("low_tail_recall") != "no_worse"
            or freeze_gate.get("false_optimism") != "no_worse"
            or freeze_gate.get("replay_regret") != "no_worse"
        ):
            raise V5CampaignError("V5 kernel challenger gate differs")
        capability = self.raw.get("capability_acceptance")
        expected_capability_steps = (
            "no_motion_recipe",
            "no_contact_protocol",
            "same_candidate_single_rollover",
            "bounded_neighbor_single_rollover",
            "home_vs_rollover_five_paired_entries",
            "four_rollover_chain",
            "verified_home",
        )
        if entry_mode == ENTRY_MODE_HOME_ONLY_V1:
            if not isinstance(capability, Mapping) or capability.get("status") != "not_applicable_home_only":
                raise V5CampaignError("V5 Home-only capability block differs")
        elif (
            not isinstance(capability, Mapping)
            or capability.get("campaign_qualification") is not False
            or capability.get("epoch_qualification") is not False
            or capability.get("three_contact_qualification") is not False
            or tuple(capability.get("steps", ())) != expected_capability_steps
            or capability.get("home_vs_rollover_pairs") != 5
            or capability.get("confidence_interval") != 0.9
            or capability.get("equivalence_margin_n") != 0.05
        ):
            raise V5CampaignError("V5 one-shot capability acceptance contract differs")

    @classmethod
    def from_path(cls, path: Path | str) -> "CampaignConfigV2":
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise V5CampaignError("V5 campaign config cannot be read") from exc
        if not isinstance(value, Mapping):
            raise V5CampaignError("V5 campaign config is not an object")
        return cls(value, schema=value.get("schema"), version=value.get("version"))


@dataclass(frozen=True)
class CampaignIdentityV2:
    role: CampaignRoleV2
    campaign_fingerprint: str
    release_identity_sha256: str
    state_root: str
    observation_namespace: str
    fixed_controller_path: Mapping[str, Any] | None = None
    primary_winner_controller_sha256: str | None = None
    primary_closeout_sha256: str | None = None
    primary_ledger_head_sha256: str | None = None
    entry_mode: str = ENTRY_MODE_ROLLOVER_CHAIN_V1
    schema: str = IDENTITY_SCHEMA
    version: int = IDENTITY_VERSION

    def __post_init__(self) -> None:
        role = _role(self.role)
        object.__setattr__(self, "role", role)
        _require_sha(self.campaign_fingerprint, "campaign fingerprint")
        _require_sha(self.release_identity_sha256, "release identity")
        if self.schema != IDENTITY_SCHEMA or self.version != IDENTITY_VERSION:
            raise V5CampaignError("campaign identity schema/version differs")
        if not isinstance(self.observation_namespace, str) or not self.observation_namespace:
            raise V5CampaignError("observation namespace is empty")
        if self.entry_mode not in {ENTRY_MODE_ROLLOVER_CHAIN_V1, ENTRY_MODE_HOME_ONLY_V1}:
            raise V5CampaignError("V5 entry mode is unknown")
        if self.observation_namespace != f"{role.value}:{self.campaign_fingerprint}":
            raise V5CampaignError("observation namespace is not role-isolated")
        root = _resolved_root(self.state_root)
        object.__setattr__(self, "state_root", root)
        if role is CampaignRoleV2.PRIMARY:
            if any(value is not None for value in (self.fixed_controller_path, self.primary_winner_controller_sha256, self.primary_closeout_sha256, self.primary_ledger_head_sha256)):
                raise V5CampaignError("PRIMARY cannot carry a fixed correction winner")
        else:
            if not isinstance(self.fixed_controller_path, Mapping) or not self.fixed_controller_path:
                raise V5CampaignError("CORRECTION requires a fixed controller path")
            if _controller_hash(self.fixed_controller_path) != self.primary_winner_controller_sha256:
                raise V5CampaignError("primary winner controller hash does not bind fixed controller path")
            _require_sha(self.primary_closeout_sha256, "primary closeout hash")
            _require_sha(self.primary_ledger_head_sha256, "primary ledger head")
            object.__setattr__(self, "fixed_controller_path", dict(self.fixed_controller_path))

    @property
    def state_namespace(self) -> str:
        return f"{self.role.value}:{self.campaign_fingerprint}:{self.release_identity_sha256}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "role": self.role.value,
            "campaign_fingerprint": self.campaign_fingerprint,
            "release_identity_sha256": self.release_identity_sha256,
            "state_root": self.state_root,
            "observation_namespace": self.observation_namespace,
            "fixed_controller_path": None if self.fixed_controller_path is None else dict(self.fixed_controller_path),
            "primary_winner_controller_sha256": self.primary_winner_controller_sha256,
            "primary_closeout_sha256": self.primary_closeout_sha256,
            "primary_ledger_head_sha256": self.primary_ledger_head_sha256,
            "entry_mode": self.entry_mode,
        }


@dataclass(frozen=True)
class ProposalReceiptV2:
    acquisition: ProposalMethodV2
    block: str
    pool_candidate_keys: tuple[str, ...]
    selected_candidate_key: str
    pending_candidate_keys: tuple[str, ...]
    campaign_fingerprint: str
    role: CampaignRoleV2
    provider: str
    provider_receipt: Mapping[str, Any] | None = None
    schema: str = PROPOSAL_SCHEMA
    version: int = PROPOSAL_VERSION

    def __post_init__(self) -> None:
        if self.schema != PROPOSAL_SCHEMA or self.version != PROPOSAL_VERSION:
            raise V5CampaignError("proposal receipt schema/version differs")
        if not isinstance(self.acquisition, ProposalMethodV2) or not isinstance(self.role, CampaignRoleV2):
            raise V5CampaignError("proposal receipt enums are not typed")
        _require_sha(self.campaign_fingerprint, "proposal fingerprint")
        if not self.block or not self.provider:
            raise V5CampaignError("proposal receipt identity is incomplete")
        keys = tuple(str(item) for item in self.pool_candidate_keys)
        pending = tuple(str(item) for item in self.pending_candidate_keys)
        if any(type(item) is not str for item in tuple(self.pool_candidate_keys) + tuple(self.pending_candidate_keys)) or len(set(keys)) != len(keys) or len(set(pending)) != len(pending) or set(keys) & set(pending) or any(not item for item in keys) or any(not item for item in pending) or len(pending) > 4:
            raise V5CampaignError("proposal pool/pending identity is invalid")
        if self.acquisition in (ProposalMethodV2.DETERMINISTIC_SOBOL, ProposalMethodV2.GLOBAL_SOBOL, ProposalMethodV2.QLOGNEI):
            if len(keys) != SOBOL_POOL_SIZE or self.selected_candidate_key not in keys:
                raise V5CampaignError("Sobol/qLogNEI receipt is not exactly a fresh 128 pool")
        elif keys or self.selected_candidate_key == "":
            raise V5CampaignError("fixed replay receipt has a pool")
        if self.acquisition is ProposalMethodV2.QLOGNEI and not isinstance(self.provider_receipt, Mapping):
            raise V5CampaignError("qLogNEI receipt lacks provider evidence")
        object.__setattr__(self, "pool_candidate_keys", keys)
        object.__setattr__(self, "pending_candidate_keys", pending)
        if self.provider_receipt is not None:
            object.__setattr__(self, "provider_receipt", dict(self.provider_receipt))

    @property
    def pool_sha256(self) -> str:
        return _sha(list(self.pool_candidate_keys))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "acquisition": self.acquisition.value,
            "block": self.block,
            "pool_candidate_keys": list(self.pool_candidate_keys),
            "pool_sha256": self.pool_sha256,
            "selected_candidate_key": self.selected_candidate_key,
            "pending_candidate_keys": list(self.pending_candidate_keys),
            "campaign_fingerprint": self.campaign_fingerprint,
            "role": self.role.value,
            "provider": self.provider,
            "provider_receipt": None if self.provider_receipt is None else dict(self.provider_receipt),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProposalReceiptV2":
        if not isinstance(value, Mapping):
            raise V5CampaignError("proposal receipt is not a mapping")
        _strict_keys(value, {"schema", "version", "acquisition", "block", "pool_candidate_keys", "pool_sha256", "selected_candidate_key", "pending_candidate_keys", "campaign_fingerprint", "role", "provider", "provider_receipt"}, "proposal receipt")
        try:
            receipt = cls(
                ProposalMethodV2(value["acquisition"]),
                str(value["block"]),
                tuple(value.get("pool_candidate_keys", ())),
                str(value["selected_candidate_key"]),
                tuple(value.get("pending_candidate_keys", ())),
                str(value["campaign_fingerprint"]),
                CampaignRoleV2(value["role"]),
                str(value["provider"]),
                value.get("provider_receipt"),
                schema=value["schema"],
                version=value["version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise V5CampaignError("proposal receipt mapping is invalid") from exc
        if value.get("pool_sha256") != receipt.pool_sha256:
            raise V5CampaignError("proposal pool hash differs")
        return receipt


@dataclass(frozen=True)
class V5TrialPlan:
    trial_id: str
    attempt_id: str
    attempt_kind: V5AttemptKind
    candidate: CompleteCandidateV1
    candidate_key: str
    candidate_token: int
    budget_ordinal: int
    proposal_receipt: ProposalReceiptV2
    stage: TrialStageV2
    requires_home: bool
    packable: bool
    wire_epoch: int
    candidate_ordinal: int
    dispatch_index: int
    repeat_of_candidate_key: str | None = None
    prefetched: bool = False
    entry_mode: str = ENTRY_MODE_ROLLOVER_CHAIN_V1
    schema: str = PLAN_SCHEMA
    version: int = PLAN_VERSION

    def __post_init__(self) -> None:
        if self.schema != PLAN_SCHEMA or self.version != PLAN_VERSION or not isinstance(self.attempt_kind, V5AttemptKind) or not isinstance(self.stage, TrialStageV2) or not isinstance(self.candidate, CompleteCandidateV1) or not isinstance(self.proposal_receipt, ProposalReceiptV2):
            raise V5CampaignError("trial plan typed schema differs")
        _positive_int(self.wire_epoch, "V5 wire session epoch")
        _positive_int(self.budget_ordinal, "budget ordinal")
        _positive_int(self.candidate_ordinal, "candidate ordinal")
        _positive_int(self.dispatch_index, "dispatch index")
        _positive_int(self.candidate_token, "candidate token")
        if self.entry_mode not in {ENTRY_MODE_ROLLOVER_CHAIN_V1, ENTRY_MODE_HOME_ONLY_V1}:
            raise V5CampaignError("trial plan entry mode is unknown")
        if type(self.requires_home) is not bool or type(self.packable) is not bool:
            raise V5CampaignError("trial plan home/packing flags are not typed")
        if self.candidate_key != self.candidate.candidate_key:
            raise V5CampaignError("trial plan candidate key differs")
        if self.candidate_token != _candidate_token(self.candidate_key):
            raise V5CampaignError("trial plan candidate token is not deterministic")
        if self.attempt_kind is not _attempt_kind(self.stage):
            raise V5CampaignError("trial plan kind/stage differs")
        if self.stage in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL):
            if self.entry_mode == ENTRY_MODE_HOME_ONLY_V1:
                if self.packable or not self.requires_home:
                    raise V5CampaignError("HOME-only novel plan boundary differs")
            elif not self.packable or self.requires_home:
                raise V5CampaignError("ordinary novel plan packing contract differs")
        else:
            if self.packable or not self.requires_home:
                raise V5CampaignError("confirm/matched plans require Home")
        if not self.trial_id or not self.attempt_id or any(ch in self.trial_id + self.attempt_id for ch in "\r\n"):
            raise V5CampaignError("trial plan IDs are invalid")
        if self.proposal_receipt.selected_candidate_key != self.candidate_key:
            raise V5CampaignError("proposal selected key differs")
        if type(self.prefetched) is not bool:
            raise V5CampaignError("prefetch flag is not typed")

    @property
    def candidate_identity(self) -> dict[str, Any]:
        return {
            "schema": "step6.autotune/figure8-v5-candidate-identity-v1",
            "version": 1,
            "epoch": self.wire_epoch,
            "ordinal": self.candidate_ordinal,
            "attempt_kind": int(self.attempt_kind),
            "candidate_token": self.candidate_token,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "trial_id": self.trial_id,
            "attempt_id": self.attempt_id,
            "attempt_kind": int(self.attempt_kind),
            "candidate": self.candidate.as_dict(),
            "candidate_key": self.candidate_key,
            "candidate_token": self.candidate_token,
            "budget_ordinal": self.budget_ordinal,
            "proposal_receipt": self.proposal_receipt.as_dict(),
            "stage": self.stage.value,
            "requires_home": self.requires_home,
            "packable": self.packable,
            "epoch": self.wire_epoch,
            "candidate_identity": self.candidate_identity,
            "candidate_ordinal": self.candidate_ordinal,
            "dispatch_index": self.dispatch_index,
            "entry_mode": self.entry_mode,
            "repeat_of_candidate_key": self.repeat_of_candidate_key,
            "prefetched": self.prefetched,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "V5TrialPlan":
        allowed = {"schema", "version", "trial_id", "attempt_id", "attempt_kind", "candidate", "candidate_key", "candidate_token", "budget_ordinal", "proposal_receipt", "stage", "requires_home", "packable", "epoch", "candidate_identity", "candidate_ordinal", "dispatch_index", "entry_mode", "repeat_of_candidate_key", "prefetched"}
        keys = set(value)
        if keys != allowed and keys != allowed - {"entry_mode"}:
            raise V5CampaignError("trial plan schema keys differ")
        try:
            parsed = cls(
                str(value["trial_id"]),
                str(value["attempt_id"]),
                V5AttemptKind(value["attempt_kind"]),
                _candidate(value["candidate"]),
                str(value["candidate_key"]),
                value["candidate_token"],
                value["budget_ordinal"],
                ProposalReceiptV2.from_mapping(value["proposal_receipt"]),
                TrialStageV2(value["stage"]),
                value["requires_home"],
                value["packable"],
                value["epoch"],
                value["candidate_ordinal"],
                value["dispatch_index"],
                value.get("repeat_of_candidate_key"),
                value.get("prefetched", False),
                value.get("entry_mode", ENTRY_MODE_ROLLOVER_CHAIN_V1),
                schema=value["schema"],
                version=value["version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise V5CampaignError("trial plan mapping is invalid") from exc
        identity = value["candidate_identity"]
        if not isinstance(identity, Mapping):
            raise V5CampaignError("trial plan candidate identity is absent")
        _strict_keys(identity, {"schema", "version", "epoch", "ordinal", "attempt_kind", "candidate_token"}, "trial plan candidate identity")
        if identity != parsed.candidate_identity:
            raise V5CampaignError("trial plan candidate identity differs")
        return parsed


@dataclass(frozen=True)
class CampaignOutcomeV2:
    trial_id: str
    candidate_key: str
    stage: TrialStageV2
    disposition: OutcomeV2
    formal_mae_n: float | None
    boundary_mode: BoundaryMode | None
    record_sha256: str | None = None
    failure_signature: str | None = None
    home_verified: bool = False
    tell_state: TellState | None = None
    physical_ledger_path: str | None = None
    # A typed recoverable owner failure consumed one physical dispatch slot,
    # even though it intentionally carries no objective/tell evidence.
    dispatch_budget_counted: bool = False
    schema: str = "step6.autotune/figure8-v5-campaign-outcome-v2"
    version: int = CAMPAIGN_VERSION

    def __post_init__(self) -> None:
        if self.schema != "step6.autotune/figure8-v5-campaign-outcome-v2" or self.version != CAMPAIGN_VERSION or not self.trial_id or not self.candidate_key or not isinstance(self.stage, TrialStageV2) or not isinstance(self.disposition, OutcomeV2) or type(self.home_verified) is not bool or type(self.dispatch_budget_counted) is not bool:
            raise V5CampaignError("campaign outcome is incomplete")
        if self.formal_mae_n is not None:
            _finite(self.formal_mae_n, "formal MAE")
        if self.record_sha256 is not None:
            _require_sha(self.record_sha256, "outcome record hash")
        if self.boundary_mode is not None and not isinstance(self.boundary_mode, BoundaryMode):
            raise V5CampaignError("outcome boundary mode is not typed")
        if self.tell_state is not None and not isinstance(self.tell_state, TellState):
            raise V5CampaignError("outcome tell state is not typed")
        if self.physical_ledger_path is not None:
            if type(self.physical_ledger_path) is not str or not self.physical_ledger_path:
                raise V5CampaignError("physical ledger path is not a resolved non-empty path")
            object.__setattr__(self, "physical_ledger_path", _resolved_root(self.physical_ledger_path))
        if self.disposition is OutcomeV2.PHYSICALLY_ELIGIBLE:
            if self.formal_mae_n is None or self.boundary_mode is None or self.record_sha256 is None or self.failure_signature is not None or self.physical_ledger_path is None:
                raise V5CampaignError("eligible outcome lacks complete physical evidence")
            if self.stage in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL) and self.tell_state is not TellState.COMMITTED:
                raise V5CampaignError("novel eligible outcome lacks COMMITTED tell state")
            if self.stage in (TrialStageV2.PRIMARY_CONFIRM, TrialStageV2.MATCHED_ZERO, TrialStageV2.MATCHED_CORRECTED) and self.tell_state is not None:
                raise V5CampaignError("confirm/matched outcome cannot carry tell state")
            if self.home_verified != (self.boundary_mode is BoundaryMode.HOME):
                raise V5CampaignError("HOME evidence flag differs from boundary")
        elif self.disposition is OutcomeV2.INELIGIBLE:
            if self.formal_mae_n is not None or self.tell_state is not None or self.failure_signature is not None or self.record_sha256 is None or self.boundary_mode is None or self.physical_ledger_path is None:
                raise V5CampaignError("ineligible outcome carries objective/tell/failure evidence")
            if self.home_verified != (self.boundary_mode is BoundaryMode.HOME):
                raise V5CampaignError("ineligible HOME evidence flag differs from boundary")
        else:
            if any(value is not None for value in (self.formal_mae_n, self.boundary_mode, self.record_sha256, self.tell_state, self.physical_ledger_path)) or self.home_verified:
                raise V5CampaignError("non-physical outcome carries physical admission evidence")
            if self.stage not in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL) or not isinstance(self.failure_signature, str) or not self.failure_signature:
                raise V5CampaignError("censor/failure outcome is not an ordinary novel disposition")
            if self.dispatch_budget_counted and self.disposition is not OutcomeV2.FAILURE:
                raise V5CampaignError("only a recoverable FAILURE may consume dispatch budget")

    @property
    def counted_exact(self) -> bool:
        return self.disposition is OutcomeV2.PHYSICALLY_ELIGIBLE and self.tell_state in (None, TellState.COMMITTED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "trial_id": self.trial_id,
            "candidate_key": self.candidate_key,
            "stage": self.stage.value,
            "disposition": self.disposition.value,
            "formal_mae_n": self.formal_mae_n,
            "boundary_mode": None if self.boundary_mode is None else self.boundary_mode.value,
            "record_sha256": self.record_sha256,
            "failure_signature": self.failure_signature,
            "home_verified": self.home_verified,
            "tell_state": None if self.tell_state is None else self.tell_state.value,
            "physical_ledger_path": self.physical_ledger_path,
            "dispatch_budget_counted": self.dispatch_budget_counted,
        }

    @property
    def primary_mae_n(self) -> float | None:
        """V5 primary [0,60) objective retained in the legacy field slot."""

        return self.formal_mae_n

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CampaignOutcomeV2":
        _strict_keys(value, {"schema", "version", "trial_id", "candidate_key", "stage", "disposition", "formal_mae_n", "boundary_mode", "record_sha256", "failure_signature", "home_verified", "tell_state", "physical_ledger_path", "dispatch_budget_counted"}, "campaign outcome", optional={"dispatch_budget_counted"})
        try:
            return cls(
                value["trial_id"],
                value["candidate_key"],
                TrialStageV2(value["stage"]),
                OutcomeV2(value["disposition"]),
                value.get("formal_mae_n"),
                None if value.get("boundary_mode") is None else BoundaryMode(value["boundary_mode"]),
                value.get("record_sha256"),
                value.get("failure_signature"),
                value["home_verified"],
                None if value.get("tell_state") is None else TellState(value["tell_state"]),
                value.get("physical_ledger_path"),
                value.get("dispatch_budget_counted", False),
                schema=value["schema"],
                version=value["version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise V5CampaignError("campaign outcome mapping is invalid") from exc


@dataclass(frozen=True)
class CampaignReportV2:
    role: CampaignRoleV2
    campaign_fingerprint: str
    exact_novel_count: int
    winner_candidate_key: str | None
    winner_candidate: Mapping[str, Any] | None
    corrected_best_candidate_key: str | None = None
    paired_differences: tuple[float, ...] = ()
    paired_mean_difference_n: float | None = None
    paired_ci90_n: tuple[float, float] | None = None
    paired_df: int | None = None
    automatic_promotion: bool = False
    decision: str | None = None
    top_candidates: tuple[Mapping[str, Any], ...] = ()
    corrected_best_candidate: Mapping[str, Any] | None = None
    primary_winner_controller_sha256: str | None = None
    matched_zero_maes_n: tuple[float, ...] = ()
    matched_corrected_maes_n: tuple[float, ...] = ()
    schema: str = REPORT_SCHEMA
    version: int = REPORT_VERSION

    def __post_init__(self) -> None:
        if self.schema != REPORT_SCHEMA or self.version != REPORT_VERSION or not isinstance(self.role, CampaignRoleV2):
            raise V5CampaignError("campaign report schema/type differs")
        _require_sha(self.campaign_fingerprint, "report fingerprint")
        _nonnegative_int(self.exact_novel_count, "report exact count")
        required_count = PRIMARY_NOVEL_TARGET if self.role is CampaignRoleV2.PRIMARY else CORRECTION_NOVEL_TARGET
        if self.exact_novel_count != required_count:
            raise V5CampaignError("campaign report exact count differs from its role target")
        if self.automatic_promotion is not False:
            raise V5CampaignError("automatic promotion must remain false")
        if self.paired_df is not None and self.paired_df != 4:
            raise V5CampaignError("paired report degrees of freedom must be four")
        if self.paired_ci90_n is not None and len(self.paired_ci90_n) != 2:
            raise V5CampaignError("paired report CI must have two bounds")
        if len({row.get("candidate_key") for row in self.top_candidates}) != len(self.top_candidates):
            raise V5CampaignError("top candidate report has duplicate candidates")
        for row in self.top_candidates:
            _strict_keys(row, {"candidate_key", "original_mae_n", "confirmation_maes_n", "total_n", "repeated_mean_n"}, "top candidate report")
            if not isinstance(row["candidate_key"], str) or not row["candidate_key"] or not isinstance(row["confirmation_maes_n"], (list, tuple)) or len(row["confirmation_maes_n"]) != CONFIRMATIONS_PER_TOP or row["total_n"] != 5:
                raise V5CampaignError("top candidate report does not contain n=5")
            _finite(row["original_mae_n"], "top candidate original MAE")
            for value in row["confirmation_maes_n"]:
                _finite(value, "top candidate confirmation MAE")
            _finite(row["repeated_mean_n"], "top candidate repeated mean")
        if self.role is CampaignRoleV2.PRIMARY:
            if self.winner_candidate_key is None or self.winner_candidate is None or self.corrected_best_candidate_key is not None or self.corrected_best_candidate is not None or self.primary_winner_controller_sha256 is not None or len(self.top_candidates) != TOP_COUNT or not isinstance(self.decision, str) or not self.decision or self.paired_differences or self.paired_mean_difference_n is not None or self.paired_ci90_n is not None or self.paired_df is not None or self.matched_zero_maes_n or self.matched_corrected_maes_n:
                raise V5CampaignError("PRIMARY report carries correction fields")
        else:
            _require_sha(self.primary_winner_controller_sha256, "report primary winner controller hash")
            if self.winner_candidate_key is not None or self.winner_candidate is not None or self.corrected_best_candidate_key is None or self.corrected_best_candidate is None or len(self.paired_differences) != MATCHED_PAIRS or len(self.matched_zero_maes_n) != MATCHED_PAIRS or len(self.matched_corrected_maes_n) != MATCHED_PAIRS or self.paired_df != 4 or self.paired_ci90_n is None or self.decision not in {"improvement", "worse", "inconclusive"}:
                raise V5CampaignError("CORRECTION report is incomplete")
            if self.top_candidates:
                raise V5CampaignError("CORRECTION report carries PRIMARY top candidates")
            for value in (*self.paired_differences, *self.matched_zero_maes_n, *self.matched_corrected_maes_n, self.paired_mean_difference_n, *self.paired_ci90_n):
                if value is None:
                    raise V5CampaignError("CORRECTION report has a missing numeric value")
                _finite(value, "CORRECTION report statistic")
        if self.role is CampaignRoleV2.PRIMARY:
            winner = _candidate(self.winner_candidate)
            if winner.candidate_key != self.winner_candidate_key or winner.as_dict() != dict(self.winner_candidate):
                raise V5CampaignError("PRIMARY winner candidate identity differs")
        else:
            corrected = _candidate(self.corrected_best_candidate)
            if corrected.candidate_key != self.corrected_best_candidate_key or corrected.as_dict() != dict(self.corrected_best_candidate):
                raise V5CampaignError("CORRECTION best candidate identity differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "role": self.role.value,
            "campaign_fingerprint": self.campaign_fingerprint,
            "exact_novel_count": self.exact_novel_count,
            "winner_candidate_key": self.winner_candidate_key,
            "winner_candidate": None if self.winner_candidate is None else dict(self.winner_candidate),
            "corrected_best_candidate_key": self.corrected_best_candidate_key,
            "paired_differences": list(self.paired_differences),
            "paired_mean_difference_n": self.paired_mean_difference_n,
            "paired_ci90_n": None if self.paired_ci90_n is None else list(self.paired_ci90_n),
            "paired_df": self.paired_df,
            "automatic_promotion": False,
            "decision": self.decision,
            "top_candidates": [dict(row) for row in self.top_candidates],
            "corrected_best_candidate": None if self.corrected_best_candidate is None else dict(self.corrected_best_candidate),
            "primary_winner_controller_sha256": self.primary_winner_controller_sha256,
            "matched_zero_maes_n": list(self.matched_zero_maes_n),
            "matched_corrected_maes_n": list(self.matched_corrected_maes_n),
        }


class _DecisionLedgerV2:
    """Small strict JSONL authority used only by the V5 campaign layer."""

    def __init__(self, path: Path, identity: CampaignIdentityV2, *, create: bool) -> None:
        self.path = Path(path)
        self.identity = identity
        if self.path.is_symlink():
            raise V5CampaignError("decision ledger must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if create and not self.path.exists():
            self._create_header()
        if not self.path.is_file():
            raise V5CampaignError("decision ledger is absent")
        self.rows, self.head_sha256 = self.cold_verify()

    def _stat_signature(self) -> tuple[int, int, int, int]:
        try:
            stat = self.path.stat()
        except OSError as exc:
            raise V5CampaignError("decision ledger stat failed") from exc
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _header(self) -> dict[str, Any]:
        row = {
            "schema": LEDGER_SCHEMA,
            "version": LEDGER_VERSION,
            "event_schema": LEDGER_EVENT_SCHEMA,
            "event_version": CAMPAIGN_VERSION,
            "record_type": "header",
            "campaign_fingerprint": self.identity.campaign_fingerprint,
            "release_identity_sha256": self.identity.release_identity_sha256,
            "role": self.identity.role.value,
            "namespace": self.identity.observation_namespace,
            "previous_sha256": GENESIS_SHA256,
        }
        row["row_sha256"] = _sha({key: value for key, value in row.items() if key != "row_sha256"})
        return row

    def _create_header(self) -> None:
        try:
            with self.path.open("x", encoding="utf-8") as stream:
                stream.write(_canonical(self._header()).decode("utf-8") + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass

    def _read(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise V5CampaignError("decision ledger cannot be read") from exc
        if not lines:
            raise V5CampaignError("decision ledger is empty")
        result: list[dict[str, Any]] = []
        for number, line in enumerate(lines, 1):
            if not line.strip():
                raise V5CampaignError(f"decision ledger has an empty row at {number}")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise V5CampaignError(f"decision ledger row {number} is invalid JSON") from exc
            if not isinstance(value, dict):
                raise V5CampaignError(f"decision ledger row {number} is not an object")
            result.append(value)
        return result

    def cold_verify(self) -> tuple[list[dict[str, Any]], str]:
        rows = self._read()
        if rows[0] != self._header():
            raise V5CampaignError("decision ledger header/schema/namespace differs")
        previous = GENESIS_SHA256
        for index, row in enumerate(rows):
            if row.get("schema") != LEDGER_SCHEMA or row.get("version") != LEDGER_VERSION or row.get("event_schema") != LEDGER_EVENT_SCHEMA or row.get("event_version") != CAMPAIGN_VERSION or row.get("campaign_fingerprint") != self.identity.campaign_fingerprint or row.get("release_identity_sha256") != self.identity.release_identity_sha256 or row.get("role") != self.identity.role.value or row.get("namespace") != self.identity.observation_namespace or row.get("previous_sha256") != previous or row.get("row_sha256") != _sha({key: value for key, value in row.items() if key != "row_sha256"}):
                raise V5CampaignError(f"decision ledger hash/namespace differs at row {index}")
            previous = row["row_sha256"]
        self._writer_stat = self._stat_signature()
        return rows, previous

    def verify_writer_state(self) -> None:
        if self._stat_signature() != self._writer_stat:
            raise V5CampaignError("decision ledger changed outside the single writer")

    def append(self, record_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not record_type or not isinstance(payload, Mapping):
            raise V5CampaignError("decision ledger event is incomplete")
        self.verify_writer_state()
        head = self.head_sha256
        row = {
            "schema": LEDGER_SCHEMA,
            "version": LEDGER_VERSION,
            "event_schema": LEDGER_EVENT_SCHEMA,
            "event_version": CAMPAIGN_VERSION,
            "record_type": record_type,
            "campaign_fingerprint": self.identity.campaign_fingerprint,
            "release_identity_sha256": self.identity.release_identity_sha256,
            "role": self.identity.role.value,
            "namespace": self.identity.observation_namespace,
            "previous_sha256": head,
            **dict(payload),
        }
        row["row_sha256"] = _sha({key: value for key, value in row.items() if key != "row_sha256"})
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(_canonical(row).decode("utf-8") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.rows = [*self.rows, row]
        self.head_sha256 = row["row_sha256"]
        self._writer_stat = self._stat_signature()
        return row


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(_canonical(value).decode("utf-8") + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class CampaignPaused(V5CampaignError):
    """Three identical ordinary failure signatures paused planning."""


class _NoopProvider:
    def __call__(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        raise V5CampaignError("qLogNEI provider is unavailable; no fallback is allowed")


class V5CampaignV2:
    """One isolated PRIMARY or CORRECTION campaign namespace."""

    def __init__(self, identity: CampaignIdentityV2, *, qlognei_provider: Callable[..., Mapping[str, Any]] | None = None, cursor_seed: int = 6016, wire_session_epoch: int = V5_WIRE_EPOCH, create: bool = False) -> None:
        if not isinstance(identity, CampaignIdentityV2):
            raise V5CampaignError("campaign requires typed CampaignIdentityV2")
        self.identity = identity
        self.qlognei_provider = qlognei_provider
        self.wire_session_epoch = _positive_int(
            wire_session_epoch, "V5 wire session epoch"
        )
        root = Path(identity.state_root)
        if create:
            if root.exists() and any(root.iterdir()):
                raise V5CampaignError("campaign state root is not fresh")
            root.mkdir(parents=True, exist_ok=True)
            self._write_marker()
        else:
            self._check_marker()
        self.decision_ledger_path = root / "decision.jsonl"
        self.sobol_cursor_path = root / "sobol.json"
        self.checkpoint_path = root / "checkpoint.json"
        self.report_path = root / "report.json"
        self._ledger = _DecisionLedgerV2(self.decision_ledger_path, identity, create=create)
        try:
            self._cursor = PersistedSobolCursorV1(self.sobol_cursor_path, seed=cursor_seed)
        except Exception as exc:
            raise V5CampaignError("Sobol cursor schema/state differs") from exc
        self._plans: dict[str, V5TrialPlan] = {}
        self._outcomes: dict[str, CampaignOutcomeV2] = {}
        self._consumed: dict[str, tuple[str, str | None]] = {}
        self._prefetch_discarded: set[str] = set()
        self._prefetch_activated: set[str] = set()
        self._paused = False
        self._pause_recorded = False
        self._failure_signature: str | None = None
        self._failure_streak = 0
        self._closeout: CampaignReportV2 | None = None
        # An extension epoch may carry a cold-verified active set from its
        # parent.  These rows are optimizer-only priors: they never count as
        # local exact attempts, never enter tell_exact, and never alter the
        # epoch's 200-attempt accounting.
        self._seed_observations = self._load_seed_observations(root)
        self._rebuild()
        self._check_checkpoint()
        active = self._active_plans()
        if active and any(
            plan.wire_epoch != self.wire_session_epoch for plan in active
        ):
            raise V5CampaignError(
                "active plan belongs to another live session epoch; resume only from a safe checkpoint"
            )

    @classmethod
    def create(cls, identity: CampaignIdentityV2, *, qlognei_provider: Callable[..., Mapping[str, Any]] | None = None, cursor_seed: int = 6016, wire_session_epoch: int = V5_WIRE_EPOCH) -> "V5CampaignV2":
        return cls(identity, qlognei_provider=qlognei_provider, cursor_seed=cursor_seed, wire_session_epoch=wire_session_epoch, create=True)

    @classmethod
    def resume(cls, identity: CampaignIdentityV2, *, qlognei_provider: Callable[..., Mapping[str, Any]] | None = None, wire_session_epoch: int = V5_WIRE_EPOCH) -> "V5CampaignV2":
        return cls(identity, qlognei_provider=qlognei_provider, wire_session_epoch=wire_session_epoch, create=False)

    @classmethod
    def from_config(cls, config_path: Path | str, identity: CampaignIdentityV2, *, qlognei_provider: Callable[..., Mapping[str, Any]] | None = None, cursor_seed: int = 6016, wire_session_epoch: int = V5_WIRE_EPOCH) -> "V5CampaignV2":
        CampaignConfigV2.from_path(config_path)
        return cls.create(identity, qlognei_provider=qlognei_provider, cursor_seed=cursor_seed, wire_session_epoch=wire_session_epoch)

    def _marker(self) -> dict[str, Any]:
        active_set = self._seed_path(Path(self.identity.state_root))
        active_set_sha = None
        if active_set.is_file():
            try:
                active_set_sha = canonical_sha256(json.loads(active_set.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise V5CampaignError("extension active-set seed is unreadable")
        return {"schema": CAMPAIGN_SCHEMA, "version": CAMPAIGN_VERSION, "identity": self.identity.as_dict(), "namespace": self.identity.state_namespace, "seed_active_set_sha256": active_set_sha}

    def _write_marker(self) -> None:
        _atomic_json(Path(self.identity.state_root) / "namespace.json", self._marker())

    def _check_marker(self) -> None:
        path = Path(self.identity.state_root) / "namespace.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise V5CampaignError("campaign state root marker is absent or invalid") from exc
        if value != self._marker():
            raise V5CampaignError("campaign state root is shared or identity changed")

    @staticmethod
    def _seed_path(root: Path) -> Path:
        candidates = (
            Path(root) / "active_set.json",
            Path(root).parent / "active_set.json",
            Path(root).parent.parent / "active_set.json",
        )
        return next((path for path in candidates if path.is_file()), candidates[0])

    @classmethod
    def _load_seed_observations(cls, root: Path) -> tuple[dict[str, Any], ...]:
        path = cls._seed_path(Path(root))
        if not path.is_file():
            return ()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise V5CampaignError("extension active-set seed is unreadable") from exc
        try:
            try:
                from step6_figure8_autotune_v1.v5_extension import verify_active_set
            except ModuleNotFoundError:  # pragma: no cover
                from tools.step6_figure8_autotune_v1.v5_extension import verify_active_set
            verified = verify_active_set(value)
        except Exception as exc:
            raise V5CampaignError("extension active-set seed is not cold-valid") from exc
        return tuple(
            {
                "candidate": dict(row["candidate"]),
                "candidate_key": str(row["candidate_key"]),
                "mae_n": float(row["mean_n"]),
                "observation_id": str(row["observation_id"]),
            }
            for row in verified["observations"]
        )

    def _check_identity(self, value: Mapping[str, Any]) -> None:
        if value.get("campaign_fingerprint") != self.identity.campaign_fingerprint or value.get("release_identity_sha256") != self.identity.release_identity_sha256 or value.get("role") != self.identity.role.value or value.get("namespace") != self.identity.observation_namespace:
            raise V5CampaignError("nested campaign identity crosses namespace")

    def _validate_plan_contract(self, plan: V5TrialPlan) -> None:
        if self.identity.role is CampaignRoleV2.PRIMARY:
            if plan.stage not in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.PRIMARY_CONFIRM) or plan.candidate.correction_weights != (0.0,) * 6 or plan.proposal_receipt.block != "controller_path":
                raise V5CampaignError("PRIMARY plan role/stage/candidate contract differs")
        else:
            if plan.stage not in (TrialStageV2.CORRECTION_NOVEL, TrialStageV2.MATCHED_ZERO, TrialStageV2.MATCHED_CORRECTED) or plan.candidate.controller_path != self.identity.fixed_controller_path or plan.proposal_receipt.block not in {"correction", "controller_path"}:
                raise V5CampaignError("CORRECTION plan role/stage/controller contract differs")
            if plan.stage is TrialStageV2.MATCHED_ZERO and plan.candidate.correction_weights != (0.0,) * 6:
                raise V5CampaignError("CORRECTION zero match is not fixed zero")
            if plan.stage in (TrialStageV2.CORRECTION_NOVEL, TrialStageV2.MATCHED_CORRECTED) and math.fsum(abs(value) for value in plan.candidate.correction_weights) == 0.0:
                raise V5CampaignError("CORRECTION nonzero plan lacks a correction block")

    def _validate_outcome_binding(self, plan: V5TrialPlan, outcome: CampaignOutcomeV2) -> None:
        if outcome.stage is not plan.stage or outcome.candidate_key != plan.candidate_key or outcome.trial_id != plan.trial_id:
            raise V5CampaignError("campaign outcome stage/identity differs from its plan")
        if outcome.trial_id not in self._consumed:
            raise V5CampaignError("campaign outcome is not tied to a consumed plan")
        _consume_key, consume_record_sha = self._consumed[outcome.trial_id]
        if outcome.record_sha256 != consume_record_sha:
            raise V5CampaignError("campaign outcome record hash differs from consume evidence")
        physical = outcome.disposition in (OutcomeV2.PHYSICALLY_ELIGIBLE, OutcomeV2.INELIGIBLE)
        if physical != (outcome.physical_ledger_path is not None) or physical != (outcome.record_sha256 is not None):
            raise V5CampaignError("physical outcome lacks matching ledger evidence")
        if not physical and (outcome.physical_ledger_path is not None or consume_record_sha is not None):
            raise V5CampaignError("non-physical outcome carries physical consume evidence")
        if outcome.dispatch_budget_counted and (
            outcome.disposition is not OutcomeV2.FAILURE
            or plan.entry_mode != ENTRY_MODE_HOME_ONLY_V1
            or not plan.requires_home
            or plan.packable
            or plan.stage not in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL)
        ):
            raise V5CampaignError(
                "dispatch-budget continuation is only valid for a serial HOME-only novel plan"
            )

    def _rebuild(self) -> None:
        rows, _head = self._ledger.cold_verify()
        self._plans.clear()
        self._outcomes.clear()
        self._consumed.clear()
        self._prefetch_discarded.clear()
        self._prefetch_activated.clear()
        self._paused = False
        self._pause_recorded = False
        self._failure_signature = None
        self._failure_streak = 0
        self._budget_monotonic = False
        self._closeout = None
        token_keys: dict[int, str] = {}
        for row in rows[1:]:
            self._check_identity(row)
            kind = row.get("record_type")
            if kind == "plan":
                _strict_keys(row, {"schema", "version", "event_schema", "event_version", "record_type", "campaign_fingerprint", "release_identity_sha256", "role", "namespace", "previous_sha256", "row_sha256", "plan", "candidate_key", "budget_ordinal"}, "plan decision row")
                plan = V5TrialPlan.from_mapping(row.get("plan", {}))
                if row.get("candidate_key") != plan.candidate_key or row.get("budget_ordinal") != plan.budget_ordinal:
                    raise V5CampaignError("plan decision identity differs")
                self._validate_plan_contract(plan)
                receipt = plan.proposal_receipt
                if receipt.campaign_fingerprint != self.identity.campaign_fingerprint or receipt.role is not self.identity.role:
                    raise V5CampaignError("plan proposal crosses campaign namespace")
                if plan.trial_id in self._plans or plan.trial_id in self._outcomes:
                    raise V5CampaignError("trial plan is duplicated")
                old_key = token_keys.get(plan.candidate_token)
                if old_key is not None and old_key != plan.candidate_key:
                    raise V5CampaignError("candidate token collision")
                token_keys[plan.candidate_token] = plan.candidate_key
                self._plans[plan.trial_id] = plan
                if plan.prefetched:
                    active = self._active_plans()
                    prefetched = self._prefetched()
                    prior_pending = (*active, *prefetched[:-1])
                    if (
                        plan.stage not in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL)
                        or len(active) != 1
                        or not 1 <= len(prefetched) <= 4
                        or plan.budget_ordinal
                        != active[0].budget_ordinal + len(prefetched)
                        or plan.proposal_receipt.pending_candidate_keys
                        != tuple(item.candidate_key for item in prior_pending)
                    ):
                        raise V5CampaignError("prefetch plan is not a bounded pending successor")
            elif kind == "consume":
                _strict_keys(row, {"schema", "version", "event_schema", "event_version", "record_type", "campaign_fingerprint", "release_identity_sha256", "role", "namespace", "previous_sha256", "row_sha256", "trial_id", "candidate_key", "record_sha256"}, "consume decision row")
                trial_id = row.get("trial_id")
                candidate_key = row.get("candidate_key")
                record_sha256 = row.get("record_sha256")
                if record_sha256 is not None:
                    _require_sha(record_sha256, "consume record hash")
                if trial_id not in self._plans or not isinstance(candidate_key, str) or candidate_key != self._plans[trial_id].candidate_key or trial_id in self._consumed:
                    raise V5CampaignError("campaign consume row differs")
                self._consumed[trial_id] = (candidate_key, record_sha256)
            elif kind == "outcome":
                _strict_keys(row, {"schema", "version", "event_schema", "event_version", "record_type", "campaign_fingerprint", "release_identity_sha256", "role", "namespace", "previous_sha256", "row_sha256", "outcome"}, "outcome decision row")
                outcome = CampaignOutcomeV2.from_mapping(row.get("outcome", {}))
                plan = self._plans.get(outcome.trial_id)
                if plan is None or outcome.trial_id in self._outcomes:
                    raise V5CampaignError("campaign outcome is not tied to a consumed plan")
                self._validate_outcome_binding(plan, outcome)
                self._outcomes[outcome.trial_id] = outcome
                self._budget_monotonic = (
                    self._budget_monotonic or outcome.dispatch_budget_counted
                )
                self._apply_failure_state(outcome, rebuilding=True)
            elif kind == "prefetch_discard":
                _strict_keys(row, {"schema", "version", "event_schema", "event_version", "record_type", "campaign_fingerprint", "release_identity_sha256", "role", "namespace", "previous_sha256", "row_sha256", "trial_id", "candidate_key", "reason"}, "prefetch decision row")
                trial_id = row.get("trial_id")
                if trial_id not in self._plans or row.get("candidate_key") != self._plans[trial_id].candidate_key or not isinstance(row.get("reason"), str) or not row["reason"] or trial_id in self._outcomes or trial_id in self._prefetch_discarded or not self._plans[trial_id].prefetched:
                    raise V5CampaignError("prefetch discard row differs")
                if trial_id in self._prefetch_activated or self._active_plans():
                    raise V5CampaignError("prefetch discard is not for an inactive successor")
                self._prefetch_discarded.add(trial_id)
            elif kind == "prefetch_activate":
                _strict_keys(row, {"schema", "version", "event_schema", "event_version", "record_type", "campaign_fingerprint", "release_identity_sha256", "role", "namespace", "previous_sha256", "row_sha256", "trial_id", "candidate_key", "budget_ordinal"}, "prefetch activation row")
                trial_id = row.get("trial_id")
                plan = self._plans.get(trial_id)
                expected_stage = TrialStageV2.PRIMARY_NOVEL if self.identity.role is CampaignRoleV2.PRIMARY else TrialStageV2.CORRECTION_NOVEL
                if plan is None or not plan.prefetched or plan.stage is not expected_stage or plan.candidate_key != row.get("candidate_key") or plan.budget_ordinal != row.get("budget_ordinal") or trial_id in self._prefetch_activated or trial_id in self._prefetch_discarded or trial_id in self._outcomes or self._active_plans():
                    raise V5CampaignError("prefetch activation row differs")
                if plan.budget_ordinal != self._next_novel_budget_ordinal():
                    raise V5CampaignError("prefetch activation ordinal is stale")
                self._prefetch_activated.add(trial_id)
            elif kind == "pause":
                _strict_keys(row, {"schema", "version", "event_schema", "event_version", "record_type", "campaign_fingerprint", "release_identity_sha256", "role", "namespace", "previous_sha256", "row_sha256", "reason", "failure_signature"}, "pause decision row")
                if row.get("reason") != "three_identical_failure_signatures" or self._pause_recorded or self._failure_streak != 3 or not isinstance(row.get("failure_signature"), str) or row.get("failure_signature") != self._failure_signature:
                    raise V5CampaignError("pause reason differs")
                recent = tuple(self._outcomes.values())[-3:]
                if len(recent) != 3 or any(item.disposition is not OutcomeV2.FAILURE or item.failure_signature != row["failure_signature"] for item in recent):
                    raise V5CampaignError("pause is not bound to three consecutive ordinary failures")
                self._paused = True
                self._pause_recorded = True
            elif kind == "closeout":
                _strict_keys(row, {"schema", "version", "event_schema", "event_version", "record_type", "campaign_fingerprint", "release_identity_sha256", "role", "namespace", "previous_sha256", "row_sha256", "report", "ledger_head_before_closeout"}, "closeout decision row")
                if self._closeout is not None:
                    raise V5CampaignError("campaign closeout is duplicated")
                report = row.get("report")
                if not isinstance(report, Mapping) or report.get("schema") != REPORT_SCHEMA or report.get("version") != REPORT_VERSION:
                    raise V5CampaignError("campaign closeout schema differs")
                if row.get("ledger_head_before_closeout") != row.get("previous_sha256"):
                    raise V5CampaignError("campaign closeout head binding differs")
                self._closeout = self._report_from_mapping(report)
            else:
                raise V5CampaignError("unknown campaign decision record")
        issued = set(self._cursor.issued_keys)
        for plan in self._plans.values():
            if plan.proposal_receipt.pool_candidate_keys and not set(plan.proposal_receipt.pool_candidate_keys).issubset(issued):
                raise V5CampaignError("Sobol cursor is behind the durable proposal pool")

    def _check_checkpoint(self) -> None:
        if not self.checkpoint_path.exists():
            return
        try:
            value = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise V5CampaignError("checkpoint is invalid") from exc
        _strict_keys(value, {"schema", "version", "namespace", "ledger_head_sha256", "plan_count", "outcome_count"}, "campaign checkpoint")
        if value["schema"] != CHECKPOINT_SCHEMA or value["version"] != CHECKPOINT_VERSION or value["namespace"] != self.identity.state_namespace or value["ledger_head_sha256"] != self._ledger.head_sha256:
            raise V5CampaignError("checkpoint is stale or crosses namespace")
        if type(value["plan_count"]) is not int or type(value["outcome_count"]) is not int or value["plan_count"] != len(self._plans) or value["outcome_count"] != len(self._outcomes):
            raise V5CampaignError("checkpoint counts differ from the decision ledger")

    def write_checkpoint(self) -> Path:
        self._full_cold_rebuild()
        _atomic_json(self.checkpoint_path, {
            "schema": CHECKPOINT_SCHEMA,
            "version": CHECKPOINT_VERSION,
            "namespace": self.identity.state_namespace,
            "ledger_head_sha256": self._ledger.head_sha256,
            "plan_count": len(self._plans),
            "outcome_count": len(self._outcomes),
        })
        return self.checkpoint_path

    def _cold_rebuild(self) -> None:
        self._ledger.verify_writer_state()

    def _full_cold_rebuild(self) -> None:
        self._ledger.rows, self._ledger.head_sha256 = self._ledger.cold_verify()
        self._rebuild()

    @property
    def ledger_head_sha256(self) -> str:
        return self._ledger.head_sha256

    @property
    def exact_novel_count(self) -> int:
        stage = TrialStageV2.PRIMARY_NOVEL if self.identity.role is CampaignRoleV2.PRIMARY else TrialStageV2.CORRECTION_NOVEL
        return sum(1 for outcome in self._outcomes.values() if outcome.stage is stage and outcome.counted_exact)

    def _next_novel_budget_ordinal(self) -> int:
        """Return the next slot, preserving a consumed physical dispatch slot."""

        if not self._budget_monotonic:
            return self.exact_novel_count + 1
        stage = (
            TrialStageV2.PRIMARY_NOVEL
            if self.identity.role is CampaignRoleV2.PRIMARY
            else TrialStageV2.CORRECTION_NOVEL
        )
        return max(
            (plan.budget_ordinal for plan in self._plans.values() if plan.stage is stage),
            default=0,
        ) + 1

    @property
    def dispatched_novel_count(self) -> int:
        stage = TrialStageV2.PRIMARY_NOVEL if self.identity.role is CampaignRoleV2.PRIMARY else TrialStageV2.CORRECTION_NOVEL
        return sum(1 for plan in self._plans.values() if plan.stage is stage)

    @property
    def plans(self) -> tuple[V5TrialPlan, ...]:
        return tuple(self._plans.values())

    @property
    def outcomes(self) -> tuple[CampaignOutcomeV2, ...]:
        return tuple(self._outcomes.values())

    @property
    def paused(self) -> bool:
        return self._paused

    def _active_plans(self) -> tuple[V5TrialPlan, ...]:
        return tuple(plan for trial_id, plan in self._plans.items() if trial_id not in self._outcomes and trial_id not in self._prefetch_discarded and (not plan.prefetched or trial_id in self._prefetch_activated))

    def _prefetched(self) -> tuple[V5TrialPlan, ...]:
        return tuple(plan for trial_id, plan in self._plans.items() if plan.prefetched and trial_id not in self._outcomes and trial_id not in self._prefetch_discarded and trial_id not in self._prefetch_activated)

    def _candidate_keys(self, stage: TrialStageV2) -> set[str]:
        return {plan.candidate_key for plan in self._plans.values() if plan.stage is stage}

    def _candidate_observations(self, stage: TrialStageV2) -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = list(self._seed_observations) if stage is TrialStageV2.PRIMARY_NOVEL else []
        for trial_id, outcome in self._outcomes.items():
            plan = self._plans[trial_id]
            if plan.stage is stage and outcome.disposition is OutcomeV2.PHYSICALLY_ELIGIBLE and outcome.tell_state is TellState.COMMITTED and outcome.primary_mae_n is not None:
                rows.append({"candidate": plan.candidate.as_dict(), "candidate_key": plan.candidate_key, "mae_n": outcome.primary_mae_n})
        return tuple(
            group.as_dict()
            for group in build_v5_observation_groups(
                rows,
                fingerprint_sha256=self.identity.campaign_fingerprint,
            )
        )

    def _fixed_candidate(self) -> CompleteCandidateV1 | None:
        if self.identity.role is CampaignRoleV2.PRIMARY:
            return None
        return CompleteCandidateV1(controller_path=self.identity.fixed_controller_path or {}, correction_weights=(0.0,) * 6)

    def _local_incumbent(self, stage: TrialStageV2) -> CompleteCandidateV1 | None:
        """Return the observed incumbent used to center local Sobol pools."""

        if stage is not TrialStageV2.PRIMARY_NOVEL:
            return self._fixed_candidate()
        groups = self._candidate_observations(stage)
        if not groups:
            return None
        best = min(
            groups,
            key=lambda row: (float(row["mean_n"]), str(row["candidate_key"])),
        )
        return _candidate(best["candidate"])

    def _method(self, ordinal: int) -> ProposalMethodV2:
        if self.identity.role is CampaignRoleV2.PRIMARY:
            if ordinal <= 3:
                return ProposalMethodV2.DETERMINISTIC_SOBOL
            if ordinal <= 24:
                return ProposalMethodV2.GLOBAL_SOBOL
            return ProposalMethodV2.GLOBAL_SOBOL if ordinal % 5 == 0 else ProposalMethodV2.QLOGNEI
        if ordinal <= 12:
            return ProposalMethodV2.GLOBAL_SOBOL
        return ProposalMethodV2.GLOBAL_SOBOL if ordinal % 5 == 0 else ProposalMethodV2.QLOGNEI

    def _proposal(self, stage: TrialStageV2, ordinal: int, *, pending: Sequence[str] = (), pending_candidates: Sequence[Mapping[str, Any]] = ()) -> tuple[CompleteCandidateV1, ProposalReceiptV2]:
        method = ProposalMethodV2.FIXED_REPLAY if stage in (TrialStageV2.PRIMARY_CONFIRM, TrialStageV2.MATCHED_ZERO, TrialStageV2.MATCHED_CORRECTED) else self._method(ordinal)
        block = "controller_path" if self.identity.role is CampaignRoleV2.PRIMARY else "correction"
        local_refinement = method is ProposalMethodV2.QLOGNEI and (
            (self.identity.role is CampaignRoleV2.PRIMARY and ordinal >= 161)
            or (self.identity.role is CampaignRoleV2.CORRECTION and ordinal >= 49)
        )
        if method is ProposalMethodV2.FIXED_REPLAY:
            raise V5CampaignError("fixed replay must provide its candidate directly")
        stage_novel = TrialStageV2.PRIMARY_NOVEL if self.identity.role is CampaignRoleV2.PRIMARY else TrialStageV2.CORRECTION_NOVEL
        evaluated = self._candidate_keys(stage_novel)
        fixed = self._local_incumbent(stage_novel) if local_refinement else self._fixed_candidate()
        if local_refinement and fixed is None:
            raise V5CampaignError("local qLogNEI pool lacks a confirmed incumbent")
        try:
            pool = self._cursor.fresh_pool(evaluated_keys=evaluated, pending_keys=pending, domain=block, count=SOBOL_POOL_SIZE, fixed_other_block=fixed, local=local_refinement)
        except Exception as exc:
            raise V5CampaignError("deterministic Sobol pool failed") from exc
        candidates = tuple(_candidate(item) for item in pool)
        keys = tuple(item.candidate_key for item in candidates)
        if len(set(keys)) != SOBOL_POOL_SIZE or set(keys) & set(evaluated) or set(keys) & set(pending):
            raise V5CampaignError("proposal pool is not fresh")
        selected: CompleteCandidateV1
        provider_name = "persisted_sobol_cursor"
        provider_receipt: Mapping[str, Any] | None = None
        if tuple(_candidate(item).candidate_key for item in pending_candidates) != tuple(pending):
            raise V5CampaignError("pending candidate identity does not match pending keys")
        if method is ProposalMethodV2.QLOGNEI:
            if self.qlognei_provider is None:
                raise V5CampaignError("qLogNEI provider is unavailable; no silent fallback")
            result = self.qlognei_provider(
                tuple(item.as_dict() for item in candidates),
                observations=self._candidate_observations(stage_novel),
                block=block,
                evaluated_keys=tuple(sorted(evaluated)),
                pending_keys=tuple(sorted(pending)),
                pending_candidates=tuple(dict(item) for item in pending_candidates),
            )
            if not isinstance(result, Mapping):
                raise V5CampaignError("qLogNEI provider did not return a typed mapping")
            acquisition = str(result.get("acquisition", "")).lower().replace(" ", "")
            if acquisition not in {"qlognei", "qlognoisyexpectedimprovement"}:
                raise V5CampaignError("qLogNEI provider returned a non-qLogNEI acquisition")
            selected = _candidate(result.get("candidate"))
            if selected.candidate_key not in keys:
                raise V5CampaignError("qLogNEI provider selected outside its fresh pool")
            provider_name = str(result.get("production_provider", type(self.qlognei_provider).__name__))
            provider_receipt = result.get("provider_receipt", {"acquisition": "qLogNEI", "provider": provider_name})
            if not isinstance(provider_receipt, Mapping):
                raise V5CampaignError("qLogNEI provider receipt is not typed")
            provider_receipt = dict(provider_receipt)
            provider_receipt["local_refinement"] = local_refinement
            bindings = {
                "pool_sha256": _sha(list(keys)),
                "pool_candidate_keys": list(keys),
                "pending_candidate_keys": list(pending),
                "pending_candidates": [dict(item) for item in pending_candidates],
                "selected_candidate_key": selected.candidate_key,
                "campaign_fingerprint": self.identity.campaign_fingerprint,
                "role": self.identity.role.value,
            }
            for field, expected in bindings.items():
                if field in provider_receipt and provider_receipt[field] != expected:
                    raise V5CampaignError(f"qLogNEI provider receipt {field} differs")
                provider_receipt[field] = expected
        else:
            selected = candidates[(ordinal - 1) % SOBOL_POOL_SIZE]
        if self.identity.role is CampaignRoleV2.PRIMARY and selected.correction_weights != (0.0,) * 6:
            raise V5CampaignError("PRIMARY proposal carries correction weights")
        if self.identity.role is CampaignRoleV2.CORRECTION and selected.controller_path != self.identity.fixed_controller_path:
            raise V5CampaignError("CORRECTION proposal changed the fixed controller path")
        if self.identity.role is CampaignRoleV2.CORRECTION and math.fsum(abs(value) for value in selected.correction_weights) == 0.0:
            raise V5CampaignError("CORRECTION proposal has zero correction weights")
        receipt = ProposalReceiptV2(method, block, keys, selected.candidate_key, tuple(pending), self.identity.campaign_fingerprint, self.identity.role, provider_name, provider_receipt)
        return selected, receipt

    @staticmethod
    def _trial_id(role: CampaignRoleV2, stage: TrialStageV2, dispatch_index: int, candidate_key: str) -> str:
        return f"v5-{role.value.lower()}-{stage.value.lower()}-{dispatch_index:06d}-{_bytes_sha(candidate_key.encode())[:16]}"

    def _make_plan(self, stage: TrialStageV2, budget_ordinal: int, candidate: CompleteCandidateV1, proposal: ProposalReceiptV2, *, repeat_of: str | None = None, prefetched: bool = False) -> V5TrialPlan:
        dispatch_index = len(self._plans) + 1
        key = candidate.candidate_key
        token = _candidate_token(key)
        for old in self._plans.values():
            if old.candidate_token == token and old.candidate_key != key:
                raise V5CampaignError("candidate token collision")
        trial_id = self._trial_id(self.identity.role, stage, dispatch_index, key)
        confirmation = stage in (
            TrialStageV2.PRIMARY_CONFIRM,
            TrialStageV2.MATCHED_ZERO,
            TrialStageV2.MATCHED_CORRECTED,
        )
        home_only = self.identity.entry_mode == ENTRY_MODE_HOME_ONLY_V1
        requires_home = confirmation or home_only
        packable = False if home_only else not confirmation
        plan = V5TrialPlan(
            trial_id,
            trial_id + "-attempt",
            _attempt_kind(stage),
            candidate,
            key,
            token,
            budget_ordinal,
            proposal,
            stage,
            requires_home,
            packable,
            self.wire_session_epoch,
            dispatch_index,
            dispatch_index,
            repeat_of,
            prefetched,
            self.identity.entry_mode,
        )
        self._ledger.append("plan", {"plan": plan.as_dict(), "candidate_key": key, "budget_ordinal": budget_ordinal})
        self._plans[trial_id] = plan
        return plan

    def prefetch_next(self) -> V5TrialPlan | None:
        self._cold_rebuild()
        if self.identity.entry_mode == ENTRY_MODE_HOME_ONLY_V1:
            return None
        active = self._active_plans()
        if len(active) != 1 or active[0].stage not in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL):
            return None
        if self._prefetched():
            raise V5CampaignError("one-step prefetch already exists")
        current = active[0]
        target = current.budget_ordinal + 1
        limit = PRIMARY_NOVEL_TARGET if self.identity.role is CampaignRoleV2.PRIMARY else CORRECTION_NOVEL_TARGET
        if target > limit:
            return None
        candidate, proposal = self._proposal(current.stage, target, pending=(current.candidate_key,), pending_candidates=(current.candidate.as_dict(),))
        return self._make_plan(current.stage, target, candidate, proposal, prefetched=True)

    def build_pending_chain(self, *, max_attempts: int = 5) -> tuple[V5TrialPlan, ...]:
        """Build up to five pending-fantasy plans; runtime loads only one successor."""

        self._cold_rebuild()
        if self.identity.entry_mode == ENTRY_MODE_HOME_ONLY_V1:
            active = tuple(self._active_plans())
            return active
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise V5CampaignError("pending chain bound must be one to five")
        active = self._active_plans()
        if (
            len(active) != 1
            or active[0].stage
            not in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL)
        ):
            return active
        plans = [active[0], *self._prefetched()]
        if len(plans) > max_attempts:
            raise V5CampaignError("existing pending chain exceeds requested bound")
        limit = (
            PRIMARY_NOVEL_TARGET
            if self.identity.role is CampaignRoleV2.PRIMARY
            else CORRECTION_NOVEL_TARGET
        )
        while len(plans) < max_attempts:
            target = active[0].budget_ordinal + len(plans)
            if target > limit:
                break
            pending = tuple(item.candidate_key for item in plans)
            pending_candidates = tuple(item.candidate.as_dict() for item in plans)
            candidate, proposal = self._proposal(
                active[0].stage,
                target,
                pending=pending,
                pending_candidates=pending_candidates,
            )
            plans.append(
                self._make_plan(
                    active[0].stage,
                    target,
                    candidate,
                    proposal,
                    prefetched=True,
                )
            )
        return tuple(plans)

    def _activate_prefetch(self, plan: V5TrialPlan) -> None:
        if not plan.prefetched or plan.trial_id in self._prefetch_activated or plan.trial_id in self._prefetch_discarded or plan.trial_id in self._outcomes or self._active_plans():
            raise V5CampaignError("prefetch activation target is not an inactive successor")
        expected_stage = TrialStageV2.PRIMARY_NOVEL if self.identity.role is CampaignRoleV2.PRIMARY else TrialStageV2.CORRECTION_NOVEL
        if plan.stage is not expected_stage or plan.budget_ordinal != self._next_novel_budget_ordinal():
            raise V5CampaignError("prefetch activation target is stale")
        self._ledger.append("prefetch_activate", {"trial_id": plan.trial_id, "candidate_key": plan.candidate_key, "budget_ordinal": plan.budget_ordinal})
        self._prefetch_activated.add(plan.trial_id)

    def _primary_top3(self) -> tuple[tuple[str, CompleteCandidateV1], ...]:
        rows = self._candidate_observations(TrialStageV2.PRIMARY_NOVEL)
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            unique.setdefault(row["candidate_key"], row)
        ranked = sorted(unique.values(), key=lambda row: (float(row["mae_n"]), str(row["candidate_key"])))
        return tuple((row["candidate_key"], _candidate(row["candidate"])) for row in ranked[:TOP_COUNT])

    def _correction_best(self) -> tuple[str, CompleteCandidateV1] | None:
        rows = self._candidate_observations(TrialStageV2.CORRECTION_NOVEL)
        if not rows:
            return None
        row = min(rows, key=lambda item: (float(item["mae_n"]), str(item["candidate_key"])))
        return str(row["candidate_key"]), _candidate(row["candidate"])

    def _next_confirmation(self) -> V5TrialPlan | None:
        top = self._primary_top3()
        if len(top) != TOP_COUNT:
            raise V5CampaignError("PRIMARY confirmation requires three exact novel candidates")
        completed = [outcome for outcome in self._outcomes.values() if outcome.stage is TrialStageV2.PRIMARY_CONFIRM]
        ordinal = len(completed)
        if ordinal >= TOP_COUNT * CONFIRMATIONS_PER_TOP:
            return None
        key, candidate = top[ordinal % TOP_COUNT]
        proposal = ProposalReceiptV2(ProposalMethodV2.FIXED_REPLAY, "controller_path", (), key, (), self.identity.campaign_fingerprint, self.identity.role, "primary_top3_round_robin")
        return self._make_plan(TrialStageV2.PRIMARY_CONFIRM, ordinal + 1, candidate, proposal, repeat_of=key)

    def _next_matched(self) -> V5TrialPlan | None:
        best = self._correction_best()
        if best is None:
            raise V5CampaignError("correction matched stage lacks an exact selected minimum")
        _key, corrected = best
        zero = CompleteCandidateV1(controller_path=self.identity.fixed_controller_path or {}, correction_weights=(0.0,) * 6)
        completed = [outcome for outcome in self._outcomes.values() if outcome.stage in (TrialStageV2.MATCHED_ZERO, TrialStageV2.MATCHED_CORRECTED)]
        index = len(completed)
        if index >= MATCHED_PAIRS * 2:
            return None
        if MATCHED_ORDER[index] == "Z":
            candidate, stage = zero, TrialStageV2.MATCHED_ZERO
        else:
            candidate, stage = corrected, TrialStageV2.MATCHED_CORRECTED
        proposal = ProposalReceiptV2(ProposalMethodV2.FIXED_REPLAY, "controller_path", (), candidate.candidate_key, (), self.identity.campaign_fingerprint, self.identity.role, "strict_primary_correction_match")
        return self._make_plan(stage, index + 1, candidate, proposal, repeat_of=candidate.candidate_key)

    def plan_next(self) -> V5TrialPlan | None:
        self._cold_rebuild()
        if self._paused:
            raise CampaignPaused("campaign is paused after three identical failures")
        active = self._active_plans()
        if active:
            if len(active) != 1:
                raise V5CampaignError("more than one non-prefetched plan is active")
            raise V5CampaignError("campaign already has a dispatched plan")
        prefetched = self._prefetched()
        if prefetched:
            candidate = prefetched[0]
            expected_stage = TrialStageV2.PRIMARY_NOVEL if self.identity.role is CampaignRoleV2.PRIMARY else TrialStageV2.CORRECTION_NOVEL
            if candidate.stage is not expected_stage or candidate.budget_ordinal != self._next_novel_budget_ordinal():
                for stale in prefetched:
                    self._discard_prefetch(stale, "stale_or_phase_mismatch")
            else:
                self._activate_prefetch(candidate)
                return candidate
        if self._closeout is not None:
            return None
        if self.identity.role is CampaignRoleV2.PRIMARY:
            if self.exact_novel_count < PRIMARY_NOVEL_TARGET:
                ordinal = self._next_novel_budget_ordinal()
                candidate, proposal = self._proposal(TrialStageV2.PRIMARY_NOVEL, ordinal)
                return self._make_plan(TrialStageV2.PRIMARY_NOVEL, ordinal, candidate, proposal)
            confirmation = self._next_confirmation()
            if confirmation is not None:
                return confirmation
            self._ensure_closeout()
            return None
        if self.exact_novel_count < CORRECTION_NOVEL_TARGET:
            ordinal = self._next_novel_budget_ordinal()
            candidate, proposal = self._proposal(TrialStageV2.CORRECTION_NOVEL, ordinal)
            return self._make_plan(TrialStageV2.CORRECTION_NOVEL, ordinal, candidate, proposal)
        matched = self._next_matched()
        if matched is not None:
            return matched
        self._ensure_closeout()
        return None

    def _plan(self, value: V5TrialPlan | str) -> V5TrialPlan:
        plan = value if isinstance(value, V5TrialPlan) else self._plans.get(value)
        if not isinstance(plan, V5TrialPlan) or plan.trial_id not in self._plans:
            raise V5CampaignError("trial plan is absent")
        if self._plans[plan.trial_id] != plan:
            raise V5CampaignError("trial plan differs from cold decision ledger")
        if plan.prefetched and plan.trial_id not in self._prefetch_activated:
            raise V5CampaignError("prefetched plan must be durably activated before recording")
        return plan

    def _discard_prefetch(self, plan: V5TrialPlan, reason: str) -> None:
        if plan.trial_id in self._prefetch_discarded:
            return
        if not plan.prefetched or plan.trial_id in self._outcomes or plan.trial_id in self._prefetch_activated:
            raise V5CampaignError("prefetch discard target is not pending")
        self._ledger.append("prefetch_discard", {"trial_id": plan.trial_id, "candidate_key": plan.candidate_key, "reason": reason})
        self._prefetch_discarded.add(plan.trial_id)

    def _append_consume(self, plan: V5TrialPlan, record_sha256: str | None) -> None:
        self._cold_rebuild()
        if plan.prefetched and plan.trial_id not in self._prefetch_activated:
            raise V5CampaignError("prefetched plan must be activated before consume")
        if record_sha256 is not None:
            _require_sha(record_sha256, "consume record hash")
        if plan.trial_id in self._consumed:
            if self._consumed[plan.trial_id] != (plan.candidate_key, record_sha256):
                raise V5CampaignError("consumed plan identity differs")
            return
        self._ledger.append("consume", {"trial_id": plan.trial_id, "candidate_key": plan.candidate_key, "record_sha256": record_sha256})
        self._consumed[plan.trial_id] = (plan.candidate_key, record_sha256)

    def _append_outcome(self, outcome: CampaignOutcomeV2) -> CampaignOutcomeV2:
        self._cold_rebuild()
        prior = self._outcomes.get(outcome.trial_id)
        if prior is not None:
            if prior != outcome:
                raise V5CampaignError("outcome retry differs")
            return prior
        if outcome.trial_id not in self._consumed:
            raise V5CampaignError("outcome lacks durable consume row")
        plan = self._plans[outcome.trial_id]
        self._validate_outcome_binding(plan, outcome)
        self._ledger.append("outcome", {"outcome": outcome.as_dict()})
        self._outcomes[outcome.trial_id] = outcome
        self._apply_failure_state(outcome, rebuilding=False)
        active = self._plans[outcome.trial_id]
        prefetched = self._prefetched()
        if prefetched and (not outcome.counted_exact or prefetched[0].stage is not active.stage or prefetched[0].budget_ordinal != self._next_novel_budget_ordinal()):
            self._discard_prefetch(prefetched[0], "current_outcome_burned_prefetch")
        return outcome

    def _apply_failure_state(self, outcome: CampaignOutcomeV2, *, rebuilding: bool) -> None:
        if outcome.disposition is OutcomeV2.FAILURE:
            signature = outcome.failure_signature or ""
            if signature == self._failure_signature:
                self._failure_streak += 1
            else:
                self._failure_signature = signature
                self._failure_streak = 1
            if self._failure_streak >= 3:
                self._paused = True
                if not rebuilding:
                    self._ledger.append("pause", {"reason": "three_identical_failure_signatures", "failure_signature": signature})
                    self._pause_recorded = True
        else:
            self._failure_signature = None
            self._failure_streak = 0

    def record_censor(self, plan_or_trial_id: V5TrialPlan | str, *, reason: str) -> CampaignOutcomeV2:
        plan = self._plan(plan_or_trial_id)
        if plan.stage not in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL) or not reason:
            raise V5CampaignError("only ordinary novel plans may receive typed censor")
        self._append_consume(plan, None)
        return self._append_outcome(CampaignOutcomeV2(plan.trial_id, plan.candidate_key, plan.stage, OutcomeV2.CENSORED, None, None, failure_signature=reason))

    def record_failure(self, plan_or_trial_id: V5TrialPlan | str, *, signature: str, next_not_ready: bool = False) -> CampaignOutcomeV2:
        plan = self._plan(plan_or_trial_id)
        if plan.stage not in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL) or not signature:
            raise V5CampaignError("only ordinary novel plans may receive a typed failure")
        disposition = OutcomeV2.NEXT_NOT_READY if next_not_ready else OutcomeV2.FAILURE
        self._append_consume(plan, None)
        return self._append_outcome(CampaignOutcomeV2(plan.trial_id, plan.candidate_key, plan.stage, disposition, None, None, failure_signature=signature))

    def record_recoverable_failure(
        self,
        plan_or_trial_id: V5TrialPlan | str,
        *,
        signature: str,
    ) -> CampaignOutcomeV2:
        """Close one typed serial HOME-only dispatch as non-GP evidence.

        The execution journal owns the partial lifecycle and Home/release
        receipt.  The campaign ledger records only the non-physical failure
        disposition plus the fact that its already-dispatched budget slot is
        consumed, so the next proposal cannot restart that slot or enter GP.
        """

        plan = self._plan(plan_or_trial_id)
        if (
            plan.entry_mode != ENTRY_MODE_HOME_ONLY_V1
            or not plan.requires_home
            or plan.packable
            or plan.stage
            not in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL)
            or not isinstance(signature, str)
            or not signature
        ):
            raise V5CampaignError(
                "recoverable failure requires a serial HOME-only novel plan"
            )
        self._append_consume(plan, None)
        outcome = CampaignOutcomeV2(
            plan.trial_id,
            plan.candidate_key,
            plan.stage,
            OutcomeV2.FAILURE,
            None,
            None,
            failure_signature=signature,
            dispatch_budget_counted=True,
        )
        self._budget_monotonic = True
        return self._append_outcome(outcome)

    @staticmethod
    def _physical_state(ledger: V5PhysicalAdmissionLedgerV2, trial_id: str) -> tuple[FigureEightPhysicalRecordV2, TellState | None, OptimizerReceiptV2 | None]:
        if not isinstance(ledger, V5PhysicalAdmissionLedgerV2):
            raise V5CampaignError("physical result requires the real V5PhysicalAdmissionLedgerV2")
        ledger.fresh_process_verify()
        records = [item for item in ledger.records if item.trial_id == trial_id]
        if len(records) != 1:
            raise V5CampaignError("physical ledger does not contain exactly one trial record")
        try:
            rows = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]
        except (OSError, json.JSONDecodeError) as exc:
            raise V5CampaignError("physical ledger cold rows cannot be read") from exc
        states: list[tuple[TellState, OptimizerReceiptV2 | None]] = []
        for row in rows[1:]:
            if row.get("record_type") == "tell_state" and row.get("trial_id") == trial_id:
                try:
                    state = TellState(row["state"])
                except (KeyError, ValueError) as exc:
                    raise V5CampaignError("physical tell state is unknown") from exc
                receipt = None if row.get("optimizer_receipt") is None else OptimizerReceiptV2.from_mapping(row["optimizer_receipt"])
                states.append((state, receipt))
        if not states:
            return records[0], None, None
        return records[0], states[-1][0], states[-1][1]

    def record_physical_result(self, plan_or_trial_id: V5TrialPlan | str, record: FigureEightPhysicalRecordV2, physical_ledger: V5PhysicalAdmissionLedgerV2) -> CampaignOutcomeV2:
        plan = self._plan(plan_or_trial_id)
        if not isinstance(record, FigureEightPhysicalRecordV2):
            raise V5CampaignError("physical result is not FigureEightPhysicalRecordV2")
        if record.campaign_fingerprint != self.identity.campaign_fingerprint or record.release_identity_sha256 != self.identity.release_identity_sha256 or record.role is not _ledger_role(self.identity.role) or record.trial_id != plan.trial_id or record.attempt_id != plan.attempt_id:
            raise V5CampaignError("physical result crosses campaign/plan identity")
        if record.candidate_identity.epoch != plan.wire_epoch or record.candidate_identity.ordinal != plan.candidate_ordinal or record.candidate_identity.attempt_kind is not plan.attempt_kind or record.candidate_identity.candidate_token != plan.candidate_token:
            raise V5CampaignError("physical result candidate identity differs from plan")
        cold_record, tell_state, optimizer_receipt = self._physical_state(physical_ledger, plan.trial_id)
        if cold_record.record_sha256 != record.record_sha256 or cold_record.source_artifact.artifact_sha256 != record.source_artifact.artifact_sha256:
            raise V5CampaignError("physical result differs from cold physical ledger")
        if not record.eligible:
            if plan.stage not in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL):
                raise V5CampaignError("confirm/matched result must be physically eligible")
            self._append_consume(plan, record.record_sha256)
            return self._append_outcome(CampaignOutcomeV2(plan.trial_id, plan.candidate_key, plan.stage, OutcomeV2.INELIGIBLE, None, record.boundary.mode, record.record_sha256, home_verified=record.boundary.mode is BoundaryMode.HOME, physical_ledger_path=str(physical_ledger.path.resolve())))
        if plan.requires_home and record.boundary.mode is not BoundaryMode.HOME:
            raise V5CampaignError("confirm/matched result requires the real HOME boundary")
        if plan.stage in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL):
            if tell_state is not TellState.COMMITTED or optimizer_receipt is None or optimizer_receipt.trial_id != plan.trial_id or optimizer_receipt.tell_token != record.tell_token() or optimizer_receipt.record_sha256 != record.record_sha256:
                raise V5CampaignError("novel result lacks the exact committed optimizer receipt")
        else:
            if tell_state is not None:
                raise V5CampaignError("confirm/matched result must not be optimizer-told")
        self._append_consume(plan, record.record_sha256)
        outcome = CampaignOutcomeV2(plan.trial_id, plan.candidate_key, plan.stage, OutcomeV2.PHYSICALLY_ELIGIBLE, record.metric_snapshot.primary_mae_n, record.boundary.mode, record.record_sha256, home_verified=record.boundary.mode is BoundaryMode.HOME, tell_state=tell_state, physical_ledger_path=str(physical_ledger.path.resolve()))
        return self._append_outcome(outcome)

    def _report_from_mapping(self, value: Mapping[str, Any]) -> CampaignReportV2:
        _strict_keys(value, {"schema", "version", "role", "campaign_fingerprint", "exact_novel_count", "winner_candidate_key", "winner_candidate", "corrected_best_candidate_key", "corrected_best_candidate", "paired_differences", "paired_mean_difference_n", "paired_ci90_n", "paired_df", "automatic_promotion", "decision", "top_candidates", "primary_winner_controller_sha256", "matched_zero_maes_n", "matched_corrected_maes_n"}, "campaign report")
        try:
            report = CampaignReportV2(
                role=CampaignRoleV2(value["role"]), campaign_fingerprint=value["campaign_fingerprint"], exact_novel_count=value["exact_novel_count"], winner_candidate_key=value["winner_candidate_key"], winner_candidate=value["winner_candidate"], corrected_best_candidate_key=value["corrected_best_candidate_key"], corrected_best_candidate=value["corrected_best_candidate"], paired_differences=tuple(value["paired_differences"]), paired_mean_difference_n=value["paired_mean_difference_n"], paired_ci90_n=None if value["paired_ci90_n"] is None else tuple(value["paired_ci90_n"]), paired_df=value["paired_df"], automatic_promotion=value["automatic_promotion"], decision=value["decision"], top_candidates=tuple(value["top_candidates"]), primary_winner_controller_sha256=value["primary_winner_controller_sha256"], matched_zero_maes_n=tuple(value["matched_zero_maes_n"]), matched_corrected_maes_n=tuple(value["matched_corrected_maes_n"]), schema=value["schema"], version=value["version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise V5CampaignError("campaign report mapping is invalid") from exc
        if report.role is not self.identity.role or report.campaign_fingerprint != self.identity.campaign_fingerprint:
            raise V5CampaignError("campaign report crosses namespace")
        self._validate_report_state(report)
        return report

    def _validate_report_state(self, report: CampaignReportV2) -> None:
        required_count = PRIMARY_NOVEL_TARGET if self.identity.role is CampaignRoleV2.PRIMARY else CORRECTION_NOVEL_TARGET
        if report.exact_novel_count != self.exact_novel_count or report.exact_novel_count != required_count:
            raise V5CampaignError("campaign report exact count is not bound to rebuilt state")
        if report.role is CampaignRoleV2.PRIMARY:
            expected_top = self._primary_top3()
            if len(report.top_candidates) != TOP_COUNT or [row["candidate_key"] for row in report.top_candidates] != [key for key, _candidate in expected_top]:
                raise V5CampaignError("PRIMARY report top3 differs from rebuilt original MAE ranking")
            novel = {outcome.candidate_key: outcome for outcome in self._outcomes.values() if outcome.stage is TrialStageV2.PRIMARY_NOVEL and outcome.counted_exact}
            ranked: list[tuple[float, str]] = []
            for row, (expected_key, expected_candidate) in zip(report.top_candidates, expected_top, strict=True):
                if row["candidate_key"] != expected_key:
                    raise V5CampaignError("PRIMARY report top candidate identity differs")
                original = novel.get(row["candidate_key"])
                confirmations = [outcome for outcome in self._outcomes.values() if outcome.stage is TrialStageV2.PRIMARY_CONFIRM and outcome.candidate_key == row["candidate_key"] and outcome.counted_exact and outcome.home_verified and outcome.boundary_mode is BoundaryMode.HOME]
                if original is None or original.formal_mae_n is None or len(confirmations) != CONFIRMATIONS_PER_TOP:
                    raise V5CampaignError("PRIMARY report is not bound to five physical values")
                values = (float(original.formal_mae_n), *(float(outcome.formal_mae_n) for outcome in confirmations if outcome.formal_mae_n is not None))
                if len(values) != 5 or row["original_mae_n"] != values[0] or tuple(row["confirmation_maes_n"]) != values[1:] or row["repeated_mean_n"] != math.fsum(values) / 5.0:
                    raise V5CampaignError("PRIMARY report metric values differ from outcomes")
                ranked.append((row["repeated_mean_n"], row["candidate_key"]))
            winner_key = min(ranked, key=lambda item: (item[0], item[1]))[1]
            winner_candidate = next(candidate for key, candidate in expected_top if key == winner_key)
            if report.winner_candidate_key != winner_key or dict(report.winner_candidate or {}) != winner_candidate.as_dict():
                raise V5CampaignError("PRIMARY report winner ranking differs")
        else:
            best = self._correction_best()
            if report.primary_winner_controller_sha256 != self.identity.primary_winner_controller_sha256 or best is None or report.corrected_best_candidate_key != best[0] or dict(report.corrected_best_candidate or {}) != best[1].as_dict():
                raise V5CampaignError("CORRECTION report best identity differs")
            matched = [outcome for outcome in self._outcomes.values() if outcome.stage in (TrialStageV2.MATCHED_ZERO, TrialStageV2.MATCHED_CORRECTED)]
            expected = [TrialStageV2.MATCHED_ZERO if label == "Z" else TrialStageV2.MATCHED_CORRECTED for label in MATCHED_ORDER]
            if [outcome.stage for outcome in matched] != expected:
                raise V5CampaignError("CORRECTION report matched order differs")
            pairs = tuple(tuple(matched[index:index + 2]) for index in range(0, len(matched), 2))
            zero = tuple(float(next(item.formal_mae_n for item in pair if item.stage is TrialStageV2.MATCHED_ZERO)) for pair in pairs)
            corrected = tuple(float(next(item.formal_mae_n for item in pair if item.stage is TrialStageV2.MATCHED_CORRECTED)) for pair in pairs)
            differences = tuple(corrected[index] - zero[index] for index in range(MATCHED_PAIRS))
            mean, ci, decision = _student_t_ci90(differences)
            if zero != report.matched_zero_maes_n or corrected != report.matched_corrected_maes_n or differences != report.paired_differences or mean != report.paired_mean_difference_n or report.paired_df != 4 or ci != report.paired_ci90_n or decision != report.decision:
                raise V5CampaignError("CORRECTION report paired values differ from outcomes")

    def _ensure_closeout(self) -> CampaignReportV2:
        if self._closeout is not None:
            return self._closeout
        if self.identity.role is CampaignRoleV2.PRIMARY:
            confirmations = [outcome for outcome in self._outcomes.values() if outcome.stage is TrialStageV2.PRIMARY_CONFIRM]
            if self.exact_novel_count != PRIMARY_NOVEL_TARGET or len(confirmations) != TOP_COUNT * CONFIRMATIONS_PER_TOP or any(not outcome.counted_exact or not outcome.home_verified for outcome in confirmations):
                raise V5CampaignError("PRIMARY closeout requires 200 exact novel and 12 HOME confirmations")
            groups: dict[str, list[float]] = {}
            for outcome in confirmations:
                groups.setdefault(outcome.candidate_key, []).append(float(outcome.formal_mae_n))
            top = self._primary_top3()
            if any(len(groups.get(key, ())) != CONFIRMATIONS_PER_TOP for key, _candidate in top):
                raise V5CampaignError("PRIMARY confirmations are not four HOME repeats per top candidate")
            top_rows: list[dict[str, Any]] = []
            for key, candidate in top:
                original = next(outcome for outcome in self._outcomes.values() if outcome.stage is TrialStageV2.PRIMARY_NOVEL and outcome.candidate_key == key and outcome.formal_mae_n is not None)
                confirmation_maes = tuple(groups[key])
                values = (float(original.formal_mae_n), *confirmation_maes)
                top_rows.append({"candidate_key": key, "original_mae_n": float(original.formal_mae_n), "confirmation_maes_n": list(confirmation_maes), "total_n": 5, "repeated_mean_n": math.fsum(values) / 5.0})
            ranked = sorted(((row["repeated_mean_n"], row["candidate_key"], candidate) for row, (_key, candidate) in zip(top_rows, top, strict=True)), key=lambda item: (item[0], item[1]))
            winner_mean, winner_key, winner = ranked[0]
            report = CampaignReportV2(role=self.identity.role, campaign_fingerprint=self.identity.campaign_fingerprint, exact_novel_count=self.exact_novel_count, winner_candidate_key=winner_key, winner_candidate=winner.as_dict(), decision=f"winner_mean_mae_n={winner_mean:.12g}", top_candidates=tuple(top_rows))
        else:
            matched = [outcome for outcome in self._outcomes.values() if outcome.stage in (TrialStageV2.MATCHED_ZERO, TrialStageV2.MATCHED_CORRECTED)]
            if self.exact_novel_count != CORRECTION_NOVEL_TARGET or len(matched) != MATCHED_PAIRS * 2 or any(not outcome.counted_exact or not outcome.home_verified for outcome in matched):
                raise V5CampaignError("CORRECTION closeout requires 60 exact novel and 10 HOME matches")
            expected_stages = [TrialStageV2.MATCHED_ZERO if label == "Z" else TrialStageV2.MATCHED_CORRECTED for label in MATCHED_ORDER]
            if [outcome.stage for outcome in matched] != expected_stages:
                raise V5CampaignError("CORRECTION matched sequence differs from the fixed paired order")
            best = self._correction_best()
            if best is None:
                raise V5CampaignError("CORRECTION closeout lacks exact selected minimum")
            corrected_key, corrected = best
            pairs = tuple(tuple(matched[index:index + 2]) for index in range(0, len(matched), 2))
            if any({item.stage for item in pair} != {TrialStageV2.MATCHED_ZERO, TrialStageV2.MATCHED_CORRECTED} for pair in pairs):
                raise V5CampaignError("each matched pair must contain one zero and one corrected trial")
            zero = [float(next(item.formal_mae_n for item in pair if item.stage is TrialStageV2.MATCHED_ZERO)) for pair in pairs]
            changed = [float(next(item.formal_mae_n for item in pair if item.stage is TrialStageV2.MATCHED_CORRECTED)) for pair in pairs]
            if len(zero) != MATCHED_PAIRS or len(changed) != MATCHED_PAIRS:
                raise V5CampaignError("matched result count differs")
            differences = tuple(changed[i] - zero[i] for i in range(MATCHED_PAIRS))
            mean, ci, decision = _student_t_ci90(differences)
            report = CampaignReportV2(role=self.identity.role, campaign_fingerprint=self.identity.campaign_fingerprint, exact_novel_count=self.exact_novel_count, winner_candidate_key=None, winner_candidate=None, corrected_best_candidate_key=corrected_key, corrected_best_candidate=corrected.as_dict(), paired_differences=differences, paired_mean_difference_n=mean, paired_ci90_n=ci, paired_df=4, automatic_promotion=False, decision=decision, primary_winner_controller_sha256=self.identity.primary_winner_controller_sha256, matched_zero_maes_n=tuple(zero), matched_corrected_maes_n=tuple(changed))
        self._ledger.append("closeout", {"report": report.as_dict(), "ledger_head_before_closeout": self._ledger.head_sha256})
        self._closeout = report
        _atomic_json(self.report_path, report.as_dict())
        return report

    def report(self) -> CampaignReportV2:
        self._full_cold_rebuild()
        return self._ensure_closeout()

    def snapshot(self) -> dict[str, Any]:
        self._cold_rebuild()
        return {
            "schema": CAMPAIGN_SCHEMA,
            "version": CAMPAIGN_VERSION,
            "identity": self.identity.as_dict(),
            "ledger_head_sha256": self._ledger.head_sha256,
            "exact_novel_count": self.exact_novel_count,
            "dispatched_novel_count": self.dispatched_novel_count,
            "plan_count": len(self._plans),
            "outcome_count": len(self._outcomes),
            "paused": self._paused,
            "closeout": None if self._closeout is None else self._closeout.as_dict(),
        }


__all__ = [
    "CampaignConfigV2", "CampaignIdentityV2", "CampaignOutcomeV2", "CampaignPaused", "CampaignReportV2", "CampaignRoleV2", "OutcomeV2", "ProposalMethodV2", "ProposalReceiptV2", "TrialStageV2", "V5CampaignError", "V5CampaignV2", "V5ObservationGroupV2", "V5TrialPlan", "V5_WIRE_EPOCH", "build_v5_observation_groups",
]
