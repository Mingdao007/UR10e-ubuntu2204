"""R012 production five-dimensional qLogNEI binding (tau-active).

The BoTorch path is intentionally imported lazily because this repository's
offline contract tests also run on a CPU-only Python installation.  When
BoTorch is present, ``fit_production_gp`` constructs exactly the requested
``SingleTaskGP(train_X, train_Y, train_Yvar=...)`` with fixed-domain
``Normalize(d=5)`` and a Matérn-5/2 ARD covariance.  The deterministic fallback
is only a read-only test oracle; it is never described as production GP evidence.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .censor import CensoredObservation, ExactObservation, Observation, validate_observation
from .common import R012ValueError, finite, json_tree


QLOGNEI_SCHEMA = "step5d.autotune-v4/r012-production-qlognei-v1"
GP_SCHEMA = "step5d.autotune-v4/r012-production-single-task-gp-v1"
DOMAIN_SCHEMA = "r012-fixed-five-dimensional-domain-v1"
MODEL_DIMENSIONS = (
    "log2_force_p_over_d",
    "log2_force_damping",
    "log2_normal_filter_tau_s",
    "log2_orientation_ko",
    "log2_motion_kp",
)
FORCE_I_GAIN = 0.0
I_OFF = True
W9C_REFERENCE_TAU_S = 0.05202781128136904
P_OVER_D_BOUNDS = (1.25e-5, 4.0e-4)  # cliff cap 1.5e-4 removed 2026-08-10; restore pre-cliff upper
DAMPING_BOUNDS = (7.0, 224.0)
TAU_BOUNDS = (0.04375, 0.0735784)
KO_BOUNDS = (0.05, 0.8)
KP_BOUNDS = (1.5, 6.0)
FORCED_REFERENCE_CANDIDATE = {
    "force_p_gain": 0.019027313840405524,
    "force_damping": 133.19119688030474,
    "normal_filter_tau_s": W9C_REFERENCE_TAU_S,
    "orientation_ko": 0.04204482076268573,
    "motion_kp": 4.242640687119286,
}
DOMAIN_RAW_BOUNDS = {
    "p_over_d": P_OVER_D_BOUNDS,
    "force_damping": DAMPING_BOUNDS,
    "normal_filter_tau_s": TAU_BOUNDS,
    "orientation_ko": KO_BOUNDS,
    "motion_kp": KP_BOUNDS,
}
DOMAIN_LOG_BOUNDS = tuple((math.log2(lo), math.log2(hi)) for lo, hi in DOMAIN_RAW_BOUNDS.values())
DOMAIN_BOUNDS = tuple((0.0, 1.0) for _ in MODEL_DIMENSIONS)
MODEL_DIMENSION_COUNT = 5
Q = 1
NOISE_FLOOR_N2 = 0.01
_LATTICE_STEP_OCTAVE = 0.25
_LATTICE_ANCHORS = {
    "force_p_gain": 0.0003535533906,
    "force_damping": 28.0,
    "normal_filter_tau_s": 0.35,
    "orientation_ko": 0.1,
    "motion_kp": 1.5,
}

# Backward-compatible alias for scheduler reference seeding.
NORMAL_FILTER_TAU_S = W9C_REFERENCE_TAU_S


class QLogNEIReuseError(R012ValueError):
    """Production qLogNEI received an invalid or shadow-only row."""


def _nearest_lattice_value(value: float, *, anchor: float, bounds: tuple[float, float]) -> float:
    candidates = []
    for step in range(-128, 129):
        candidate = anchor * 2.0 ** (step * _LATTICE_STEP_OCTAVE)
        if bounds[0] <= candidate <= bounds[1]:
            candidates.append(candidate)
    if not candidates:
        raise QLogNEIReuseError("R012 executable lattice has no value inside bounds")
    return min(candidates, key=lambda item: (abs(math.log2(item / value)), item))


def snap_candidate_to_live_lattice(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Map one continuous acquisition point to the exact mature-controller grid."""

    damping_raw = _candidate_number(candidate, "force_damping")
    damping = _nearest_lattice_value(
        damping_raw,
        anchor=_LATTICE_ANCHORS["force_damping"],
        bounds=DAMPING_BOUNDS,
    )
    p_raw = _candidate_number(candidate, "force_p_gain")
    p = _nearest_lattice_value(
        p_raw,
        anchor=_LATTICE_ANCHORS["force_p_gain"],
        bounds=(P_OVER_D_BOUNDS[0] * damping, P_OVER_D_BOUNDS[1] * damping),
    )
    result = {
        "force_p_gain": p,
        "force_damping": damping,
        "force_i_gain": FORCE_I_GAIN,
        "i_off": I_OFF,
        "normal_filter_tau_s": _nearest_lattice_value(
            _candidate_number(candidate, "normal_filter_tau_s"),
            anchor=_LATTICE_ANCHORS["normal_filter_tau_s"],
            bounds=TAU_BOUNDS,
        ),
        "orientation_ko": _nearest_lattice_value(
            _candidate_number(candidate, "orientation_ko"),
            anchor=_LATTICE_ANCHORS["orientation_ko"],
            bounds=KO_BOUNDS,
        ),
        "motion_kp": _nearest_lattice_value(
            _candidate_number(candidate, "motion_kp"),
            anchor=_LATTICE_ANCHORS["motion_kp"],
            bounds=KP_BOUNDS,
        ),
        "target_force_n": float(candidate.get("target_force_n", 5.0)),
    }
    candidate_to_normalized(result)
    return result


def _candidate_number(candidate: Mapping[str, Any], key: str) -> float:
    if key not in candidate or isinstance(candidate[key], bool):
        raise QLogNEIReuseError(f"candidate lacks numeric {key}")
    return finite(candidate[key], f"candidate {key}")


def _in_box(p: float, damping: float, tau: float, ko: float, kp: float) -> bool:
    ratio = p / damping
    return (
        P_OVER_D_BOUNDS[0] <= ratio <= P_OVER_D_BOUNDS[1]
        and DAMPING_BOUNDS[0] <= damping <= DAMPING_BOUNDS[1]
        and TAU_BOUNDS[0] <= tau <= TAU_BOUNDS[1]
        and KO_BOUNDS[0] <= ko <= KO_BOUNDS[1]
        and KP_BOUNDS[0] <= kp <= KP_BOUNDS[1]
    )


def physical_candidate_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    """Identity of the physical controller candidate, including pinned I axes."""

    p = _candidate_number(candidate, "force_p_gain")
    damping = _candidate_number(candidate, "force_damping")
    i_gain = _candidate_number(candidate, "force_i_gain")
    tau = _candidate_number(candidate, "normal_filter_tau_s")
    ko = _candidate_number(candidate, "orientation_ko")
    kp = _candidate_number(candidate, "motion_kp")
    i_off = candidate.get("i_off")
    if i_off is not True or i_gain != FORCE_I_GAIN:
        raise QLogNEIReuseError("R012 I pin is not exact")
    if p <= 0.0 or damping <= 0.0 or tau <= 0.0 or ko <= 0.0 or kp <= 0.0:
        raise QLogNEIReuseError("candidate violates R012 physical positivity")
    ratio = p / damping
    if ratio < P_OVER_D_BOUNDS[0] or ratio > P_OVER_D_BOUNDS[1]:
        raise QLogNEIReuseError("candidate P/D outside R012 BO box")
    return (p, damping, i_gain, i_off, tau, ko, kp, candidate.get("target_force_n", 5.0))


def _log_features_from_physical(p: float, damping: float, tau: float, ko: float, kp: float) -> tuple[float, ...]:
    return (math.log2(p / damping), math.log2(damping), math.log2(tau), math.log2(ko), math.log2(kp))


def candidate_to_log_features(candidate: Mapping[str, Any]) -> tuple[float, ...]:
    key = physical_candidate_key(candidate)
    p, damping, _i, _off, tau, ko, kp, _target = key
    if not _in_box(p, damping, tau, ko, kp):
        raise QLogNEIReuseError("candidate is outside the finite R012 BO box")
    return _log_features_from_physical(p, damping, tau, ko, kp)


def candidate_to_normalized(candidate: Mapping[str, Any]) -> tuple[float, ...]:
    values = candidate_to_log_features(candidate)
    normalized = tuple((value - low) / (high - low) for value, (low, high) in zip(values, DOMAIN_LOG_BOUNDS))
    if any(value < -1e-12 or value > 1.0 + 1e-12 for value in normalized):
        raise QLogNEIReuseError("normalized candidate is outside [0,1]^5")
    return tuple(min(1.0, max(0.0, value)) for value in normalized)


def _reference_identity(candidate: Mapping[str, Any]) -> bool:
    ref = {**FORCED_REFERENCE_CANDIDATE, "force_i_gain": FORCE_I_GAIN, "i_off": I_OFF}
    return all(candidate.get(key) == value for key, value in ref.items())


def _forced_reference_log_features(candidate: Mapping[str, Any]) -> tuple[float, ...] | None:
    if not _reference_identity(candidate):
        return None
    ref = FORCED_REFERENCE_CANDIDATE
    return _log_features_from_physical(
        ref["force_p_gain"],
        ref["force_damping"],
        ref["normal_filter_tau_s"],
        ref["orientation_ko"],
        ref["motion_kp"],
    )


def forced_reference_training_only(candidate: Mapping[str, Any] | None = None) -> bool:
    """True when W9c reference Ko is outside the finite BO box."""

    ko = FORCED_REFERENCE_CANDIDATE["orientation_ko"]
    return ko < KO_BOUNDS[0] or ko > KO_BOUNDS[1]


def normalized_to_candidate(values: Sequence[float], *, template: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if len(values) != MODEL_DIMENSION_COUNT:
        raise QLogNEIReuseError("R012 model point must have five dimensions")
    unit = tuple(finite(value, "normalized model point") for value in values)
    if any(value < 0.0 or value > 1.0 for value in unit):
        raise QLogNEIReuseError("normalized model point is outside [0,1]^5")
    logs = tuple(low + value * (high - low) for value, (low, high) in zip(unit, DOMAIN_LOG_BOUNDS))
    base = dict(template or {})
    damping = 2.0 ** logs[1]
    base.update(
        {
            "force_damping": damping,
            "force_p_gain": (2.0 ** logs[0]) * damping,
            "force_i_gain": FORCE_I_GAIN,
            "i_off": I_OFF,
            "normal_filter_tau_s": 2.0 ** logs[2],
            "orientation_ko": 2.0 ** logs[3],
            "motion_kp": 2.0 ** logs[4],
        }
    )
    return base


@dataclass(frozen=True)
class ProductionQLogNEIBinding:
    component: str = "r012_production_qlognei"
    acquisition: str = "qLogNEI"
    pending_model: str = "production_qlognei"
    q: int = Q
    censored_observations_allowed: bool = False
    theory_shadow_separate: bool = True
    schema: str = QLOGNEI_SCHEMA

    @property
    def reused_r010_component(self) -> str:
        return "none"

    def __post_init__(self) -> None:
        if (
            self.schema != QLOGNEI_SCHEMA
            or self.component != "r012_production_qlognei"
            or self.acquisition != "qLogNEI"
            or self.q != Q
            or self.censored_observations_allowed
            or not self.theory_shadow_separate
        ):
            raise QLogNEIReuseError("R012 production qLogNEI binding differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "component": self.component,
            "acquisition": self.acquisition,
            "pending_model": self.pending_model,
            "q": self.q,
            "censored_observations_allowed": False,
            "theory_shadow_separate": True,
            "domain_schema": DOMAIN_SCHEMA,
            "model_dimensions": list(MODEL_DIMENSIONS),
            "fixed_domain_raw_bounds": {key: list(value) for key, value in DOMAIN_RAW_BOUNDS.items()},
            "fixed_domain_log2_bounds": [list(item) for item in DOMAIN_LOG_BOUNDS],
            "pinned_axes": {"force_i_gain": FORCE_I_GAIN, "i_off": I_OFF},
            "reused_r010_component": "none",
        }


@dataclass(frozen=True)
class ProductionGPConfig:
    domain_schema: str = DOMAIN_SCHEMA
    noise_floor_n2: float = NOISE_FLOOR_N2
    initial_lengthscales_unit: tuple[float, ...] = (0.35, 0.35, 0.35, 0.35, 0.35)
    lengthscale_bounds_unit: tuple[tuple[float, float], ...] = ((0.03, 5.0),) * MODEL_DIMENSION_COUNT
    kernel: str = "Matern52_ARD_normalized_unit_cube"
    version: str = "r012-production-gp-v1"

    def __post_init__(self) -> None:
        if (
            self.domain_schema != DOMAIN_SCHEMA
            or len(self.initial_lengthscales_unit) != MODEL_DIMENSION_COUNT
            or len(self.lengthscale_bounds_unit) != MODEL_DIMENSION_COUNT
        ):
            raise QLogNEIReuseError("R012 GP config dimensions differ")
        if any(finite(value, "initial lengthscale") <= 0.0 for value in self.initial_lengthscales_unit):
            raise QLogNEIReuseError("R012 GP lengthscales must be positive")
        if finite(self.noise_floor_n2, "noise_floor_n2") <= 0.0:
            raise QLogNEIReuseError("R012 GP noise floor must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": GP_SCHEMA,
            "version": self.version,
            "domain_schema": self.domain_schema,
            "model_dimensions": list(MODEL_DIMENSIONS),
            "fixed_domain_log2_bounds": [list(item) for item in DOMAIN_LOG_BOUNDS],
            "normalize_bounds": [list(item) for item in DOMAIN_LOG_BOUNDS],
            "noise_floor_n2": self.noise_floor_n2,
            "kernel": self.kernel,
            "initial_lengthscales_unit": list(self.initial_lengthscales_unit),
            "lengthscale_bounds_unit": [list(item) for item in self.lengthscale_bounds_unit],
            "repeated_rows": "preserved",
            "outcome_transform": "default_Standardize",
        }


@dataclass(frozen=True)
class FallbackGP:
    X: tuple[tuple[float, ...], ...]
    Y: tuple[float, ...]
    Yvar: tuple[float, ...]
    config: ProductionGPConfig
    backend: str = "offline-deterministic-fallback"

    def predict(self, point: Sequence[float]) -> tuple[float, float]:
        if not self.X:
            return (0.0, 1.0)
        x = tuple(float(value) for value in point)
        normalized_x = tuple((value - low) / (high - low) for value, (low, high) in zip(x, DOMAIN_LOG_BOUNDS))
        weights = []
        for row, y, var in zip(self.X, self.Y, self.Yvar):
            normalized_row = tuple((value - low) / (high - low) for value, (low, high) in zip(row, DOMAIN_LOG_BOUNDS))
            distance = sum(
                ((a - b) / max(self.config.initial_lengthscales_unit[index], 1e-9)) ** 2
                for index, (a, b) in enumerate(zip(normalized_row, normalized_x))
            )
            weight = math.exp(-0.5 * distance)
            weights.append((weight, y, var))
        total = sum(item[0] for item in weights)
        if total <= 1e-15:
            return (statistics.fmean(self.Y), statistics.pvariance(self.Y) + self.config.noise_floor_n2)
        mean = sum(weight * y for weight, y, _ in weights) / total
        variance = self.config.noise_floor_n2 + sum(weight * (y - mean) ** 2 for weight, y, _ in weights) / total
        return (mean, max(self.config.noise_floor_n2, variance))


@dataclass(frozen=True)
class ProductionGPFit:
    model: Any
    train_X: tuple[tuple[float, ...], ...]
    train_Y: tuple[float, ...]
    train_Yvar: tuple[float, ...]
    config: ProductionGPConfig
    backend: str
    fit_completed: bool = False
    device: str = "unavailable"
    fit_receipt: Mapping[str, Any] = ()

    def __post_init__(self) -> None:
        if self.fit_receipt == ():
            object.__setattr__(self, "fit_receipt", {})


def _row_and_variance(observation: Observation | Mapping[str, Any], variance: float | None) -> tuple[ExactObservation, float]:
    row = validate_observation(observation)
    if isinstance(row, CensoredObservation):
        raise QLogNEIReuseError("censored rows are excluded from the production GP")
    value = row.observation_variance_n2 if variance is None else finite(variance, "observation variance")
    if value <= 0.0:
        raise QLogNEIReuseError("observation variance must be positive")
    return row, value


def production_qlognei_observations(observations: Sequence[Observation | Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    rows = []
    for observation in observations:
        row = validate_observation(observation)
        if isinstance(row, CensoredObservation):
            continue
        rows.append(row.as_dict())
    return tuple(rows)


def _unbounded_log_features(
    candidate: Mapping[str, Any],
    *,
    enforce_i_pin: bool = True,
    enforce_box: bool = False,
) -> tuple[float, ...]:
    p = _candidate_number(candidate, "force_p_gain")
    damping = _candidate_number(candidate, "force_damping")
    tau = _candidate_number(candidate, "normal_filter_tau_s")
    ko = _candidate_number(candidate, "orientation_ko")
    kp = _candidate_number(candidate, "motion_kp")
    i_gain = _candidate_number(candidate, "force_i_gain")
    if enforce_i_pin and (candidate.get("i_off") is not True or i_gain != FORCE_I_GAIN):
        raise QLogNEIReuseError("R012 I pin is not exact")
    ratio = p / damping
    if min(p, damping, tau, ratio, ko, kp) <= 0.0:
        raise QLogNEIReuseError("candidate raw log coordinate is not positive")
    if enforce_box and not (
        P_OVER_D_BOUNDS[0] <= ratio <= P_OVER_D_BOUNDS[1]
        and DAMPING_BOUNDS[0] <= damping <= DAMPING_BOUNDS[1]
        and TAU_BOUNDS[0] <= tau <= TAU_BOUNDS[1]
        and KO_BOUNDS[0] <= ko <= KO_BOUNDS[1]
        and KP_BOUNDS[0] <= kp <= KP_BOUNDS[1]
    ):
        raise QLogNEIReuseError("candidate outside R012 BO box")
    return _log_features_from_physical(p, damping, tau, ko, kp)


def candidate_to_log_features_unbounded(candidate: Mapping[str, Any], *, historical: bool = False) -> tuple[float, ...]:
    # Historical rows may sit outside the future BO box (incl. former 1.5e-4 cliff
    # points); production ask still uses candidate_to_log_features → box.
    return _unbounded_log_features(candidate, enforce_i_pin=not historical, enforce_box=False)


def _repeat_noise_variances(rows: Sequence[ExactObservation], floor: float) -> tuple[float, ...]:
    groups: dict[tuple[Any, ...], list[float]] = {}
    for row in rows:
        key = tuple(
            row.candidate.get(name)
            for name in (
                "force_p_gain",
                "force_damping",
                "force_i_gain",
                "i_off",
                "normal_filter_tau_s",
                "orientation_ko",
                "motion_kp",
                "target_force_n",
            )
        )
        groups.setdefault(key, []).append(float(row.objective_n))
    local: dict[tuple[Any, ...], float] = {}
    for key, values in groups.items():
        if len(values) >= 2:
            local[key] = max(floor, float(statistics.variance(values)))
    parent = math.exp(statistics.fmean(math.log(value) for value in local.values())) if local else floor
    result: list[float] = []
    for row in rows:
        key = tuple(
            row.candidate.get(name)
            for name in (
                "force_p_gain",
                "force_damping",
                "force_i_gain",
                "i_off",
                "normal_filter_tau_s",
                "orientation_ko",
                "motion_kp",
                "target_force_n",
            )
        )
        if key not in local:
            result.append(floor)
            continue
        count = len(groups[key])
        weight = count / (count + 4.0)
        result.append(math.exp(weight * math.log(local[key]) + (1.0 - weight) * math.log(parent)))
    return tuple(result)


def _training_arrays(
    observations: Sequence[Observation | Mapping[str, Any]],
    observation_variances: Sequence[float] | None,
    *,
    allow_historical_out_of_box: bool,
    noise_floor_n2: float = NOISE_FLOOR_N2,
) -> tuple[tuple[tuple[float, ...], ...], tuple[float, ...], tuple[float, ...]]:
    rows = tuple(observations)
    if observation_variances is not None and len(observation_variances) != len(rows):
        raise QLogNEIReuseError("per-observation variance length differs")
    train_x: list[tuple[float, ...]] = []
    train_y: list[float] = []
    train_yvar: list[float] = []
    exact_rows = tuple(row for row in (validate_observation(observation) for observation in rows) if isinstance(row, ExactObservation))
    estimated_variances = _repeat_noise_variances(exact_rows, noise_floor_n2) if observation_variances is None else ()
    estimated_index = 0
    used_forced_reference = False
    for index, observation in enumerate(rows):
        row = validate_observation(observation)
        if isinstance(row, CensoredObservation):
            continue
        exact, variance = _row_and_variance(
            row,
            estimated_variances[estimated_index] if observation_variances is None else observation_variances[index],
        )
        estimated_index += 1
        if allow_historical_out_of_box:
            features = _unbounded_log_features(exact.candidate, enforce_i_pin=False, enforce_box=False)
        else:
            try:
                features = candidate_to_log_features(exact.candidate)
            except QLogNEIReuseError:
                features = _forced_reference_log_features(exact.candidate)
                if features is None:
                    raise
                used_forced_reference = True
        train_x.append(features)
        train_y.append(float(exact.objective_n))
        train_yvar.append(float(variance))
    return tuple(train_x), tuple(train_y), tuple(train_yvar)


def fit_test_oracle(
    observations: Sequence[Observation | Mapping[str, Any]],
    *,
    observation_variances: Sequence[float] | None = None,
    config: ProductionGPConfig = ProductionGPConfig(),
) -> ProductionGPFit:
    x_tuple, y_tuple, yvar_tuple = _training_arrays(
        observations,
        observation_variances,
        allow_historical_out_of_box=False,
        noise_floor_n2=config.noise_floor_n2,
    )
    model = FallbackGP(x_tuple, y_tuple, yvar_tuple, config)
    return ProductionGPFit(
        model,
        x_tuple,
        y_tuple,
        yvar_tuple,
        config,
        "test_oracle.fallback",
        False,
        "cpu",
        {"backend": "test_oracle.fallback", "fit_completed": False, "production_proposal_allowed": False},
    )


def fit_production_gp(
    observations: Sequence[Observation | Mapping[str, Any]],
    *,
    observation_variances: Sequence[float] | None = None,
    config: ProductionGPConfig = ProductionGPConfig(),
    allow_historical_out_of_box: bool = False,
    previous_fit_receipts: Sequence[Mapping[str, Any]] = (),
) -> ProductionGPFit:
    x_tuple, y_tuple, yvar_tuple = _training_arrays(
        observations,
        observation_variances,
        allow_historical_out_of_box=allow_historical_out_of_box,
        noise_floor_n2=config.noise_floor_n2,
    )
    if not x_tuple:
        raise QLogNEIReuseError("production GP requires at least one exact row")
    try:
        import torch
        import gpytorch
        from botorch.fit import fit_gpytorch_mll
        from botorch.models import SingleTaskGP
        from botorch.models.transforms.input import Normalize
        from botorch.models.transforms.outcome import Standardize
        from gpytorch.mlls import ExactMarginalLogLikelihood
    except (ImportError, ModuleNotFoundError) as exc:
        raise QLogNEIReuseError("production BoTorch runtime is unavailable; use fit_test_oracle explicitly") from exc
    if not torch.cuda.is_available():
        raise QLogNEIReuseError("production R012 GP requires the attested CUDA optimizer runtime")
    device = torch.device("cuda")
    bounds = torch.tensor(
        [[low for low, _high in DOMAIN_LOG_BOUNDS], [high for _low, high in DOMAIN_LOG_BOUNDS]],
        dtype=torch.double,
        device=device,
    )
    tx = torch.tensor(x_tuple, dtype=torch.double, device=device)
    ty = torch.tensor(y_tuple, dtype=torch.double, device=device).unsqueeze(-1)
    tyvar = torch.tensor(yvar_tuple, dtype=torch.double, device=device).unsqueeze(-1)
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
        lengthscale=torch.tensor(config.initial_lengthscales_unit, dtype=torch.double, device=device).reshape(
            1, MODEL_DIMENSION_COUNT
        )
    )
    mll = ExactMarginalLogLikelihood(model.likelihood, model)
    model.train()
    model.likelihood.train()
    try:
        fit_gpytorch_mll(mll, optimizer_kwargs={"options": {"maxiter": 200}})
    except Exception as exc:
        raise QLogNEIReuseError(f"production GP MLL fit failed: {type(exc).__name__}: {exc}") from exc
    model.eval()
    model.likelihood.eval()
    lengthscales = tuple(float(value) for value in model.covar_module.base_kernel.lengthscale.detach().cpu().reshape(-1))
    outputscale = float(model.covar_module.outputscale.detach().cpu())
    group_counts: dict[tuple[Any, ...], int] = {}
    for observation in observations:
        row = validate_observation(observation)
        if isinstance(row, CensoredObservation):
            continue
        key = tuple(
            row.candidate.get(name, 5.0 if name == "target_force_n" else None)
            for name in (
                "force_p_gain",
                "force_damping",
                "force_i_gain",
                "i_off",
                "normal_filter_tau_s",
                "orientation_ko",
                "motion_kp",
                "target_force_n",
            )
        )
        group_counts[key] = group_counts.get(key, 0) + 1
    repeat_groups = sum(count >= 2 for count in group_counts.values())
    train_yvar_geometric_mean_n2 = math.exp(statistics.fmean(math.log(value) for value in yvar_tuple))
    exact_rows = len(x_tuple)
    freeze_allowed = False
    if previous_fit_receipts and exact_rows >= 30 and repeat_groups >= 8:
        previous = previous_fit_receipts[-1]
        previous_lengths = previous.get("lengthscales_unit_fitted")
        previous_noise = previous.get("train_yvar_geometric_mean_n2")
        if isinstance(previous_lengths, Sequence) and len(previous_lengths) == MODEL_DIMENSION_COUNT and previous_noise is not None:
            lengths_ok = max(
                abs(float(a) - float(b)) / max(abs(float(a)), 1e-12)
                for a, b in zip(previous_lengths, lengthscales)
            ) <= 0.20
            previous_noise_n2 = finite(previous_noise, "previous train yvar geometric mean")
            noise_ok = previous_noise_n2 > 0.0 and abs(previous_noise_n2 - train_yvar_geometric_mean_n2) / previous_noise_n2 <= 0.25
            freeze_allowed = lengths_ok and noise_ok
    receipt = {
        "schema": "r012-production-gp-fit-receipt-v1",
        "backend": "botorch.SingleTaskGP",
        "botorch_version": __import__("botorch").__version__,
        "torch_version": torch.__version__,
        "device": str(device),
        "cuda_device": torch.cuda.get_device_name(device),
        "row_count": len(x_tuple),
        "fit_completed": True,
        "model_eval": not model.training,
        "likelihood_eval": not model.likelihood.training,
        "lengthscales_unit_fitted": list(lengthscales),
        "outputscale_fitted": outputscale,
        "fixed_normalize_raw_log_bounds": [list(item) for item in DOMAIN_LOG_BOUNDS],
        "noise_semantics": "per-row train_Yvar is observation variance; repeated raw rows preserved; no FixedNoiseGaussianLikelihood",
        "train_yvar_geometric_mean_n2": train_yvar_geometric_mean_n2,
        "forced_reference_training_only": any(
            _reference_identity(validate_observation(observation).candidate)
            for observation in observations
            if isinstance(validate_observation(observation), ExactObservation)
        )
        and allow_historical_out_of_box
        and forced_reference_training_only(),
        "production_proposal_allowed": True,
        "repeat_group_count": repeat_groups,
        "hyperparameter_freeze_allowed": freeze_allowed,
        "freeze_gate": {
            "minimum_exact_rows": 30,
            "minimum_repeat_groups": 8,
            "consecutive_refits": 2,
            "relative_lengthscale_change_le_0.20": True,
            "relative_noise_change_le_0.25": True,
        },
    }
    return ProductionGPFit(model, x_tuple, y_tuple, yvar_tuple, config, "botorch.SingleTaskGP", True, str(device), receipt)


def build_qlognei_acquisition(fit: ProductionGPFit, *, pending_X: Sequence[Sequence[float]] = ()) -> Any:
    if fit.backend != "botorch.SingleTaskGP" or not fit.fit_completed:
        raise QLogNEIReuseError("qLogNEI production acquisition requires completed production BoTorch fit")
    import torch
    from botorch.acquisition.logei import qLogNoisyExpectedImprovement
    from botorch.acquisition.objective import GenericMCObjective

    fit.model.eval()
    device = next(fit.model.parameters()).device
    baseline = torch.tensor(fit.train_X, dtype=torch.double, device=device)
    pending = None if not pending_X else torch.tensor(pending_X, dtype=torch.double, device=device)
    objective = GenericMCObjective(negative_mae_mc_objective)
    return qLogNoisyExpectedImprovement(
        model=fit.model,
        X_baseline=baseline,
        X_pending=pending,
        objective=objective,
        prune_baseline=False,
    )


def negative_mae_mc_objective(samples: Any, X: Any = None) -> Any:
    del X
    return -samples.squeeze(-1) if hasattr(samples, "squeeze") else -samples


@dataclass(frozen=True)
class CandidateProposal:
    candidate: Mapping[str, Any]
    normalized_X: tuple[float, ...]
    acquisition_value: float
    q: int = Q
    excluded_keys: tuple[tuple[Any, ...], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate", json_tree(self.candidate))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r012-candidate-proposal-v1",
            "candidate": json_tree(self.candidate),
            "normalized_X": list(self.normalized_X),
            "acquisition_value": self.acquisition_value,
            "q": self.q,
            "excluded_key_count": len(self.excluded_keys),
        }


def ask_qlognei(
    fit: ProductionGPFit,
    candidates: Sequence[Mapping[str, Any]],
    *,
    evaluated_keys: Iterable[tuple[Any, ...]] = (),
    pending_keys: Iterable[tuple[Any, ...]] = (),
    pending_X: Sequence[Sequence[float]] = (),
) -> CandidateProposal:
    if fit.backend != "botorch.SingleTaskGP" or not fit.fit_completed:
        raise QLogNEIReuseError("production proposal requires completed production BoTorch fit")
    excluded = set(evaluated_keys) | set(pending_keys)
    admissible = [
        (candidate, physical_candidate_key(candidate), candidate_to_log_features(candidate), candidate_to_normalized(candidate))
        for candidate in candidates
    ]
    admissible = [item for item in admissible if item[1] not in excluded]
    if not admissible:
        raise QLogNEIReuseError("all finite candidates are evaluated or pending")
    acquisition = build_qlognei_acquisition(fit, pending_X=pending_X)
    scored: list[tuple[float, Mapping[str, Any], tuple[float, ...]]] = []
    if fit.backend == "botorch.SingleTaskGP":
        import torch

        device = next(fit.model.parameters()).device
        with torch.no_grad():
            values = acquisition(
                torch.tensor([item[2] for item in admissible], dtype=torch.double, device=device).unsqueeze(1)
            ).reshape(-1)
        scored = [(float(value), item[0], item[3]) for value, item in zip(values, admissible)]
    score, candidate, point = max(scored, key=lambda item: (item[0], tuple(-value for value in item[2])))
    return CandidateProposal(candidate, point, score, Q, tuple(sorted(excluded, key=repr)))


def validate_production_binding(value: Mapping[str, Any]) -> ProductionQLogNEIBinding:
    if not isinstance(value, Mapping):
        raise QLogNEIReuseError("qLogNEI binding must be an object")
    return ProductionQLogNEIBinding(
        component=value.get("component", "r012_production_qlognei"),
        acquisition=value["acquisition"],
        pending_model=value["pending_model"],
        q=value.get("q", Q),
        censored_observations_allowed=value["censored_observations_allowed"],
        theory_shadow_separate=value["theory_shadow_separate"],
        schema=value["schema"],
    )


__all__ = [
    "CandidateProposal",
    "DOMAIN_BOUNDS",
    "DOMAIN_LOG_BOUNDS",
    "DOMAIN_RAW_BOUNDS",
    "DAMPING_BOUNDS",
    "FORCE_I_GAIN",
    "FORCED_REFERENCE_CANDIDATE",
    "GP_SCHEMA",
    "I_OFF",
    "KO_BOUNDS",
    "KP_BOUNDS",
    "MODEL_DIMENSIONS",
    "MODEL_DIMENSION_COUNT",
    "NOISE_FLOOR_N2",
    "NORMAL_FILTER_TAU_S",
    "P_OVER_D_BOUNDS",
    "ProductionGPConfig",
    "ProductionGPFit",
    "ProductionQLogNEIBinding",
    "Q",
    "QLOGNEI_SCHEMA",
    "QLogNEIReuseError",
    "TAU_BOUNDS",
    "W9C_REFERENCE_TAU_S",
    "ask_qlognei",
    "build_qlognei_acquisition",
    "candidate_to_log_features",
    "candidate_to_log_features_unbounded",
    "candidate_to_normalized",
    "fit_production_gp",
    "fit_test_oracle",
    "forced_reference_training_only",
    "negative_mae_mc_objective",
    "normalized_to_candidate",
    "physical_candidate_key",
    "production_qlognei_observations",
    "snap_candidate_to_live_lattice",
    "validate_production_binding",
]
