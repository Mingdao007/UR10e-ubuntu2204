"""Qualification-aware fixed-confidence finite-stopping engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Iterable

from .catalog import CATALOG_SIZE, build_frozen_catalog
from .common import R014Error, finite


ETA_N = 0.10
DELTA = 0.05
SIGMA0_N = 0.10
PHYSICAL_ATTEMPT_CAP = 520


class CertificationOutcome(str, Enum):
    RUNNING = "running"
    CERTIFIED = "certified"
    PAUSED_QUALIFICATION = "paused_qualification_inconclusive"
    INVALID_SAFETY = "invalid_safety"
    INCONCLUSIVE_CAP = "inconclusive_cap"
    INVALID_ASSUMPTION = "invalid_assumption"


@dataclass(frozen=True)
class AttemptAdmission:
    arm_id: str
    loss_n: float | None
    motion_gate: bool
    timing_gate: bool
    qualification: bool
    exact: bool
    sealed: bool
    full_duration: bool
    censored: bool = False
    hard_safety_veto: bool = False
    certification_pull: bool = False
    # The owner supplied physical-attempt identity is scoped to one campaign.
    # None is retained for compatibility with old in-memory callers; new
    # callers should always bind the original physical attempt id.
    physical_attempt_id: str | None = None

    @property
    def exact_eligible(self) -> bool:
        return (
            self.motion_gate
            and self.timing_gate
            and self.qualification
            and self.exact
            and self.sealed
            and self.full_duration
            and not self.censored
            and not self.hard_safety_veto
            and self.loss_n is not None
        )

    @property
    def gp_eligible(self) -> bool:
        return self.exact_eligible


def _attempt_payload(attempt: AttemptAdmission) -> tuple[Any, ...]:
    return (
        attempt.arm_id,
        attempt.loss_n,
        attempt.motion_gate,
        attempt.timing_gate,
        attempt.qualification,
        attempt.exact,
        attempt.sealed,
        attempt.full_duration,
        attempt.censored,
        attempt.hard_safety_veto,
        attempt.certification_pull,
    )


def validate_attempt(attempt: AttemptAdmission) -> None:
    """Validate an original physical admission before it reaches any sink."""

    if not isinstance(attempt, AttemptAdmission):
        raise R014Error("attempt admission must be typed")
    if not isinstance(attempt.arm_id, str) or not attempt.arm_id.strip():
        raise R014Error("attempt arm id must be a nonempty string")
    if (
        not isinstance(attempt.physical_attempt_id, str)
        or not attempt.physical_attempt_id.strip()
    ):
        raise R014Error("physical attempt id is required and must be nonempty")
    for name in (
        "motion_gate",
        "timing_gate",
        "qualification",
        "exact",
        "sealed",
        "full_duration",
        "censored",
        "hard_safety_veto",
        "certification_pull",
    ):
        if type(getattr(attempt, name)) is not bool:
            raise R014Error(f"attempt {name} must be a strict boolean")
    if attempt.loss_n is not None:
        loss = finite(attempt.loss_n, "loss_n")
        if loss < 0.0:
            raise R014Error("loss_n must be nonnegative")


@dataclass
class ArmEvidence:
    exact_losses_n: list[float] = field(default_factory=list)
    consecutive_qualification_failures: int = 0
    forced_full_seen: bool = False

    @property
    def n(self) -> int:
        return len(self.exact_losses_n)

    @property
    def mean(self) -> float:
        if not self.exact_losses_n:
            return math.inf
        return sum(self.exact_losses_n) / len(self.exact_losses_n)


@dataclass(frozen=True)
class CertificateSnapshot:
    outcome: CertificationOutcome
    attempt_count: int
    certification_pulls: int
    best_arm_id: str | None
    challenger_arm_id: str | None
    upper_best_n: float | None
    lower_challenger_n: float | None
    eta_n: float
    delta: float
    sigma0_n: float
    all_arms_forced_full: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "attempt_count": self.attempt_count,
            "certification_pulls": self.certification_pulls,
            "best_arm_id": self.best_arm_id,
            "challenger_arm_id": self.challenger_arm_id,
            "upper_best_n": self.upper_best_n,
            "lower_challenger_n": self.lower_challenger_n,
            "eta_n": self.eta_n,
            "delta": self.delta,
            "sigma0_n": self.sigma0_n,
            "all_arms_forced_full": self.all_arms_forced_full,
        }


def anytime_radius(n: int, *, sigma0_n: float = SIGMA0_N, delta: float = DELTA) -> float:
    if type(n) is not int or n < 1:
        raise R014Error("confidence radius requires n >= 1")
    sigma = finite(sigma0_n, "sigma0_n")
    probability = finite(delta, "delta")
    if sigma <= 0.0 or not 0.0 < probability < 1.0:
        raise R014Error("invalid confidence-radius assumptions")
    return sigma * math.sqrt(
        2.0 / n * math.log(math.pi**2 * CATALOG_SIZE * n**2 / (3.0 * probability))
    )


class CertificationEngine:
    def __init__(self) -> None:
        arm_ids = tuple(arm.arm_id for arm in build_frozen_catalog())
        self.arms = {arm_id: ArmEvidence() for arm_id in arm_ids}
        self.attempt_count = 0
        self.certification_pulls = 0
        self.outcome = CertificationOutcome.RUNNING
        self.assumption_valid = True
        self._seen_attempts: dict[
            str, tuple[tuple[Any, ...], CertificateSnapshot]
        ] = {}

    def _remember(
        self, attempt: AttemptAdmission, snapshot: CertificateSnapshot
    ) -> CertificateSnapshot:
        if attempt.physical_attempt_id is not None:
            self._seen_attempts[attempt.physical_attempt_id] = (
                _attempt_payload(attempt),
                snapshot,
            )
        return snapshot

    def record(self, attempt: AttemptAdmission) -> CertificateSnapshot:
        validate_attempt(attempt)
        physical_id = attempt.physical_attempt_id
        if physical_id is not None and physical_id in self._seen_attempts:
            previous_payload, previous_snapshot = self._seen_attempts[physical_id]
            if previous_payload != _attempt_payload(attempt):
                raise R014Error("physical attempt id payload conflicts with prior record")
            return previous_snapshot
        if self.outcome is not CertificationOutcome.RUNNING:
            raise R014Error(f"certificate engine is terminal: {self.outcome.value}")
        if attempt.arm_id not in self.arms:
            raise R014Error(f"attempt arm is outside frozen catalog: {attempt.arm_id}")
        self.attempt_count += 1
        evidence = self.arms[attempt.arm_id]

        if attempt.hard_safety_veto:
            self.outcome = CertificationOutcome.INVALID_SAFETY
            return self._remember(attempt, self.snapshot())

        if not attempt.qualification:
            evidence.consecutive_qualification_failures += 1
            if evidence.consecutive_qualification_failures >= 3:
                self.outcome = CertificationOutcome.PAUSED_QUALIFICATION
                return self._remember(attempt, self.snapshot())
        else:
            evidence.consecutive_qualification_failures = 0

        # Discovery may be useful to the GP, but it is never a certification
        # loss.  Only a fully eligible, explicitly marked certification pull
        # changes certificate-arm evidence and forced-full coverage.
        certification_eligible = attempt.certification_pull and attempt.exact_eligible
        if certification_eligible:
            assert attempt.loss_n is not None
            evidence.exact_losses_n.append(finite(attempt.loss_n, "loss_n"))
            self.certification_pulls += 1
            evidence.forced_full_seen = True

        if self.attempt_count >= PHYSICAL_ATTEMPT_CAP:
            possible = self.snapshot()
            if possible.outcome is not CertificationOutcome.CERTIFIED:
                self.outcome = CertificationOutcome.INCONCLUSIVE_CAP
            return self._remember(attempt, self.snapshot())

        return self._remember(attempt, self.snapshot())

    def invalidate_assumption(self) -> CertificateSnapshot:
        if self.outcome is CertificationOutcome.RUNNING:
            self.assumption_valid = False
            self.outcome = CertificationOutcome.INVALID_ASSUMPTION
        return self.snapshot()

    def _bounds(self) -> tuple[str, str, float, float] | None:
        if not all(evidence.forced_full_seen and evidence.n >= 1 for evidence in self.arms.values()):
            return None
        best = min(self.arms, key=lambda arm_id: (self.arms[arm_id].mean, arm_id))
        challengers = [arm_id for arm_id in self.arms if arm_id != best]
        challenger = min(
            challengers,
            key=lambda arm_id: (
                self.arms[arm_id].mean - anytime_radius(self.arms[arm_id].n),
                arm_id,
            ),
        )
        upper_best = self.arms[best].mean + anytime_radius(self.arms[best].n)
        lower_challenger = self.arms[challenger].mean - anytime_radius(
            self.arms[challenger].n
        )
        return best, challenger, upper_best, lower_challenger

    def snapshot(self) -> CertificateSnapshot:
        bounds = self._bounds() if self.assumption_valid else None
        if self.outcome is CertificationOutcome.RUNNING and bounds is not None:
            if bounds[2] <= bounds[3] + ETA_N:
                self.outcome = CertificationOutcome.CERTIFIED
        return CertificateSnapshot(
            outcome=self.outcome,
            attempt_count=self.attempt_count,
            certification_pulls=self.certification_pulls,
            best_arm_id=None if bounds is None else bounds[0],
            challenger_arm_id=None if bounds is None else bounds[1],
            upper_best_n=None if bounds is None else bounds[2],
            lower_challenger_n=None if bounds is None else bounds[3],
            eta_n=ETA_N,
            delta=DELTA,
            sigma0_n=SIGMA0_N,
            all_arms_forced_full=bounds is not None,
        )

    def next_arm(self) -> str:
        if self.outcome is not CertificationOutcome.RUNNING:
            raise R014Error(f"certificate engine is terminal: {self.outcome.value}")
        missing = [arm_id for arm_id, evidence in self.arms.items() if not evidence.forced_full_seen]
        if missing:
            return missing[0]
        if self.certification_pulls > 0 and self.certification_pulls % CATALOG_SIZE == 0:
            return min(self.arms, key=lambda arm_id: (self.arms[arm_id].n, arm_id))
        bounds = self._bounds()
        if bounds is None:
            raise R014Error("certificate bounds unavailable after forced-full coverage")
        best, challenger = bounds[:2]
        best_radius = anytime_radius(self.arms[best].n)
        challenger_radius = anytime_radius(self.arms[challenger].n)
        return best if best_radius >= challenger_radius else challenger


def gp_training_rows(attempts: Iterable[AttemptAdmission]) -> list[AttemptAdmission]:
    """Central admission seam: censored or ineligible attempts never enter the GP."""

    rows: list[AttemptAdmission] = []
    seen: dict[str, tuple[Any, ...]] = {}
    for attempt in attempts:
        validate_attempt(attempt)
        assert attempt.physical_attempt_id is not None
        payload = _attempt_payload(attempt)
        previous = seen.get(attempt.physical_attempt_id)
        if previous is not None:
            if previous != payload:
                raise R014Error("physical attempt id payload conflicts in GP rows")
            continue
        seen[attempt.physical_attempt_id] = payload
        if attempt.gp_eligible:
            rows.append(attempt)
    return rows
