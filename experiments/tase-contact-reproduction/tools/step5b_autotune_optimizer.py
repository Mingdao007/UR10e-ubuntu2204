#!/usr/bin/env python3
"""Single-context safe Bayesian candidate selection for Step5b autotune."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from step5b_autotune_contract import (
    OBJECTIVE_NAME,
    OBJECTIVE_UNIT,
    PARAMETER_TRUNCATION_FAILURES,
    TARGET_CONTEXTS_N,
    Candidate,
    is_parameter_constraint_failure,
    normalized_candidate,
    one_step_neighbors,
)
from step5b_autotune_evidence import (
    BACKEND_ID,
    EVALUATION_SCHEMA,
    candidate_uid_from_payload,
    physical_capture_uid_from_sha256,
    quarantine_jsonl_record,
    read_evaluation_jsonl,
    sha256_file,
    source_config_fingerprint,
    trial_uid_from_identity,
)


BASELINE_BY_CONTEXT = {
    target: Candidate(target_force_n=target)
    for target in TARGET_CONTEXTS_N
}
CUDA_FIT_MODES = ("serial", "verified_parallel")


@dataclass(frozen=True)
class Observation:
    candidate: Candidate
    feasible: bool
    objective: float | None
    full_trial: bool
    run_dir: str
    trial_uid: str = ""
    backend_id: str = ""
    fingerprint_sha256: str = ""
    candidate_uid: str = ""
    physical_capture_uid: str = ""
    disposition: str = ""
    session_uid: str = ""
    trial_id: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Observation":
        if payload.get("schema_version") != EVALUATION_SCHEMA:
            raise ValueError(f"observation schema is not {EVALUATION_SCHEMA}")
        if payload.get("objective_name") != OBJECTIVE_NAME or payload.get("objective_unit") != OBJECTIVE_UNIT:
            raise ValueError("observation objective is not force_mae_n in N")
        if payload.get("eligible") is not True:
            raise ValueError("observation is not explicitly training eligible")
        if payload.get("quarantined") is not False or payload.get("quarantine_path") is not None:
            raise ValueError("training-eligible observation is quarantined")
        if payload.get("supervisor_closure_verified") is not True:
            raise ValueError("training-eligible observation lacks verified supervisor closure")
        failures = payload.get("failures")
        if not isinstance(failures, list) or any(not isinstance(item, str) for item in failures):
            raise ValueError("observation failures must be a string list")
        candidate = Candidate(**payload["candidate"])
        candidate.validate(tier2_unlocked=True)
        candidate_uid = candidate_uid_from_payload(candidate.payload())
        if payload.get("candidate_uid") != candidate_uid:
            raise ValueError("eligible observation candidate_uid mismatch")
        session_uid = str(payload.get("session_uid", ""))
        trial_id = int(payload.get("trial_id", 0))
        trial_uid = trial_uid_from_identity(session_uid, trial_id, candidate_uid)
        if payload.get("trial_uid") != trial_uid:
            raise ValueError("eligible observation trial_uid mismatch")
        physical_capture_uid = str(payload.get("physical_capture_uid", ""))
        bridge_provenance = payload.get("provenance", {}).get("bridge_csv")
        if not isinstance(bridge_provenance, dict):
            raise ValueError("eligible observation bridge capture provenance missing")
        bridge_sha256 = sha256_file(Path(str(bridge_provenance.get("path", ""))))
        if bridge_sha256 is None or bridge_sha256 != bridge_provenance.get("sha256"):
            raise ValueError("eligible observation bridge capture bytes changed")
        if physical_capture_uid_from_sha256(bridge_sha256) != physical_capture_uid:
            raise ValueError("eligible observation physical_capture_uid mismatch")
        disposition = str(payload.get("disposition", ""))
        if disposition not in {"OBJECTIVE", "PARAMETER_CONSTRAINT"}:
            raise ValueError(f"observation disposition is not trainable: {disposition}")
        if payload.get("backend_id") != BACKEND_ID:
            raise ValueError("eligible observation backend identity mismatch")
        fingerprint = payload.get("fingerprint")
        if not isinstance(fingerprint, dict) or not fingerprint.get("verified"):
            raise ValueError("eligible observation lacks a verified source/config fingerprint")
        fingerprint_sha256 = str(fingerprint.get("post_combined_sha256", ""))
        if (
            len(fingerprint_sha256) != 64
            or fingerprint.get("pre_combined_sha256") != fingerprint_sha256
        ):
            raise ValueError("eligible observation fingerprint digest is invalid")
        if len(str(payload.get("trial_spec_sha256", ""))) != 64:
            raise ValueError("eligible observation trial_spec_sha256 is invalid")
        objective = payload.get("objective")
        if objective is not None:
            objective = float(objective)
            if not math.isfinite(objective) or objective < 0.0:
                raise ValueError("observation objective must be a finite non-negative scalar")
        feasible = bool(payload.get("feasible", False))
        full_trial = bool(payload.get("full_trial", False))
        if disposition == "OBJECTIVE" and (
            not feasible or not full_trial or objective is None or failures
        ):
            raise ValueError("OBJECTIVE observation lacks feasible full-trial scalar")
        if disposition == "PARAMETER_CONSTRAINT":
            constraint_events = [
                failure for failure in failures if is_parameter_constraint_failure(failure)
            ]
            invalid_failures = [
                failure
                for failure in failures
                if not is_parameter_constraint_failure(failure)
                and failure not in PARAMETER_TRUNCATION_FAILURES
            ]
            if feasible or objective is not None or not constraint_events or invalid_failures:
                raise ValueError("PARAMETER_CONSTRAINT observation invariant mismatch")
        return cls(
            candidate=candidate,
            feasible=feasible,
            objective=objective,
            full_trial=full_trial,
            run_dir=str(payload.get("run_dir", "")),
            trial_uid=trial_uid,
            backend_id=BACKEND_ID,
            fingerprint_sha256=fingerprint_sha256,
            candidate_uid=candidate_uid,
            physical_capture_uid=physical_capture_uid,
            disposition=disposition,
            session_uid=session_uid,
            trial_id=trial_id,
        )


def read_observations(
    path: Path,
    *,
    expected_fingerprint_sha256: str | None = None,
) -> list[Observation]:
    result: list[Observation] = []
    seen_trial_uids: set[str] = set()
    seen_physical_capture_uids: set[str] = set()
    history_fingerprint = expected_fingerprint_sha256
    for record in read_evaluation_jsonl(path):
        payload = record.payload
        if not payload.get("eligible", False):
            continue
        try:
            observation = Observation.from_payload(payload)
            if observation.trial_uid in seen_trial_uids:
                raise ValueError(f"duplicate trial_uid {observation.trial_uid}")
            if observation.physical_capture_uid in seen_physical_capture_uids:
                raise ValueError(
                    f"duplicate physical_capture_uid {observation.physical_capture_uid}"
                )
            if history_fingerprint is not None and observation.fingerprint_sha256 != history_fingerprint:
                raise ValueError("observation backend fingerprint differs from active history epoch")
        except (KeyError, TypeError, ValueError) as exc:
            quarantine_jsonl_record(
                path,
                record.line_number,
                record.raw_line,
                f"optimizer_observation_rejected:{type(exc).__name__}:{exc}",
            )
            continue
        seen_trial_uids.add(observation.trial_uid)
        seen_physical_capture_uids.add(observation.physical_capture_uid)
        history_fingerprint = history_fingerprint or observation.fingerprint_sha256
        result.append(observation)
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


def deterministic_exploration(
    center: Candidate,
    visits: list[Observation],
    *,
    tier2_unlocked: bool,
) -> Candidate:
    candidates = one_step_neighbors(center, tier2_unlocked=tier2_unlocked)
    ordered = [candidate for candidate in candidates if candidate != center]
    visited = {observation.candidate for observation in visits}
    unseen = [candidate for candidate in ordered if candidate not in visited]
    if unseen:
        return unseen[0]
    return ordered[len(visits) % len(ordered)] if ordered else center


def _botorch_candidate(
    observations: list[Observation],
    candidates: list[Candidate],
    *,
    incumbent_candidate: Candidate,
    feasibility_probability_min: float,
    cuda_fit_mode: str,
) -> tuple[Candidate, dict[str, Any]]:
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
    if cuda_fit_mode not in CUDA_FIT_MODES:
        raise ValueError(f"unsupported CUDA fit mode: {cuda_fit_mode}")
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
            input_transform=Normalize(d=4),
            outcome_transform=Standardize(m=1),
        )
        stream = torch.cuda.Stream(device=device)
        with torch.cuda.stream(stream):
            fit_gpytorch_mll(ExactMarginalLogLikelihood(model.likelihood, model))
        stream.synchronize()
        return model, train_x

    objective_result: tuple[Any, Any] | None = None
    if len(feasible_observations) >= 2:
        if cuda_fit_mode == "verified_parallel":
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                feasibility_future = pool.submit(fit_feasibility)
                objective_future = pool.submit(fit_objective)
                feasibility_model, feasibility_likelihood = feasibility_future.result()
                objective_result = objective_future.result()
        else:
            feasibility_model, feasibility_likelihood = fit_feasibility()
            objective_result = fit_objective()
    else:
        feasibility_model, feasibility_likelihood = fit_feasibility()

    fit_workers = 2 if cuda_fit_mode == "verified_parallel" and objective_result is not None else 1

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
            "gpu_workers": fit_workers,
            "cuda_fit_mode": cuda_fit_mode,
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
            "cuda_fit_mode": cuda_fit_mode,
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
        "gpu_workers": fit_workers,
        "cuda_fit_mode": cuda_fit_mode,
        "safe_candidate_count": len(safe_indices),
        "selected_acquisition": float(values[local_index]),
        "selected_feasibility_probability": safe_probability[selected_index].detach().item(),
    }


def choose_candidate(
    observations: list[Observation],
    *,
    require_botorch: bool = True,
    cuda_fit_mode: str = "serial",
    parallel_cuda_verified: bool = False,
) -> tuple[Candidate, dict[str, Any]]:
    if cuda_fit_mode not in CUDA_FIT_MODES:
        raise ValueError(f"unsupported CUDA fit mode: {cuda_fit_mode}")
    if cuda_fit_mode == "verified_parallel" and not parallel_cuda_verified:
        raise ValueError("verified_parallel CUDA fitting requires explicit verification attestation")
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
            "cuda_fit_mode": cuda_fit_mode,
            "cuda_probe": float(probe.item()),
        }

    target = next_context(observations)
    visits = context_observations(observations, target)
    unlocked = tier2_is_unlocked(observations)
    center = incumbent(observations, target)
    if not unlocked and not math.isclose(center.force_i_gain, 0.00001, abs_tol=1e-12):
        center = Candidate(**{**center.payload(), "force_i_gain": 0.00001})

    if not observations:
        return center, {
            "selection": "fixed_12n_baseline_seed",
            "target_context_n": target,
            "tier2_unlocked": False,
            "trust_region_candidates": len(one_step_neighbors(center, tier2_unlocked=False)),
            **cuda_details,
        }

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
        selected = deterministic_exploration(center, visits, tier2_unlocked=unlocked)
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
            cuda_fit_mode=cuda_fit_mode,
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
    parser.add_argument("--cuda-fit-mode", choices=CUDA_FIT_MODES, default="serial")
    parser.add_argument("--parallel-cuda-verified", action="store_true")
    args = parser.parse_args()
    selected, details = choose_candidate(
        read_observations(
            args.observations,
            expected_fingerprint_sha256=source_config_fingerprint()["combined_sha256"],
        ),
        require_botorch=True,
        cuda_fit_mode=args.cuda_fit_mode,
        parallel_cuda_verified=args.parallel_cuda_verified,
    )
    print(json.dumps({"candidate": selected.payload(), "details": details}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
