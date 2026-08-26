"""Serial q=1 discovery schedule and tightly bounded early censor policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np

from .catalog import (
    CatalogArm,
    build_frozen_catalog,
    deterministic_maximin_sobol,
    normalized_log_coordinates,
)
from .certification import AttemptAdmission, gp_training_rows
from .common import R014Error, finite


DISCOVERY_ATTEMPTS = 64
WARM_START_ATTEMPTS = 8
KNOWN_NOISE_VARIANCE_N2 = 0.01
PREFIX_BINS = 25
CENSOR_MULTIPLIER = 2.0


def warm_start_arm_ids() -> tuple[str, ...]:
    return (
        "human-anchor",
        "r013-incumbent-coordinates",
        *deterministic_maximin_sobol(6),
    )


@dataclass(frozen=True)
class DiscoveryProposal:
    physical_attempt_index: int
    arm_id: str
    role: str
    censor_allowed: bool
    forced_full: bool


def warm_start_proposal(physical_attempt_index: int) -> DiscoveryProposal:
    if not 1 <= physical_attempt_index <= WARM_START_ATTEMPTS:
        raise R014Error("warm-start proposal index must be in 1..8")
    arm_id = warm_start_arm_ids()[physical_attempt_index - 1]
    role = "anchor" if physical_attempt_index <= 2 else "deterministic_maximin"
    return DiscoveryProposal(
        physical_attempt_index=physical_attempt_index,
        arm_id=arm_id,
        role=role,
        censor_allowed=False,
        forced_full=True,
    )


def should_censor(
    *,
    physical_attempt_index: int,
    role: str,
    prefix_bin_mae_n: Iterable[float],
    qualification_incumbent_repeat_mean_n: float,
) -> bool:
    """Return true only at the predeclared ordinary-novel discovery seam."""

    if not 9 <= physical_attempt_index <= DISCOVERY_ATTEMPTS:
        return False
    if role != "ordinary_novel_bo":
        return False
    prefix = tuple(finite(value, "prefix_bin_mae_n") for value in prefix_bin_mae_n)
    if len(prefix) != PREFIX_BINS:
        return False
    threshold = CENSOR_MULTIPLIER * finite(
        qualification_incumbent_repeat_mean_n,
        "qualification_incumbent_repeat_mean_n",
    )
    if threshold <= 0.0:
        raise R014Error("early-censor incumbent mean must be positive")
    return sum(prefix) / len(prefix) > threshold


class DiscreteQLogNEISelector:
    """Optional BoTorch bridge; no fallback may masquerade as qLogNEI."""

    def select(
        self,
        attempts: Iterable[AttemptAdmission],
        *,
        excluded_arm_ids: Iterable[str] = (),
    ) -> str:
        training = gp_training_rows(attempts)
        if len(training) < WARM_START_ATTEMPTS:
            raise R014Error("qLogNEI requires the eight forced-full warm-start attempts")
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
            raise R014Error("qLogNEI backend is unavailable; no optimizer fallback is permitted") from exc

        catalog = build_frozen_catalog()
        by_id = {arm.arm_id: arm for arm in catalog}
        excluded = set(excluded_arm_ids) | {attempt.arm_id for attempt in training}
        choices = [arm for arm in catalog if arm.arm_id not in excluded]
        if not choices:
            raise R014Error("qLogNEI has no untried frozen-catalog arms")
        if any(attempt.arm_id not in by_id for attempt in training):
            raise R014Error("qLogNEI training row is outside the frozen catalog")

        dtype = torch.double
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        train_x = torch.as_tensor(
            np.vstack([normalized_log_coordinates(by_id[item.arm_id]) for item in training]),
            dtype=dtype,
            device=device,
        )
        # Minimize loss by fitting its negative. Known physical observation
        # variance is frozen at 0.01 N^2 for discovery only.
        train_y = torch.as_tensor(
            [[-float(item.loss_n)] for item in training], dtype=dtype, device=device
        )
        train_yvar = torch.full_like(train_y, KNOWN_NOISE_VARIANCE_N2)
        model = SingleTaskGP(
            train_x,
            train_y,
            train_Yvar=train_yvar,
            input_transform=Normalize(d=6),
            outcome_transform=Standardize(m=1),
        )
        fit_gpytorch_mll(ExactMarginalLogLikelihood(model.likelihood, model))
        acquisition = qLogNoisyExpectedImprovement(
            model=model,
            X_baseline=train_x,
            sampler=SobolQMCNormalSampler(sample_shape=torch.Size([256]), seed=20260821),
            prune_baseline=True,
        )
        choice_tensor = torch.as_tensor(
            np.vstack([normalized_log_coordinates(arm) for arm in choices]),
            dtype=dtype,
            device=device,
        )
        selected, _value = optimize_acqf_discrete(
            acq_function=acquisition,
            q=1,
            choices=choice_tensor,
            unique=True,
        )
        selected_row = selected[0].detach().cpu().numpy()
        distances = np.linalg.norm(
            np.vstack([normalized_log_coordinates(arm) for arm in choices]) - selected_row,
            axis=1,
        )
        index = int(np.argmin(distances))
        if distances[index] > 1e-10:
            raise R014Error("qLogNEI discrete selection did not map back to one frozen arm")
        return choices[index].arm_id


def censored_revisit_ids(attempts: Iterable[AttemptAdmission]) -> tuple[str, ...]:
    """Certification imports coordinates to revisit, never censored losses."""

    return tuple(sorted({attempt.arm_id for attempt in attempts if attempt.censored}))
