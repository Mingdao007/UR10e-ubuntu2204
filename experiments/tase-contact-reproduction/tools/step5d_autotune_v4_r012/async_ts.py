"""Deterministic finite-lattice Gaussian Thompson-sampling theory shadow."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .censor import Observation, validate_observation
from .common import R012ValueError, canonical_bytes, finite, freeze_tree, json_tree
from .offline_checksums import digest


ASYNC_TS_SCHEMA = "step5d.autotune-v4/r011-theory-shadow-async-ts-v3"
FEATURE_SCHEMA = "r011-finite-lattice-numeric-categorical-onehot-v1"
KERNEL_SCHEMA = "r011-rbf-positive-definite-v1"
MAX_CHOLESKY_JITTER_ATTEMPTS = 8


class AsyncTSShadowError(R012ValueError):
    """Theory-shadow input is invalid; no production action is taken."""


@dataclass(frozen=True)
class TheoryCompletedObservation:
    candidate: Mapping[str, Any]
    dispatch_id: str
    campaign_id: str
    run_id: str
    attempt_id: str
    exact_value_n: float | None = None
    lower_bound_n: float | None = None
    censored: bool = False
    noise_variance_n2: float = 0.005
    sealing_delay_s: float = 0.0
    completed: bool = True
    sealed: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.censored, bool) or self.completed is not True or self.sealed is not True:
            raise AsyncTSShadowError("theory row must be completed and sealed")
        if not all(isinstance(value, str) and value for value in (self.dispatch_id, self.campaign_id, self.run_id, self.attempt_id)):
            raise AsyncTSShadowError("theory row identity is invalid")
        if finite(self.sealing_delay_s, "sealing_delay_s") < 0.0 or finite(self.noise_variance_n2, "noise_variance_n2") <= 0.0:
            raise AsyncTSShadowError("theory row delay/noise is invalid")
        if self.censored:
            if self.lower_bound_n is None or self.exact_value_n is not None:
                raise AsyncTSShadowError("censored theory row must carry only a lower bound")
            finite(self.lower_bound_n, "lower_bound_n")
        else:
            if self.exact_value_n is None or self.lower_bound_n is not None:
                raise AsyncTSShadowError("exact theory row must carry only an exact value")
            finite(self.exact_value_n, "exact_value_n")
        object.__setattr__(self, "candidate", freeze_tree(json_tree(self.candidate)))

    @property
    def value_n(self) -> float:
        return float(self.lower_bound_n if self.censored else self.exact_value_n)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate": json_tree(self.candidate),
            "dispatch_id": self.dispatch_id, "campaign_id": self.campaign_id, "run_id": self.run_id, "attempt_id": self.attempt_id,
            "exact_value_n": self.exact_value_n,
            "lower_bound_n": self.lower_bound_n,
            "censored": self.censored,
            "noise_variance_n2": self.noise_variance_n2,
            "sealing_delay_s": self.sealing_delay_s,
            "completed": self.completed,
            "sealed": self.sealed,
        }

    @classmethod
    def from_observation(
        cls,
        value: Observation | Mapping[str, Any],
        *,
        max_sealing_delay_s: float,
        expected_campaign_id: str | None = None,
        expected_run_id: str | None = None,
        expected_attempt_id: str | None = None,
        default_noise_variance_n2: float = 0.005,
    ) -> "TheoryCompletedObservation":
        try:
            base_value = value
            if isinstance(value, Mapping):
                base_value = {key: item for key, item in value.items() if key not in {"noise_variance_n2", "sealing_delay_s"}}
            row = validate_observation(base_value)
        except Exception as exc:
            raise AsyncTSShadowError("malformed theory observation is rejected") from exc
        if expected_campaign_id is not None and (row.campaign_id, row.run_id, row.attempt_id) != (expected_campaign_id, expected_run_id, expected_attempt_id):
            raise AsyncTSShadowError("theory campaign/run/attempt differs")
        delay = finite(value.get("sealing_delay_s", 0.0), "sealing_delay_s") if isinstance(value, Mapping) else 0.0
        if delay > finite(max_sealing_delay_s, "max_sealing_delay_s"):
            raise AsyncTSShadowError("theory row exceeds configured max sealing delay")
        variance = finite(value.get("noise_variance_n2", default_noise_variance_n2), "noise_variance_n2") if isinstance(value, Mapping) else default_noise_variance_n2
        return cls(row.candidate, row.dispatch_id, row.campaign_id, row.run_id, row.attempt_id, None if row.censored else row.objective_n, row.lower_bound_n if row.censored else None, row.censored, variance, delay, row.completed, getattr(row, "sealed", True))


CompletedTheoryObservation = TheoryCompletedObservation


@dataclass(frozen=True)
class AsyncTSConfig:
    seed: int = 20260809
    allow_hypothetical_censoring: bool = True
    max_sealing_delay_s: float = 60.0
    default_noise_variance_n2: float = 0.005
    rbf_lengthscale: float = 1.0
    jitter: float = 1e-10
    expected_campaign_id: str | None = None
    expected_run_id: str | None = None
    expected_attempt_id: str | None = None
    schema: str = ASYNC_TS_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ASYNC_TS_SCHEMA or isinstance(self.seed, bool) or self.seed < 0 or not isinstance(self.allow_hypothetical_censoring, bool):
            raise AsyncTSShadowError("Async TS config identity differs")
        if finite(self.max_sealing_delay_s, "max_sealing_delay_s") < 0.0 or finite(self.default_noise_variance_n2, "default_noise_variance_n2") <= 0.0 or finite(self.rbf_lengthscale, "rbf_lengthscale") <= 0.0 or finite(self.jitter, "jitter") <= 0.0:
            raise AsyncTSShadowError("Async TS numeric config is invalid")
        values = (self.expected_campaign_id, self.expected_run_id, self.expected_attempt_id)
        if any(value is not None for value in values) and not all(isinstance(value, str) and value for value in values):
            raise AsyncTSShadowError("expected Async TS campaign/run/attempt must be complete")


@dataclass(frozen=True)
class AsyncTSProposal:
    candidate: Mapping[str, Any]
    branch: str
    epsilon: float
    completed_count: int
    pending_excluded: tuple[str, ...]
    force_full_evaluation: bool
    hypothetical_censoring: bool
    seed: int
    campaign_id: str
    run_id: str
    attempt_id: str
    completed_observations_id: str
    feature_schema: str = FEATURE_SCHEMA
    kernel_schema: str = KERNEL_SCHEMA
    posterior_schema: str = "gaussian_posterior_heteroscedastic_v1"
    joint_posterior_sampling: bool = True
    censored_approximation: str = "none"
    consistency_path: str = "forced_full_exact_subsequence"
    schema: str = ASYNC_TS_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ASYNC_TS_SCHEMA or self.branch not in {"exploration", "thompson_sampling"} or not 0.0 <= self.epsilon <= 0.2 or self.completed_count < 0:
            raise AsyncTSShadowError("Async TS proposal bounds differ")
        if self.branch == "exploration" and not self.force_full_evaluation:
            raise AsyncTSShadowError("exploration must force full evaluation")
        if self.joint_posterior_sampling is not True:
            raise AsyncTSShadowError("Async TS requires one joint posterior draw")
        if not all(isinstance(value, str) and value for value in (self.campaign_id, self.run_id, self.attempt_id, self.completed_observations_id)):
            raise AsyncTSShadowError("Async TS proposal identity is invalid")
        object.__setattr__(self, "candidate", freeze_tree(json_tree(self.candidate)))
        object.__setattr__(self, "pending_excluded", tuple(self.pending_excluded))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "candidate": json_tree(self.candidate), "branch": self.branch,
            "epsilon": self.epsilon, "completed_count": self.completed_count,
            "pending_excluded": list(self.pending_excluded), "force_full_evaluation": self.force_full_evaluation,
            "hypothetical_censoring": self.hypothetical_censoring, "seed": self.seed,
            "campaign_id": self.campaign_id, "run_id": self.run_id, "attempt_id": self.attempt_id,
            "completed_observations_id": self.completed_observations_id,
            "feature_schema": self.feature_schema, "kernel_schema": self.kernel_schema,
            "posterior_schema": self.posterior_schema, "joint_posterior_sampling": True,
            "censored_approximation": self.censored_approximation,
            "consistency_path": self.consistency_path,
        }

    @property
    def receipt_id(self) -> str:
        return digest(self.as_dict())


def _candidate_key(candidate: Mapping[str, Any]) -> str:
    if not isinstance(candidate, Mapping):
        raise AsyncTSShadowError("lattice candidate must be an object")
    return canonical_bytes(candidate).decode("utf-8")


def _feature_schema(lattice: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]], dict[str, tuple[float, float]]]:
    keys = tuple(sorted(lattice[0]))
    if any(tuple(sorted(candidate)) != keys for candidate in lattice):
        raise AsyncTSShadowError("lattice candidate keys differ")
    categories: dict[str, tuple[str, ...]] = {}
    scales: dict[str, tuple[float, float]] = {}
    for key in keys:
        values = [candidate[key] for candidate in lattice]
        if any(isinstance(value, (Mapping, list, tuple)) for value in values):
            raise AsyncTSShadowError("nested feature values are unsupported")
        if all(isinstance(value, bool) for value in values):
            continue
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            numbers = tuple(float(value) for value in values)
            scale = max(max(abs(value) for value in numbers), 1.0)
            scales[key] = (-scale, scale)
            continue
        if all(isinstance(value, str) for value in values):
            categories[key] = tuple(sorted(set(values)))
            continue
        raise AsyncTSShadowError("mixed feature types are unsupported")
    return keys, categories, scales


def _encode(candidate: Mapping[str, Any], schema: tuple[tuple[str, ...], dict[str, tuple[str, ...]], dict[str, tuple[float, float]]]) -> tuple[float, ...]:
    keys, categories, scales = schema
    vector: list[float] = []
    for key in keys:
        value = candidate.get(key)
        if key in categories:
            vector.extend(1.0 if value == category else 0.0 for category in categories[key])
        elif key in scales:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise AsyncTSShadowError("observation feature type differs from lattice")
            vector.append(float(value) / scales[key][1])
        elif isinstance(value, bool):
            vector.append(1.0 if value else 0.0)
        else:
            raise AsyncTSShadowError("observation feature key/type differs from lattice")
    return tuple(vector)


def _kernel(left: tuple[float, ...], right: tuple[float, ...], lengthscale: float) -> float:
    return math.exp(-0.5 * sum((a - b) ** 2 for a, b in zip(left, right)) / (lengthscale * lengthscale))


def _cholesky(matrix: Sequence[Sequence[float]], jitter: float) -> tuple[tuple[tuple[float, ...], ...], float]:
    n = len(matrix)
    if n == 0:
        return (), 0.0
    extra = 0.0
    for _ in range(MAX_CHOLESKY_JITTER_ATTEMPTS):
        lower = [[0.0] * n for _ in range(n)]
        failed = False
        for i in range(n):
            for j in range(i + 1):
                value = matrix[i][j] + (extra if i == j else 0.0) - sum(lower[i][k] * lower[j][k] for k in range(j))
                if i == j:
                    if value <= 0.0:
                        failed = True
                        break
                    lower[i][j] = math.sqrt(value)
                else:
                    lower[i][j] = value / lower[j][j]
            if failed:
                break
        if not failed:
            return tuple(tuple(row) for row in lower), extra
        extra = jitter if extra == 0.0 else extra * 10.0
    raise AsyncTSShadowError("posterior kernel is not positive definite after bounded jitter")


def _cholesky_solve(lower: Sequence[Sequence[float]], rhs: Sequence[float]) -> tuple[float, ...]:
    n = len(rhs)
    forward = [0.0] * n
    for i in range(n):
        forward[i] = (rhs[i] - sum(lower[i][k] * forward[k] for k in range(i))) / lower[i][i]
    result = [0.0] * n
    for i in range(n - 1, -1, -1):
        result[i] = (forward[i] - sum(lower[k][i] * result[k] for k in range(i + 1, n))) / lower[i][i]
    return tuple(result)


def _joint_posterior_sample(
    candidates: Sequence[Mapping[str, Any]], observations: Sequence[TheoryCompletedObservation],
    config: AsyncTSConfig, rng: random.Random,
    schema: tuple[tuple[str, ...], dict[str, tuple[str, ...]], dict[str, tuple[float, float]]],
) -> tuple[tuple[float, ...], tuple[tuple[float, ...], ...]]:
    query = [_encode(candidate, schema) for candidate in candidates]
    if observations:
        x = [_encode(row.candidate, schema) for row in observations]
        k_xx = [[_kernel(x[i], x[j], config.rbf_lengthscale) + (observations[i].noise_variance_n2 if i == j else 0.0) for j in range(len(x))] for i in range(len(x))]
        lower, _ = _cholesky(k_xx, config.jitter)
        alpha = _cholesky_solve(lower, [row.value_n for row in observations])
        k_qx = [[_kernel(q, x_item, config.rbf_lengthscale) for x_item in x] for q in query]
        means = tuple(sum(value * weight for value, weight in zip(row, alpha)) for row in k_qx)
        solved_q = [_cholesky_solve(lower, [k_qx[j][i] for i in range(len(x))]) for j in range(len(query))]
        covariance = [[_kernel(query[i], query[j], config.rbf_lengthscale) - sum(k_qx[i][k] * solved_q[j][k] for k in range(len(x))) for j in range(len(query))] for i in range(len(query))]
    else:
        means = tuple(0.0 for _ in query)
        covariance = [[_kernel(query[i], query[j], config.rbf_lengthscale) for j in range(len(query))] for i in range(len(query))]
    covariance = [[0.5 * (covariance[i][j] + covariance[j][i]) for j in range(len(query))] for i in range(len(query))]
    lower_q, _ = _cholesky(covariance, config.jitter)
    standard = [rng.gauss(0.0, 1.0) for _ in candidates]
    sample = tuple(means[i] + sum(lower_q[i][j] * standard[j] for j in range(i + 1)) for i in range(len(candidates)))
    return sample, tuple(tuple(row) for row in covariance)


def completed_observation_set_id(observations: Sequence[TheoryCompletedObservation]) -> str:
    return digest(sorted((row.as_dict() for row in observations), key=canonical_bytes))


def joint_posterior_sample(
    candidates: Sequence[Mapping[str, Any]],
    observations: Sequence[TheoryCompletedObservation],
    admissible_lattice: Sequence[Mapping[str, Any]],
    *,
    config: AsyncTSConfig = AsyncTSConfig(),
    seed: int | None = None,
) -> tuple[tuple[float, ...], tuple[tuple[float, ...], ...]]:
    """Return one deterministic joint draw and its full posterior covariance."""
    if not candidates:
        raise AsyncTSShadowError("joint posterior requires candidates")
    schema = _feature_schema(tuple(admissible_lattice))
    return _joint_posterior_sample(tuple(candidates), tuple(observations), config, random.Random(config.seed if seed is None else seed), schema)


def propose_async_ts(
    completed_observations: Sequence[Observation | Mapping[str, Any] | TheoryCompletedObservation],
    pending_candidates: Sequence[Mapping[str, Any]], admissible_lattice: Sequence[Mapping[str, Any]],
    *, config: AsyncTSConfig = AsyncTSConfig(), expected_campaign_id: str | None = None, expected_run_id: str | None = None, expected_attempt_id: str | None = None,
) -> AsyncTSProposal:
    lattice = tuple(admissible_lattice)
    if not lattice:
        raise AsyncTSShadowError("Async TS requires a non-empty finite lattice")
    lattice_keys = [_candidate_key(candidate) for candidate in lattice]
    if len(set(lattice_keys)) != len(lattice_keys):
        raise AsyncTSShadowError("Async TS lattice contains duplicate candidates")
    feature_schema = _feature_schema(lattice)
    pending_keys = tuple(sorted({_candidate_key(candidate) for candidate in pending_candidates}))
    if set(pending_keys) - set(lattice_keys):
        raise AsyncTSShadowError("pending candidate is outside the admissible lattice")
    available = tuple(candidate for candidate, key in zip(lattice, lattice_keys) if key not in set(pending_keys))
    if not available:
        raise AsyncTSShadowError("all admissible candidates are pending")
    expected_campaign = expected_campaign_id if expected_campaign_id is not None else config.expected_campaign_id
    expected_run = expected_run_id if expected_run_id is not None else config.expected_run_id
    expected_attempt = expected_attempt_id if expected_attempt_id is not None else config.expected_attempt_id
    if not all(isinstance(value, str) and value for value in (expected_campaign, expected_run, expected_attempt)):
        raise AsyncTSShadowError("Async TS requires an explicit campaign/run/attempt")
    completed: list[TheoryCompletedObservation] = []
    for value in completed_observations:
        row = value if isinstance(value, TheoryCompletedObservation) else TheoryCompletedObservation.from_observation(value, max_sealing_delay_s=config.max_sealing_delay_s, expected_campaign_id=expected_campaign, expected_run_id=expected_run, expected_attempt_id=expected_attempt, default_noise_variance_n2=config.default_noise_variance_n2)
        if row.sealing_delay_s > config.max_sealing_delay_s:
            raise AsyncTSShadowError("theory row exceeds configured max sealing delay")
        if (row.campaign_id, row.run_id, row.attempt_id) != (expected_campaign, expected_run, expected_attempt):
            raise AsyncTSShadowError("theory observation campaign/run/attempt differs")
        if row.censored and not config.allow_hypothetical_censoring:
            raise AsyncTSShadowError("censored theory observations are disabled")
        if _candidate_key(row.candidate) not in set(lattice_keys):
            raise AsyncTSShadowError("completed candidate is outside the admissible lattice")
        _encode(row.candidate, feature_schema)
        completed.append(row)
    n = len(completed)
    epsilon = min(0.2, n ** -0.5 if n else 1.0)
    rng = random.Random(config.seed)
    if rng.random() < epsilon:
        candidate = available[rng.randrange(len(available))]
        branch, force_full, hypothetical = "exploration", True, False
    else:
        samples, _ = _joint_posterior_sample(available, completed, config, rng, feature_schema)
        _, _, candidate = min((sample, index, item) for index, (sample, item) in enumerate(zip(samples, available)))
        branch, force_full, hypothetical = "thompson_sampling", False, config.allow_hypothetical_censoring
    return AsyncTSProposal(candidate, branch, epsilon, n, pending_keys, force_full, hypothetical, config.seed, expected_campaign, expected_run, expected_attempt, completed_observation_set_id(completed), censored_approximation="lower_bound_shadow_only" if any(row.censored for row in completed) else "none")


def theory_shadow_receipt(proposal: AsyncTSProposal) -> dict[str, Any]:
    if not isinstance(proposal, AsyncTSProposal):
        raise AsyncTSShadowError("theory shadow receipt requires a typed proposal")
    return {**proposal.as_dict(), "receipt_id": proposal.receipt_id, "production_qlognei_state": "untouched", "production_ledger_authority": False, "execution_authority": False, "live_authority": False}


__all__ = ["ASYNC_TS_SCHEMA", "AsyncTSConfig", "AsyncTSProposal", "AsyncTSShadowError", "CompletedTheoryObservation", "TheoryCompletedObservation", "completed_observation_set_id", "joint_posterior_sample", "propose_async_ts", "theory_shadow_receipt"]
