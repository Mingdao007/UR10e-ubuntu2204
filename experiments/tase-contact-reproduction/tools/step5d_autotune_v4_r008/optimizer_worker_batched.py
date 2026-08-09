"""r008 batched qLogNEI worker (Stage A/B overlay).

Reuses frozen r006 request/attest/fit/kernel helpers and replaces only the
per-combination acquisition loop with one t-batched forward ``(N, q, 7)``.
Keeps ``tools/step5d_autotune_v4_r006/optimizer_worker.py`` byte-identical.

Phase A: train/pending/choices/scoring use ``feature_map_r008`` (log2(P/D)
performance axis) instead of r006 ``_features``. Warm-start noise floor in the
local initializer matches FixedNoise ``2.5e-5``. Kernel stays Matérn 5/2.
"""

from __future__ import annotations

import itertools
import os
import statistics
from typing import Any, Mapping, Sequence

import step5d_autotune_v4_r006.optimizer_worker as _r006_worker
from step5d_autotune_v4_r006.optimizer_worker import (
    OptimizerWorkerError,
    RESPONSE_SCHEMA,
    _load_model_state,
    _model_state_payload,
    _point,
    _self_attest,
)
from pathlib import Path

from step5d_autotune_v4_r008.bounded_worker_artifact_binding import (
    bounded_artifact_binding as _artifact_binding,
)
from step5d_autotune_v4_r008.early_abort_penalty import load_early_abort_penalties
from step5d_autotune_v4_r008.hard_stop_penalty import (
    load_penalties,
    merge_penalties_into_grouped,
)
from step5d_autotune_v4_r008.optimizer import feature_map_r008
from step5d_autotune_v4_r010.kernel import build_conditional_matern52_kernel

# Calibrated FixedNoise / warm-start noise floor (σ≈0.005 N).
FIXED_NOISE_N2 = 2.5e-5


def _features(point: Any) -> tuple[float, ...]:
    """GP feature map for the r008 batched worker (alias of feature_map_r008)."""

    return feature_map_r008(point)


def _deterministic_initialization(
    model: Any,
    *,
    points: Sequence[Any],
    values: Sequence[float],
    features: Any,
    torch: Any,
    r010_calibration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """r008-local warm-start seed: same probes as r006, noise floor = FIXED_NOISE_N2.

    Leaves frozen r006 ``_deterministic_initialization`` (1e-4 floor) untouched.
    """

    if not points or len(points) != len(values):
        raise OptimizerWorkerError("r008 deterministic GP initializer has no observations")
    by_point: dict[tuple[Any, ...], list[float]] = {}
    for point, value in zip(points, values, strict=True):
        by_point.setdefault(tuple(point.key), []).append(float(value))
    variance = statistics.variance if r010_calibration is not None else statistics.pvariance
    repeat_variances = [variance(row) for row in by_point.values() if len(row) > 1]
    floor = float(
        r010_calibration["noise"]["selected_floor_n2"]
        if r010_calibration is not None
        else FIXED_NOISE_N2
    )
    repeat_noise_n2 = max(
        floor, statistics.fmean(repeat_variances) if repeat_variances else floor
    )
    signal_scale_n = max(
        floor,
        statistics.pstdev(values) if len(values) > 1 else abs(float(values[0])) * 0.1,
    )
    matrix = torch.as_tensor(features, dtype=torch.double, device=model.train_inputs[0].device)
    spans = (matrix.max(dim=0).values - matrix.min(dim=0).values).clamp_min(1.0)
    base = model.covar_module.base_kernel
    if r010_calibration is None:
        initializations = (spans, spans, spans)
    else:
        policy = r010_calibration["lengthscales"]
        initializations = tuple(
            torch.tensor(policy[name], dtype=torch.double, device=matrix.device)
            for name in ("shared", "same_mode", "i_on_only")
        )
    for component, initialization in zip(
        (base.shared, base.same_mode, base.i_on_only), initializations, strict=True
    ):
        component.initialize(lengthscale=initialization.reshape(1, 1, -1))
    model.covar_module.initialize(
        outputscale=torch.tensor(signal_scale_n**2, dtype=torch.double, device=matrix.device)
    )
    return {
        "method": "repeats_plus_minus_probes",
        "signal_scale_n": float(signal_scale_n),
        "lengthscales": (
            [float(value) for value in spans.detach().cpu().tolist()]
            if r010_calibration is None
            else {
                name: [float(value) for value in initialization.detach().cpu().tolist()]
                for name, initialization in zip(
                    ("shared", "same_mode", "i_on_only"), initializations, strict=True
                )
            }
        ),
        "repeat_noise_n2": float(repeat_noise_n2),
        "repeat_noise_n2_floor": floor,
        "repeat_point_count": sum(1 for row in by_point.values() if len(row) > 1),
        "variance_estimator": (
            "statistics.variance"
            if r010_calibration is not None
            else "statistics.pvariance"
        ),
    }

# r006._request() validates by calling module-global ``_artifact_binding`` and
# discards the result; then ``_fit_and_ask`` binds again. Left on the frozen
# r006 binder that meant every ask paid full-sidecar cold_read_verify (~200s)
# *plus* the r008 bounded bind (~65s) — the ~266s wall after dedup. Point the
# r006 module name at the r008 bounded binder so both passes share it (and the
# keepalive receipt cache).
_r006_worker._artifact_binding = _artifact_binding
from step5d_autotune_v4_r008.qlognei_compile import (
    COMPILE_ENV,
    compile_enabled_from_env,
    is_compiled_acquisition,
    prepare_scoring_acquisition,
)
from step5d_optimizer_runtime import RESPONSE_SCHEMA as _RUNTIME_RESPONSE_SCHEMA

assert RESPONSE_SCHEMA == _RUNTIME_RESPONSE_SCHEMA

# Stage B: optional compile via qlognei_compile. Default OFF — measured slower
# than plain batched on this BoTorch/GPyTorch stack (~0.8s vs ~5–12ms for N=76
# after recompile storms). Set R008_QLOGNEI_COMPILE=1 only for experiments.
_COMPILE_ENV = COMPILE_ENV
_CHUNK_ENV = "R008_QLOGNEI_CHUNK"
_DEFAULT_CHUNK_FALLBACK = 64


def _chunk_size() -> int | None:
    raw = os.environ.get(_CHUNK_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise OptimizerWorkerError(f"{_CHUNK_ENV} must be a positive int") from exc
    if value <= 0:
        raise OptimizerWorkerError(f"{_CHUNK_ENV} must be a positive int")
    return value


def _maybe_compile_acquisition(acquisition: Any, *, example_x: Any = None, torch: Any = None) -> Any:
    """Stage B hook: env-gated compile with fail-open to Stage A acquisition."""

    scorer, _compiled = prepare_scoring_acquisition(
        acquisition,
        enabled=compile_enabled_from_env(),
        example_x=example_x,
        torch_module=torch,
    )
    return scorer


def _score_combination_batches(
    acquisition: Any,
    choices: tuple[Any, ...],
    requested_q: int,
    *,
    device: Any,
    torch: Any,
) -> tuple[Any, list[float], str]:
    batches = list(itertools.combinations(choices, requested_q))
    if not batches:
        raise OptimizerWorkerError("r006 qLogNEI has no finite candidate batch")
    X = torch.tensor(
        [[_features(point) for point in batch] for batch in batches],
        dtype=torch.double,
        device=device,
    )
    # Warmup on a single row so compile/capture cost stays off the timed path.
    scorer = _maybe_compile_acquisition(acquisition, example_x=X[:1], torch=torch)
    chunk = _chunk_size()
    scoring = "batched_tbatch"
    if is_compiled_acquisition(scorer):
        scoring = "batched_tbatch_compile"

    def _forward(tensor: Any) -> Any:
        with torch.inference_mode():
            return scorer(tensor)

    try:
        if chunk is None or chunk >= len(X):
            scores = _forward(X)
        else:
            scoring = f"{scoring}_chunk{chunk}"
            parts = [_forward(X[i : i + chunk]) for i in range(0, len(X), chunk)]
            scores = torch.cat(parts, dim=0)
    except RuntimeError as exc:
        message = str(exc).lower()
        if "out of memory" not in message or (chunk is not None and chunk <= _DEFAULT_CHUNK_FALLBACK):
            raise
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        scoring = f"{scoring}_oom_chunk{_DEFAULT_CHUNK_FALLBACK}"
        parts = [
            _forward(X[i : i + _DEFAULT_CHUNK_FALLBACK])
            for i in range(0, len(X), _DEFAULT_CHUNK_FALLBACK)
        ]
        scores = torch.cat(parts, dim=0)

    score_list = [float(value) for value in scores.detach().cpu().tolist()]
    if len(score_list) != len(batches):
        raise OptimizerWorkerError("batched qLogNEI score count differs from combinations")
    scored = [
        (score_list[index], tuple(point.key for point in batches[index]), batches[index])
        for index in range(len(batches))
    ]
    selected_batch = max(
        scored,
        key=lambda row: (row[0], tuple(str(value) for value in row[1])),
    )[2]
    return selected_batch, score_list, scoring


def _fit_and_ask(
    payload: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    r010_calibration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    import torch
    import gpytorch
    from botorch.acquisition.logei import qLogNoisyExpectedImprovement
    from botorch.acquisition.objective import GenericMCObjective
    from botorch.fit import fit_gpytorch_mll
    from botorch.models import SingleTaskGP
    from gpytorch.mlls import ExactMarginalLogLikelihood
    from gpytorch.likelihoods import FixedNoiseGaussianLikelihood

    attestation = _self_attest(expected)
    artifact_binding = _artifact_binding(payload["artifact_binding"])
    operation = str(payload.get("operation", "ask"))
    choices = tuple(_point(key) for key in payload["choices"])
    pending = tuple(_point(key) for key in payload["pending"])
    if operation not in {"ask", "fit", "certificate"}:
        raise OptimizerWorkerError("r006 optimizer operation is invalid")
    if operation == "ask" and not choices:
        raise OptimizerWorkerError("qLogNEI choice set is empty")
    seed = payload["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise OptimizerWorkerError("qLogNEI seed is invalid")
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    rows = [row for row in artifact_binding["rows"] if row["receipt"].trainable]
    if len(rows) < 2:
        raise OptimizerWorkerError("fresh r006 sidecar has fewer than two trainable observations")
    if any(row.get("point_key") is None for row in rows):
        raise OptimizerWorkerError("r006 artifact row lacks a typed point key")

    # r008 dedup (found live 2026-08-04): repeated candidates (ANCHOR/
    # STAIRCASE validation repeats, revisited BO_TRIAL points) share the
    # exact same feature vector, which makes two rows of the training
    # covariance matrix exactly identical -- a kernel is a deterministic
    # function of (x1, x2), so duplicate x rows force duplicate K rows/cols,
    # i.e. an exactly singular matrix. Measured live: 89 trainable rows had
    # only 60 unique feature vectors (25 duplicate groups, 34 zero-distance
    # pairs), which pushed Cholesky through repeated jitter retries (1e-8 up
    # to 1e-3) into botorch's eigendecomposition fallback -- costing
    # ~200-260s per ask(), independent of candidate count, batching, or the
    # artifact-binding cost already fixed earlier tonight. Merge exact-
    # duplicate points into one training row (mean objective; noise
    # variance from the empirical between-repeat spread, floored at the
    # fixed 2.5e-5 measurement-noise estimate) before building train_x/train_y.
    grouped: dict[tuple[Any, ...], list[float]] = {}
    group_order: list[tuple[Any, ...]] = []
    for row in rows:
        key = tuple(row["point_key"])
        if key not in grouped:
            group_order.append(key)
            grouped[key] = []
        grouped[key].append(float(row["receipt"].objective))
    # Hard-stop penalties (r008-hard-stop-penalty.jsonl): BO-only training
    # rows with MAE = historical max at stop time. Not raw-path evidence.
    # Early-abort penalties: only rows with enters_gp_training=True (shadow
    # rows stay False and must not poison GP labels).
    sidecar_path = artifact_binding.get("sidecar_path")
    penalty_count = 0
    early_abort_penalty_count = 0
    if isinstance(sidecar_path, str) and sidecar_path:
        sidecar_dir = Path(sidecar_path).resolve().parent
        penalty_count = merge_penalties_into_grouped(
            grouped,
            group_order,
            load_penalties(sidecar_dir),
        )
        early_abort_rows = [
            row
            for row in load_early_abort_penalties(sidecar_dir)
            if row.get("enters_gp_training") is True
        ]
        early_abort_penalty_count = merge_penalties_into_grouped(
            grouped,
            group_order,
            early_abort_rows,
        )
    train_points = tuple(_point(list(key)) for key in group_order)
    device = torch.device("cuda:0")
    train_x = torch.tensor([_features(point) for point in train_points], dtype=torch.double, device=device)
    # Calibrated down from 1e-4 (σ≈0.01 N): ANCHOR I-off repeats are ~0.003 N sd;
    # 2.5e-5 ⇒ σ≈0.005 N keeps a small floor without drowning sealed MAE signal.
    fixed_noise_n2 = float(
        r010_calibration["noise"]["selected_floor_n2"]
        if r010_calibration is not None
        else FIXED_NOISE_N2
    )
    variance = statistics.variance if r010_calibration is not None else statistics.pvariance
    train_y_values = [statistics.fmean(grouped[key]) for key in group_order]
    train_yvar_values = [
        max(fixed_noise_n2, variance(grouped[key])) if len(grouped[key]) > 1 else fixed_noise_n2
        for key in group_order
    ]
    train_y = torch.tensor([[value] for value in train_y_values], dtype=torch.double, device=device)
    train_yvar = torch.tensor([[value] for value in train_yvar_values], dtype=torch.double, device=device)
    initializer_features = train_x.detach()

    model = SingleTaskGP(
        train_X=train_x,
        train_Y=train_y,
        train_Yvar=train_yvar,
        likelihood=FixedNoiseGaussianLikelihood(noise=train_yvar.squeeze(-1)),
        covar_module=gpytorch.kernels.ScaleKernel(
            build_conditional_matern52_kernel(gpytorch),
            outputscale_prior=gpytorch.priors.GammaPrior(2.0, 0.15),
        ),
    ).to(device=device, dtype=torch.double)
    initialization = _deterministic_initialization(
        model,
        points=train_points,
        values=train_y_values,
        features=initializer_features,
        torch=torch,
        r010_calibration=r010_calibration,
    )
    if bool(payload["hyperparameters_frozen"]):
        _load_model_state(model, payload.get("frozen_model_state"), torch=torch)
    else:
        mll = ExactMarginalLogLikelihood(model.likelihood, model)
        fit_gpytorch_mll(mll)
    model.eval()
    requested_q = int(payload.get("q", 1))
    if requested_q not in (1, 4):
        raise OptimizerWorkerError("r006 qLogNEI q must be 1 or 4")
    if operation != "ask" and requested_q != 1:
        raise OptimizerWorkerError("r006 fit/certificate operations require q=1")
    metadata = {
        "worker": "r008_batched_cuda_botorch",
        "kernel": "conditional_matern52_shared_same_mode_i_on_only",
        "train_yvar_n2_floor": fixed_noise_n2,
        "qlognei": "qLogNEI",
        "x_pending": True,
        "x_pending_count": len(pending),
        "fit_group": payload["fit_group"],
        "hyperparameters_frozen": bool(payload["hyperparameters_frozen"]),
        "observation_count": len(train_points),
        "raw_trainable_row_count": len(rows),
        "hard_stop_penalty_row_count": int(penalty_count),
        "early_abort_penalty_row_count": int(early_abort_penalty_count),
        "deduplicated_row_count": len(rows) - len(train_points),
        "q": requested_q,
        "initialization": initialization,
        "child_attestation": attestation,
    }
    if r010_calibration is not None:
        metadata["r010_calibration"] = {
            "calibration_sha256": r010_calibration["calibration_sha256"],
            "noise_floor_n2": fixed_noise_n2,
            "variance_estimator": r010_calibration["noise"]["variance_estimator"],
            "lengthscale_policy": r010_calibration["lengthscales"]["policy"],
            "kernel_implementation_sha256": r010_calibration["kernel"]["implementation_sha256"],
        }
    if operation == "fit":
        metadata["fit_only"] = True
        return {
            "metadata": metadata,
            "model_state": _model_state_payload(model, torch=torch),
            "attestation": attestation,
        }
    if operation == "certificate":
        if not choices:
            raise OptimizerWorkerError("certificate region is empty")
        region_x = torch.tensor(
            [_features(point) for point in choices], dtype=torch.double, device=device
        )
        posterior = model.posterior(region_x)
        means = posterior.mean.reshape(-1)
        variances = posterior.variance.reshape(-1).clamp_min(0.0)
        z = statistics.NormalDist().inv_cdf(1.0 - 0.05 / (2.0 * len(choices)))
        incumbent_point = _point(payload["incumbent"])
        incumbent_index = next(
            (index for index, point in enumerate(choices) if point == incumbent_point),
            None,
        )
        if incumbent_index is None:
            raise OptimizerWorkerError("certificate incumbent is outside the finite region")
        incumbent_ucb = float(means[incumbent_index].item()) + z * float(torch.sqrt(variances[incumbent_index]).item())
        minimum_lcb = float(torch.min(means - z * torch.sqrt(variances)).item())
        metadata["certificate"] = {
            "incumbent_ucb_n": incumbent_ucb,
            "minimum_lcb_n": minimum_lcb,
            "epsilon_n": float(payload["epsilon_n"]),
            "region_size": len(choices),
            "simultaneous_z95": z,
            "global_convergence_claim": False,
        }
        return {"metadata": metadata, "attestation": attestation}
    baseline = train_x
    pending_x = (
        torch.tensor([_features(point) for point in pending], dtype=torch.double, device=device)
        if pending
        else None
    )
    objective = GenericMCObjective(lambda samples, X=None: -samples.squeeze(-1))
    acquisition = qLogNoisyExpectedImprovement(
        model=model,
        X_baseline=baseline,
        X_pending=pending_x,
        objective=objective,
        prune_baseline=False,
    )
    selected_batch, _score_list, scoring = _score_combination_batches(
        acquisition,
        choices,
        requested_q,
        device=device,
        torch=torch,
    )
    metadata["scoring"] = scoring
    selected = selected_batch[0]
    return {
        "selected_point_key": list(selected.key),
        "selected_point_keys": [list(point.key) for point in selected_batch],
        "metadata": {
            **metadata,
        },
        "attestation": attestation,
    }


def run(request: Mapping[str, Any]) -> dict[str, Any]:
    # Uses r006._request after the module-global binder patch above.
    expected, payload = _r006_worker._request(request)
    return _fit_and_ask(payload, expected)


__all__ = [
    "FIXED_NOISE_N2",
    "RESPONSE_SCHEMA",
    "run",
    "_features",
    "_deterministic_initialization",
    "_score_combination_batches",
    "_maybe_compile_acquisition",
    "_COMPILE_ENV",
]
