"""V4 attempt-ledger, GP eligibility, guard escalation, and promotion rules."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .contracts import TARGET_FORCE_N, V4Contract


class Disposition(str, Enum):
    GP_OBSERVATION = "gp_observation"
    ATTEMPT_LEDGER_ONLY = "attempt_ledger_only"
    FREEZE_BO = "freeze_bo"


@dataclass(frozen=True)
class AttemptEvidence:
    campaign_fingerprint: str
    eoat_sha256: str
    target_force_n: float
    kunwei_only_receipt: bool
    complete_bins: int
    terminal_closure: bool
    completion_closure: bool
    replay_closure: bool
    safety_failure: bool
    structural_failure: bool
    stale_or_nonfinite: bool
    incomplete: bool
    objective: float | None
    mae_n: float | None
    p99_normal_n: float | None
    max_force_norm_n: float | None
    max_torque_norm_nm: float | None


@dataclass(frozen=True)
class EligibilityDecision:
    disposition: Disposition
    eligible: bool
    reasons: tuple[str, ...]
    freeze_bo: bool
    zero_qdot_and_stop: bool


def evaluate_attempt(
    contract: V4Contract,
    attempt: AttemptEvidence,
    *,
    prior_eligible_count: int,
) -> EligibilityDecision:
    reasons: list[str] = []
    if attempt.safety_failure:
        reasons.append("safety_failure")
    if attempt.structural_failure:
        reasons.append("structural_failure")
    freeze = bool(reasons)
    if freeze:
        return EligibilityDecision(
            Disposition.FREEZE_BO,
            eligible=False,
            reasons=tuple(reasons),
            freeze_bo=True,
            zero_qdot_and_stop=True,
        )
    if attempt.campaign_fingerprint != contract.campaign_fingerprint:
        reasons.append("campaign_fingerprint_mismatch")
    if attempt.eoat_sha256 != contract.eoat_sha256:
        reasons.append("eoat_sha256_mismatch")
    if not math.isclose(
        attempt.target_force_n, TARGET_FORCE_N, rel_tol=0.0, abs_tol=1e-12
    ):
        reasons.append("target_not_5n")
    if not attempt.kunwei_only_receipt:
        reasons.append("kunwei_only_receipt_missing")
    if attempt.complete_bins != 550:
        reasons.append("objective_bins_not_550")
    if not attempt.terminal_closure:
        reasons.append("terminal_closure_missing")
    if not attempt.completion_closure:
        reasons.append("completion_closure_missing")
    if not attempt.replay_closure:
        reasons.append("replay_closure_missing")
    if attempt.stale_or_nonfinite:
        reasons.append("stale_or_nonfinite")
    if attempt.incomplete:
        reasons.append("incomplete")
    for name, value in (
        ("objective", attempt.objective),
        ("mae_n", attempt.mae_n),
        ("p99_normal_n", attempt.p99_normal_n),
        ("max_force_norm_n", attempt.max_force_norm_n),
        ("max_torque_norm_nm", attempt.max_torque_norm_nm),
    ):
        if value is None or not math.isfinite(float(value)):
            reasons.append(f"{name}_invalid")
    if prior_eligible_count < 10 and not reasons:
        assert attempt.p99_normal_n is not None
        assert attempt.max_force_norm_n is not None
        assert attempt.max_torque_norm_nm is not None
        if attempt.p99_normal_n > 8.0:
            reasons.append("first10_p99_normal_over_8n")
        if attempt.max_force_norm_n > 10.0:
            reasons.append("first10_force_norm_over_10n")
        if attempt.max_torque_norm_nm > 0.30:
            reasons.append("first10_torque_over_0p30nm")
    eligible = not reasons
    return EligibilityDecision(
        Disposition.GP_OBSERVATION
        if eligible
        else Disposition.ATTEMPT_LEDGER_ONLY,
        eligible=eligible,
        reasons=tuple(reasons),
        freeze_bo=False,
        zero_qdot_and_stop=False,
    )


def hard_guards_for_campaign(
    eligible_attempts: Sequence[AttemptEvidence],
) -> tuple[float, float, float]:
    if len(eligible_attempts) < 10:
        return (15.0, 20.0, 1.0)
    first_ten = eligible_attempts[:10]
    if all(
        not item.safety_failure
        and not item.structural_failure
        and item.p99_normal_n is not None
        and item.p99_normal_n <= 8.0
        and item.max_force_norm_n is not None
        and item.max_force_norm_n <= 10.0
        and item.max_torque_norm_nm is not None
        and item.max_torque_norm_nm <= 0.30
        for item in first_ten
    ):
        return (25.0, 30.0, 1.5)
    return (15.0, 20.0, 1.0)


def promotion_allowed(
    *,
    anchor_objective: float,
    retest_mae_n: Sequence[float],
    retest_objectives: Sequence[float],
) -> bool:
    if (
        len(retest_mae_n) != 3
        or len(retest_objectives) != 3
        or not math.isfinite(anchor_objective)
        or anchor_objective <= 0.0
        or not all(
            math.isfinite(value) for value in (*retest_mae_n, *retest_objectives)
        )
    ):
        return False
    passing_mae = sum(value <= 0.30 for value in retest_mae_n)
    median_objective = statistics.median(retest_objectives)
    return passing_mae >= 2 and median_objective <= 0.95 * anchor_objective


__all__ = [
    "AttemptEvidence",
    "Disposition",
    "EligibilityDecision",
    "evaluate_attempt",
    "hard_guards_for_campaign",
    "promotion_allowed",
]
