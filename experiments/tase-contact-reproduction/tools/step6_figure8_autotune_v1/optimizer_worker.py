#!/usr/bin/env python3
"""CUDA-only Figure-eight qLogNEI worker.

The live control owner invokes this file as a short-lived subprocess.  That
keeps Torch/BoTorch outside the real-time control environment while retaining
one physical writer and one serial q=1 campaign.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from step6_figure8_autotune_v1.core import (  # noqa: E402
    CompleteCandidateV1,
    SOBOL_POOL_SIZE,
    _sha256,
)
from step6_figure8_autotune_v1.v5_design_domain import (  # noqa: E402
    V5_DESIGN_DIMENSIONS,
    V5_FEATURE_BOUNDS,
    feature_bounds_receipt,
)
from step6_figure8_autotune_v1.v5_kernel_selection import (  # noqa: E402
    INCUMBENT_KERNEL,
    KernelSelectionError,
    KernelSelectionReceiptV1,
)


class FigureEightOptimizerError(RuntimeError):
    """A typed optimizer request could not produce a production proposal."""


def _controller_features(candidate: CompleteCandidateV1) -> tuple[float, ...]:
    block = candidate.controller_path
    p_gain = float(block["force_p_gain"])
    damping = float(block["force_damping"])
    return (
        math.log2(p_gain / damping),
        math.log2(damping),
        math.log2(float(block["normal_filter_tau_s"])),
        math.log2(float(block["orientation_ko"])),
        math.log2(float(block["motion_kp"])),
        math.log2(float(block["force_i_gain"]) / p_gain),
    )


def _features(candidate: CompleteCandidateV1, block: str) -> tuple[float, ...]:
    if block == "controller_path":
        return _controller_features(candidate)
    if block == "correction":
        return tuple(float(value) for value in candidate.correction_weights)
    raise FigureEightOptimizerError(f"unknown Figure-eight BO block: {block}")


def _kernel_selection_receipt(request: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Resolve the frozen kernel without making a live-side heuristic choice."""

    raw = request.get("kernel_selection_receipt")
    if raw is None:
        return INCUMBENT_KERNEL, {
            "schema": "step6.autotune/figure8-v5-kernel-selection-default-v1",
            "version": 1,
            "selected_kernel": INCUMBENT_KERNEL,
            "decision": "retain_incumbent",
            "calibration_observation_count": 0,
            "status": "pre_calibration_incumbent",
        }
    if not isinstance(raw, Mapping):
        raise FigureEightOptimizerError("Figure-eight kernel selection receipt is invalid")
    try:
        receipt = KernelSelectionReceiptV1(
            selected_kernel=str(raw["selected_kernel"]),
            calibration_observation_count=raw["calibration_observation_count"],
            incumbent_metrics=raw["incumbent_metrics"],
            challenger_metrics=raw["challenger_metrics"],
            decision=str(raw["decision"]),
            source=str(raw.get("source", "offline_replay")),
            schema=raw["schema"],
            version=raw["version"],
        )
    except (KeyError, TypeError, ValueError, KernelSelectionError) as exc:
        raise FigureEightOptimizerError("Figure-eight kernel selection receipt is invalid") from exc
    return receipt.selected_kernel, receipt.as_dict()


def _build_kernel(gpytorch: Any, kernel_name: str) -> Any:
    if kernel_name == "rbf_ard":
        base = gpytorch.kernels.RBFKernel(ard_num_dims=V5_DESIGN_DIMENSIONS)
    elif kernel_name == "matern52_ard":
        base = gpytorch.kernels.MaternKernel(nu=2.5, ard_num_dims=V5_DESIGN_DIMENSIONS)
    elif kernel_name == "matern32_ard":
        base = gpytorch.kernels.MaternKernel(nu=1.5, ard_num_dims=V5_DESIGN_DIMENSIONS)
    else:
        raise FigureEightOptimizerError(f"unknown Figure-eight kernel: {kernel_name}")
    return gpytorch.kernels.ScaleKernel(base)


def _feature_bounds_for_block(block: str) -> tuple[tuple[float, float], ...]:
    if block == "controller_path":
        return V5_FEATURE_BOUNDS
    if block == "correction":
        # Correction weights are already in their typed physical coordinates;
        # the L1/slew policy is checked by the candidate DTO.  The optimizer
        # still receives a fixed six-dimensional box rather than an
        # observation-derived min/max transform.
        return ((-0.5, 0.5),) * V5_DESIGN_DIMENSIONS
    raise FigureEightOptimizerError(f"unknown Figure-eight feature block: {block}")


def _feature_bounds_receipt(block: str) -> dict[str, Any]:
    receipt = feature_bounds_receipt()
    if block == "controller_path":
        return receipt
    if block == "correction":
        return {
            **receipt,
            "feature_names": [f"correction_weight_{index}" for index in range(V5_DESIGN_DIMENSIONS)],
            "feature_bounds_log2": [[-0.5, 0.5] for _ in range(V5_DESIGN_DIMENSIONS)],
            "physical_bounds": {"correction_weight": [-0.5, 0.5]},
            "normalization": "fixed_correction_design_bounds_not_observation_minmax",
        }
    raise FigureEightOptimizerError(f"unknown Figure-eight feature block: {block}")


def propose(request: Mapping[str, Any]) -> dict[str, Any]:
    pool_raw = request.get("pool")
    observations = request.get("observations")
    pending_raw = request.get("pending_candidates", ())
    pending_keys = tuple(str(value) for value in request.get("pending_keys", ()))
    block = str(request.get("block", ""))
    if not isinstance(pool_raw, Sequence) or len(pool_raw) != SOBOL_POOL_SIZE:
        raise FigureEightOptimizerError("Figure-eight qLogNEI pool is not exactly 128")
    if not isinstance(observations, Sequence) or not observations:
        raise FigureEightOptimizerError("Figure-eight qLogNEI has no grouped observations")
    pool = tuple(CompleteCandidateV1.from_mapping(item) for item in pool_raw)
    if not isinstance(pending_raw, Sequence) or isinstance(pending_raw, (str, bytes)):
        raise FigureEightOptimizerError("Figure-eight pending candidates are invalid")
    pending = tuple(CompleteCandidateV1.from_mapping(item) for item in pending_raw)
    if tuple(sorted(candidate.candidate_key for candidate in pending)) != tuple(
        sorted(pending_keys)
    ):
        raise FigureEightOptimizerError(
            "Figure-eight pending candidates do not match pending keys"
        )
    keys = tuple(candidate.candidate_key for candidate in pool)
    if len(set(keys)) != SOBOL_POOL_SIZE:
        raise FigureEightOptimizerError("Figure-eight qLogNEI pool contains duplicates")
    parsed_observations: list[tuple[tuple[float, ...], float, float, str]] = []
    fingerprints: set[str] = set()
    for row in observations:
        if not isinstance(row, Mapping):
            raise FigureEightOptimizerError("Figure-eight grouped observation is invalid")
        candidate = CompleteCandidateV1.from_mapping(row["candidate"])
        mean_n = float(row["mae_n"])
        yvar_n2 = float(row["yvar_n2"])
        fingerprint = str(row["fingerprint_sha256"])
        if (
            not math.isfinite(mean_n)
            or mean_n < 0.0
            or not 1e-4 <= yvar_n2 <= 2e-2
            or len(fingerprint) != 64
        ):
            raise FigureEightOptimizerError("Figure-eight grouped observation bounds differ")
        parsed_observations.append(
            (_features(candidate, block), mean_n, yvar_n2, candidate.candidate_key)
        )
        fingerprints.add(fingerprint)
    if len(fingerprints) != 1:
        raise FigureEightOptimizerError("Figure-eight qLogNEI mixes fingerprints")

    try:
        import botorch
        import gpytorch
        import torch
        from botorch.acquisition.logei import qLogNoisyExpectedImprovement
        from botorch.acquisition.objective import GenericMCObjective
        from botorch.fit import fit_gpytorch_mll
        from botorch.models import SingleTaskGP
        from botorch.models.transforms.input import Normalize
        from botorch.models.transforms.outcome import Standardize
        from gpytorch.mlls import ExactMarginalLogLikelihood
    except (ImportError, ModuleNotFoundError) as exc:
        raise FigureEightOptimizerError("Figure-eight CUDA optimizer imports are unavailable") from exc
    if not torch.cuda.is_available():
        raise FigureEightOptimizerError("Figure-eight production qLogNEI requires CUDA")
    torch.manual_seed(int(request.get("seed", 6016)))
    device = torch.device("cuda")
    train_x = torch.tensor(
        [row[0] for row in parsed_observations], dtype=torch.double, device=device
    )
    train_y = torch.tensor(
        [[row[1]] for row in parsed_observations], dtype=torch.double, device=device
    )
    train_yvar = torch.tensor(
        [[row[2]] for row in parsed_observations], dtype=torch.double, device=device
    )
    kernel_name, kernel_selection = _kernel_selection_receipt(request)
    feature_bounds = _feature_bounds_receipt(block)
    block_bounds = _feature_bounds_for_block(block)
    bounds = torch.tensor(
        [
            [float(lower) for lower, _ in block_bounds],
            [float(upper) for _, upper in block_bounds],
        ],
        dtype=torch.double,
        device=device,
    )
    if bool(torch.any(train_x < bounds[0] - 1.0e-10).item()) or bool(
        torch.any(train_x > bounds[1] + 1.0e-10).item()
    ):
        raise FigureEightOptimizerError(
            "Figure-eight observation is outside fixed design bounds"
        )
    initial_kernel = _build_kernel(gpytorch, kernel_name)
    initial_lengthscales = tuple(
        float(value)
        for value in initial_kernel.base_kernel.lengthscale.detach().cpu().reshape(-1)
    )
    model = SingleTaskGP(
        train_x,
        train_y,
        train_Yvar=train_yvar,
        input_transform=Normalize(d=V5_DESIGN_DIMENSIONS, bounds=bounds),
        outcome_transform=Standardize(m=1),
        covar_module=initial_kernel,
    ).to(device=device, dtype=torch.double)
    mll = ExactMarginalLogLikelihood(model.likelihood, model)
    fit_gpytorch_mll(mll)
    model.eval()
    objective = GenericMCObjective(lambda samples, X=None: -samples.squeeze(-1))
    acquisition = qLogNoisyExpectedImprovement(
        model=model,
        X_baseline=train_x,
        objective=objective,
        prune_baseline=False,
        X_pending=(
            None
            if not pending
            else torch.tensor(
                [_features(candidate, block) for candidate in pending],
                dtype=torch.double,
                device=device,
            )
        ),
    )
    pool_x = torch.tensor(
        [_features(candidate, block) for candidate in pool],
        dtype=torch.double,
        device=device,
    )
    with torch.no_grad():
        scores = acquisition(pool_x.unsqueeze(1)).reshape(-1)
        posterior = model.posterior(pool_x)
        means = posterior.mean.reshape(-1)
        variances = posterior.variance.reshape(-1).clamp_min(1e-12)
        observed_posterior = model.posterior(train_x)
        observed_means = observed_posterior.mean.reshape(-1)
        observed_variances = observed_posterior.variance.reshape(-1).clamp_min(1e-12)
        robust_incumbent_index = int(torch.argmin(observed_means).item())
        robust_incumbent = float(observed_means[robust_incumbent_index])
        improvement_threshold = robust_incumbent - 0.01
        threshold = torch.tensor(
            improvement_threshold, dtype=torch.double, device=device
        )
        probabilities = torch.distributions.Normal(
            means, variances.sqrt()
        ).cdf(threshold)
        observed_probabilities = torch.distributions.Normal(
            observed_means, observed_variances.sqrt()
        ).cdf(threshold)
    scored = [
        {
            "candidate_key": candidate.candidate_key,
            "acquisition_value": float(score),
            "posterior_probability_of_robust_improvement": float(probability),
            "fresh": True,
        }
        for candidate, score, probability in zip(pool, scores, probabilities, strict=True)
    ]
    selected_index = max(
        range(len(scored)),
        key=lambda index: (scored[index]["acquisition_value"], keys[index]),
    )
    try:
        lengthscales = tuple(
            float(value)
            for value in model.covar_module.lengthscale.detach().cpu().reshape(-1)
        )
    except Exception:
        lengthscales = ()
    fit_receipt = {
        "schema": "step6.autotune/figure8-production-gp-fit-v1",
        "version": 1,
        "backend": "botorch.SingleTaskGP",
        "block": block,
        "train_group_count": len(parsed_observations),
        "train_yvar_count": len(parsed_observations),
        "campaign_fingerprint_sha256": next(iter(fingerprints)),
        "observed_group_mean_minimum_n": min(
            row[1] for row in parsed_observations
        ),
        "posterior_incumbent_n": robust_incumbent,
        "posterior_incumbent_candidate_key": parsed_observations[
            robust_incumbent_index
        ][3],
        "improvement_threshold_n": improvement_threshold,
        "model_dimensions": V5_DESIGN_DIMENSIONS,
        "q": 1,
        "pending_candidate_count": len(pending),
        "asynchronous_pending_fantasy": bool(pending),
        "candidate_pool_size": SOBOL_POOL_SIZE,
        "botorch_version": botorch.__version__,
        "gpytorch_version": gpytorch.__version__,
        "torch_version": torch.__version__,
        "device": str(device),
        "cuda_device": torch.cuda.get_device_name(device),
        "kernel": kernel_name,
        "kernel_selection": kernel_selection,
        "feature_bounds": feature_bounds,
        "normalization": "fixed_design_bounds_not_observation_minmax",
        "prior": {
            "input_transform": "Normalize",
            "outcome_transform": "Standardize",
            "noise_model": "provided_yvar_n2",
        },
        "initial_lengthscales_unit": list(initial_lengthscales),
        "lengthscales_unit_fitted": list(lengthscales),
        "observation_group_sha256": _sha256(list(observations)),
    }
    return {
        "schema": "step6.autotune/figure8-qlognei-proposal-v1",
        "version": 1,
        "candidate": pool[selected_index].as_dict(),
        "candidate_key": keys[selected_index],
        "acquisition": "qLogNEI",
        "acquisition_value": scored[selected_index]["acquisition_value"],
        "posterior_beating_probability": scored[selected_index][
            "posterior_probability_of_robust_improvement"
        ],
        "scored_pool": scored,
        # Repeat admission is based on the posterior for already observed
        # canonical groups, not the selected fresh proposal.  Returning this
        # mapping from the same fitted model prevents the control process from
        # inventing a second probability calculation.
        "observed_posterior_probability": {
            row[3]: float(probability)
            for row, probability in zip(
                parsed_observations, observed_probabilities, strict=True
            )
        },
        "observed_posterior_mean_n": {
            row[3]: float(mean)
            for row, mean in zip(
                parsed_observations, observed_means, strict=True
            )
        },
        "observed_posterior_variance_n2": {
            row[3]: float(variance)
            for row, variance in zip(
                parsed_observations, observed_variances, strict=True
            )
        },
        "fit_receipt": fit_receipt,
        "production_provider": "step6_figure8_cuda_subprocess_qlognei_v1",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        response = propose(request)
        args.response.write_text(
            json.dumps(response, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        return 0
    except Exception as exc:
        args.response.write_text(
            json.dumps(
                {
                    "schema": "step6.autotune/figure8-qlognei-error-v1",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
