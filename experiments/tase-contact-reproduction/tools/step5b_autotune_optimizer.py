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
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            result.append(Observation.from_payload(json.loads(line)))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"corrupt observation JSONL at {path}:{line_number}: {exc}") from exc
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
        repeats = [
            float(obs.objective)
            for obs in feasible
            if obs.candidate == inc and obs.objective is not None
        ]
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
    import concurrent.futures
    import torch
    from botorch.acquisition.logei import qLogNoisyExpectedImprovement
    from botorch.fit import fit_gpytorch_mll
    from botorch.models import SingleTaskGP
    from botorch.models.approximate_gp import SingleTaskVariationalGP
    from botorch.models.transforms import Normalize, Standardize
    from botorch.sampling.normal import SobolQMCNormalSampler
    from gpytorch.likelihoods import BernoulliLikelihood
    from gpytorch.mlls import ExactMarginalLogLikelihood, VariationalELBO

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for live Step5b Bayesian optimization")
    device = torch.device("cuda:0")
    torch.cuda.set_device(device)
    torch.set_default_dtype(torch.double)
    torch.set_num_threads(1)
    all_x = torch.tensor([normalized_candidate(obs.candidate) for obs in observations], device=device)
    all_feas = torch.tensor([1.0 if obs.feasible else 0.0 for obs in observations], device=device)
    candidate_x = torch.tensor(
        [normalized_candidate(candidate) for candidate in candidates], device=device
    ).unsqueeze(1)

    def fit_feasibility() -> tuple[Any, Any]:
        likelihood = BernoulliLikelihood().to(device=device, dtype=torch.double)
        model = SingleTaskVariationalGP(
            all_x,
            likelihood=likelihood,
        ).to(device=device, dtype=torch.double)
        model.train()
        likelihood.train()
        optimizer = torch.optim.Adam(model.parameters(), lr=0.05)
        mll = VariationalELBO(likelihood, model.model, num_data=all_feas.numel())
        stream = torch.cuda.Stream(device=device)
        with torch.cuda.stream(stream):
            for _ in range(100):
                optimizer.zero_grad(set_to_none=True)
                loss = -mll(model(all_x), all_feas)
                loss.backward()
                optimizer.step()
        stream.synchronize()
        model.eval()
        likelihood.eval()
        return model, likelihood
    feasible_observations = [
        obs for obs in observations if obs.feasible and obs.objective is not None and obs.full_trial
    ]
    def fit_objective() -> Any:
        train_x = torch.tensor(
            [normalized_candidate(obs.candidate) for obs in feasible_observations], device=device
        )
        train_y = torch.tensor(
            [[-float(obs.objective)] for obs in feasible_observations], device=device
        )
        model = SingleTaskGP(
            train_x,
            train_y,
            train_Yvar=torch.full_like(train_y, 1e-5),
            input_transform=Normalize(d=5),
            outcome_transform=Standardize(m=1),
        )
        stream = torch.cuda.Stream(device=device)
        with torch.cuda.stream(stream):
            fit_gpytorch_mll(ExactMarginalLogLikelihood(model.likelihood, model))
        stream.synchronize()
        return model, train_x

    objective_result: tuple[Any, Any] | None = None
    if len(feasible_observations) >= 2:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            feasibility_future = pool.submit(fit_feasibility)
            objective_future = pool.submit(fit_objective)
            feasibility_model, feasibility_likelihood = feasibility_future.result()
            objective_result = objective_future.result()
    else:
        feasibility_model, feasibility_likelihood = fit_feasibility()

    with torch.no_grad():
        safe_probability = feasibility_likelihood(
            feasibility_model(candidate_x.squeeze(1))
        ).mean
    safe_indices = [
        index for index, probability in enumerate(safe_probability.tolist())
        if probability >= feasibility_probability_min
    ]
    if not safe_indices:
        return incumbent_candidate, {
            "backend": "botorch_bernoulli_no_safe_candidate_repeat_incumbent",
            "device": str(device),
            "gpu_name": torch.cuda.get_device_name(device),
            "gpu_workers": 2 if objective_result is not None else 1,
            "safe_candidate_count": 0,
            "max_feasibility_probability": torch.max(safe_probability).detach().item(),
        }

    if objective_result is None:
        best_index = max(
            safe_indices,
            key=lambda index: safe_probability[index].detach().item(),
        )
        return candidates[best_index], {
            "backend": "botorch_feasibility_only",
            "device": str(device),
            "gpu_name": torch.cuda.get_device_name(device),
            "gpu_workers": 1,
            "safe_candidate_count": len(safe_indices),
            "selected_feasibility_probability": safe_probability[best_index].detach().item(),
        }

    objective_model, train_x = objective_result
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
        "gpu_workers": 2,
        "safe_candidate_count": len(safe_indices),
        "selected_acquisition": float(values[local_index]),
        "selected_feasibility_probability": safe_probability[selected_index].detach().item(),
    }


def choose_candidate(
    observations: list[Observation],
    *,
    require_botorch: bool = True,
) -> tuple[Candidate, dict[str, Any]]:
    cuda_details: dict[str, Any] = {}
    if require_botorch:
        try:
            import torch
        except ImportError:
            raise RuntimeError("PyTorch is required for live Step5b Bayesian optimization") from None
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is required before any live Step5b candidate selection")
        device = torch.device("cuda:0")
        torch.cuda.set_device(device)
        probe = torch.tensor([1.0, 2.0], device=device).square().sum()
        torch.cuda.synchronize(device)
        cuda_details = {
            "device": str(device),
            "gpu_name": torch.cuda.get_device_name(device),
            "gpu_workers": 1,
            "cuda_probe": float(probe.item()),
        }

    target = next_context(observations)
    visits = context_observations(observations, target)
    unlocked = tier2_is_unlocked(observations)
    center = incumbent(observations, target)
    if not unlocked and not math.isclose(center.force_i_gain, 0.00001, abs_tol=1e-12):
        center = Candidate(**{**center.payload(), "force_i_gain": 0.00001})

    # Every fourth visit is a physical-noise replicate of the incumbent.
    if visits and (len(visits) + 1) % 4 == 0:
        return center, {
            "selection": "scheduled_incumbent_replication",
            "target_context_n": target,
            "tier2_unlocked": unlocked,
            **cuda_details,
        }

    candidates = one_step_neighbors(center, tier2_unlocked=unlocked)
    if len(observations) < 6 or sum(obs.feasible for obs in observations) < 3:
        selected = deterministic_exploration(center, len(visits), tier2_unlocked=unlocked)
        return selected, {
            "selection": "bounded_initial_exploration",
            "target_context_n": target,
            "tier2_unlocked": unlocked,
            "trust_region_candidates": len(candidates),
            **cuda_details,
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
