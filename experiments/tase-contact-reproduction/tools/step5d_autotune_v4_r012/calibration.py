"""True grouped-CV BoTorch calibration for the R012 5-D tau-active GP."""

from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Any, Sequence

from step5d_force_objective import FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT

from .offline_checksums import digest, sha256_bytes
from .censor import ExactObservation
from .qlognei import DOMAIN_LOG_BOUNDS, NOISE_FLOOR_N2, ProductionGPConfig, candidate_to_log_features_unbounded, fit_production_gp


CALIBRATION_SCHEMA = "step5d.autotune-v4/r012-gp-calibration-comparison-v1"
DEFAULT_R010_LEDGER = Path(
    "/home/andy/.codex-worktrees/step5d-v4-r004-20260801/experiments/tase-contact-reproduction/runs/step5d_autotune_v4_r008/live_20260807_025535_b3_phase5_raw2/r006-observations.jsonl"
)
DEFAULT_R010_ARTIFACT = Path(__file__).resolve().parents[2] / "config/step5d/autotune_v4_r010_gp_calibration.json"
R010_REFERENCE_NLPD = -0.762361112606967
FOLD_ASSIGNMENT_EXPECTED = "1a4041ece86d5c69642f03c812cbfa8663a920bca412fb8ea7290f76adfeae57"
IDENTITY = "0" * 64


def _metrics(actual: Sequence[float], predicted: Sequence[float], variances: Sequence[float]) -> dict[str, float]:
    n = len(actual)
    covered = sum(abs(y - mean) <= 1.96 * math.sqrt(max(var, 1e-12)) for y, mean, var in zip(actual, predicted, variances))
    nlpd = statistics.fmean(
        0.5 * (math.log(2.0 * math.pi * max(var, 1e-12)) + (y - mean) ** 2 / max(var, 1e-12))
        for y, mean, var in zip(actual, predicted, variances)
    )

    def rank(values: Sequence[float]) -> list[int]:
        return [position for _value, position in sorted((value, index) for index, value in enumerate(values))]

    actual_order, predicted_order = rank(actual), rank(predicted)
    ar = {index: position for position, index in enumerate(actual_order)}
    pr = {index: position for position, index in enumerate(predicted_order)}
    mean_a, mean_p = (n - 1) / 2.0, (n - 1) / 2.0
    numerator = sum((ar[i] - mean_a) * (pr[i] - mean_p) for i in range(n))
    denominator = math.sqrt(sum((ar[i] - mean_a) ** 2 for i in range(n)) * sum((pr[i] - mean_p) ** 2 for i in range(n)))
    decile = max(1, n // 10)
    low_actual = set(sorted(range(n), key=lambda i: actual[i])[:decile])
    low_pred = set(sorted(range(n), key=lambda i: predicted[i])[:decile])
    high_actual = set(sorted(range(n), key=lambda i: actual[i])[-decile:])
    high_pred = set(sorted(range(n), key=lambda i: predicted[i])[-decile:])
    return {
        "rows": n,
        "coverage95": covered / n,
        "mean_nlpd": nlpd,
        "all_candidate_rank_correlation": numerator / denominator if denominator else 0.0,
        "low_tail_recall": len(low_actual & low_pred) / decile,
        "top_decile_recall": len(high_actual & high_pred) / decile,
    }


def _load_admitted(ledger_path: Path):
    from step5d_autotune_v4_r010.gp_calibration import (
        admit_phase5_ledger,
        deterministic_grouped_folds,
        fold_assignment_sha256,
        load_calibration_artifact,
    )

    admission = admit_phase5_ledger(Path(ledger_path), expected_rows=567, expected_timing_ineligible=13)
    folds = deterministic_grouped_folds(admission.rows)
    artifact = load_calibration_artifact(DEFAULT_R010_ARTIFACT)
    if (
        artifact["source"]["dataset_sha256"] != admission.dataset_sha256
        or artifact["source"]["admitted_rows"] != len(admission.rows)
        or artifact["cross_validation"]["assignment_sha256"] != fold_assignment_sha256(folds)
        or fold_assignment_sha256(folds) != FOLD_ASSIGNMENT_EXPECTED
    ):
        raise ValueError("R010 published dataset/fold binding differs")
    return admission, folds, artifact


def _row_variances(rows: Sequence[Any], floor: float = NOISE_FLOOR_N2) -> dict[tuple[Any, ...], float]:
    grouped: dict[tuple[Any, ...], list[float]] = {}
    for row in rows:
        grouped.setdefault(row.gain_key, []).append(float(row.objective_n))
    return {key: max(floor, statistics.variance(values) if len(values) > 1 else floor) for key, values in grouped.items()}


def _as_exact(row: Any) -> ExactObservation:
    candidate = dict(row.candidate)
    candidate.setdefault("force_i_gain", 0.0)
    candidate.setdefault("i_off", True)
    candidate.setdefault("target_force_n", 5.0)
    return ExactObservation(
        f"calibration-{row.attempt_sequence}",
        candidate,
        float(row.objective_n),
        "r012-calibration",
        "r012-calibration-run",
        "a001",
        kind=row.kind,
    )


def _run_r012(rows: Sequence[Any], folds: Sequence[Sequence[tuple[Any, ...]]]) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    fold_for_key = {key: index for index, fold in enumerate(folds) for key in fold}
    predictions: list[tuple[float, float, float]] = []
    receipts: list[dict[str, Any]] = []
    for fold_index in range(5):
        train = [row for row in rows if fold_for_key[row.gain_key] != fold_index]
        test = [row for row in rows if fold_for_key[row.gain_key] == fold_index]
        train_variances = _row_variances(train)
        config = ProductionGPConfig()
        fit = fit_production_gp(
            tuple(_as_exact(row) for row in train),
            observation_variances=tuple(train_variances[row.gain_key] for row in train),
            config=config,
            allow_historical_out_of_box=True,
        )
        import torch

        device = next(fit.model.parameters()).device
        x = torch.tensor(
            [candidate_to_log_features_unbounded(row.candidate, historical=True) for row in test],
            dtype=torch.double,
            device=device,
        )
        with torch.no_grad():
            posterior = fit.model.posterior(x)
        means = posterior.mean.detach().cpu().reshape(-1).tolist()
        latent_vars = posterior.variance.detach().cpu().reshape(-1).tolist()
        predictions.extend(
            (float(row.objective_n), float(mean), float(var) + config.noise_floor_n2)
            for row, mean, var in zip(test, means, latent_vars)
        )
        receipts.append({"fold": fold_index, "train_rows": len(train), "test_rows": len(test), "fit": fit.fit_receipt})
    actual = [item[0] for item in predictions]
    predicted = [item[1] for item in predictions]
    variance = [item[2] for item in predictions]
    return _metrics(actual, predicted, variance), tuple(receipts)


def _run_r010_recomputed(rows: Sequence[Any], folds: Sequence[Sequence[tuple[Any, ...]]]) -> dict[str, Any]:
    import torch
    import gpytorch
    from botorch.fit import fit_gpytorch_mll
    from botorch.models import SingleTaskGP
    from gpytorch.mlls import ExactMarginalLogLikelihood
    from step5d_autotune_v4_r010.gp_calibration import feature_map

    fold_for_key = {key: index for index, fold in enumerate(folds) for key in fold}
    predictions: list[tuple[float, float, float]] = []
    for fold_index in range(5):
        train = [row for row in rows if fold_for_key[row.gain_key] != fold_index]
        test = [row for row in rows if fold_for_key[row.gain_key] == fold_index]
        variances = _row_variances(train, floor=0.005)
        if not torch.cuda.is_available():
            raise RuntimeError("R010 recomputed baseline requires the same CUDA optimizer runtime")
        device = torch.device("cuda")
        tx = torch.tensor([feature_map(row.candidate) for row in train], dtype=torch.double, device=device)
        ty = torch.tensor([row.objective_n for row in train], dtype=torch.double, device=device).unsqueeze(-1)
        tv = torch.tensor([variances[row.gain_key] for row in train], dtype=torch.double, device=device).unsqueeze(-1)
        covar = gpytorch.kernels.ScaleKernel(gpytorch.kernels.MaternKernel(nu=2.5, ard_num_dims=7))
        model = SingleTaskGP(tx, ty, train_Yvar=tv, covar_module=covar).to(device=device, dtype=torch.double)
        model.train()
        model.likelihood.train()
        fit_gpytorch_mll(ExactMarginalLogLikelihood(model.likelihood, model), optimizer_kwargs={"options": {"maxiter": 200}})
        model.eval()
        model.likelihood.eval()
        x = torch.tensor([feature_map(row.candidate) for row in test], dtype=torch.double, device=device)
        with torch.no_grad():
            posterior = model.posterior(x)
        means = posterior.mean.detach().cpu().reshape(-1).tolist()
        latent = posterior.variance.detach().cpu().reshape(-1).tolist()
        predictions.extend(
            (float(row.objective_n), float(mean), float(var) + 0.005) for row, mean, var in zip(test, means, latent)
        )
    actual = [item[0] for item in predictions]
    predicted = [item[1] for item in predictions]
    variance = [item[2] for item in predictions]
    return _metrics(actual, predicted, variance)


def historical_calibration_comparison(*, ledger_path: Path = DEFAULT_R010_LEDGER, artifact_path: Path | None = None) -> dict[str, Any]:
    del artifact_path
    admission, folds, artifact = _load_admitted(Path(ledger_path))
    r012, fit_receipts = _run_r012(admission.rows, folds)
    r010 = _run_r010_recomputed(admission.rows, folds)
    published_nlpd = float(artifact["noise"]["selected_mean_nlpd"])
    acceptance = {
        "coverage95": 0.925 <= r012["coverage95"] <= 0.975,
        "mean_nlpd": r012["mean_nlpd"] <= R010_REFERENCE_NLPD + 0.05,
        "low_tail_recall": r012["low_tail_recall"] >= r010["low_tail_recall"],
    }
    out_of_box = sum(
        any(value < low or value > high for value, (low, high) in zip(candidate_to_log_features_unbounded(row.candidate, historical=True), DOMAIN_LOG_BOUNDS))
        for row in admission.rows
    )
    report = {
        "schema": CALIBRATION_SCHEMA,
        "objective_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
        "source": {
            "ledger_path": str(ledger_path),
            "ledger_sha256": sha256_bytes(Path(ledger_path).read_bytes()),
            "artifact_path": str(DEFAULT_R010_ARTIFACT),
            "artifact_sha256": sha256_bytes(DEFAULT_R010_ARTIFACT.read_bytes()),
            "admitted_rows": len(admission.rows),
            "dataset_sha256": admission.dataset_sha256,
            "fold_assignment_sha256": FOLD_ASSIGNMENT_EXPECTED,
            "fixed_domain_out_of_box_raw_count": out_of_box,
            "raw_features_clipped_or_projected": False,
        },
        "new_r012_5d_tau_active_raw_repeat": r012,
        "r012_fit_receipts": fit_receipts,
        "r010_recomputed_baseline": r010,
        "published_r010_reference_mean_nlpd": published_nlpd,
        "acceptance": acceptance,
        "model": ProductionGPConfig().as_dict(),
        "noise_semantics": f"train repeat keys use max({NOISE_FLOOR_N2}, statistics.variance(raw objectives)); train singleton and unseen held-out test keys use {NOISE_FLOOR_N2}; raw rows preserved; no test-outcome variance leakage",
    }
    report["comparison_sha256"] = digest(report)
    return report


__all__ = ["CALIBRATION_SCHEMA", "DEFAULT_R010_ARTIFACT", "DEFAULT_R010_LEDGER", "historical_calibration_comparison"]
