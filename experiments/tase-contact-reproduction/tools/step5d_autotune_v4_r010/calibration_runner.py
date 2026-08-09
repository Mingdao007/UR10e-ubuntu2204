"""CUDA-backed deterministic five-fold GP calibration for R010."""

from __future__ import annotations

import math
import statistics
from typing import Any, Sequence

from .gp_calibration import (
    CALIBRATED_FEATURE_INDICES,
    CV_SEED,
    CalibrationRow,
    FoldMetric,
    NOISE_FLOOR_GRID_N2,
    NoiseCandidateResult,
    R010CalibrationError,
    deterministic_grouped_folds,
    grouped_training_rows,
    span_initialization,
)


def _fit_fold(
    rows: Sequence[CalibrationRow],
    *,
    test_keys: frozenset[tuple[Any, ...]],
    noise_floor_n2: float,
    fold_index: int,
    maxiter: int,
) -> FoldMetric:
    import gpytorch
    import torch
    from botorch.fit import fit_gpytorch_mll
    from botorch.models import SingleTaskGP
    from gpytorch.constraints import Interval
    from gpytorch.likelihoods import FixedNoiseGaussianLikelihood
    from gpytorch.mlls import ExactMarginalLogLikelihood

    if not torch.cuda.is_available():
        raise R010CalibrationError("R010 GP calibration requires CUDA; no fallback is allowed")
    device = torch.device("cuda:0")
    torch.cuda.set_device(device)
    torch.manual_seed(CV_SEED + fold_index)
    torch.cuda.manual_seed_all(CV_SEED + fold_index)
    torch.use_deterministic_algorithms(True, warn_only=False)

    train_rows = [row for row in rows if row.gain_key not in test_keys]
    test_rows = [row for row in rows if row.gain_key in test_keys]
    if len(train_rows) < 2 or not test_rows:
        raise R010CalibrationError("R010 CV fold is empty")
    grouped = grouped_training_rows(train_rows, noise_floor_n2=noise_floor_n2)
    active = CALIBRATED_FEATURE_INDICES
    train_x = torch.tensor(
        [[item["features"][index] for index in active] for item in grouped],
        dtype=torch.double,
        device=device,
    )
    train_y = torch.tensor(
        [[item["objective_n"]] for item in grouped], dtype=torch.double, device=device
    )
    train_yvar = torch.tensor(
        [[item["noise_n2"]] for item in grouped], dtype=torch.double, device=device
    )
    active_spans = (train_x.max(dim=0).values - train_x.min(dim=0).values).clamp_min(1.0)
    lower = (active_spans / 100.0).clamp_min(1.0e-4)
    upper = active_spans * 100.0
    constraint_lower = lower.detach().cpu()
    constraint_upper = upper.detach().cpu()
    base = gpytorch.kernels.MaternKernel(
        nu=2.5,
        ard_num_dims=len(active),
        lengthscale_prior=gpytorch.priors.GammaPrior(3.0, 6.0),
        lengthscale_constraint=Interval(
            constraint_lower,
            constraint_upper,
        ),
    )
    base.initialize(lengthscale=active_spans.detach().cpu().reshape(1, -1))
    covar = gpytorch.kernels.ScaleKernel(
        base,
        outputscale_prior=gpytorch.priors.GammaPrior(2.0, 0.15),
    )
    signal_scale_n = max(
        math.sqrt(noise_floor_n2),
        statistics.pstdev(item["objective_n"] for item in grouped),
    )
    covar.initialize(outputscale=signal_scale_n**2)
    model = SingleTaskGP(
        train_X=train_x,
        train_Y=train_y,
        train_Yvar=train_yvar,
        likelihood=FixedNoiseGaussianLikelihood(noise=train_yvar.squeeze(-1)),
        covar_module=covar,
    ).to(device=device, dtype=torch.double)
    mll = ExactMarginalLogLikelihood(model.likelihood, model)
    fit_gpytorch_mll(mll, optimizer_kwargs={"options": {"maxiter": int(maxiter)}})
    model.eval()

    test_x = torch.tensor(
        [[row.features[index] for index in active] for row in test_rows],
        dtype=torch.double,
        device=device,
    )
    test_y = torch.tensor(
        [row.objective_n for row in test_rows], dtype=torch.double, device=device
    )
    with torch.inference_mode():
        posterior = model.posterior(test_x)
        mean = posterior.mean.reshape(-1)
        variance = posterior.variance.reshape(-1).clamp_min(1.0e-12) + noise_floor_n2
        sigma = torch.sqrt(variance)
        covered = torch.abs(test_y - mean) <= 1.959963984540054 * sigma
        nlpd = 0.5 * torch.log(2.0 * math.pi * variance) + 0.5 * (test_y - mean) ** 2 / variance
    fitted_active = base.lengthscale.detach().reshape(-1).cpu().tolist()
    lower_values = lower.detach().cpu().tolist()
    upper_values = upper.detach().cpu().tolist()
    full_spans = span_initialization(train_rows)
    full_lengths = list(full_spans)
    full_hits = [False] * 7
    for active_index, feature_index in enumerate(active):
        value = float(fitted_active[active_index])
        full_lengths[feature_index] = value
        full_hits[feature_index] = (
            value <= float(lower_values[active_index]) * 1.001
            or value >= float(upper_values[active_index]) * 0.999
        )
    return FoldMetric(
        fold=fold_index,
        coverage95=float(covered.to(dtype=torch.double).mean().item()),
        mean_nlpd=float(nlpd.mean().item()),
        lengthscales=tuple(full_lengths),
        boundary_hits=tuple(full_hits),
        test_count=len(test_rows),
    )


def run_cross_validation(
    rows: Sequence[CalibrationRow],
    *,
    maxiter: int = 200,
    noise_grid: Sequence[float] = NOISE_FLOOR_GRID_N2,
) -> tuple[NoiseCandidateResult, ...]:
    if tuple(noise_grid) != NOISE_FLOOR_GRID_N2:
        raise R010CalibrationError("release calibration must evaluate the canonical noise grid")
    folds = deterministic_grouped_folds(rows)
    results: list[NoiseCandidateResult] = []
    for floor in noise_grid:
        fold_results = tuple(
            _fit_fold(
                rows,
                test_keys=frozenset(test_keys),
                noise_floor_n2=floor,
                fold_index=fold_index,
                maxiter=maxiter,
            )
            for fold_index, test_keys in enumerate(folds)
        )
        results.append(NoiseCandidateResult(noise_floor_n2=floor, folds=fold_results))
    return tuple(results)


__all__ = ["run_cross_validation"]
