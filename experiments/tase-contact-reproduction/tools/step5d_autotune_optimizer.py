#!/usr/bin/env python3
"""Bounded log2-native search policy for Step5d force autotuning."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np

from step5d_autotune_contract import (
    LOG2_LATTICE_OCTAVE,
    Evaluation,
    ForceCandidate,
    SearchAttestation,
    SearchTier,
    require_sha256,
)


CUDA_FIT_MODES = {"serial", "verified_parallel"}
R008_NOISE_VARIANCE_FLOOR_N2 = 1e-4


@dataclass(frozen=True)
class OutcomeRecord:
    """One chronological campaign outcome, including its replayable trace identity."""

    candidate: ForceCandidate
    evaluation: Evaluation
    profile_id: str
    plant_epoch: int
    latest_trace_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, ForceCandidate):
            raise ValueError("candidate must be a ForceCandidate")
        if not isinstance(self.evaluation, Evaluation):
            raise ValueError("evaluation must be an Evaluation")
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("profile_id must be a non-empty string")
        if isinstance(self.plant_epoch, bool) or not isinstance(self.plant_epoch, int) or self.plant_epoch < 1:
            raise ValueError("plant_epoch must be a positive integer")
        if self.latest_trace_sha256 is not None:
            require_sha256("latest_trace_sha256", self.latest_trace_sha256)

    @property
    def eligible(self) -> bool:
        objective = self.evaluation.objective_mae_n
        return (
            self.evaluation.eligible
            and objective is not None
            and math.isfinite(float(objective))
        )

    @property
    def objective(self) -> float:
        if not self.eligible:
            raise ValueError("ineligible observation has no optimizer objective")
        return float(self.evaluation.objective_mae_n)


# Keep the original public name while making the complete timeline semantics
# explicit for new callers.
Observation = OutcomeRecord


def candidate_vector(candidate: ForceCandidate) -> tuple[float, float, float, float]:
    return (
        candidate.log2_p,
        candidate.log2_damping,
        0.0 if candidate.force_i_gain == 0.0 else candidate.log2_i,
        1.0 if candidate.force_i_gain == 0.0 else 0.0,
    )


def _changed_coordinates(a: ForceCandidate, b: ForceCandidate) -> tuple[str, ...]:
    changed: list[str] = []
    if not math.isclose(a.log2_p, b.log2_p, abs_tol=1e-9):
        changed.append("p")
    if not math.isclose(a.log2_damping, b.log2_damping, abs_tol=1e-9):
        changed.append("damping")
    if a.i_mode != b.i_mode:
        changed.append("i_mode")
    elif a.i_mode == "positive" and not math.isclose(a.log2_i, b.log2_i, abs_tol=1e-9):
        changed.append("i")
    return tuple(changed)


def _coordinate(candidate: ForceCandidate, axis: str) -> float:
    if axis == "p":
        return candidate.log2_p
    if axis == "damping":
        return candidate.log2_damping
    if axis == "i" and candidate.i_mode == "positive":
        return candidate.log2_i
    raise ValueError(f"candidate has no continuous {axis} coordinate")


def live_trust_region_step(incumbent: ForceCandidate, candidate: ForceCandidate) -> bool:
    changed = _changed_coordinates(incumbent, candidate)
    if len(changed) != 1:
        return False
    field = changed[0]
    if field == "i_mode":
        return (
            math.isclose(incumbent.log2_p, candidate.log2_p, abs_tol=1e-9)
            and math.isclose(incumbent.log2_damping, candidate.log2_damping, abs_tol=1e-9)
        )
    if field == "p":
        delta = abs(candidate.log2_p - incumbent.log2_p)
    elif field == "damping":
        delta = abs(candidate.log2_damping - incumbent.log2_damping)
    else:
        delta = abs(candidate.log2_i - incumbent.log2_i)
    return math.isclose(delta, LOG2_LATTICE_OCTAVE, abs_tol=1e-9)


def one_step_neighbors(incumbent: ForceCandidate, tier: SearchTier) -> tuple[ForceCandidate, ...]:
    candidates: set[ForceCandidate] = set()
    p, damping = incumbent.log2_p, incumbent.log2_damping
    i = 0.0 if incumbent.force_i_gain == 0.0 else incumbent.log2_i
    for delta in (-LOG2_LATTICE_OCTAVE, LOG2_LATTICE_OCTAVE):
        candidates.add(ForceCandidate.from_log2(p=p + delta, damping=damping, i=i, i_off=incumbent.i_mode == "off"))
        candidates.add(ForceCandidate.from_log2(p=p, damping=damping + delta, i=i, i_off=incumbent.i_mode == "off"))
        if tier is not SearchTier.T1 and incumbent.i_mode == "positive":
            candidates.add(ForceCandidate.from_log2(p=p, damping=damping, i=i + delta))
    if tier is not SearchTier.T1:
        candidates.add(
            ForceCandidate.from_log2(
                p=p,
                damping=damping,
                i=0.0 if incumbent.i_mode == "off" else incumbent.log2_i,
                i_off=incumbent.i_mode != "off",
            )
        )
    return tuple(
        sorted(
            (candidate for candidate in candidates if candidate.within_tier(tier) and live_trust_region_step(incumbent, candidate)),
            key=lambda candidate: candidate_vector(candidate),
        )
    )


def _one_step_toward(
    actual: ForceCandidate,
    target: ForceCandidate,
) -> ForceCandidate:
    """Return one executable lattice transition from ``actual`` toward ``target``."""

    if actual == target:
        return actual
    for axis in ("p", "damping", "i"):
        if axis == "i" and (
            actual.i_mode != "positive" or target.i_mode != "positive"
        ):
            continue
        before = _coordinate(actual, axis)
        after = _coordinate(target, axis)
        if math.isclose(before, after, abs_tol=1e-9):
            continue
        coordinate = before + math.copysign(
            min(LOG2_LATTICE_OCTAVE, abs(after - before)), after - before
        )
        kwargs = {
            "p": actual.log2_p,
            "damping": actual.log2_damping,
            "i": 0.0 if actual.i_mode == "off" else actual.log2_i,
            "i_off": actual.i_mode == "off",
        }
        kwargs[axis] = coordinate
        candidate = ForceCandidate.from_log2(**kwargs)
        if not live_trust_region_step(actual, candidate):
            raise RuntimeError("one-step transition construction violated live trust region")
        return candidate
    if actual.i_mode != target.i_mode:
        candidate = ForceCandidate.from_log2(
            p=actual.log2_p,
            damping=actual.log2_damping,
            i=0.0 if actual.i_mode == "off" else actual.log2_i,
            i_off=actual.i_mode != "off",
        )
        if not live_trust_region_step(actual, candidate):
            raise RuntimeError("I-mode transition construction violated live trust region")
        return candidate
    raise RuntimeError("distinct candidates have no executable transition")


def eligible_for_context(
    observations: Iterable[Observation],
    *,
    profile_id: str,
    plant_epoch: int,
) -> list[Observation]:
    return [
        observation
        for observation in observations
        if observation.profile_id == profile_id
        and observation.plant_epoch == plant_epoch
        and observation.eligible
    ]


def observations_for_context(
    observations: Iterable[Observation],
    *,
    profile_id: str,
    plant_epoch: int,
) -> list[Observation]:
    return [
        observation
        for observation in observations
        if observation.profile_id == profile_id and observation.plant_epoch == plant_epoch
    ]


def incumbent(observations: Iterable[Observation], *, fallback: ForceCandidate | None = None) -> ForceCandidate:
    eligible = [observation for observation in observations if observation.eligible]
    if not eligible:
        return fallback or ForceCandidate()
    return min(eligible, key=lambda observation: observation.objective).candidate


def _recent_structural_failure(outcomes: list[Observation]) -> bool:
    return any(outcome.evaluation.structural_failures for outcome in outcomes[-6:])


def _transition_axis_direction(
    before: ForceCandidate,
    after: ForceCandidate,
) -> tuple[str, int] | None:
    changed = _changed_coordinates(before, after)
    if len(changed) != 1 or changed[0] not in {"p", "damping", "i"}:
        return None
    if not live_trust_region_step(before, after):
        return None
    axis = changed[0]
    delta = _coordinate(after, axis) - _coordinate(before, axis)
    return axis, int(math.copysign(1, delta))


def _is_t2_boundary(candidate: ForceCandidate, axis: str, direction: int) -> bool:
    if axis in {"p", "damping"}:
        radius = SearchTier.T2.p_d_radius_octaves
    elif axis == "i" and candidate.i_mode == "positive":
        radius = SearchTier.T2.positive_i_radius_octaves
    else:
        return False
    return math.isclose(_coordinate(candidate, axis), direction * radius, abs_tol=1e-9)


def _outward_improvement_chains(
    outcomes: list[Observation],
    boundary_incumbent: ForceCandidate,
) -> tuple[tuple[str, int], ...]:
    """Derive two adjacent improving steps from objective-bearing outcomes."""

    eligible = [outcome for outcome in outcomes if outcome.eligible]
    chains: set[tuple[str, int]] = set()
    for first_index, first in enumerate(eligible):
        for second_index in range(first_index + 1, len(eligible)):
            second = eligible[second_index]
            first_step = _transition_axis_direction(first.candidate, second.candidate)
            if first_step is None or second.objective >= first.objective:
                continue
            for third in eligible[second_index + 1 :]:
                second_step = _transition_axis_direction(second.candidate, third.candidate)
                if (
                    second_step == first_step
                    and third.objective < second.objective
                    and third.candidate == boundary_incumbent
                    and _is_t2_boundary(
                        boundary_incumbent, first_step[0], first_step[1]
                    )
                ):
                    chains.add(first_step)
    return tuple(sorted(chains))


def _t3_unlocked_frontiers(
    outcomes: list[Observation],
) -> tuple[tuple[str, int, ForceCandidate], ...]:
    """Return historically unlocked T2 boundary rays for pointwise T3 growth."""

    eligible = [outcome for outcome in outcomes if outcome.eligible]
    frontiers: set[tuple[str, int, ForceCandidate]] = set()
    for boundary in {outcome.candidate for outcome in eligible}:
        if not boundary.within_tier(SearchTier.T2):
            continue
        for axis, direction in _outward_improvement_chains(outcomes, boundary):
            frontiers.add((axis, direction, boundary))
    return tuple(
        sorted(
            frontiers,
            key=lambda row: (row[0], row[1], candidate_vector(row[2])),
        )
    )


def _same_ray(
    candidate: ForceCandidate,
    *,
    boundary: ForceCandidate,
    axis: str,
    direction: int,
) -> bool:
    if candidate.i_mode != boundary.i_mode:
        return False
    for other_axis in ("p", "damping", "i"):
        if other_axis == axis:
            continue
        if other_axis == "i" and candidate.i_mode != "positive":
            continue
        if not math.isclose(
            _coordinate(candidate, other_axis),
            _coordinate(boundary, other_axis),
            abs_tol=1e-9,
        ):
            return False
    distance = direction * (
        _coordinate(candidate, axis) - _coordinate(boundary, axis)
    )
    return distance >= -1e-9


def search_attestation_matches(
    outcomes: list[Observation],
    *,
    pending_candidate: ForceCandidate,
    attestation: SearchAttestation,
) -> bool:
    """Verify a T3 frontier proof against the complete ordered context."""

    if not outcomes or not isinstance(attestation, SearchAttestation):
        return False
    contexts = {(outcome.profile_id, outcome.plant_epoch) for outcome in outcomes}
    if len(contexts) != 1:
        return False
    profile_id, plant_epoch = next(iter(contexts))
    eligible = [outcome for outcome in outcomes if outcome.eligible]
    if not eligible:
        return False
    best = incumbent(eligible)
    latest = outcomes[-1]
    if (
        not latest.eligible
        or latest.candidate != best
        or latest.latest_trace_sha256 is None
        or attestation.profile_id != profile_id
        or attestation.plant_epoch != plant_epoch
        or attestation.source_trial_uid != latest.evaluation.trial_uid
        or attestation.latest_trace_sha256 != latest.latest_trace_sha256
        or attestation.from_candidate != best
        or attestation.to_candidate != pending_candidate
        or attestation.next_candidate_uid != pending_candidate.candidate_uid
        or not attestation.exact_replay_passed
        or pending_candidate.within_tier(SearchTier.T2)
        or not pending_candidate.within_tier(SearchTier.T3)
    ):
        return False
    step = _transition_axis_direction(best, pending_candidate)
    if step != (attestation.outward_axis, attestation.outward_direction):
        return False
    for axis, direction, boundary in _t3_unlocked_frontiers(outcomes):
        if step != (axis, direction):
            continue
        if not _same_ray(best, boundary=boundary, axis=axis, direction=direction):
            continue
        if not _same_ray(
            pending_candidate,
            boundary=boundary,
            axis=axis,
            direction=direction,
        ):
            continue
        if direction * (
            _coordinate(pending_candidate, axis) - _coordinate(best, axis)
        ) > 0.0:
            return True
    return False


def unlocked_tier(
    observations: list[Observation],
    *,
    pending_candidate: ForceCandidate | None = None,
    search_attestation: SearchAttestation | None = None,
) -> SearchTier:
    # Tier gates deliberately inspect the complete ordered outcome timeline.
    # Only GP fitting below filters down to eligible objective observations.
    outcomes = list(observations)
    if not outcomes:
        return SearchTier.T1
    if len({(item.profile_id, item.plant_epoch) for item in outcomes}) != 1:
        return SearchTier.T1
    eligible = [observation for observation in outcomes if observation.eligible]
    if len(eligible) < 6 or _recent_structural_failure(outcomes):
        return SearchTier.T1
    if pending_candidate is None or search_attestation is None:
        return SearchTier.T2
    if any(not outcome.eligible for outcome in outcomes[-6:]):
        return SearchTier.T2
    if search_attestation_matches(
        outcomes,
        pending_candidate=pending_candidate,
        attestation=search_attestation,
    ):
        return SearchTier.T3
    return SearchTier.T2


def success_confirmed(
    observations: Iterable[Observation],
    *,
    profile_id: str,
    plant_epoch: int,
    threshold_n: float = 0.30,
) -> bool:
    return any(
        observation.eligible
        and observation.profile_id == profile_id
        and observation.plant_epoch == plant_epoch
        and observation.objective <= threshold_n
        for observation in observations
    )


def _deterministic_unseen(
    catalog: list[ForceCandidate],
    observations: list[Observation],
    forbidden_candidate_uids: frozenset[str] = frozenset(),
) -> ForceCandidate:
    visited = {observation.candidate for observation in observations}
    for candidate in catalog:
        if (
            candidate not in visited
            and candidate.candidate_uid not in forbidden_candidate_uids
        ):
            return candidate
    raise RuntimeError("no untried candidate remains in the bounded catalog")


def _cuda_botorch_candidate(
    observations: list[Observation],
    candidates: list[ForceCandidate],
    *,
    cuda_fit_mode: str,
    parallel_cuda_verified: bool,
) -> tuple[ForceCandidate, dict[str, Any]]:
    if cuda_fit_mode not in CUDA_FIT_MODES:
        raise ValueError(f"unsupported CUDA fit mode: {cuda_fit_mode}")
    if cuda_fit_mode == "verified_parallel" and not parallel_cuda_verified:
        raise ValueError("verified_parallel requires explicit verification attestation")
    try:
        import torch
        from botorch.acquisition.logei import qLogNoisyExpectedImprovement
        from botorch.fit import fit_gpytorch_mll
        from botorch.models import SingleTaskGP
        from botorch.models.transforms import Normalize, Standardize
        from botorch.sampling.normal import SobolQMCNormalSampler
        from gpytorch.mlls import ExactMarginalLogLikelihood
    except ImportError as exc:
        raise RuntimeError("PyTorch, BoTorch, and GPyTorch are required") from exc
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for live Step5d Bayesian optimization; no CPU fallback")
    if cuda_fit_mode == "verified_parallel":
        raise RuntimeError("verified_parallel is reserved until a non-hanging Step5d validation is recorded")

    # Defense in depth: outcome timelines include failures, but GP fitting is
    # permitted to consume eligible objective rows only.
    trainable = [observation for observation in observations if observation.eligible]
    if not trainable:
        raise ValueError("CUDA fitting requires at least one eligible objective observation")

    device = torch.device("cuda:0")
    torch.cuda.set_device(device)
    torch.set_default_dtype(torch.double)
    torch.set_num_threads(1)
    train_x = torch.tensor([candidate_vector(item.candidate) for item in trainable], device=device)
    train_y = torch.tensor([[-item.objective] for item in trainable], device=device)
    train_yvar = torch.tensor(
        [[value] for value in replicate_noise_variances(trainable)],
        device=device,
    )
    model = SingleTaskGP(
        train_x,
        train_y,
        train_Yvar=train_yvar,
        input_transform=Normalize(d=4),
        outcome_transform=Standardize(m=1),
    )
    fit_gpytorch_mll(ExactMarginalLogLikelihood(model.likelihood, model))
    candidate_x = torch.tensor([candidate_vector(item) for item in candidates], device=device).unsqueeze(1)
    acquisition = qLogNoisyExpectedImprovement(
        model=model,
        X_baseline=train_x,
        sampler=SobolQMCNormalSampler(sample_shape=torch.Size([256])),
        prune_baseline=True,
    )
    values = acquisition(candidate_x).detach().cpu().numpy().reshape(-1)
    index = int(np.argmax(values))
    return candidates[index], {
        "selection": "botorch_qLogNoisyExpectedImprovement_q1_cuda",
        "device": str(device),
        "gpu_name": torch.cuda.get_device_name(device),
        "gpu_workers": 1,
        "cuda_fit_mode": "serial",
        "training_observation_count": len(trainable),
        "selected_acquisition": float(values[index]),
    }


def replicate_noise_variances(
    observations: Sequence[Observation],
    *,
    anchor: ForceCandidate | None = None,
    variance_floor_n2: float = R008_NOISE_VARIANCE_FLOOR_N2,
) -> tuple[float, ...]:
    """Return occurrence-level fixed noise without collapsing repeated controls."""

    if not math.isfinite(variance_floor_n2) or variance_floor_n2 <= 0.0:
        raise ValueError("variance_floor_n2 must be positive and finite")
    trainable = [item for item in observations if item.eligible]
    if not trainable:
        return ()
    anchor = anchor or ForceCandidate()
    grouped: dict[str, list[float]] = defaultdict(list)
    for item in trainable:
        grouped[item.candidate.candidate_uid].append(item.objective)

    def sample_variance(values: Sequence[float]) -> float | None:
        if len(values) < 2:
            return None
        mean = sum(values) / len(values)
        return sum((value - mean) ** 2 for value in values) / (len(values) - 1)

    anchor_variance = sample_variance(grouped.get(anchor.candidate_uid, ()))
    if anchor_variance is None:
        repeated = [
            value
            for values in grouped.values()
            if (value := sample_variance(values)) is not None
        ]
        anchor_variance = (
            sum(repeated) / len(repeated) if repeated else variance_floor_n2
        )
    anchor_variance = max(float(anchor_variance), variance_floor_n2)
    group_variance = {
        uid: max(
            variance_floor_n2,
            anchor_variance if (value := sample_variance(values)) is None else value,
        )
        for uid, values in grouped.items()
    }
    return tuple(group_variance[item.candidate.candidate_uid] for item in trainable)


def cuda_botorch_joint_candidates(
    observations: Sequence[Observation],
    candidates: Sequence[ForceCandidate],
    *,
    q: int,
    seed: int = 8008,
    anchor: ForceCandidate | None = None,
) -> tuple[tuple[ForceCandidate, ...], dict[str, Any]]:
    """Fit once and optimize one unique discrete qLogNEI joint batch on CUDA."""

    if q not in {4, 5}:
        raise ValueError("r008 joint batch q must be 4 or 5")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    anchor = anchor or ForceCandidate()
    trainable = [item for item in observations if item.eligible]
    if len(trainable) < 6:
        raise ValueError("r008 BoTorch gate requires at least 6 eligible observations")
    if sum(item.candidate == anchor for item in trainable) < 3:
        raise ValueError("r008 BoTorch gate requires at least 3 eligible anchor repeats")
    choices = tuple(
        item
        for item in candidates
        if item != anchor
        and item.candidate_uid
        not in {observation.candidate.candidate_uid for observation in trainable}
    )
    if len({item.candidate_uid for item in choices}) != len(choices):
        raise ValueError("r008 discrete choices must be unique")
    if len(choices) < q:
        raise ValueError("r008 discrete catalog has fewer choices than q")
    try:
        import torch
        from botorch.acquisition.logei import qLogNoisyExpectedImprovement
        from botorch.fit import fit_gpytorch_mll
        from botorch.models import SingleTaskGP
        from botorch.models.transforms import Normalize, Standardize
        from botorch.optim import optimize_acqf_discrete
        from botorch.sampling.normal import SobolQMCNormalSampler
        from gpytorch.mlls import ExactMarginalLogLikelihood
    except ImportError as exc:
        raise RuntimeError("PyTorch, BoTorch, and GPyTorch are required") from exc
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for r008 Bayesian optimization; no CPU fallback")
    device = torch.device("cuda:0")
    torch.cuda.set_device(device)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.set_default_dtype(torch.double)
    torch.set_num_threads(1)
    train_x = torch.tensor(
        [candidate_vector(item.candidate) for item in trainable], device=device
    )
    train_y = torch.tensor([[-item.objective] for item in trainable], device=device)
    train_yvar = torch.tensor(
        [[value] for value in replicate_noise_variances(trainable, anchor=anchor)],
        device=device,
    )
    model = SingleTaskGP(
        train_x,
        train_y,
        train_Yvar=train_yvar,
        input_transform=Normalize(d=4),
        outcome_transform=Standardize(m=1),
    )
    fit_gpytorch_mll(ExactMarginalLogLikelihood(model.likelihood, model))
    acquisition = qLogNoisyExpectedImprovement(
        model=model,
        X_baseline=train_x,
        sampler=SobolQMCNormalSampler(sample_shape=torch.Size([256]), seed=seed),
        prune_baseline=True,
    )
    choice_x = torch.tensor([candidate_vector(item) for item in choices], device=device)
    selected_x, value = optimize_acqf_discrete(
        acq_function=acquisition,
        q=q,
        choices=choice_x,
        unique=True,
    )
    vectors = [tuple(float(value) for value in row) for row in selected_x.cpu()]
    by_vector = {candidate_vector(item): item for item in choices}
    selected = tuple(by_vector[vector] for vector in vectors)
    if len({item.candidate_uid for item in selected}) != q or anchor in selected:
        raise RuntimeError("r008 joint optimizer violated uniqueness or anchor exclusion")
    return selected, {
        "selection": f"botorch_qLogNoisyExpectedImprovement_q{q}_cuda_discrete_joint",
        "device": str(device),
        "gpu_name": torch.cuda.get_device_name(device),
        "seed": seed,
        "training_observation_count": len(trainable),
        "anchor_repeat_count": sum(item.candidate == anchor for item in trainable),
        "noise_variance_floor_n2": R008_NOISE_VARIANCE_FLOOR_N2,
        "selected_acquisition": float(value.detach().cpu().reshape(-1)[0]),
        "selected_candidate_uids": [item.candidate_uid for item in selected],
    }


def choose_candidate(
    observations: list[Observation],
    *,
    profile_id: str,
    plant_epoch: int,
    require_cuda_botorch: bool = True,
    cuda_fit_mode: str = "serial",
    parallel_cuda_verified: bool = False,
    search_attestations: Iterable[SearchAttestation] = (),
    forbidden_candidate_uids: frozenset[str] = frozenset(),
) -> tuple[ForceCandidate, dict[str, Any]]:
    attestations = tuple(search_attestations)
    if any(not isinstance(item, SearchAttestation) for item in attestations):
        raise ValueError("search_attestations must contain SearchAttestation values")
    forbidden_candidate_uids = frozenset(
        set(forbidden_candidate_uids)
        | {
            observation.candidate.candidate_uid
            for observation in observations
            if observation.profile_id == profile_id
        }
    )
    context = observations_for_context(
        observations, profile_id=profile_id, plant_epoch=plant_epoch,
    )
    current = [observation for observation in context if observation.eligible]
    tier = unlocked_tier(context)
    actual = context[-1].candidate if context else None

    def matching_attestation(candidate: ForceCandidate) -> SearchAttestation | None:
        matches = sorted(
            (
                item
                for item in attestations
                if item.profile_id == profile_id
                and item.plant_epoch == plant_epoch
                and item.to_candidate == candidate
                and item.next_candidate_uid == candidate.candidate_uid
                and item.exact_replay_passed
            ),
            key=lambda item: item.attestation_uid,
        )
        return matches[0] if matches else None

    def finalize(
        desired: ForceCandidate,
        details: dict[str, Any],
        *,
        preferred_attestation: SearchAttestation | None = None,
    ) -> tuple[ForceCandidate, dict[str, Any]]:
        selected = desired
        result = dict(details)
        if actual is not None and selected != actual and not live_trust_region_step(
            actual, selected
        ):
            selected = _one_step_toward(actual, selected)
            result["actual_transition"] = "one_coordinate_toward_selection"
            result["intended_candidate_uid"] = desired.candidate_uid
        if actual is not None and selected != actual and not live_trust_region_step(
            actual, selected
        ):
            raise RuntimeError("selected candidate skipped the latest actual lattice point")
        if selected.candidate_uid in forbidden_candidate_uids:
            anchor = actual if actual is not None else selected
            replacements = sorted(
                one_step_neighbors(anchor, tier),
                key=lambda item: (
                    not item.within_tier(SearchTier.T2),
                    candidate_vector(item),
                ),
            )
            replacement = next(
                (
                    item
                    for item in replacements
                    if item.candidate_uid not in forbidden_candidate_uids
                ),
                None,
            )
            if replacement is None:
                raise RuntimeError(
                    "no untried candidate remains in the current trust region"
                )
            selected = replacement
            result["intended_candidate_uid"] = desired.candidate_uid
            result["selection"] = "duplicate_rejected_unseen_neighbor"
            result["exact_parameter_set_reuse_allowed"] = False
        transition = (
            None
            if actual is None or selected == actual
            else _transition_axis_direction(actual, selected)
        )
        requires_outward_attestation = bool(
            not selected.within_tier(SearchTier.T2)
            and transition is not None
            and abs(_coordinate(selected, transition[0]))
            > abs(_coordinate(actual, transition[0])) + 1e-9
        )
        if requires_outward_attestation:
            proof = (
                preferred_attestation
                if preferred_attestation is not None
                and preferred_attestation.to_candidate == selected
                and preferred_attestation.profile_id == profile_id
                and preferred_attestation.plant_epoch == plant_epoch
                and preferred_attestation.exact_replay_passed
                else matching_attestation(selected)
            )
            if proof is None:
                raise ValueError(
                    "outside-T2 selection requires an exact matching profile/epoch replay attestation"
                )
            result["search_attestation_uid"] = proof.attestation_uid
        return selected, result

    if not context:
        prior_epoch_eligible = [
            item
            for item in observations
            if item.plant_epoch != plant_epoch and item.eligible
        ]
        if prior_epoch_eligible:
            anchor = prior_epoch_eligible[-1].candidate
            return finalize(
                anchor,
                {
                    "selection": "plant_epoch_anchor_replication",
                    "tier": (
                        SearchTier.T2.value
                        if anchor.within_tier(SearchTier.T2)
                        else SearchTier.T3.value
                    ),
                    "source_plant_epoch": prior_epoch_eligible[-1].plant_epoch,
                    "source_profile_id": prior_epoch_eligible[-1].profile_id,
                    "source_trial_uid": (
                        prior_epoch_eligible[-1].evaluation.trial_uid
                    ),
                },
            )
        return finalize(
            ForceCandidate(),
            {"selection": "exact_v35_baseline", "tier": SearchTier.T1.value},
        )
    if not current:
        return finalize(
            ForceCandidate(),
            {"selection": "exact_v35_baseline_retry", "tier": SearchTier.T1.value},
        )

    search_center = incumbent(current)
    candidate_attestations: dict[ForceCandidate, SearchAttestation] = {}
    if tier is SearchTier.T2:
        for candidate in one_step_neighbors(search_center, SearchTier.T3):
            for attestation in attestations:
                if unlocked_tier(
                    context,
                    pending_candidate=candidate,
                    search_attestation=attestation,
                ) is SearchTier.T3:
                    candidate_attestations[candidate] = attestation
                    break

    if candidate_attestations:
        tier = SearchTier.T3
        center = search_center
    else:
        center = search_center
        if not center.within_tier(tier):
            center = incumbent(
                [item for item in current if item.candidate.within_tier(tier)],
                fallback=ForceCandidate(),
            )

    if candidate_attestations:
        catalog = sorted(candidate_attestations, key=candidate_vector)
    else:
        catalog = list(one_step_neighbors(center, tier))
    if not catalog:
        raise RuntimeError("bounded search has no untried neighbor")
    if len(current) < 6:
        selected = _deterministic_unseen(
            catalog, context, forbidden_candidate_uids
        )
        return finalize(
            selected,
            {
                "selection": "bounded_initial_axial_exploration",
                "tier": tier.value,
                "catalog_size": len(catalog),
            },
            preferred_attestation=candidate_attestations.get(selected),
        )
    if not require_cuda_botorch:
        selected = _deterministic_unseen(
            catalog, context, forbidden_candidate_uids
        )
        details: dict[str, Any] = {
            "selection": "offline_deterministic_no_cuda",
            "tier": tier.value,
            "catalog_size": len(catalog),
        }
        return finalize(
            selected,
            details,
            preferred_attestation=candidate_attestations.get(selected),
        )
    selected, details = _cuda_botorch_candidate(
        current,
        catalog,
        cuda_fit_mode=cuda_fit_mode,
        parallel_cuda_verified=parallel_cuda_verified,
    )
    return finalize(
        selected,
        {**details, "tier": tier.value, "catalog_size": len(catalog)},
        preferred_attestation=candidate_attestations.get(selected),
    )
