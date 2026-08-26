#!/usr/bin/env python3
"""CUDA-isolated qLogNEI worker for V4 Stage A/B."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import math
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r013.v4_two_stage_campaign import V4Stage  # type: ignore[import-not-found]
from step5d_autotune_v4_r013.v4_stage_optimizer import (  # type: ignore[import-not-found]
    V4OptimizerError,
    propose_qlognei,
)
from step5d_autotune_v4_r013.v4_two_stage_campaign import (  # type: ignore[import-not-found]
    candidate_features,
    candidate_key,
    validate_candidate,
)


WORKER_SCHEMA = "step5d.autotune-v4/v4-stage-qlognei-worker-v1"


def _bounds(stage: V4Stage) -> tuple[tuple[float, float], ...]:
    from step5d_autotune_v4_r013.v4_stage_optimizer import (
        DAMPING_BOUNDS,
        I_OVER_P_BOUNDS,
        KO_BOUNDS,
        MOTION_KP_BOUNDS,
        P_OVER_D_BOUNDS,
        TAU_BOUNDS,
    )

    base = (
        (math.log2(P_OVER_D_BOUNDS[0]), math.log2(P_OVER_D_BOUNDS[1])),
        (math.log2(DAMPING_BOUNDS[0]), math.log2(DAMPING_BOUNDS[1])),
        (math.log2(TAU_BOUNDS[0]), math.log2(TAU_BOUNDS[1])),
        (math.log2(KO_BOUNDS[0]), math.log2(KO_BOUNDS[1])),
        (math.log2(MOTION_KP_BOUNDS[0]), math.log2(MOTION_KP_BOUNDS[1])),
    )
    if stage is V4Stage.FF_ION_LIMIT_7D_100:
        from step5d_autotune_v4_r013.v4_stage_optimizer import INTEGRAL_STATE_LIMIT_BOUNDS
        return base + (
            (math.log2(I_OVER_P_BOUNDS[0]), math.log2(I_OVER_P_BOUNDS[1])),
            (math.log2(INTEGRAL_STATE_LIMIT_BOUNDS[0]), math.log2(INTEGRAL_STATE_LIMIT_BOUNDS[1])),
        )
    return base + ((math.log2(I_OVER_P_BOUNDS[0]), math.log2(I_OVER_P_BOUNDS[1])),) if stage is V4Stage.FF_ION_6D_100 else base


def _fit_cuda(request: Mapping[str, Any], stage: V4Stage) -> dict[str, Any]:
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
        raise V4OptimizerError("V4 stage CUDA optimizer imports are unavailable") from exc
    if not torch.cuda.is_available():
        raise V4OptimizerError("V4 stage CUDA optimizer requires CUDA")
    observations = request.get("observations")
    pool = request.get("pool")
    if not isinstance(observations, Sequence) or not observations:
        raise V4OptimizerError("V4 stage worker has no exact observations")
    if not isinstance(pool, Sequence) or not pool:
        raise V4OptimizerError("V4 stage worker has no candidate pool")
    parsed_stage = stage
    groups = []
    fingerprints: set[str] = set()
    for row in observations:
        if not isinstance(row, Mapping):
            raise V4OptimizerError("V4 stage observation group is invalid")
        candidate = validate_candidate(row["candidate"], parsed_stage)
        groups.append((candidate_features(candidate, parsed_stage), float(row["mean_n"]), float(row["yvar_n2"]), candidate_key(candidate, parsed_stage)))
        fingerprints.add(str(row.get("fingerprint_sha256", request.get("fingerprint_sha256", ""))))
    if len(fingerprints) > 1:
        raise V4OptimizerError("V4 stage worker mixes fingerprints")
    device = torch.device("cuda")
    torch.manual_seed(int(request.get("seed", 6013)))
    tx = torch.tensor([row[0] for row in groups], dtype=torch.double, device=device)
    ty = torch.tensor([[row[1]] for row in groups], dtype=torch.double, device=device)
    tyvar = torch.tensor([[row[2]] for row in groups], dtype=torch.double, device=device)
    kernel = gpytorch.kernels.ScaleKernel(
        gpytorch.kernels.MaternKernel(
            nu=2.5,
            ard_num_dims=tx.shape[-1],
            lengthscale_prior=gpytorch.priors.GammaPrior(3.0, 6.0),
            lengthscale_constraint=gpytorch.constraints.Interval(0.03, 5.0),
        )
    )
    bounds = torch.tensor(_bounds(parsed_stage), dtype=torch.double, device=device).T
    model = SingleTaskGP(
        tx,
        ty,
        train_Yvar=tyvar,
        covar_module=kernel,
        input_transform=Normalize(d=tx.shape[-1], bounds=bounds),
        outcome_transform=Standardize(m=1),
    ).to(device=device, dtype=torch.double)
    mll = ExactMarginalLogLikelihood(model.likelihood, model)
    model.train()
    model.likelihood.train()
    fit_gpytorch_mll(mll)
    model.eval()
    model.likelihood.eval()
    objective = GenericMCObjective(lambda samples, X=None: -samples.squeeze(-1))
    train_x = tx
    acquisition = qLogNoisyExpectedImprovement(
        model=model,
        X_baseline=train_x,
        objective=objective,
        prune_baseline=False,
    )
    parsed_pool = tuple(validate_candidate(item, parsed_stage) for item in pool)
    pool_x = torch.tensor([candidate_features(item, parsed_stage) for item in parsed_pool], dtype=torch.double, device=device)
    with torch.no_grad():
        scores = acquisition(pool_x.unsqueeze(1)).reshape(-1)
    selected = int(torch.argmax(scores).item())
    lengthscales = tuple(float(value) for value in model.covar_module.base_kernel.lengthscale.detach().cpu().reshape(-1))
    return {
        "schema": WORKER_SCHEMA,
        "version": 1,
        "ok": True,
        "stage": parsed_stage.value,
        "candidate": parsed_pool[selected],
        "candidate_key": candidate_key(parsed_pool[selected], parsed_stage),
        "acquisition": "qLogNEI",
        "backend": "botorch.SingleTaskGP",
        "kernel": "matern52_ard",
        "fit_receipt": {
            "botorch_version": botorch.__version__,
            "gpytorch_version": gpytorch.__version__,
            "torch_version": torch.__version__,
            "device": str(device),
            "bounds": [list(pair) for pair in _bounds(parsed_stage)],
            "lengthscales_unit_fitted": list(lengthscales),
            "observation_group_count": len(groups),
            "fingerprint_sha256": next(iter(fingerprints), None),
        },
    }


def propose(request: Mapping[str, Any]) -> dict[str, Any]:
    try:
        stage = V4Stage(str(request["stage"]))
        if bool(request.get("require_cuda", True)):
            return _fit_cuda(request, stage)
        fallback_observations = tuple(
            {
                **dict(row),
                "mae_n": row.get("mae_n", row.get("mean_n")),
            }
            for row in request.get("observations", ())
        )
        return {
            "ok": True,
            **propose_qlognei(
                stage=stage,
                observations=fallback_observations,
                pool=request.get("pool", ()),
                local=bool(request.get("local_refinement", False)),
                require_cuda=False,
                seed=int(request.get("seed", 6013)),
            ),
            "worker_schema": WORKER_SCHEMA,
        }
    except Exception as exc:  # pragma: no cover - CLI boundary
        return {
            "schema": WORKER_SCHEMA,
            "version": 1,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    response = propose(request)
    args.response.write_text(json.dumps(response, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return 0 if response.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
