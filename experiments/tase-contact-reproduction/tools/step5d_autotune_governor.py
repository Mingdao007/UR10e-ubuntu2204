#!/usr/bin/env python3
"""Low-frequency adaptive governor for Step5d execution profiles."""

from __future__ import annotations

import math
from statistics import median
from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from step5d_autotune_contract import (
    ExecutionProfile,
    GovernorDecision,
    require_sha256,
)


PROFILE_LADDER = (0.1, 0.2, 0.5)
NORMAL_RATE_LADDER = (0.010, 0.015, 0.020)


@dataclass(frozen=True)
class SaturationSample:
    timestamp_s: float
    normal_filter_limited: bool = False
    qdot_limited: bool = False
    host_slew_limited: bool = False
    tp_accel_utilization: float = 0.0
    lag_s: float | None = None
    correlation: float | None = None
    nrmse: float | None = None
    cadence_ok: bool = True
    evidence_eligible: bool = True


@dataclass(frozen=True)
class SaturationTriggerEvidence:
    """Derived persistent-bottleneck facts for one immutable trial trace."""

    normal_filter_persistent: bool
    qdot_persistent: bool
    host_slew_persistent: bool
    tp_accel_persistent: bool
    tracking_degraded: bool

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")

    @classmethod
    def from_samples(
        cls,
        samples: Iterable[SaturationSample],
    ) -> "SaturationTriggerEvidence":
        rows = tuple(samples)
        if any(not isinstance(row, SaturationSample) for row in rows):
            raise ValueError("trigger samples must contain SaturationSample values")
        eligible = _eligible(rows)
        return cls(
            normal_filter_persistent=persistent_saturation(
                eligible, lambda sample: sample.normal_filter_limited
            ),
            qdot_persistent=persistent_saturation(
                eligible, lambda sample: sample.qdot_limited
            ),
            host_slew_persistent=persistent_saturation(
                eligible, lambda sample: sample.host_slew_limited
            ),
            tp_accel_persistent=persistent_saturation(
                eligible, lambda sample: sample.tp_accel_utilization >= 0.98
            ),
            tracking_degraded=any(
                (sample.lag_s is not None and sample.lag_s > 0.010)
                or (
                    sample.correlation is not None
                    and sample.correlation < 0.97
                )
                or (sample.nrmse is not None and sample.nrmse > 0.25)
                for sample in eligible
            ),
        )

    @classmethod
    def from_payload(cls, payload: object) -> "SaturationTriggerEvidence":
        if not isinstance(payload, Mapping) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise ValueError("saturation trigger payload fields are invalid")
        return cls(**dict(payload))

    def payload(self) -> dict[str, bool]:
        return {
            name: getattr(self, name) for name in self.__dataclass_fields__
        }


@dataclass(frozen=True)
class AbTrialIdentity:
    trial_uid: str
    force_candidate_uid: str
    profile_id: str
    plant_epoch: int

    def __post_init__(self) -> None:
        require_sha256("A/B trial_uid", self.trial_uid)
        require_sha256("A/B force_candidate_uid", self.force_candidate_uid)
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("A/B profile_id must be a non-empty string")
        if (
            isinstance(self.plant_epoch, bool)
            or not isinstance(self.plant_epoch, int)
            or self.plant_epoch < 1
        ):
            raise ValueError("A/B plant_epoch must be a positive integer")


def _finite_metric(name: str, value: object, *, non_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    if non_negative and parsed < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


@dataclass(frozen=True)
class AbEvidence:
    burden_a: float
    burden_b: float
    mae_a_n: float
    mae_b_n: float
    tracking_not_worse: bool
    orientation_not_worse: bool
    guards_clean: bool
    safe_closure: bool
    lag_improvement_s: float = 0.0
    nrmse_improvement_ratio: float = 0.0
    correlation_improvement: float = 0.0
    phase: str = "ab"
    identity_a: AbTrialIdentity | None = None
    identity_b: AbTrialIdentity | None = None
    identity_a_prime: AbTrialIdentity | None = None
    burden_a_prime: float | None = None
    mae_a_prime_n: float | None = None

    def __post_init__(self) -> None:
        for name in ("burden_a", "burden_b", "mae_a_n", "mae_b_n"):
            object.__setattr__(
                self,
                name,
                _finite_metric(name, getattr(self, name), non_negative=True),
            )
        for name in (
            "lag_improvement_s",
            "nrmse_improvement_ratio",
            "correlation_improvement",
        ):
            object.__setattr__(self, name, _finite_metric(name, getattr(self, name)))
        for name in (
            "tracking_not_worse",
            "orientation_not_worse",
            "guards_clean",
            "safe_closure",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if self.phase not in {"ab", "a_prime"}:
            raise ValueError("A/B evidence phase must be ab or a_prime")
        for name in ("identity_a", "identity_b", "identity_a_prime"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, AbTrialIdentity):
                raise ValueError(f"{name} must be AbTrialIdentity or None")
        for name in ("burden_a_prime", "mae_a_prime_n"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _finite_metric(name, value, non_negative=True),
                )


def _eligible(samples: Iterable[SaturationSample]) -> list[SaturationSample]:
    return sorted(
        (sample for sample in samples if sample.evidence_eligible and sample.cadence_ok),
        key=lambda sample: sample.timestamp_s,
    )


def persistent_saturation(
    samples: Iterable[SaturationSample],
    predicate,
    *,
    window_s: float = 5.0,
    minimum_fraction: float = 0.20,
    continuous_s: float = 2.0,
    maximum_sample_gap_s: float = 0.25,
) -> bool:
    rows = _eligible(samples)
    if len(rows) < 2:
        return False
    if (
        not math.isfinite(window_s)
        or not math.isfinite(minimum_fraction)
        or not math.isfinite(continuous_s)
        or not math.isfinite(maximum_sample_gap_s)
        or window_s <= 0.0
        or continuous_s <= 0.0
        or maximum_sample_gap_s <= 0.0
        or not 0.0 <= minimum_fraction <= 1.0
    ):
        raise ValueError("saturation coverage thresholds are invalid")
    deltas = [
        later.timestamp_s - earlier.timestamp_s
        for earlier, later in zip(rows, rows[1:])
    ]
    if any(
        not math.isfinite(row.timestamp_s) for row in rows
    ) or any(delta <= 0.0 for delta in deltas):
        return False
    nominal_period_s = median(deltas)
    if nominal_period_s > maximum_sample_gap_s:
        return False
    maximum_contiguous_gap_s = min(
        maximum_sample_gap_s,
        max(0.01, 2.5 * nominal_period_s),
    )
    run_start: float | None = None
    previous_time: float | None = None
    for sample in rows:
        if previous_time is not None and sample.timestamp_s - previous_time > maximum_contiguous_gap_s:
            run_start = None
        if predicate(sample):
            if run_start is None:
                run_start = sample.timestamp_s
            if sample.timestamp_s - run_start >= continuous_s:
                return True
        else:
            run_start = None
        previous_time = sample.timestamp_s

    origin = rows[0].timestamp_s
    windows: dict[int, list[bool]] = {}
    for sample in rows:
        index = int(math.floor((sample.timestamp_s - origin) / window_s))
        windows.setdefault(index, []).append(bool(predicate(sample)))
    saturated_windows = []
    for index, values in windows.items():
        window_rows = [
            sample
            for sample in rows
            if int(math.floor((sample.timestamp_s - origin) / window_s)) == index
        ]
        window_start_s = origin + index * window_s
        window_end_s = window_start_s + window_s
        window_gaps = [
            later.timestamp_s - earlier.timestamp_s
            for earlier, later in zip(window_rows, window_rows[1:])
        ]
        minimum_rows = max(2, math.ceil(window_s / maximum_sample_gap_s))
        coverage_complete = bool(
            len(window_rows) >= minimum_rows
            and window_rows[0].timestamp_s - window_start_s
            <= maximum_contiguous_gap_s
            and window_end_s
            - (window_rows[-1].timestamp_s + nominal_period_s)
            <= maximum_contiguous_gap_s
            and all(gap <= maximum_contiguous_gap_s for gap in window_gaps)
        )
        if (
            coverage_complete
            and values
            and sum(values) / len(values) >= minimum_fraction
        ):
            saturated_windows.append(index)
    return len(saturated_windows) >= 2


def profile_orientation_qualified(metrics: dict[str, float | None]) -> bool:
    p95 = metrics.get("orientation_error_p95_rad")
    maximum = metrics.get("orientation_error_max_rad")
    angular_duty = metrics.get("angular_saturation_duty")
    return (
        p95 is not None
        and maximum is not None
        and angular_duty is not None
        and p95 <= 0.036
        and maximum <= 0.05
        and angular_duty <= 0.05
    )


def _next(value: float, ladder: tuple[float, ...]) -> float | None:
    for item in ladder:
        if item > value + 1e-12:
            return item
    return None


def _profile_id(normal_rate: float, host_slew: float, tp_accel: float) -> str:
    return (
        f"nf{round(normal_rate * 1000):03d}"
        f"-slew{round(host_slew * 100):03d}"
        f"-a{round(tp_accel * 100):03d}"
    )


def propose_change(
    profile: ExecutionProfile,
    samples: Iterable[SaturationSample],
    *,
    plant_epoch: int,
    cooldown_remaining: int,
) -> tuple[ExecutionProfile | None, GovernorDecision]:
    rows = _eligible(samples)
    trigger = SaturationTriggerEvidence.from_samples(rows)
    return propose_change_from_trigger(
        profile,
        trigger,
        plant_epoch=plant_epoch,
        cooldown_remaining=cooldown_remaining,
        evidence_available=bool(rows),
    )


def propose_change_from_trigger(
    profile: ExecutionProfile,
    trigger: SaturationTriggerEvidence,
    *,
    plant_epoch: int,
    cooldown_remaining: int,
    evidence_available: bool = True,
) -> tuple[ExecutionProfile | None, GovernorDecision]:
    """Choose one profile probe from immutable, already-derived evidence."""

    if not isinstance(trigger, SaturationTriggerEvidence):
        raise ValueError("trigger must be SaturationTriggerEvidence")
    if type(evidence_available) is not bool:
        raise ValueError("evidence_available must be a boolean")
    if cooldown_remaining > 0:
        return None, GovernorDecision(
            action="hold",
            layer="cooldown",
            from_profile_id=profile.profile_id,
            to_profile_id=None,
            keep=None,
            reason=f"cooldown_{cooldown_remaining}_eligible_trials_remaining",
            plant_epoch_before=plant_epoch,
            plant_epoch_after=plant_epoch,
        )
    if not evidence_available:
        return None, GovernorDecision(
            action="hold",
            layer="evidence",
            from_profile_id=profile.profile_id,
            to_profile_id=None,
            keep=None,
            reason="no_cadence_eligible_saturation_evidence",
            plant_epoch_before=plant_epoch,
            plant_epoch_after=plant_epoch,
        )

    if trigger.normal_filter_persistent:
        rate = _next(profile.normal_max_rate_rad_s, NORMAL_RATE_LADDER)
        if rate is not None:
            candidate = replace(
                profile,
                profile_id=_profile_id(rate, profile.host_qdot_slew_rad_s2, profile.tp_speedj_accel_rad_s2),
                normal_max_rate_rad_s=rate,
            )
            return candidate, GovernorDecision(
                action="ab_probe",
                layer="normal_filter_rate",
                from_profile_id=profile.profile_id,
                to_profile_id=candidate.profile_id,
                keep=None,
                reason="persistent_normal_filter_rate_limiting",
                plant_epoch_before=plant_epoch,
                plant_epoch_after=plant_epoch,
            )

    # qdot=.5 is telemetry-only by contract, even when saturation is observed.
    if trigger.host_slew_persistent:
        slew = _next(profile.host_qdot_slew_rad_s2, PROFILE_LADDER)
        if slew is not None and slew > profile.tp_speedj_accel_rad_s2 + 1e-12:
            accel = _next(profile.tp_speedj_accel_rad_s2, PROFILE_LADDER)
            if accel is not None:
                candidate = replace(
                    profile,
                    profile_id=_profile_id(
                        profile.normal_max_rate_rad_s,
                        profile.host_qdot_slew_rad_s2,
                        accel,
                    ),
                    tp_speedj_accel_rad_s2=accel,
                )
                return candidate, GovernorDecision(
                    action="ab_probe",
                    layer="tp_speedj_acceleration",
                    from_profile_id=profile.profile_id,
                    to_profile_id=candidate.profile_id,
                    keep=None,
                    reason="tp_headroom_probe_before_host_slew_increase",
                    plant_epoch_before=plant_epoch,
                    plant_epoch_after=plant_epoch,
                )
        elif slew is not None:
            candidate = replace(
                profile,
                profile_id=_profile_id(profile.normal_max_rate_rad_s, slew, profile.tp_speedj_accel_rad_s2),
                host_qdot_slew_rad_s2=slew,
            )
            return candidate, GovernorDecision(
                action="ab_probe",
                layer="host_qdot_slew",
                from_profile_id=profile.profile_id,
                to_profile_id=candidate.profile_id,
                keep=None,
                reason="persistent_host_slew_limiting",
                plant_epoch_before=plant_epoch,
                plant_epoch_after=plant_epoch,
            )

    if trigger.tp_accel_persistent and trigger.tracking_degraded:
        accel = _next(profile.tp_speedj_accel_rad_s2, PROFILE_LADDER)
        if accel is not None:
            candidate = replace(
                profile,
                profile_id=_profile_id(profile.normal_max_rate_rad_s, profile.host_qdot_slew_rad_s2, accel),
                tp_speedj_accel_rad_s2=accel,
            )
            return candidate, GovernorDecision(
                action="ab_probe",
                layer="tp_speedj_acceleration",
                from_profile_id=profile.profile_id,
                to_profile_id=candidate.profile_id,
                keep=None,
                reason="persistent_tp_accel_demand_with_tracking_degradation",
                plant_epoch_before=plant_epoch,
                plant_epoch_after=plant_epoch,
            )

    return None, GovernorDecision(
        action="hold",
        layer="none",
        from_profile_id=profile.profile_id,
        to_profile_id=None,
        keep=None,
        reason="no_persistent_actionable_bottleneck",
        plant_epoch_before=plant_epoch,
        plant_epoch_after=plant_epoch,
        evidence={
            "qdot_saturation_telemetry_only": trigger.qdot_persistent,
        },
    )


def _relative_delta(a: float, b: float) -> float:
    return abs(a - b) / max(min(a, b), 1e-12)


def _validate_ab_identities(
    *,
    profile_a: ExecutionProfile,
    profile_b: ExecutionProfile,
    evidence: AbEvidence,
    plant_epoch: int,
) -> tuple[AbTrialIdentity, AbTrialIdentity, AbTrialIdentity | None]:
    identity_a = evidence.identity_a
    identity_b = evidence.identity_b
    if identity_a is None or identity_b is None:
        raise ValueError("A/B evidence requires exact A and B trial identities")
    if identity_a.profile_id != profile_a.profile_id:
        raise ValueError("A trial identity does not match from profile")
    if identity_b.profile_id != profile_b.profile_id:
        raise ValueError("B trial identity does not match to profile")
    if identity_a.plant_epoch != plant_epoch or identity_b.plant_epoch != plant_epoch:
        raise ValueError("A/B trial identities do not match plant epoch")
    if identity_a.force_candidate_uid != identity_b.force_candidate_uid:
        raise ValueError("A/B trials must bind the same force candidate UID")
    if identity_a.trial_uid == identity_b.trial_uid:
        raise ValueError("A/B trials must have distinct trial UIDs")

    identity_a_prime = evidence.identity_a_prime
    if evidence.phase == "ab":
        if (
            identity_a_prime is not None
            or evidence.burden_a_prime is not None
            or evidence.mae_a_prime_n is not None
        ):
            raise ValueError("ab phase must not contain A-prime evidence")
        return identity_a, identity_b, None

    if (
        identity_a_prime is None
        or evidence.burden_a_prime is None
        or evidence.mae_a_prime_n is None
    ):
        raise ValueError("a_prime phase requires exact A-prime identity and metrics")
    if identity_a_prime.profile_id != profile_a.profile_id:
        raise ValueError("A-prime trial identity must use the from profile")
    if identity_a_prime.plant_epoch != plant_epoch:
        raise ValueError("A-prime trial identity does not match plant epoch")
    if identity_a_prime.force_candidate_uid != identity_a.force_candidate_uid:
        raise ValueError("A/B/A-prime trials must bind the same force candidate UID")
    if identity_a_prime.trial_uid in {identity_a.trial_uid, identity_b.trial_uid}:
        raise ValueError("A/B/A-prime trials must have distinct trial UIDs")
    return identity_a, identity_b, identity_a_prime


def assess_ab(
    *,
    layer: str,
    profile_a: ExecutionProfile,
    profile_b: ExecutionProfile,
    evidence: AbEvidence,
    plant_epoch: int,
) -> GovernorDecision:
    identity_a, identity_b, identity_a_prime = _validate_ab_identities(
        profile_a=profile_a,
        profile_b=profile_b,
        evidence=evidence,
        plant_epoch=plant_epoch,
    )
    reference_burden = evidence.burden_a
    reference_mae = evidence.mae_a_n
    a_prime_repeatable: bool | None = None
    if evidence.phase == "a_prime":
        assert evidence.burden_a_prime is not None
        assert evidence.mae_a_prime_n is not None
        a_prime_repeatable = bool(
            _relative_delta(evidence.burden_a, evidence.burden_a_prime) <= 0.15
            and _relative_delta(evidence.mae_a_n, evidence.mae_a_prime_n) <= 0.15
        )
        # A-prime exists to estimate baseline burden noise.  Using min(A,A')
        # makes every initially-ambiguous 10-30% result mathematically unable
        # to clear the 30% keep threshold.  Average the repeatable burden pair,
        # while retaining the smaller (more conservative) MAE baseline.
        reference_burden = 0.5 * (
            evidence.burden_a + evidence.burden_a_prime
        )
        reference_mae = min(evidence.mae_a_n, evidence.mae_a_prime_n)
    burden_reduction = (reference_burden - evidence.burden_b) / max(
        reference_burden, 1e-12
    )
    mae_ok = evidence.mae_b_n <= 1.15 * reference_mae
    common_ok = (
        mae_ok
        and evidence.tracking_not_worse
        and evidence.orientation_not_worse
        and evidence.guards_clean
        and evidence.safe_closure
        and a_prime_repeatable is not False
    )
    tp_tracking_improved = (
        evidence.lag_improvement_s >= 0.002
        or evidence.nrmse_improvement_ratio >= 0.20
        or evidence.correlation_improvement >= 0.01
    )
    keep = burden_reduction >= 0.30 and common_ok
    if layer == "tp_speedj_acceleration":
        keep = keep and tp_tracking_improved
    ambiguous = 0.10 <= burden_reduction < 0.30 and common_ok
    identity_evidence = {
        "phase": evidence.phase,
        "force_candidate_uid": identity_a.force_candidate_uid,
        "trial_a_uid": identity_a.trial_uid,
        "trial_b_uid": identity_b.trial_uid,
        "from_profile_id": identity_a.profile_id,
        "to_profile_id": identity_b.profile_id,
        "plant_epoch": identity_a.plant_epoch,
        "trial_a_prime_uid": (
            None if identity_a_prime is None else identity_a_prime.trial_uid
        ),
    }
    if ambiguous and evidence.phase == "ab":
        return GovernorDecision(
            action="repeat_a_prime",
            layer=layer,
            from_profile_id=profile_a.profile_id,
            to_profile_id=profile_b.profile_id,
            keep=None,
            reason="ab_improvement_ambiguous_10_to_30_percent",
            plant_epoch_before=plant_epoch,
            plant_epoch_after=plant_epoch,
            evidence={
                **identity_evidence,
                "burden_reduction": burden_reduction,
                "a_prime_required": True,
            },
        )
    if evidence.phase == "a_prime" and not keep:
        failure_reason = (
            "a_prime_repeatability_failed"
            if a_prime_repeatable is False
            else "a_prime_confirmation_failed"
        )
    else:
        failure_reason = "ab_acceptance_failed"
    return GovernorDecision(
        action="keep" if keep else "revert",
        layer=layer,
        from_profile_id=profile_a.profile_id,
        to_profile_id=profile_b.profile_id,
        keep=keep,
        reason="ab_acceptance_passed" if keep else failure_reason,
        plant_epoch_before=plant_epoch,
        plant_epoch_after=plant_epoch + 1 if keep else plant_epoch,
        evidence={
            **identity_evidence,
            "burden_reduction": burden_reduction,
            "mae_non_regression": mae_ok,
            "tp_tracking_improved": tp_tracking_improved,
            "a_prime_repeatable": a_prime_repeatable,
            "reference_burden": reference_burden,
            "reference_mae_n": reference_mae,
            "a_prime_burden_aggregation": (
                "repeatable_pair_mean" if evidence.phase == "a_prime" else None
            ),
        },
    )
