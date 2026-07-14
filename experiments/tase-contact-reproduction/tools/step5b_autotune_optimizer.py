#!/usr/bin/env python3
"""Contextual safe Bayesian candidate selection for Step5b autotune."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from step5b_autotune_contract import (
    TARGET_CONTEXTS_N,
    Candidate,
    normalized_candidate,
    one_step_neighbors,
)


BASELINE_BY_CONTEXT = {
    target: Candidate(target_force_n=target)
    for target in TARGET_CONTEXTS_N
}


@dataclass(frozen=True)
class Observation:
    candidate: Candidate
    feasible: bool
    objective: float | None
    full_trial: bool
    run_dir: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Observation":
        candidate = Candidate(**payload["candidate"])
        candidate.validate(tier2_unlocked=True)
        objective = payload.get("objective")
        if objective is not None:
            objective = float(objective)
        return cls(
            candidate=candidate,
            feasible=bool(payload.get("feasible", False)),
            objective=objective,
            full_trial=float(payload.get("metrics", {}).get("max_path_progress_s", 0.0)) >= 59.9,
            run_dir=str(payload.get("run_dir", "")),
        )


def read_observations(path: Path) -> list[Observation]:
    if not path.is_file():
        return []
    result: list[Observation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            result.append(Observation.from_payload(json.loads(line)))
        except (KeyError, TypeError, ValueError):
            continue
    return result


def context_observations(observations: Iterable[Observation], target: float) -> list[Observation]:
    return [obs for obs in observations if math.isclose(obs.candidate.target_force_n, target)]


def incumbent(observations: Iterable[Observation], target: float) -> Candidate:
    feasible = [
        obs
        for obs in context_observations(observations, target)
        if obs.feasible and obs.objective is not None and obs.full_trial
    ]
    if not feasible:
        return BASELINE_BY_CONTEXT[target]
    return min(feasible, key=lambda obs: float(obs.objective)).candidate


def tier2_is_unlocked(observations: list[Observation]) -> bool:
    for target in TARGET_CONTEXTS_N:
        feasible = [
            obs for obs in context_observations(observations, target)
            if obs.feasible and obs.objective is not None and obs.full_trial
        ]
        if len(feasible) < 5:
            return False
        inc = incumbent(observations, target)
        repeats = sorted(
            [float(obs.objective) for obs in feasible if obs.candidate == inc and obs.objective is not None]
        )
        if len(repeats) < 2:
            return False
        denominator = max(1e-9, min(repeats[-2:]))
        if abs(repeats[-1] - repeats[-2]) / denominator > 0.15:
            return False
    if len(observations) < 6 or any(not obs.feasible for obs in observations[-6:]):
        return False
    return True


def next_context(observations: list[Observation]) -> float:
    visits = {target: len(context_observations(observations, target)) for target in TARGET_CONTEXTS_N}
    minimum = min(visits.values())
    for target in TARGET_CONTEXTS_N:
        if visits[target] == minimum:
            return target
    raise AssertionError("unreachable")


def deterministic_exploration(center: Candidate, visit_index: int, *, tier2_unlocked: bool) -> Candidate:
    candidates = one_step_neighbors(center, tier2_unlocked=tier2_unlocked)
    ordered = [candidate for candidate in candidates if candidate != center]
    return ordered[visit_index % len(ordered)] if ordered else center


def _botorch_candidate(
    observations: list[Observation],
    candidates: list[Candidate],
    *,
    incumbent_candidate: Candidate,
    feasibility_probability_min: float,
) -> tuple[Candidate, dict[str, Any]]:
    import torch
    from botorch.acquisition.logei import qLogNoisyExpectedImprovement
    from botorch.fit import fit_gpytorch_mll
    from botorch.models import SingleTaskGP
    from botorch.models.transforms import Normalize, Standardize
    from botorch.sampling.normal import SobolQMCNormalSampler
    from gpytorch.mlls import ExactMarginalLogLikelihood

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for live Step5b Bayesian optimization")
    device = torch.device("cuda:0")
    torch.cuda.set_device(device)
    torch.set_default_dtype(torch.double)
    torch.set_num_threads(1)
    all_x = torch.tensor([normalized_candidate(obs.candidate) for obs in observations], device=device)
    all_feas = torch.tensor([[1.0 if obs.feasible else 0.0] for obs in observations], device=device)
    candidate_x = torch.tensor(
        [normalized_candidate(candidate) for candidate in candidates], device=device
    ).unsqueeze(1)

    feasible_flags = [obs.feasible for obs in observations]
    if all(feasible_flags):
        # A constant all-safe label carries no classification information and
        # Standardize cannot infer a useful output scale from it. Treat every
        # bounded trust-region candidate as safe until contrary evidence arrives.
        safe_probability = torch.ones(len(candidates), device=device)
    elif not any(feasible_flags):
        return incumbent_candidate, {
            "backend": "botorch_no_feasible_evidence_repeat_incumbent",
            "device": str(device),
            "gpu_name": torch.cuda.get_device_name(device),
            "safe_candidate_count": 0,
            "max_feasibility_probability": 0.0,
        }
    else:
        feasibility_model = SingleTaskGP(
            all_x,
            all_feas,
            train_Yvar=torch.full_like(all_feas, 0.02),
            input_transform=Normalize(d=5),
            outcome_transform=Standardize(m=1),
        )
        fit_gpytorch_mll(ExactMarginalLogLikelihood(feasibility_model.likelihood, feasibility_model))
        feasibility_posterior = feasibility_model.posterior(candidate_x.squeeze(1))
        mean = feasibility_posterior.mean.squeeze(-1)
        std = feasibility_posterior.variance.clamp_min(1e-12).sqrt().squeeze(-1)
        normal = torch.distributions.Normal(0.0, 1.0)
        safe_probability = normal.cdf((mean - 0.5) / std)
    safe_indices = [
        index for index, probability in enumerate(safe_probability.tolist())
        if probability >= feasibility_probability_min
    ]
    if not safe_indices:
        return incumbent_candidate, {
            "backend": "botorch_no_safe_candidate_repeat_incumbent",
            "device": str(device),
            "gpu_name": torch.cuda.get_device_name(device),
            "safe_candidate_count": 0,
            "max_feasibility_probability": torch.max(safe_probability).detach().item(),
        }

    feasible_observations = [
        obs for obs in observations if obs.feasible and obs.objective is not None and obs.full_trial
    ]
    if len(feasible_observations) < 2:
        best_index = max(
            safe_indices,
            key=lambda index: safe_probability[index].detach().item(),
        )
        return candidates[best_index], {
            "backend": "botorch_feasibility_only",
            "device": str(device),
            "gpu_name": torch.cuda.get_device_name(device),
            "safe_candidate_count": len(safe_indices),
            "selected_feasibility_probability": safe_probability[best_index].detach().item(),
        }

    train_x = torch.tensor(
        [normalized_candidate(obs.candidate) for obs in feasible_observations], device=device
    )
    # Maximize negative loss.
    train_y = torch.tensor(
        [[-float(obs.objective)] for obs in feasible_observations], device=device
    )
    objective_model = SingleTaskGP(
        train_x,
        train_y,
        train_Yvar=torch.full_like(train_y, 1e-5),
        input_transform=Normalize(d=5),
        outcome_transform=Standardize(m=1),
    )
    fit_gpytorch_mll(ExactMarginalLogLikelihood(objective_model.likelihood, objective_model))
    acquisition = qLogNoisyExpectedImprovement(
        model=objective_model,
        X_baseline=train_x,
        sampler=SobolQMCNormalSampler(sample_shape=torch.Size([256])),
        prune_baseline=True,
    )
    safe_x = candidate_x[safe_indices]
    values = acquisition(safe_x).detach().cpu().numpy().reshape(-1)
    local_index = int(np.argmax(values))
    selected_index = safe_indices[local_index]
    return candidates[selected_index], {
        "backend": "botorch_qLogNoisyExpectedImprovement_q1_cuda",
        "device": str(device),
        "gpu_name": torch.cuda.get_device_name(device),
        "safe_candidate_count": len(safe_indices),
        "selected_acquisition": float(values[local_index]),
        "selected_feasibility_probability": safe_probability[selected_index].detach().item(),
    }


def choose_candidate(
    observations: list[Observation],
    *,
    require_botorch: bool = True,
) -> tuple[Candidate, dict[str, Any]]:
    target = next_context(observations)
    visits = context_observations(observations, target)
    center = incumbent(observations, target)
    unlocked = tier2_is_unlocked(observations)

    # Every fourth visit is a physical-noise replicate of the incumbent.
    if visits and (len(visits) + 1) % 4 == 0:
        return center, {
            "selection": "scheduled_incumbent_replication",
            "target_context_n": target,
            "tier2_unlocked": unlocked,
        }

    candidates = one_step_neighbors(center, tier2_unlocked=unlocked)
    if len(observations) < 6 or sum(obs.feasible for obs in observations) < 3:
        selected = deterministic_exploration(center, len(visits), tier2_unlocked=unlocked)
        return selected, {
            "selection": "bounded_initial_exploration",
            "target_context_n": target,
            "tier2_unlocked": unlocked,
            "trust_region_candidates": len(candidates),
        }

    try:
        selected, details = _botorch_candidate(
            observations,
            candidates,
            incumbent_candidate=center,
            feasibility_probability_min=0.95,
        )
    except ImportError:
        if require_botorch:
            raise RuntimeError(
                "BoTorch environment missing; run step5b-autotune.sh install before live start"
            ) from None
        selected = center
        details = {"backend": "no_botorch_test_fallback", "safe_candidate_count": 0}
    details.update(
        {
            "selection": details.get("backend", "bayesian"),
            "target_context_n": target,
            "tier2_unlocked": unlocked,
            "trust_region_candidates": len(candidates),
        }
    )
    return selected, details


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--allow-no-botorch", action="store_true")
    args = parser.parse_args()
    selected, details = choose_candidate(
        read_observations(args.observations),
        require_botorch=not args.allow_no_botorch,
    )
    print(json.dumps({"candidate": selected.payload(), "details": details}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
