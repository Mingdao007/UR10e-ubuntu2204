"""R011 serial scheduler and repeat-confirmed incumbent aggregation."""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .censor import CensoredObservation, ExactObservation, Observation, validate_observation
from .common import R012ValueError, finite, json_tree
from .qlognei import NORMAL_FILTER_TAU_S, physical_candidate_key


SCHEDULER_SCHEMA = "step5d.autotune-v4/r011-confirmed-incumbent-scheduler-v2"
OBJECTIVE_SEMANTICS = "force-mae-v2-sealed"
MAX_RUNTIME_S = 10 * 60 * 60
MAX_ATTEMPTS = 320
REPEAT_CONFIRMATIONS = 3
NOVEL_RETEST_INTERVAL = 8

REFERENCE_CANDIDATE: dict[str, Any] = {
    # Reuse a sealed, eligible B3 physical point (attempt 589) as the R012
    # reference.  The prior theoretical reference hard-stopped on force norm;
    # this point completed PATH60 with MAE 0.7717202989 N on the same bench.
    "force_p_gain": 0.006727171322157698,
    "force_damping": 56.0,
    "force_i_gain": 0.0,
    "i_off": True,
    "normal_filter_tau_s": NORMAL_FILTER_TAU_S,
    "orientation_ko": 0.05946035575013606,
    "motion_kp": 1.5,
    "target_force_n": 5.0,
}


class SchedulerError(R012ValueError):
    """Candidate aggregation or serial scheduling is invalid."""


def scheduler_candidate_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    """Physical identity that permits the published Ko anchor outside BO box."""

    required = ("force_p_gain", "force_damping", "force_i_gain", "i_off", "normal_filter_tau_s", "orientation_ko", "motion_kp")
    if any(key not in candidate for key in required):
        raise SchedulerError("candidate is missing physical gain fields")
    return tuple(candidate[key] for key in required) + (candidate.get("target_force_n", 5.0),)


@dataclass(frozen=True)
class CandidateAggregate:
    key: tuple[Any, ...]
    candidate: Mapping[str, Any]
    exact_observations: tuple[ExactObservation, ...]
    censored_count: int = 0

    @property
    def exact_count(self) -> int:
        return len(self.exact_observations)

    @property
    def confirmed(self) -> bool:
        return self.exact_count >= REPEAT_CONFIRMATIONS and all(row.full_observation and row.eligible for row in self.exact_observations)

    @property
    def arithmetic_mean_sealed_mae_n(self) -> float | None:
        return None if not self.exact_observations else sum(row.objective_n for row in self.exact_observations) / self.exact_count

    @property
    def single_observed_minimum_n(self) -> float | None:
        return None if not self.exact_observations else min(row.objective_n for row in self.exact_observations)

    def as_dict(self) -> dict[str, Any]:
        return {"key": json_tree(self.key), "candidate": json_tree(self.candidate), "exact_count": self.exact_count, "censored_count": self.censored_count, "confirmed": self.confirmed, "arithmetic_mean_sealed_mae_n": self.arithmetic_mean_sealed_mae_n, "single_observed_minimum_n": self.single_observed_minimum_n}


@dataclass(frozen=True)
class ScheduledCandidate:
    candidate: Mapping[str, Any]
    kind: str
    ordinal: int
    confirmation_index: int | None = None
    abort_allowed: bool = False

    def __post_init__(self) -> None:
        if self.kind in {"CHALLENGER_CONFIRM", "REFERENCE", "INCUMBENT_RETEST", "QUALIFICATION"} and self.abort_allowed:
            raise SchedulerError("confirmation/qualification runs cannot early-abort")
        object.__setattr__(self, "candidate", json_tree(self.candidate))

    @property
    def key(self) -> tuple[Any, ...]:
        return scheduler_candidate_key(self.candidate)

    def as_dict(self) -> dict[str, Any]:
        return {"schema": "step5d.autotune-v4/r012-scheduled-candidate-v1", "candidate": json_tree(self.candidate), "kind": self.kind, "ordinal": self.ordinal, "confirmation_index": self.confirmation_index, "abort_allowed": self.abort_allowed}


class ConfirmedIncumbentScheduler:
    """A serial FIFO scheduler with exact repeat confirmation semantics."""

    def __init__(self, *, campaign_id: str, run_id: str, attempt_id: str, objective_semantics: str = OBJECTIVE_SEMANTICS, max_runtime_s: float = MAX_RUNTIME_S, max_attempts: int = MAX_ATTEMPTS) -> None:
        if not all(isinstance(value, str) and value for value in (campaign_id, run_id, attempt_id)):
            raise SchedulerError("scheduler campaign/run/attempt identity is invalid")
        if objective_semantics != OBJECTIVE_SEMANTICS or max_runtime_s != MAX_RUNTIME_S or max_attempts != MAX_ATTEMPTS:
            raise SchedulerError("R011 scheduler bound/objective differs")
        self.campaign_id = campaign_id
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.objective_semantics = objective_semantics
        self.max_runtime_s = max_runtime_s
        self.max_attempts = max_attempts
        self._queue: deque[ScheduledCandidate] = deque()
        self._observations: list[Observation] = []
        self._aggregates: dict[tuple[Any, ...], list[ExactObservation]] = defaultdict(list)
        self._censored: dict[tuple[Any, ...], int] = defaultdict(int)
        self._pending: dict[tuple[Any, ...], int] = defaultdict(int)
        self._in_flight: ScheduledCandidate | None = None
        self._ordinal = 0
        self._dispatch_count = 0
        self._runtime_start = time.monotonic()
        self._novel_bo_dispatches = 0
        self._single_minimum: float | None = None
        self._confirmation_enqueued: dict[tuple[Any, ...], int] = defaultdict(int)
        self._fresh_reference_queue()

    def _fresh_reference_queue(self) -> None:
        self._enqueue(REFERENCE_CANDIDATE, "REFERENCE", 1, 1)

    def _enqueue(self, candidate: Mapping[str, Any], kind: str, confirmation_index: int | None = None, ordinal: int | None = None) -> None:
        if self._queue or self._in_flight is not None:
            raise SchedulerError("R012 serial q=1 already has an active item")
        self._ordinal = max(self._ordinal, ordinal or self._ordinal + 1)
        item = ScheduledCandidate(candidate, kind, self._ordinal, confirmation_index, kind in {"BO_TRIAL", "NOVEL_BO"})
        self._queue.append(item)
        self._pending[item.key] += 1

    @property
    def observations(self) -> tuple[Observation, ...]:
        return tuple(self._observations)

    @property
    def pending(self) -> tuple[ScheduledCandidate, ...]:
        return tuple(self._queue)

    @property
    def in_flight(self) -> ScheduledCandidate | None:
        return self._in_flight

    @property
    def aggregates(self) -> tuple[CandidateAggregate, ...]:
        keys = set(self._aggregates) | set(self._censored)
        return tuple(CandidateAggregate(key, self._aggregates[key][0].candidate if self._aggregates[key] else {}, tuple(self._aggregates[key]), self._censored[key]) for key in sorted(keys, key=repr))

    @property
    def confirmed_incumbent(self) -> CandidateAggregate | None:
        candidates = [aggregate for aggregate in self.aggregates if aggregate.confirmed and aggregate.arithmetic_mean_sealed_mae_n is not None]
        return min(candidates, key=lambda item: (float(item.arithmetic_mean_sealed_mae_n), repr(item.key))) if candidates else None

    @property
    def confirmed_incumbent_mean_n(self) -> float | None:
        incumbent = self.confirmed_incumbent
        return None if incumbent is None else incumbent.arithmetic_mean_sealed_mae_n

    @property
    def single_observed_minimum_n(self) -> float | None:
        return self._single_minimum

    @property
    def novel_bo_dispatches(self) -> int:
        return self._novel_bo_dispatches

    @property
    def evaluated_keys(self) -> tuple[tuple[Any, ...], ...]:
        return tuple(sorted(set(self._aggregates) | set(self._censored), key=repr))

    def next(self) -> ScheduledCandidate:
        if self._in_flight is not None:
            raise SchedulerError("serial q=1 dispatch already in flight")
        self._check_bounds()
        if not self._queue:
            raise SchedulerError("R011 scheduler queue is empty")
        item = self._queue.popleft()
        self._dispatch_count += 1
        self._pending[item.key] -= 1
        if self._pending[item.key] <= 0:
            del self._pending[item.key]
        self._in_flight = item
        if item.kind in {"BO_TRIAL", "NOVEL_BO"}:
            self._novel_bo_dispatches += 1
        return item

    def _check_bounds(self) -> None:
        if self._dispatch_count >= self.max_attempts:
            raise SchedulerError("R011 attempt bound reached; scheduler is fail-closed")
        if time.monotonic() - self._runtime_start >= self.max_runtime_s:
            raise SchedulerError("R011 runtime bound reached; scheduler is fail-closed")

    @property
    def dispatch_count(self) -> int:
        return self._dispatch_count

    def assert_within_bounds(self, *, elapsed_s: float | None = None) -> None:
        """Deterministic bound check used by offline campaign-runner tests."""

        if self._dispatch_count >= self.max_attempts or (elapsed_s is not None and (not math.isfinite(elapsed_s) or elapsed_s >= self.max_runtime_s)):
            raise SchedulerError("R011 campaign bound reached; no further dispatch is permitted")

    def enqueue_bo(self, candidate: Mapping[str, Any]) -> ScheduledCandidate:
        key = scheduler_candidate_key(candidate)
        if key in set(self.evaluated_keys) or self._pending.get(key, 0) > 0 or (self._in_flight is not None and self._in_flight.key == key):
            raise SchedulerError("BO candidate is evaluated or pending")
        self._enqueue(candidate, "BO_TRIAL")
        return self._queue[-1]

    def _maybe_enqueue_incumbent_retest(self) -> None:
        if self._novel_bo_dispatches <= 0 or self._novel_bo_dispatches % NOVEL_RETEST_INTERVAL != 0:
            return
        # Challenger confirmation has priority at the same milestone.  Keep
        # q=1 and defer the periodic incumbent retest until the slot is free.
        if self._queue or self._in_flight is not None:
            return
        incumbent = self.confirmed_incumbent
        milestone = self._novel_bo_dispatches // NOVEL_RETEST_INTERVAL
        if incumbent is not None and milestone > 0 and milestone != getattr(self, "_last_retest_milestone", 0) and self._pending.get(incumbent.key, 0) == 0 and not (self._in_flight and self._in_flight.key == incumbent.key):
            self._enqueue(incumbent.candidate, "INCUMBENT_RETEST")
            self._last_retest_milestone = milestone

    def _maybe_enqueue_challenger_confirmation(self) -> None:
        if self._queue or self._in_flight is not None:
            return
        incumbent_mean = self.confirmed_incumbent_mean_n
        if incumbent_mean is None:
            return
        challengers = [
            aggregate
            for aggregate in self.aggregates
            if 0 < aggregate.exact_count < REPEAT_CONFIRMATIONS
            and aggregate.arithmetic_mean_sealed_mae_n is not None
            and aggregate.arithmetic_mean_sealed_mae_n < incumbent_mean
        ]
        if not challengers:
            return
        challenger = min(
            challengers,
            key=lambda item: (float(item.arithmetic_mean_sealed_mae_n), repr(item.key)),
        )
        self._confirmation_enqueued[challenger.key] += 1
        self._enqueue(
            challenger.candidate,
            "CHALLENGER_CONFIRM",
            challenger.exact_count + 1,
        )

    def resume_current_policy(self, *, discard_unpersisted_pending: bool = False) -> None:
        """Refill policy work only after persisted history has replayed."""

        if discard_unpersisted_pending:
            if self._in_flight is not None:
                raise SchedulerError("cannot discard policy pending while a dispatch is in flight")
            self._queue.clear()
            self._pending.clear()
        self._maybe_enqueue_challenger_confirmation()
        self._maybe_enqueue_incumbent_retest()

    def record(self, observation: Observation | Mapping[str, Any], *, run_kind: str | None = None, historical_policy: bool = False) -> CandidateAggregate | None:
        row = validate_observation(observation)
        if run_kind is None:
            run_kind = row.kind
        elif run_kind != row.kind:
            raise SchedulerError("run_kind differs from the typed observation kind")
        if (row.campaign_id, row.run_id, row.attempt_id) != (self.campaign_id, self.run_id, self.attempt_id):
            raise SchedulerError("observation campaign/run/attempt differs")
        if self._in_flight is None:
            raise SchedulerError("record requires an in-flight dispatch")
        if self._in_flight.key != scheduler_candidate_key(row.candidate) or self._in_flight.kind != row.kind:
            raise SchedulerError("observation does not match in-flight physical key/kind")
        if isinstance(row, CensoredObservation) and (not self._in_flight.abort_allowed or self._in_flight.kind not in {"BO_TRIAL", "NOVEL_BO"}):
            raise SchedulerError("censor is only valid for an abort-allowed novel BO dispatch")
        key = scheduler_candidate_key(row.candidate)
        self._observations.append(row)
        if isinstance(row, CensoredObservation):
            self._censored[key] += 1
            self._in_flight = None
            self._maybe_enqueue_incumbent_retest()
            return None
        self._aggregates[key].append(row)
        self._in_flight = None
        self._single_minimum = row.objective_n if self._single_minimum is None else min(self._single_minimum, row.objective_n)
        aggregate = CandidateAggregate(key, row.candidate, tuple(self._aggregates[key]), self._censored[key])
        if run_kind == "REFERENCE" and aggregate.exact_count < REPEAT_CONFIRMATIONS:
            self._enqueue(REFERENCE_CANDIDATE, "REFERENCE", aggregate.exact_count + 1)
        if historical_policy:
            incumbent_mean = self.confirmed_incumbent_mean_n
            if run_kind in {"BO_TRIAL", "NOVEL_BO"} and incumbent_mean is not None and aggregate.arithmetic_mean_sealed_mae_n is not None and aggregate.arithmetic_mean_sealed_mae_n < incumbent_mean and aggregate.exact_count < REPEAT_CONFIRMATIONS:
                needed = REPEAT_CONFIRMATIONS - aggregate.exact_count - self._confirmation_enqueued[key]
                if needed > 0 and not self._queue and self._in_flight is None:
                    self._confirmation_enqueued[key] += 1
                    self._enqueue(row.candidate, "CHALLENGER_CONFIRM", aggregate.exact_count + self._confirmation_enqueued[key])
        else:
            self._maybe_enqueue_challenger_confirmation()
        self._maybe_enqueue_incumbent_retest()
        return aggregate

    def receipt(self) -> dict[str, Any]:
        incumbent = self.confirmed_incumbent
        return {
            "schema": SCHEDULER_SCHEMA, "campaign_id": self.campaign_id, "run_id": self.run_id, "attempt_id": self.attempt_id,
            "initial_reference_repeats": REPEAT_CONFIRMATIONS, "novel_retest_interval": NOVEL_RETEST_INTERVAL,
            "novel_bo_dispatches": self._novel_bo_dispatches, "single_observed_minimum_n": self.single_observed_minimum_n,
            "confirmed_incumbent": None if incumbent is None else incumbent.as_dict(), "pending": [item.as_dict() for item in self.pending],
            "evaluated_key_count": len(self.evaluated_keys), "serial_q": 1, "runtime_bound_s": self.max_runtime_s, "attempt_bound": self.max_attempts, "dispatch_count": self._dispatch_count,
            "in_flight": None if self._in_flight is None else self._in_flight.as_dict(), "pending_multiplicity": {repr(key): count for key, count in self._pending.items()},
        }


__all__ = [
    "CandidateAggregate", "ConfirmedIncumbentScheduler", "MAX_ATTEMPTS", "MAX_RUNTIME_S", "NOVEL_RETEST_INTERVAL", "OBJECTIVE_SEMANTICS", "REFERENCE_CANDIDATE", "REPEAT_CONFIRMATIONS", "SCHEDULER_SCHEMA", "ScheduledCandidate", "SchedulerError", "scheduler_candidate_key",
]
