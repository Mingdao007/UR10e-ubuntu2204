"""R013 CUDA GP and serial finite-pool qLogNEI implementations."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .domain import (
    DOMAIN_LOG_BOUNDS,
    DOMAIN_SCHEMA,
    MODEL_DIMENSION_COUNT,
    MODEL_DIMENSIONS,
    candidate_to_log_features,
    candidate_to_normalized,
    physical_candidate_key,
)
from .identity import CampaignFingerprint, validate_campaign_fingerprint


GP_SCHEMA = "step5d.autotune-v4/r013-production-single-task-gp-v1"
QLOGNEI_SCHEMA = "step5d.autotune-v4/r013-production-qlognei-v1"
FIT_RECEIPT_SCHEMA = "step5d.autotune-v4/r013-gp-fit-receipt-v1"
Q = 1
CANDIDATE_POOL_SIZE = 128
YVAR_MIN_N2 = 1e-4
YVAR_MAX_N2 = 2e-2
REPEAT_PRIOR_DEGREES_OF_FREEDOM = 2
CORRECTION_GP_SCHEMA = "step5d.autotune-v4/r013-correction-single-task-gp-v1"
CORRECTION_OBSERVATION_GROUP_SCHEMA = (
    "step5d.autotune-v4/r013-correction-observation-group-v1"
)
CORRECTION_PROPOSAL_SCHEMA = "step5d.autotune-v4/r013-correction-qlognei-proposal-v1"
CORRECTION_MODEL_DIMENSION_COUNT = 6
CORRECTION_WEIGHT_BOUNDS = ((-0.5, 0.5),) * CORRECTION_MODEL_DIMENSION_COUNT
CORRECTION_IMPROVEMENT_THRESHOLD_N = 0.01
TRIAL_ANTI_WINDUP_SCHEMA = "step5d.autotune-v4/r013-trial-anti-windup-v1"
ANTI_WINDUP_POLICY = "conditional-double-clamp-v1"


class R013GPError(ValueError):
    """The R013 production model/fit/proposal contract is invalid."""


def _strict_weights(value: Any, name: str = "correction weights") -> tuple[float, ...]:
    if getattr(value, "is_legacy_migration", False) is True:
        raise R013GPError(f"R013 {name} must use real v2 weights")
    if isinstance(value, Mapping):
        if "coordinates" in value or "weights" not in value:
            raise R013GPError(f"R013 {name} must use real v2 weights")
        value = value["weights"]
    elif hasattr(value, "weights") and not isinstance(value, (str, bytes)):
        value = getattr(value, "weights")
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise R013GPError(f"R013 {name} must contain six weights")
    if len(value) != CORRECTION_MODEL_DIMENSION_COUNT:
        raise R013GPError(f"R013 {name} must contain six weights")
    parsed = tuple(_finite(item, f"{name}[{index}]") for index, item in enumerate(value))
    if any(
        parsed[index] < low or parsed[index] > high
        for index, (low, high) in enumerate(CORRECTION_WEIGHT_BOUNDS)
    ):
        raise R013GPError(f"R013 {name} is outside the v2 weight bounds")
    return parsed


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise R013GPError(f"{name} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise R013GPError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise R013GPError(f"{name} must be finite")
    return parsed


@dataclass(frozen=True)
class CorrectionObservationGroupV1:
    """One repeat-aware correction-weight observation group for the 6D GP."""

    campaign_fingerprint: CampaignFingerprint | Mapping[str, Any]
    weights: tuple[float, float, float, float, float, float]
    n: int
    mean_n: float
    yvar_n2: float
    schema: str = CORRECTION_OBSERVATION_GROUP_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != CORRECTION_OBSERVATION_GROUP_SCHEMA or self.version != 1:
            raise R013GPError("R013 correction observation-group schema/version differs")
        try:
            fingerprint = validate_campaign_fingerprint(self.campaign_fingerprint)
        except (TypeError, ValueError) as exc:
            raise R013GPError("R013 correction observation-group fingerprint is invalid") from exc
        if type(self.n) is not int or self.n < 1:
            raise R013GPError("R013 correction observation-group n must be at least one")
        weights = _strict_weights(self.weights)
        mean = _finite(self.mean_n, "correction observation-group mean_n")
        yvar = _finite(self.yvar_n2, "correction observation-group yvar_n2")
        if mean < 0.0:
            raise R013GPError("R013 correction observation-group mean_n is out of bounds")
        if not YVAR_MIN_N2 <= yvar <= YVAR_MAX_N2:
            raise R013GPError(
                "R013 correction observation-group Yvar is outside [1e-4,2e-2] N^2"
            )
        object.__setattr__(self, "campaign_fingerprint", fingerprint)
        object.__setattr__(self, "weights", weights)
        object.__setattr__(self, "mean_n", mean)
        object.__setattr__(self, "yvar_n2", yvar)

    @property
    def fingerprint_key(self) -> str:
        return self.campaign_fingerprint.sha256

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "campaign_fingerprint": self.campaign_fingerprint.as_dict(),
            "weights": list(self.weights),
            "n": self.n,
            "mean_n": self.mean_n,
            "yvar_n2": self.yvar_n2,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CorrectionObservationGroupV1":
        required = {
            "schema", "version", "campaign_fingerprint", "weights", "n", "mean_n", "yvar_n2"
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise R013GPError("R013 correction observation-group fields differ")
        if "coordinates" in value:
            raise R013GPError("R013 legacy placeholder correction coordinates are rejected")
        return cls(
            campaign_fingerprint=value["campaign_fingerprint"],
            weights=value["weights"],
            n=value["n"],
            mean_n=value["mean_n"],
            yvar_n2=value["yvar_n2"],
            schema=value["schema"],
            version=value["version"],
        )


def validate_correction_observation_groups(
    groups: Sequence[CorrectionObservationGroupV1 | Mapping[str, Any]],
    *,
    campaign_fingerprint: CampaignFingerprint | Mapping[str, Any] | None = None,
) -> tuple[CorrectionObservationGroupV1, ...]:
    if isinstance(groups, (str, bytes)) or not isinstance(groups, Sequence) or not groups:
        raise R013GPError("R013 correction GP requires typed observation groups")
    parsed = tuple(
        item if isinstance(item, CorrectionObservationGroupV1)
        else CorrectionObservationGroupV1.from_mapping(item)
        for item in groups
    )
    if campaign_fingerprint is None:
        expected = None
    else:
        try:
            expected = validate_campaign_fingerprint(campaign_fingerprint)
        except (TypeError, ValueError) as exc:
            raise R013GPError("R013 correction GP campaign fingerprint is invalid") from exc
    expected_key = None if expected is None else expected.sha256
    if expected_key is not None and any(item.fingerprint_key != expected_key for item in parsed):
        raise R013GPError("R013 correction GP observation groups mix campaign fingerprints")
    fingerprints = {item.fingerprint_key for item in parsed}
    if len(fingerprints) != 1:
        raise R013GPError("R013 correction GP observation groups mix campaign fingerprints")
    by_weights: dict[tuple[float, ...], CorrectionObservationGroupV1] = {}
    for item in parsed:
        if item.weights in by_weights:
            previous = by_weights[item.weights]
            if previous != item:
                raise R013GPError("R013 duplicate incompatible correction observation groups")
            raise R013GPError("R013 duplicate correction observation groups")
        by_weights[item.weights] = item
    return tuple(sorted(parsed, key=lambda item: item.weights))


@dataclass(frozen=True)
class CorrectionGPConfigV1:
    """Versioned configuration for the production six-weight correction GP."""

    weight_bounds: tuple[tuple[float, float], ...] = CORRECTION_WEIGHT_BOUNDS
    initial_lengthscales_unit: tuple[float, ...] = (0.35,) * CORRECTION_MODEL_DIMENSION_COUNT
    lengthscale_bounds_unit: tuple[tuple[float, float], ...] = (
        (0.03, 5.0),
    ) * CORRECTION_MODEL_DIMENSION_COUNT
    improvement_threshold_n: float = CORRECTION_IMPROVEMENT_THRESHOLD_N
    candidate_pool_size: int = CANDIDATE_POOL_SIZE
    q: int = Q
    schema: str = CORRECTION_GP_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != CORRECTION_GP_SCHEMA or self.version != 1:
            raise R013GPError("R013 correction GP config schema/version differs")
        if (
            len(self.weight_bounds) != 6
            or len(self.initial_lengthscales_unit) != 6
            or len(self.lengthscale_bounds_unit) != 6
        ):
            raise R013GPError("R013 correction GP config must be six-dimensional")
        for index, pair in enumerate(self.weight_bounds):
            if len(pair) != 2:
                raise R013GPError(f"R013 correction GP weight bound {index} is invalid")
            low = _finite(pair[0], f"correction GP bound {index} low")
            high = _finite(pair[1], f"correction GP bound {index} high")
            if (low, high) != CORRECTION_WEIGHT_BOUNDS[index]:
                raise R013GPError("R013 correction GP weight bounds differ")
        if any(
            _finite(value, "correction GP lengthscale") <= 0.0
            for value in self.initial_lengthscales_unit
        ):
            raise R013GPError("R013 correction GP lengthscales are invalid")
        parsed_bounds: list[tuple[float, float]] = []
        for index, pair in enumerate(self.lengthscale_bounds_unit):
            if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)) or len(pair) != 2:
                raise R013GPError(f"R013 correction GP lengthscale bound {index} is invalid")
            low = _finite(pair[0], f"correction GP lengthscale bound {index} low")
            high = _finite(pair[1], f"correction GP lengthscale bound {index} high")
            if low <= 0.0 or low >= high:
                raise R013GPError(f"R013 correction GP lengthscale bound {index} is invalid")
            parsed_bounds.append((low, high))
        initial = tuple(_finite(value, "correction GP initial lengthscale") for value in self.initial_lengthscales_unit)
        if any(
            not low <= value <= high
            for value, (low, high) in zip(initial, parsed_bounds, strict=True)
        ):
            raise R013GPError("R013 correction GP initial lengthscales exceed bounds")
        threshold = _finite(self.improvement_threshold_n, "correction GP improvement threshold")
        if not math.isclose(
            threshold, CORRECTION_IMPROVEMENT_THRESHOLD_N, rel_tol=0.0, abs_tol=1e-12
        ):
            raise R013GPError("R013 correction GP improvement threshold must equal 0.01 N")
        if (
            type(self.candidate_pool_size) is not int
            or type(self.q) is not int
            or self.candidate_pool_size != CANDIDATE_POOL_SIZE
            or self.q != Q
        ):
            raise R013GPError("R013 correction GP pool/q contract differs")
        object.__setattr__(self, "initial_lengthscales_unit", initial)
        object.__setattr__(self, "lengthscale_bounds_unit", tuple(parsed_bounds))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "weight_bounds": [list(pair) for pair in self.weight_bounds],
            "model_dimensions": CORRECTION_MODEL_DIMENSION_COUNT,
            "initial_lengthscales_unit": list(self.initial_lengthscales_unit),
            "lengthscale_bounds_unit": [list(pair) for pair in self.lengthscale_bounds_unit],
            "improvement_threshold_n": self.improvement_threshold_n,
            "candidate_pool_size": self.candidate_pool_size,
            "q": self.q,
            "acquisition": "qLogNoisyExpectedImprovement",
        }


@dataclass(frozen=True)
class CorrectionProductionGPFitV1:
    model: Any
    train_X: tuple[tuple[float, ...], ...]
    train_Y: tuple[float, ...]
    train_Yvar: tuple[float, ...]
    groups: tuple[CorrectionObservationGroupV1, ...]
    config: CorrectionGPConfigV1
    campaign_fingerprint: CampaignFingerprint
    robust_incumbent_n: float
    backend: str
    device: str
    fit_completed: bool
    lengthscales_unit_fitted: tuple[float, ...]
    fit_receipt: Mapping[str, Any]


@dataclass(frozen=True)
class CorrectionCandidateProposalV1:
    weights: tuple[float, ...]
    acquisition_value: float
    posterior_probability_of_improvement: float
    improvement_threshold_n: float
    fit_receipt: Mapping[str, Any]
    q: int = Q
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise R013GPError("R013 correction proposal version differs")
        object.__setattr__(self, "weights", _strict_weights(self.weights))
        acquisition = _finite(self.acquisition_value, "correction acquisition value")
        probability = _finite(
            self.posterior_probability_of_improvement,
            "correction posterior improvement probability",
        )
        if not 0.0 <= probability <= 1.0 or type(self.q) is not int or self.q != Q:
            raise R013GPError("R013 correction proposal probability/q is invalid")
        if not math.isclose(
            _finite(self.improvement_threshold_n, "correction proposal threshold"),
            CORRECTION_IMPROVEMENT_THRESHOLD_N,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise R013GPError("R013 correction proposal threshold must equal 0.01 N")
        object.__setattr__(self, "acquisition_value", acquisition)

    @property
    def block_input(self) -> tuple[float, ...]:
        return self.weights

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CORRECTION_PROPOSAL_SCHEMA,
            "version": self.version,
            "weights": list(self.weights),
            "block_input": list(self.weights),
            "model_dimensions": CORRECTION_MODEL_DIMENSION_COUNT,
            "acquisition": "qLogNoisyExpectedImprovement",
            "acquisition_value": self.acquisition_value,
            "posterior_probability_of_improvement": self.posterior_probability_of_improvement,
            "improvement_threshold_n": self.improvement_threshold_n,
            "fit_receipt": dict(self.fit_receipt),
            "q": self.q,
        }
def fit_correction_production_gp(
    groups: Sequence[CorrectionObservationGroupV1 | Mapping[str, Any]],
    *,
    robust_incumbent_n: float,
    campaign_fingerprint: CampaignFingerprint | Mapping[str, Any] | None = None,
    config: CorrectionGPConfigV1 | None = None,
) -> CorrectionProductionGPFitV1:
    parsed_groups = validate_correction_observation_groups(
        groups, campaign_fingerprint=campaign_fingerprint
    )
    selected_config = config or CorrectionGPConfigV1()
    fingerprint = parsed_groups[0].campaign_fingerprint
    incumbent = _finite(robust_incumbent_n, "robust correction incumbent")
    if incumbent < 0.0:
        raise R013GPError("R013 robust correction incumbent is out of bounds")
    train_x = tuple(group.weights for group in parsed_groups)
    train_y = tuple(group.mean_n for group in parsed_groups)
    train_yvar = tuple(group.yvar_n2 for group in parsed_groups)
    try:
        import botorch
        import gpytorch
        import torch
        from botorch.fit import fit_gpytorch_mll
        from botorch.models import SingleTaskGP
        from botorch.models.transforms.input import Normalize
        from botorch.models.transforms.outcome import Standardize
        from gpytorch.mlls import ExactMarginalLogLikelihood
    except (ImportError, ModuleNotFoundError) as exc:
        raise R013GPError("R013 correction production BoTorch runtime is unavailable") from exc
    if not torch.cuda.is_available():
        raise R013GPError("R013 correction production GP requires CUDA")
    torch.manual_seed(6013)
    torch.cuda.manual_seed_all(6013)
    device = torch.device("cuda")
    bounds = torch.tensor(
        [
            [low for low, _high in CORRECTION_WEIGHT_BOUNDS],
            [high for _low, high in CORRECTION_WEIGHT_BOUNDS],
        ],
        dtype=torch.double,
        device=device,
    )
    tx = torch.tensor(train_x, dtype=torch.double, device=device)
    ty = torch.tensor(train_y, dtype=torch.double, device=device).unsqueeze(-1)
    tyvar = torch.tensor(train_yvar, dtype=torch.double, device=device).unsqueeze(-1)
    covar = gpytorch.kernels.ScaleKernel(
        gpytorch.kernels.MaternKernel(
            nu=2.5,
            ard_num_dims=CORRECTION_MODEL_DIMENSION_COUNT,
            lengthscale_prior=gpytorch.priors.GammaPrior(3.0, 6.0),
            lengthscale_constraint=gpytorch.constraints.Interval(
                *selected_config.lengthscale_bounds_unit[0]
            ),
        )
    )
    model = SingleTaskGP(
        tx,
        ty,
        train_Yvar=tyvar,
        covar_module=covar,
        input_transform=Normalize(d=CORRECTION_MODEL_DIMENSION_COUNT, bounds=bounds),
        outcome_transform=Standardize(m=1),
    ).to(device=device, dtype=torch.double)
    model.covar_module.base_kernel.initialize(
        lengthscale=torch.tensor(
            selected_config.initial_lengthscales_unit,
            dtype=torch.double,
            device=device,
        ).reshape(1, CORRECTION_MODEL_DIMENSION_COUNT)
    )
    mll = ExactMarginalLogLikelihood(model.likelihood, model)
    model.train()
    model.likelihood.train()
    try:
        fit_gpytorch_mll(mll, optimizer_kwargs={"options": {"maxiter": 200}})
    except Exception as exc:
        raise R013GPError(
            f"R013 correction GP fit failed: {type(exc).__name__}: {exc}"
        ) from exc
    model.eval()
    model.likelihood.eval()
    lengthscales = tuple(
        float(value)
        for value in model.covar_module.base_kernel.lengthscale.detach().cpu().reshape(-1)
    )
    receipt = {
        "schema": CORRECTION_GP_SCHEMA,
        "version": 1,
        "backend": "botorch.SingleTaskGP",
        "botorch_version": botorch.__version__,
        "torch_version": torch.__version__,
        "device": str(device),
        "cuda_device": torch.cuda.get_device_name(device),
        "model_dimensions": CORRECTION_MODEL_DIMENSION_COUNT,
        "ard_num_dims": CORRECTION_MODEL_DIMENSION_COUNT,
        "q": Q,
        "acquisition": "qLogNoisyExpectedImprovement",
        "training_rows": len(train_x),
        "fit_completed": True,
        "lengthscales_unit_fitted": list(lengthscales),
        "robust_incumbent_n": incumbent,
        "improvement_threshold_n": selected_config.improvement_threshold_n,
        "campaign_fingerprint": fingerprint.as_dict(),
        "group_receipts": [group.as_dict() for group in parsed_groups],
    }
    return CorrectionProductionGPFitV1(
        model=model,
        train_X=train_x,
        train_Y=train_y,
        train_Yvar=train_yvar,
        groups=parsed_groups,
        config=selected_config,
        campaign_fingerprint=fingerprint,
        robust_incumbent_n=incumbent,
        backend="botorch.SingleTaskGP",
        device=str(device),
        fit_completed=True,
        lengthscales_unit_fitted=lengthscales,
        fit_receipt=receipt,
    )


def build_correction_qlognei_acquisition(fit: CorrectionProductionGPFitV1) -> Any:
    if (
        not isinstance(fit, CorrectionProductionGPFitV1)
        or not fit.fit_completed
        or fit.backend != "botorch.SingleTaskGP"
    ):
        raise R013GPError("R013 correction qLogNEI requires a completed production fit")
    try:
        import torch
        from botorch.acquisition.logei import qLogNoisyExpectedImprovement
        from botorch.acquisition.objective import GenericMCObjective
    except (ImportError, ModuleNotFoundError) as exc:
        raise R013GPError("R013 correction qLogNEI runtime is unavailable") from exc
    if not torch.cuda.is_available():
        raise R013GPError("R013 correction qLogNEI requires CUDA")
    device = next(fit.model.parameters()).device
    baseline = torch.tensor(fit.train_X, dtype=torch.double, device=device)
    objective = GenericMCObjective(lambda samples, X=None: -samples.squeeze(-1))
    return qLogNoisyExpectedImprovement(
        model=fit.model,
        X_baseline=baseline,
        objective=objective,
        prune_baseline=False,
    )


def _correction_pool_weights(value: Any) -> tuple[float, ...]:
    return _strict_weights(value, "correction candidate")


def ask_correction_qlognei(
    fit: CorrectionProductionGPFitV1,
    candidates: Sequence[Any],
    *,
    evaluated_weights: Iterable[Sequence[float]] = (),
    pending_weights: Iterable[Sequence[float]] = (),
    improvement_threshold_n: float | None = None,
) -> CorrectionCandidateProposalV1:
    if (
        not isinstance(fit, CorrectionProductionGPFitV1)
        or not fit.fit_completed
        or fit.backend != "botorch.SingleTaskGP"
    ):
        raise R013GPError("R013 correction GP fit is not complete")
    if len(candidates) != CANDIDATE_POOL_SIZE:
        raise R013GPError("R013 correction qLogNEI requires exactly 128 candidates")
    threshold = fit.config.improvement_threshold_n if improvement_threshold_n is None else _finite(
        improvement_threshold_n, "correction improvement threshold"
    )
    if not math.isclose(
        threshold, CORRECTION_IMPROVEMENT_THRESHOLD_N, rel_tol=0.0, abs_tol=1e-12
    ):
        raise R013GPError("R013 correction improvement threshold must equal 0.01 N")
    evaluated = {tuple(_correction_pool_weights(item)) for item in evaluated_weights}
    pending = {tuple(_correction_pool_weights(item)) for item in pending_weights}
    admissible = tuple(_correction_pool_weights(item) for item in candidates)
    if len(set(admissible)) != CANDIDATE_POOL_SIZE:
        raise R013GPError("R013 correction qLogNEI pool contains duplicate weights")
    if any(item in evaluated or item in pending for item in admissible):
        raise R013GPError("R013 correction qLogNEI pool is not exactly 128 fresh weights")
    acquisition = build_correction_qlognei_acquisition(fit)
    try:
        import torch
    except (ImportError, ModuleNotFoundError) as exc:
        raise R013GPError("R013 correction qLogNEI runtime is unavailable") from exc
    device = next(fit.model.parameters()).device
    x = torch.tensor(admissible, dtype=torch.double, device=device).unsqueeze(1)
    with torch.no_grad():
        scores = acquisition(x).reshape(-1)
        posterior = fit.model.posterior(x[:, 0, :])
        mean = posterior.mean.reshape(-1)
        variance = posterior.variance.reshape(-1).clamp_min(1e-12)
        probability = torch.distributions.Normal(mean, variance.sqrt()).cdf(
            torch.tensor(
                fit.robust_incumbent_n - threshold,
                dtype=torch.double,
                device=device,
            )
        )
    scored = tuple(
        (float(score), weights, float(probability_value))
        for score, weights, probability_value in zip(
            scores, admissible, probability, strict=True
        )
    )
    score, weights, probability_value = max(
        scored,
        key=lambda item: (item[0], tuple(-value for value in item[1])),
    )
    receipt = {
        **dict(fit.fit_receipt),
        "schema": CORRECTION_PROPOSAL_SCHEMA,
        "pool_size": CANDIDATE_POOL_SIZE,
        "fresh_candidate_count": CANDIDATE_POOL_SIZE,
        "selected_acquisition_value": score,
        "selected_posterior_probability_of_improvement": probability_value,
        "improvement_threshold_n": threshold,
    }
    return CorrectionCandidateProposalV1(
        weights=weights,
        acquisition_value=score,
        posterior_probability_of_improvement=probability_value,
        improvement_threshold_n=threshold,
        fit_receipt=receipt,
    )


@dataclass(frozen=True)
class CorrectionQLogNEIProposalProviderV1:
    """Small typed adapter suitable for the offline coordinator seam."""

    fit: CorrectionProductionGPFitV1
    evaluated_weights: tuple[tuple[float, ...], ...] = ()
    pending_weights: tuple[tuple[float, ...], ...] = ()

    def __call__(self, pool: Sequence[Any]) -> CorrectionCandidateProposalV1:
        return ask_correction_qlognei(
            self.fit,
            pool,
            evaluated_weights=self.evaluated_weights,
            pending_weights=self.pending_weights,
        )


def snapshot_sha256(snapshot: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(snapshot), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProductionGPConfig:
    """No noise default: construction must consume a sealed snapshot."""

    noise_floor_n2: float
    noise_snapshot_sha256: str
    domain_schema: str = DOMAIN_SCHEMA
    initial_lengthscales_unit: tuple[float, ...] = (0.35,) * MODEL_DIMENSION_COUNT
    lengthscale_bounds_unit: tuple[tuple[float, float], ...] = ((0.03, 5.0),) * MODEL_DIMENSION_COUNT
    kernel: str = "Matern52_ARD_normalized_unit_cube"
    version: str = "r013-production-gp-v1"

    def __post_init__(self) -> None:
        if self.domain_schema != DOMAIN_SCHEMA:
            raise R013GPError("R013 GP domain schema differs")
        if len(self.initial_lengthscales_unit) != MODEL_DIMENSION_COUNT:
            raise R013GPError("R013 GP must have six initial lengthscales")
        if len(self.lengthscale_bounds_unit) != MODEL_DIMENSION_COUNT:
            raise R013GPError("R013 GP must have six lengthscale bounds")
        if _finite(self.noise_floor_n2, "noise_floor_n2") <= 0.0:
            raise R013GPError("R013 GP noise floor must be positive")
        if len(self.noise_snapshot_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.noise_snapshot_sha256
        ):
            raise R013GPError("R013 GP requires a snapshot SHA-256")
        if any(_finite(value, "lengthscale") <= 0.0 for value in self.initial_lengthscales_unit):
            raise R013GPError("R013 GP lengthscales must be positive")

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "ProductionGPConfig":
        if snapshot.get("schema") != "step5d.autotune-v4/r013-optimizer-snapshot-v1":
            raise R013GPError("R013 optimizer snapshot schema differs")
        if "noise_floor_n2" not in snapshot:
            raise R013GPError("R013 optimizer snapshot lacks noise_floor_n2")
        return cls(
            noise_floor_n2=_finite(snapshot["noise_floor_n2"], "snapshot noise_floor_n2"),
            noise_snapshot_sha256=snapshot_sha256(snapshot),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": GP_SCHEMA,
            "version": self.version,
            "domain_schema": self.domain_schema,
            "model_dimensions": list(MODEL_DIMENSIONS),
            "noise_floor_n2": self.noise_floor_n2,
            "noise_snapshot_sha256": self.noise_snapshot_sha256,
            "kernel": self.kernel,
            "initial_lengthscales_unit": list(self.initial_lengthscales_unit),
            "lengthscale_bounds_unit": [list(value) for value in self.lengthscale_bounds_unit],
            "sealed_mae_transform": "identity",
        }


@dataclass(frozen=True)
class ProductionGPFit:
    model: Any
    train_X: tuple[tuple[float, ...], ...]
    train_Y: tuple[float, ...]
    train_Yvar: tuple[float, ...]
    config: ProductionGPConfig
    backend: str
    fit_completed: bool
    device: str
    fit_receipt: Mapping[str, Any]


@dataclass(frozen=True)
class CoreProductionGPFit:
    """Four-dimensional controller-core fit contract for floor discovery."""

    model: Any
    train_X: tuple[tuple[float, ...], ...]
    train_Y: tuple[float, ...]
    train_Yvar: tuple[float, ...]
    config: ProductionGPConfig
    backend: str
    fit_completed: bool
    device: str
    fit_receipt: Mapping[str, Any]
    robust_incumbent_n: float
    lengthscales_unit_fitted: tuple[float, ...]


@dataclass(frozen=True)
class CoreCandidateProposal:
    """Typed result of the real 4-D serial qLogNEI acquisition."""

    candidate: Mapping[str, Any]
    normalized_X: tuple[float, ...]
    acquisition_value: float
    posterior_beating_probability: float
    incumbent_threshold_n: float
    fit_receipt: Mapping[str, Any]
    q: int = Q

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r013-core-candidate-proposal-v1",
            "candidate": dict(self.candidate),
            "normalized_X": list(self.normalized_X),
            "acquisition_value": self.acquisition_value,
            "posterior_beating_probability": self.posterior_beating_probability,
            "incumbent_threshold_n": self.incumbent_threshold_n,
            "fit_receipt": dict(self.fit_receipt),
            "model_dimensions": 4,
            "acquisition": "qLogNoisyExpectedImprovement",
            "q": self.q,
        }


CORE_FEATURE_INDICES = (0, 1, 2, 5)
CORE_LOG_BOUNDS = tuple(DOMAIN_LOG_BOUNDS[index] for index in CORE_FEATURE_INDICES)


def fit_core_production_gp(
    observations: Sequence[Mapping[str, Any]],
    *,
    config: ProductionGPConfig,
    robust_incumbent_n: float | None = None,
) -> CoreProductionGPFit:
    """Fit a CUDA BoTorch SingleTaskGP on exactly the four core axes."""

    groups = grouped_observation_noise(observations, config=config)
    train_x = tuple(
        tuple(candidate_to_log_features(group["candidate"])[index] for index in CORE_FEATURE_INDICES)
        for group in groups
    )
    train_y = tuple(float(group["mean_n"]) for group in groups)
    train_yvar = tuple(float(group["yvar_n2"]) for group in groups)
    incumbent = min(train_y) if robust_incumbent_n is None else _finite(
        robust_incumbent_n, "robust incumbent"
    )
    try:
        import botorch
        import gpytorch
        import torch
        from botorch.fit import fit_gpytorch_mll
        from botorch.models import SingleTaskGP
        from botorch.models.transforms.input import Normalize
        from botorch.models.transforms.outcome import Standardize
        from gpytorch.mlls import ExactMarginalLogLikelihood
    except (ImportError, ModuleNotFoundError) as exc:
        raise R013GPError("R013 core production BoTorch runtime is unavailable") from exc
    if not torch.cuda.is_available():
        raise R013GPError("R013 core production GP requires CUDA")
    torch.manual_seed(6013)
    torch.cuda.manual_seed_all(6013)
    device = torch.device("cuda")
    bounds = torch.tensor(
        [[low for low, _high in CORE_LOG_BOUNDS], [high for _low, high in CORE_LOG_BOUNDS]],
        dtype=torch.double,
        device=device,
    )
    tx = torch.tensor(train_x, dtype=torch.double, device=device)
    ty = torch.tensor(train_y, dtype=torch.double, device=device).unsqueeze(-1)
    tyvar = torch.tensor(train_yvar, dtype=torch.double, device=device).unsqueeze(-1)
    core_initial_lengthscales = tuple(config.initial_lengthscales_unit[index] for index in CORE_FEATURE_INDICES)
    core_lengthscale_bounds = config.lengthscale_bounds_unit[CORE_FEATURE_INDICES[0]]
    covar = gpytorch.kernels.ScaleKernel(
        gpytorch.kernels.MaternKernel(
            nu=2.5,
            ard_num_dims=4,
            lengthscale_prior=gpytorch.priors.GammaPrior(3.0, 6.0),
            lengthscale_constraint=gpytorch.constraints.Interval(*core_lengthscale_bounds),
        )
    )
    model = SingleTaskGP(
        tx,
        ty,
        train_Yvar=tyvar,
        covar_module=covar,
        input_transform=Normalize(d=4, bounds=bounds),
        outcome_transform=Standardize(m=1),
    ).to(device=device, dtype=torch.double)
    model.covar_module.base_kernel.initialize(
        lengthscale=torch.tensor(
            core_initial_lengthscales, dtype=torch.double, device=device,
        ).reshape(1, 4)
    )
    mll = ExactMarginalLogLikelihood(model.likelihood, model)
    model.train()
    model.likelihood.train()
    try:
        fit_gpytorch_mll(mll, optimizer_kwargs={"options": {"maxiter": 200}})
    except Exception as exc:
        raise R013GPError(f"R013 core GP fit failed: {type(exc).__name__}: {exc}") from exc
    model.eval()
    model.likelihood.eval()
    lengthscales = tuple(
        float(value)
        for value in model.covar_module.base_kernel.lengthscale.detach().cpu().reshape(-1)
    )
    receipt = {
        "schema": "step5d.autotune-v4/r013-core-gp-fit-receipt-v1",
        "version": 1,
        "backend": "botorch.SingleTaskGP",
        "botorch_version": botorch.__version__,
        "torch_version": torch.__version__,
        "device": str(device),
        "cuda_device": torch.cuda.get_device_name(device),
        "model_dimensions": 4,
        "core_feature_indices": list(CORE_FEATURE_INDICES),
        "q": Q,
        "acquisition": "qLogNoisyExpectedImprovement",
        "train_rows": len(train_x),
        "train_Yvar_rows": len(train_yvar),
        "fit_completed": True,
        "lengthscales_unit_fitted": list(lengthscales),
        "robust_incumbent_n": incumbent,
        "incumbent_threshold_source": (
            "explicit_input" if robust_incumbent_n is not None
            else "minimum_observed_group_mean"
        ),
        "observation_groups": [dict(group) for group in groups],
    }
    return CoreProductionGPFit(
        model=model, train_X=train_x, train_Y=train_y, train_Yvar=train_yvar,
        config=config, backend="botorch.SingleTaskGP", fit_completed=True,
        device=str(device), fit_receipt=receipt, robust_incumbent_n=incumbent,
        lengthscales_unit_fitted=lengthscales,
    )


def build_core_qlognei_acquisition(fit: CoreProductionGPFit) -> Any:
    if not isinstance(fit, CoreProductionGPFit) or not fit.fit_completed or fit.backend != "botorch.SingleTaskGP":
        raise R013GPError("R013 core qLogNEI requires a completed production fit")
    try:
        import torch
        from botorch.acquisition.logei import qLogNoisyExpectedImprovement
        from botorch.acquisition.objective import GenericMCObjective
    except (ImportError, ModuleNotFoundError) as exc:
        raise R013GPError("R013 core qLogNEI runtime is unavailable") from exc
    if not torch.cuda.is_available():
        raise R013GPError("R013 core qLogNEI requires CUDA")
    device = next(fit.model.parameters()).device
    objective = GenericMCObjective(lambda samples, X=None: -samples.squeeze(-1))
    baseline = torch.tensor(fit.train_X, dtype=torch.double, device=device)
    return qLogNoisyExpectedImprovement(
        model=fit.model,
        X_baseline=baseline,
        objective=objective,
        prune_baseline=False,
    )


def ask_core_qlognei(
    fit: CoreProductionGPFit,
    candidates: Sequence[Mapping[str, Any]],
    *,
    evaluated_keys: Iterable[tuple[Any, ...]] = (),
    pending_keys: Iterable[tuple[Any, ...]] = (),
) -> CoreCandidateProposal:
    """Score exactly 128 fresh core candidates with real qLogNEI."""

    if (
        not isinstance(fit, CoreProductionGPFit)
        or not fit.fit_completed
        or fit.backend != "botorch.SingleTaskGP"
    ):
        raise R013GPError("R013 core GP fit is not complete")
    if len(fit.train_X) == 0 or any(len(row) != 4 for row in fit.train_X):
        raise R013GPError("R013 core GP input is not four-dimensional")
    if len(candidates) != CANDIDATE_POOL_SIZE:
        raise R013GPError("R013 core qLogNEI requires exactly 128 candidates")
    excluded = set(evaluated_keys) | set(pending_keys)
    admissible = tuple(
        (
            dict(candidate),
            physical_candidate_key(candidate),
            tuple(candidate_to_log_features(candidate)[index] for index in CORE_FEATURE_INDICES),
        )
        for candidate in candidates
        if physical_candidate_key(candidate) not in excluded
    )
    if len(admissible) != CANDIDATE_POOL_SIZE:
        raise R013GPError("R013 core qLogNEI pool is not exactly 128 fresh candidates")
    if len({key for _candidate, key, _features in admissible}) != CANDIDATE_POOL_SIZE:
        raise R013GPError("R013 core qLogNEI pool contains duplicate candidates")
    import torch

    acquisition = build_core_qlognei_acquisition(fit)
    device = next(fit.model.parameters()).device
    x = torch.tensor(
        [features for _candidate, _key, features in admissible],
        dtype=torch.double,
        device=device,
    ).unsqueeze(1)
    with torch.no_grad():
        scores = acquisition(x).reshape(-1)
        posterior = fit.model.posterior(x[:, 0, :])
        mean = posterior.mean.reshape(-1)
        variance = posterior.variance.reshape(-1).clamp_min(1e-12)
        probability = torch.distributions.Normal(mean, variance.sqrt()).cdf(
            torch.tensor(fit.robust_incumbent_n, dtype=torch.double, device=device)
        )
    scored = tuple(
        (float(score), candidate, features, float(prob))
        for score, (candidate, _key, features), prob in zip(scores, admissible, probability)
    )
    score, candidate, features, beat_probability = max(
        scored,
        key=lambda item: (item[0], tuple(-value for value in item[2])),
    )
    bounds = CORE_LOG_BOUNDS
    normalized = tuple(
        (value - low) / (high - low)
        for value, (low, high) in zip(features, bounds, strict=True)
    )
    receipt = {
        **dict(fit.fit_receipt),
        "schema": "step5d.autotune-v4/r013-core-qlognei-proposal-receipt-v1",
        "acquisition": "qLogNoisyExpectedImprovement",
        "pool_size": CANDIDATE_POOL_SIZE,
        "q": Q,
        "selected_acquisition_value": score,
        "selected_posterior_beating_probability": beat_probability,
    }
    return CoreCandidateProposal(
        candidate=candidate,
        normalized_X=normalized,
        acquisition_value=score,
        posterior_beating_probability=beat_probability,
        incumbent_threshold_n=fit.robust_incumbent_n,
        fit_receipt=receipt,
    )


@dataclass(frozen=True)
class CandidateProposal:
    candidate: Mapping[str, Any]
    normalized_X: tuple[float, ...]
    acquisition_value: float
    excluded_keys: tuple[tuple[Any, ...], ...]
    q: int = Q

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r013-candidate-proposal-v1",
            "candidate": dict(self.candidate),
            "normalized_X": list(self.normalized_X),
            "acquisition_value": self.acquisition_value,
            "excluded_key_count": len(self.excluded_keys),
            "q": self.q,
        }


def _exact_row(row: Mapping[str, Any]) -> tuple[Mapping[str, Any], float, float]:
    required_true = ("completed", "sealed", "full_observation", "eligible")
    if row.get("schema") != "step5d.autotune-v4/r013-exact-observation-v1":
        raise R013GPError("R013 GP accepts only R013 exact rows")
    if any(row.get(name) is not True for name in required_true) or row.get("censored") is True:
        raise R013GPError("R013 GP row is not exact, sealed, full, and eligible")
    candidate = row.get("candidate")
    if not isinstance(candidate, Mapping):
        raise R013GPError("R013 exact row candidate is invalid")
    physical_candidate_key(candidate)
    anti_windup = row.get("anti_windup")
    if not isinstance(anti_windup, Mapping):
        raise R013GPError("R013 exact row lacks anti-windup evidence")
    if (
        anti_windup.get("schema") != TRIAL_ANTI_WINDUP_SCHEMA
        or anti_windup.get("policy") != ANTI_WINDUP_POLICY
        or anti_windup.get("invariant_violation_count") != 0
        or anti_windup.get("path_gain_hot_switch") is not False
    ):
        raise R013GPError("R013 exact row anti-windup evidence differs")
    try:
        max_abs_integral = _finite(
            anti_windup.get("max_abs_integral_n_s"),
            "anti_windup max_abs_integral_n_s",
        )
        max_abs_i_term = _finite(
            anti_windup.get("max_abs_i_term"),
            "anti_windup max_abs_i_term",
        )
    except R013GPError:
        raise
    if max_abs_integral < 0.0 or max_abs_integral > 1.0 + 1e-12:
        raise R013GPError("R013 exact row exceeded integral state limit")
    if (
        max_abs_i_term < 0.0
        or max_abs_i_term > 0.5 * float(candidate["force_p_gain"]) + 1e-12
    ):
        raise R013GPError("R013 exact row exceeded I-term authority")
    objective = _finite(row.get("objective_n"), "objective_n")
    variance = _finite(row.get("observation_variance_n2"), "observation_variance_n2")
    if objective < 0.0 or variance <= 0.0:
        raise R013GPError("R013 objective/variance must be positive")
    return candidate, objective, variance


def _fingerprint_key(row: Mapping[str, Any]) -> str:
    value = row.get("campaign_fingerprint")
    if value is None:
        return "__legacy_r013_fingerprint__"
    if not isinstance(value, Mapping):
        raise R013GPError("R013 exact row campaign fingerprint is invalid")
    try:
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise R013GPError("R013 exact row campaign fingerprint is invalid") from exc


def _clip_yvar(value: float) -> float:
    if not math.isfinite(value) or value <= 0.0:
        raise R013GPError("R013 grouped observation variance is invalid")
    return min(YVAR_MAX_N2, max(YVAR_MIN_N2, float(value)))


def grouped_observation_noise(
    observations: Sequence[Mapping[str, Any]],
    *,
    config: ProductionGPConfig,
) -> tuple[dict[str, Any], ...]:
    """Reduce exact rows to candidate/fingerprint means and repeat noise."""

    groups: dict[tuple[str, tuple[Any, ...]], dict[str, Any]] = {}
    for row in observations:
        candidate, objective, _row_variance = _exact_row(row)
        key = ( _fingerprint_key(row), physical_candidate_key(candidate) )
        group = groups.setdefault(
            key,
            {"candidate": dict(candidate), "campaign_fingerprint": row.get("campaign_fingerprint"), "values": []},
        )
        group["values"].append(objective)
    if not groups:
        raise R013GPError("R013 GP requires at least one exact row")
    by_fingerprint: dict[str, list[dict[str, Any]]] = {}
    for (fingerprint, _candidate_key), group in groups.items():
        values = tuple(float(value) for value in group["values"])
        group["fingerprint_key"] = fingerprint
        group["n"] = len(values)
        group["mean_n"] = math.fsum(values) / len(values)
        group["sample_variance_n2"] = statistics.variance(values) if len(values) >= 2 else None
        by_fingerprint.setdefault(fingerprint, []).append(group)
    pooled_by_fingerprint: dict[str, float] = {}
    for fingerprint, fingerprint_groups in by_fingerprint.items():
        numerator = math.fsum(
            (int(group["n"]) - 1) * float(group["sample_variance_n2"])
            for group in fingerprint_groups
            if int(group["n"]) >= 2
        )
        denominator = sum(int(group["n"]) - 1 for group in fingerprint_groups if int(group["n"]) >= 2)
        pooled_by_fingerprint[fingerprint] = _clip_yvar(
            numerator / denominator if denominator else float(config.noise_floor_n2)
        )
    receipts: list[dict[str, Any]] = []
    for (_fingerprint, candidate_key), group in sorted(groups.items(), key=lambda item: repr(item[0])):
        n = int(group["n"])
        pooled = pooled_by_fingerprint[group["fingerprint_key"]]
        sample = pooled if n < 2 else float(group["sample_variance_n2"])
        if n < 2:
            shrunk = pooled
        else:
            shrunk = (
                (n - 1) * sample + REPEAT_PRIOR_DEGREES_OF_FREEDOM * pooled
            ) / (n - 1 + REPEAT_PRIOR_DEGREES_OF_FREEDOM)
        yvar = _clip_yvar(shrunk / n)
        receipts.append(
            {
                "schema": "step5d.autotune-v4/r013-observation-noise-group-v1",
                "campaign_fingerprint": group["campaign_fingerprint"],
                "campaign_fingerprint_key": group["fingerprint_key"],
                "candidate": dict(group["candidate"]),
                "candidate_key": list(candidate_key),
                "n": n,
                "mean_n": group["mean_n"],
                "sample_variance_n2": (
                    None if n < 2 else float(group["sample_variance_n2"])
                ),
                "pooled_within_fingerprint_variance_n2": pooled,
                "shrinkage_nu0": REPEAT_PRIOR_DEGREES_OF_FREEDOM,
                "yvar_n2": yvar,
            }
        )
    return tuple(receipts)


def _training_arrays(
    observations: Sequence[Mapping[str, Any]],
    config: ProductionGPConfig,
) -> tuple[tuple[tuple[float, ...], ...], tuple[float, ...], tuple[float, ...]]:
    groups = grouped_observation_noise(observations, config=config)
    return (
        tuple(candidate_to_log_features(group["candidate"]) for group in groups),
        tuple(float(group["mean_n"]) for group in groups),
        tuple(float(group["yvar_n2"]) for group in groups),
    )


def fit_production_gp(
    observations: Sequence[Mapping[str, Any]],
    *,
    config: ProductionGPConfig,
) -> ProductionGPFit:
    train_x, train_y, train_yvar = _training_arrays(observations, config)
    noise_groups = grouped_observation_noise(observations, config=config)
    try:
        import botorch
        import gpytorch
        import torch
        from botorch.fit import fit_gpytorch_mll
        from botorch.models import SingleTaskGP
        from botorch.models.transforms.input import Normalize
        from botorch.models.transforms.outcome import Standardize
        from gpytorch.mlls import ExactMarginalLogLikelihood
    except (ImportError, ModuleNotFoundError) as exc:
        raise R013GPError("R013 production BoTorch runtime is unavailable") from exc
    if not torch.cuda.is_available():
        raise R013GPError("R013 production GP requires CUDA")
    torch.manual_seed(6013)
    torch.cuda.manual_seed_all(6013)
    device = torch.device("cuda")
    bounds = torch.tensor(
        [[low for low, _high in DOMAIN_LOG_BOUNDS], [high for _low, high in DOMAIN_LOG_BOUNDS]],
        dtype=torch.double,
        device=device,
    )
    tx = torch.tensor(train_x, dtype=torch.double, device=device)
    ty = torch.tensor(train_y, dtype=torch.double, device=device).unsqueeze(-1)
    tyvar = torch.tensor(train_yvar, dtype=torch.double, device=device).unsqueeze(-1)
    covar = gpytorch.kernels.ScaleKernel(
        gpytorch.kernels.MaternKernel(
            nu=2.5,
            ard_num_dims=MODEL_DIMENSION_COUNT,
            lengthscale_prior=gpytorch.priors.GammaPrior(3.0, 6.0),
            lengthscale_constraint=gpytorch.constraints.Interval(*config.lengthscale_bounds_unit[0]),
        )
    )
    model = SingleTaskGP(
        tx,
        ty,
        train_Yvar=tyvar,
        covar_module=covar,
        input_transform=Normalize(d=MODEL_DIMENSION_COUNT, bounds=bounds),
        outcome_transform=Standardize(m=1),
    ).to(device=device, dtype=torch.double)
    model.covar_module.base_kernel.initialize(
        lengthscale=torch.tensor(
            config.initial_lengthscales_unit,
            dtype=torch.double,
            device=device,
        ).reshape(1, MODEL_DIMENSION_COUNT)
    )
    mll = ExactMarginalLogLikelihood(model.likelihood, model)
    model.train()
    model.likelihood.train()
    try:
        fit_gpytorch_mll(mll, optimizer_kwargs={"options": {"maxiter": 200}})
    except Exception as exc:
        raise R013GPError(f"R013 GP fit failed: {type(exc).__name__}: {exc}") from exc
    model.eval()
    model.likelihood.eval()
    lengthscales = tuple(
        float(value)
        for value in model.covar_module.base_kernel.lengthscale.detach().cpu().reshape(-1)
    )
    receipt = {
        "schema": FIT_RECEIPT_SCHEMA,
        "backend": "botorch.SingleTaskGP",
        "botorch_version": botorch.__version__,
        "torch_version": torch.__version__,
        "device": str(device),
        "cuda_device": torch.cuda.get_device_name(device),
        "model_dimensions": list(MODEL_DIMENSIONS),
        "row_count": len(train_x),
        "raw_observation_count": len(observations),
        "observation_group_count": len(noise_groups),
        "fit_completed": True,
        "q": Q,
        "candidate_pool_size": CANDIDATE_POOL_SIZE,
        "configured_noise_floor_n2": config.noise_floor_n2,
        "noise_snapshot_sha256": config.noise_snapshot_sha256,
        "noise_semantics": "uncertainty_only; sealed objective_n is unchanged",
        "observation_groups": [dict(group) for group in noise_groups],
        "lengthscales_unit_fitted": list(lengthscales),
        "train_yvar_geometric_mean_n2": math.exp(
            statistics.fmean(math.log(value) for value in train_yvar)
        ),
        "production_proposal_allowed": True,
    }
    return ProductionGPFit(
        model, train_x, train_y, train_yvar, config,
        "botorch.SingleTaskGP", True, str(device), receipt,
    )


def build_qlognei_acquisition(fit: ProductionGPFit) -> Any:
    if fit.backend != "botorch.SingleTaskGP" or not fit.fit_completed:
        raise R013GPError("R013 qLogNEI requires a completed production fit")
    import torch
    from botorch.acquisition.logei import qLogNoisyExpectedImprovement
    from botorch.acquisition.objective import GenericMCObjective

    device = next(fit.model.parameters()).device
    baseline = torch.tensor(fit.train_X, dtype=torch.double, device=device)
    objective = GenericMCObjective(lambda samples, X=None: -samples.squeeze(-1))
    return qLogNoisyExpectedImprovement(
        model=fit.model,
        X_baseline=baseline,
        objective=objective,
        prune_baseline=False,
    )


def ask_qlognei(
    fit: ProductionGPFit,
    candidates: Sequence[Mapping[str, Any]],
    *,
    evaluated_keys: Iterable[tuple[Any, ...]] = (),
    pending_keys: Iterable[tuple[Any, ...]] = (),
) -> CandidateProposal:
    if len(candidates) != CANDIDATE_POOL_SIZE:
        raise R013GPError("R013 ask requires exactly 128 valid lattice candidates")
    excluded = set(evaluated_keys) | set(pending_keys)
    admissible = tuple(
        (
            candidate,
            physical_candidate_key(candidate),
            candidate_to_log_features(candidate),
            candidate_to_normalized(candidate),
        )
        for candidate in candidates
        if physical_candidate_key(candidate) not in excluded
    )
    if len(admissible) != CANDIDATE_POOL_SIZE:
        raise R013GPError(
            "R013 ask must score exactly 128 unevaluated valid lattice candidates"
        )
    acquisition = build_qlognei_acquisition(fit)
    import torch

    device = next(fit.model.parameters()).device
    with torch.no_grad():
        scores = acquisition(
            torch.tensor(
                [features for _candidate, _key, features, _unit in admissible],
                dtype=torch.double,
                device=device,
            ).unsqueeze(1)
        ).reshape(-1)
    scored = tuple(
        (float(score), candidate, unit)
        for score, (candidate, _key, _features, unit) in zip(scores, admissible)
    )
    score, candidate, unit = max(
        scored,
        key=lambda item: (item[0], tuple(-value for value in item[2])),
    )
    return CandidateProposal(
        dict(candidate), unit, score, tuple(sorted(excluded, key=repr)), Q,
    )


__all__ = [
    "CANDIDATE_POOL_SIZE", "CandidateProposal", "CoreCandidateProposal", "CORE_FEATURE_INDICES",
    "CORE_LOG_BOUNDS", "FIT_RECEIPT_SCHEMA", "GP_SCHEMA", "CoreProductionGPFit",
    "ProductionGPConfig", "ProductionGPFit", "Q", "QLOGNEI_SCHEMA", "R013GPError",
    "ask_core_qlognei", "ask_qlognei", "build_core_qlognei_acquisition", "build_qlognei_acquisition",
    "fit_core_production_gp",
    "fit_production_gp", "grouped_observation_noise",
    "snapshot_sha256", "YVAR_MIN_N2", "YVAR_MAX_N2", "REPEAT_PRIOR_DEGREES_OF_FREEDOM",
    "CORRECTION_GP_SCHEMA", "CORRECTION_OBSERVATION_GROUP_SCHEMA",
    "CORRECTION_PROPOSAL_SCHEMA", "CORRECTION_MODEL_DIMENSION_COUNT",
    "CORRECTION_WEIGHT_BOUNDS", "CORRECTION_IMPROVEMENT_THRESHOLD_N",
    "CorrectionObservationGroupV1", "CorrectionGPConfigV1",
    "CorrectionProductionGPFitV1", "CorrectionCandidateProposalV1",
    "CorrectionQLogNEIProposalProviderV1",
    "validate_correction_observation_groups", "fit_correction_production_gp",
    "build_correction_qlognei_acquisition", "ask_correction_qlognei",
]
