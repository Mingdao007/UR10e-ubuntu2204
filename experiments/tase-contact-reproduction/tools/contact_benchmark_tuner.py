"""Offline candidate proposer for the six-law contact benchmark.

This lane has a deliberately small contract.  Each law receives 24 units:
eight deterministic Sobol seed points, twelve Bayesian expected-improvement
points, and four repeats of the observed incumbent.  The caller owns trial
execution and supplies completed *training* observations, including failed
units.  A failed unit consumes its ordinal but never contributes a fabricated
objective.

Only the common physical parameters ``m``, ``mu`` and ``g`` are proposed.  All
other parameters from ``contact_benchmark_laws.json`` remain fixed for the
law, including the latest MSFC structural/memory seed.  The search bounds are
provisional same-bound policy for this offline lane; they are not live safety
limits or hardware qualification evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any

from contact_laws import DEFAULT_CONFIG_PATH, PARAMETER_ORDER, PUBLIC_LAWS


TUNER_SCHEMA = "ur10e.contact-benchmark-tuner-v1"
BINDING_SCHEMA = "ur10e.contact-benchmark-tuner-binding-v1"
TOTAL_UNITS = 24
INITIAL_UNITS = 8
EI_UNITS = 12
REPEAT_UNITS = 4
M = (4.0, 16.0)
MU_RELATIVE = (0.125, 8.0)
G_RELATIVE = (0.03, 2.0)
# Keep the short name for callers that treat the shared policy dimensions as
# public constants; its values are relative factors, just like MU_RELATIVE.
G = G_RELATIVE
_TUNED = ("m", "mu", "g")
_STATUSES = frozenset(("completed", "failed"))


class ContactBenchmarkTunerError(ValueError):
    """Invalid training input or an unavailable offline proposal."""


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool):
        raise ContactBenchmarkTunerError(f"{role} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ContactBenchmarkTunerError(f"{role} must be a finite number") from error
    if not math.isfinite(parsed):
        raise ContactBenchmarkTunerError(f"{role} must be a finite number")
    return parsed


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise ContactBenchmarkTunerError(f"value is not canonical JSON: {error}") from error


def _clip(value: float, bounds: tuple[float, float]) -> float:
    return max(bounds[0], min(bounds[1], value))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _halton(index: int, base: int) -> float:
    result = 0.0
    factor = 1.0 / float(base)
    remainder = int(index)
    while remainder > 0:
        remainder, digit = divmod(remainder, base)
        result += digit * factor
        factor /= float(base)
    return result


def _unit_points(seed: int, count: int) -> tuple[tuple[tuple[float, ...], ...], str]:
    """Return deterministic unit-cube points and an honest backend label."""

    if count <= 0 or count & (count - 1):
        raise ContactBenchmarkTunerError("internal unit pool size must be a power of two")
    try:
        from scipy.stats import qmc

        sampler = qmc.Sobol(d=3, scramble=True, seed=int(seed))
        points = sampler.random_base2(m=int(math.log2(count)))
        return tuple(tuple(float(value) for value in row) for row in points), "scipy_sobol"
    except (ImportError, ModuleNotFoundError):
        # The fallback is not hidden: proposal metadata says Sobol was
        # unavailable.  It keeps the offline proposer dependency-light.
        primes = (2, 3, 5)
        return (
            tuple(
                tuple(_halton(seed + row + 1, primes[index]) for index in range(3))
                for row in range(count)
            ),
            "deterministic_halton",
        )


def matern52_expected_improvement(train_x, train_y, candidate_x, *, lengthscale: float = 0.35):
    """Fixed Matérn-5/2 expected improvement; one finite EI value per candidate.

    This is the shared numerical kernel.  Callers own candidate generation,
    bounds, and whether a missing backend is fatal.  A space-filling fallback
    is not an EI result.
    """

    try:
        import numpy as np
    except (ImportError, ModuleNotFoundError) as error:
        raise ContactBenchmarkTunerError("numpy is unavailable for Bayesian EI") from error
    train_x = np.asarray(train_x, dtype=float)
    train_y = np.asarray(train_y, dtype=float)
    candidate_x = np.asarray(candidate_x, dtype=float)
    if train_x.ndim != 2 or train_x.shape[1] != 3 or train_x.shape[0] == 0 or not np.all(np.isfinite(train_x)):
        raise ContactBenchmarkTunerError("Bayesian EI training features are invalid")
    if train_y.ndim != 1 or train_y.shape[0] != train_x.shape[0] or not np.all(np.isfinite(train_y)):
        raise ContactBenchmarkTunerError("Bayesian EI training objectives are invalid")
    if candidate_x.ndim != 2 or candidate_x.shape[1] != 3 or candidate_x.shape[0] == 0 or not np.all(
        np.isfinite(candidate_x)
    ):
        raise ContactBenchmarkTunerError("Bayesian EI candidate features are invalid")

    def matern52(lhs, rhs):
        distance = np.sqrt(
            np.maximum(0.0, np.sum(((lhs[:, None, :] - rhs[None, :, :]) / lengthscale) ** 2, axis=2))
        )
        scaled = math.sqrt(5.0) * distance
        return (1.0 + scaled + scaled * scaled / 3.0) * np.exp(-scaled)

    scale = max(float(np.var(train_y)), 1.0e-6)
    kernel = scale * matern52(train_x, train_x)
    # This is numerical jitter, not a hidden feasibility or objective gate.
    jitter = max(1.0e-10, scale * 1.0e-8)
    kernel = kernel + jitter * np.eye(kernel.shape[0])
    try:
        centered = train_y - float(np.mean(train_y))
        alpha = np.linalg.solve(kernel, centered)
        cross = scale * matern52(train_x, candidate_x)
        posterior_mean = float(np.mean(train_y)) + cross.T @ alpha
        solved_cross = np.linalg.solve(kernel, cross)
        posterior_variance = scale - np.sum(cross * solved_cross, axis=0)
    except (np.linalg.LinAlgError, FloatingPointError, ValueError) as error:
        raise ContactBenchmarkTunerError(f"Bayesian EI GP solve failed: {error}") from error
    if not np.all(np.isfinite(posterior_mean)) or not np.all(np.isfinite(posterior_variance)):
        raise ContactBenchmarkTunerError("Bayesian EI posterior is nonfinite")
    posterior_sigma = np.sqrt(np.maximum(0.0, posterior_variance))
    best = float(np.min(train_y))
    improvement = best - posterior_mean
    normalizer = math.sqrt(2.0)
    cdf = 0.5 * (
        1.0
        + np.vectorize(math.erf)(
            improvement / np.where(posterior_sigma > 0.0, posterior_sigma * normalizer, 1.0)
        )
    )
    pdf = np.exp(
        -0.5 * (improvement / np.where(posterior_sigma > 0.0, posterior_sigma, 1.0)) ** 2
    ) / math.sqrt(2.0 * math.pi)
    ei = np.where(
        posterior_sigma > 0.0,
        improvement * cdf + posterior_sigma * pdf,
        np.maximum(improvement, 0.0),
    )
    if not np.all(np.isfinite(ei)):
        raise ContactBenchmarkTunerError("Bayesian EI acquisition is nonfinite")
    return ei


@dataclass(frozen=True)
class ContactLawCandidate:
    """One full native-law parameter set with only ``m``, ``mu`` and ``g`` free."""

    law: str
    m: float
    mu: float
    g: float
    fixed_parameters: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.law, str) or self.law not in PUBLIC_LAWS:
            raise ContactBenchmarkTunerError(f"unknown public law {self.law!r}")
        tuned_values = []
        for name in _TUNED:
            tuned_values.append(_finite(getattr(self, name), f"{self.law} {name}"))
        object.__setattr__(self, "m", tuned_values[0])
        object.__setattr__(self, "mu", tuned_values[1])
        object.__setattr__(self, "g", tuned_values[2])
        names: set[str] = set()
        fixed: list[tuple[str, float]] = []
        try:
            raw_fixed = tuple(self.fixed_parameters)
        except TypeError as error:
            raise ContactBenchmarkTunerError(f"{self.law} fixed parameters are invalid") from error
        for item in raw_fixed:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ContactBenchmarkTunerError(f"{self.law} fixed parameters are invalid")
            name, value = item
            if not isinstance(name, str) or name in names or name in _TUNED:
                raise ContactBenchmarkTunerError(f"{self.law} fixed parameter names are invalid")
            names.add(name)
            fixed.append((name, _finite(value, f"{self.law} {name}")))
        expected_fixed = {
            name for name in PARAMETER_ORDER[self.law] if name not in _TUNED
        }
        if names != expected_fixed:
            raise ContactBenchmarkTunerError(
                f"{self.law} fixed parameter fields differ from its native parameter order"
            )
        fixed_by_name = dict(fixed)
        object.__setattr__(
            self,
            "fixed_parameters",
            tuple(
                (name, fixed_by_name[name])
                for name in PARAMETER_ORDER[self.law]
                if name not in _TUNED
            ),
        )

    @property
    def parameters(self) -> dict[str, float]:
        result = {"m": float(self.m), "mu": float(self.mu), "g": float(self.g)}
        result.update({name: float(value) for name, value in self.fixed_parameters})
        return result

    @property
    def key(self) -> str:
        return _sha256_bytes(
            _canonical(
                {
                    "schema": "ur10e.contact-benchmark-candidate-v1",
                    "law": self.law,
                    "parameters": self.parameters,
                }
            )
        )

    def __getitem__(self, name: str) -> float:
        return self.parameters[name]

    def as_dict(self) -> dict[str, Any]:
        return {"law": self.law, **self.parameters}


@dataclass(frozen=True)
class TrainingObservation:
    """Caller-supplied training row; failed rows carry no objective."""

    law: str
    candidate: ContactLawCandidate | Mapping[str, Any]
    status: str
    # Training observations always carry a measured boolean feasibility value.
    nominal_feasible: bool
    objective: float | None = None
    split: str = "training"


@dataclass(frozen=True)
class TuningProposal:
    """Structured next-unit proposal and the binding needed to audit it."""

    law: str
    candidate: ContactLawCandidate
    unit_index: int
    phase: str
    reason: str
    # A new candidate has not been measured yet.  ``None`` therefore means
    # unknown; only a repeat selected from a completed training row may carry
    # the observed boolean value.
    nominal_feasible: bool | None
    bo_used: bool
    backend: str
    bindings: Mapping[str, Any]
    acquisition_value: float | None = None
    incumbent_objective: float | None = None
    schema: str = TUNER_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != TUNER_SCHEMA or self.version != 1:
            raise ContactBenchmarkTunerError("tuning proposal schema/version differs")
        if self.law not in PUBLIC_LAWS or self.candidate.law != self.law:
            raise ContactBenchmarkTunerError("proposal law/candidate law differs")
        if type(self.unit_index) is not int or not 0 <= self.unit_index < TOTAL_UNITS:
            raise ContactBenchmarkTunerError("proposal unit_index must be in [0, 23]")
        if self.phase not in {"initial_sobol", "bayesian_ei", "repeat_incumbent"}:
            raise ContactBenchmarkTunerError("proposal phase is unsupported")
        if not isinstance(self.reason, str) or not self.reason:
            raise ContactBenchmarkTunerError("proposal reason is missing")
        if self.nominal_feasible is not None and type(self.nominal_feasible) is not bool:
            raise ContactBenchmarkTunerError("proposal nominal_feasible must be bool or None")
        if type(self.bo_used) is not bool:
            raise ContactBenchmarkTunerError("proposal BO flag must be bool")
        if self.acquisition_value is not None:
            _finite(self.acquisition_value, "proposal acquisition_value")
        if self.incumbent_objective is not None:
            _finite(self.incumbent_objective, "proposal incumbent_objective")
        object.__setattr__(self, "bindings", _freeze(dict(self.bindings)))

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": self.schema,
            "version": self.version,
            "law": self.law,
            "candidate": self.candidate.as_dict(),
            "unit_index": self.unit_index,
            "unit_number": self.unit_index + 1,
            "phase": self.phase,
            "reason": self.reason,
            "nominal_feasible": self.nominal_feasible,
            "bo_used": self.bo_used,
            "backend": self.backend,
            "acquisition_value": self.acquisition_value,
            "incumbent_objective": self.incumbent_objective,
            "bindings": _thaw(self.bindings),
        }
        return result


@dataclass(frozen=True)
class _ParsedObservation:
    candidate: ContactLawCandidate
    status: str
    nominal_feasible: bool
    objective: float | None
    split: str


class ContactBenchmarkTuner:
    """Deterministic, offline six-law candidate proposer."""

    def __init__(
        self,
        config_path: Path | str = DEFAULT_CONFIG_PATH,
        *,
        seed: int = 20260918,
    ) -> None:
        if type(seed) is not int or seed < 0:
            raise ContactBenchmarkTunerError("tuner seed must be a non-negative integer")
        self.seed = seed
        supplied_config_path = Path(config_path).expanduser()
        if supplied_config_path.is_symlink():
            raise ContactBenchmarkTunerError(
                f"tuner config must not be a symlink: {supplied_config_path}"
            )
        self.config_path = supplied_config_path.resolve()
        if not self.config_path.is_file():
            raise ContactBenchmarkTunerError(f"tuner config is not a regular file: {self.config_path}")
        try:
            raw_bytes = self.config_path.read_bytes()
            root = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ContactBenchmarkTunerError(f"tuner config is unreadable: {error}") from error
        if not isinstance(root, dict):
            raise ContactBenchmarkTunerError("tuner config must be a JSON object")
        if root.get("schema") != "ur10e.contact-benchmark-laws-v1" or root.get("version") != 1:
            raise ContactBenchmarkTunerError("tuner config schema/version differs")
        if root.get("offline_only") is not True or root.get("hardware_qualified") is not False:
            raise ContactBenchmarkTunerError("tuner config is not explicitly offline and unqualified")
        if root.get("endpoint") != "none":
            raise ContactBenchmarkTunerError("tuner config endpoint must be none")
        if tuple(root.get("public_laws", ())) != PUBLIC_LAWS:
            raise ContactBenchmarkTunerError("tuner config public_laws differ")
        laws = root.get("laws")
        if not isinstance(laws, Mapping) or set(laws) != set(PUBLIC_LAWS):
            raise ContactBenchmarkTunerError("tuner config law set differs")
        self.config_sha256 = _sha256_bytes(raw_bytes)
        self._specs: dict[str, dict[str, Any]] = {}
        self._seed_parameters: dict[str, dict[str, float]] = {}
        self._fixed_parameters: dict[str, tuple[tuple[str, float], ...]] = {}
        self._law_seeds: dict[str, int] = {}
        for law in PUBLIC_LAWS:
            spec = laws[law]
            if not isinstance(spec, Mapping) or not isinstance(spec.get("parameters"), Mapping):
                raise ContactBenchmarkTunerError(f"tuner config {law} parameters are missing")
            order = tuple(spec.get("parameter_order", ()))
            if order != tuple(PARAMETER_ORDER[law]):
                raise ContactBenchmarkTunerError(f"tuner config {law} parameter order differs")
            raw_parameters = spec["parameters"]
            if set(raw_parameters) != set(order):
                raise ContactBenchmarkTunerError(f"tuner config {law} parameter fields differ")
            parameters = {name: _finite(raw_parameters[name], f"{law} seed {name}") for name in order}
            for name in _TUNED:
                if name not in parameters or parameters[name] <= 0.0:
                    raise ContactBenchmarkTunerError(f"tuner config {law} seed {name} must be positive")
            fixed = tuple((name, parameters[name]) for name in order if name not in _TUNED)
            seed_material = {
                "schema": TUNER_SCHEMA,
                "config_sha256": self.config_sha256,
                "base_seed": self.seed,
                "law": law,
                "parameters": parameters,
            }
            law_seed = int(_sha256_bytes(_canonical(seed_material))[:8], 16) % (2**32 - 1)
            self._specs[law] = dict(spec)
            self._seed_parameters[law] = parameters
            self._fixed_parameters[law] = fixed
            self._law_seeds[law] = law_seed

    @property
    def bounds(self) -> Mapping[str, tuple[float, float]]:
        """The provisional common search policy (``mu`` and ``g`` are relative)."""

        return MappingProxyType(
            {
                "m": M,
                "mu_relative": MU_RELATIVE,
                # ``g`` is retained as a compact public alias; both names
                # denote a factor relative to that law's config seed.
                "g": G_RELATIVE,
                "g_relative": G_RELATIVE,
            }
        )

    def seed_candidate(self, law: str) -> ContactLawCandidate:
        selected = self._normalise_law(law)
        seed = self._seed_parameters[selected]
        return self._make_candidate(
            selected,
            m=_clip(seed["m"], M),
            mu=_clip(seed["mu"], (seed["mu"] * MU_RELATIVE[0], seed["mu"] * MU_RELATIVE[1])),
            g=_clip(seed["g"], (seed["g"] * G_RELATIVE[0], seed["g"] * G_RELATIVE[1])),
        )

    def law_seed(self, law: str) -> int:
        return self._law_seeds[self._normalise_law(law)]

    def _normalise_law(self, law: str) -> str:
        if not isinstance(law, str) or law not in PUBLIC_LAWS:
            raise ContactBenchmarkTunerError(
                f"law must be one of {', '.join(PUBLIC_LAWS)}; RPSFC is unavailable"
            )
        return law

    def _make_candidate(self, law: str, *, m: float, mu: float, g: float) -> ContactLawCandidate:
        seed_mu = self._seed_parameters[law]["mu"]
        seed_g = self._seed_parameters[law]["g"]
        if not M[0] <= m <= M[1]:
            raise ContactBenchmarkTunerError("candidate m is outside provisional bounds")
        if not seed_mu * MU_RELATIVE[0] <= mu <= seed_mu * MU_RELATIVE[1]:
            raise ContactBenchmarkTunerError("candidate relative mu is outside provisional bounds")
        if not seed_g * G_RELATIVE[0] <= g <= seed_g * G_RELATIVE[1]:
            raise ContactBenchmarkTunerError("candidate relative g is outside provisional bounds")
        return ContactLawCandidate(
            law=law,
            m=float(m),
            mu=float(mu),
            g=float(g),
            fixed_parameters=self._fixed_parameters[law],
        )

    def _candidate_from_unit(self, law: str, point: Sequence[float]) -> ContactLawCandidate:
        if len(point) != 3:
            raise ContactBenchmarkTunerError("unit point must have three coordinates")
        seed_mu = self._seed_parameters[law]["mu"]
        seed_g = self._seed_parameters[law]["g"]
        log_low = math.log2(MU_RELATIVE[0])
        log_high = math.log2(MU_RELATIVE[1])
        m = M[0] + float(point[0]) * (M[1] - M[0])
        mu = seed_mu * 2.0 ** (log_low + float(point[1]) * (log_high - log_low))
        g_log_low = math.log2(G_RELATIVE[0])
        g_log_high = math.log2(G_RELATIVE[1])
        g = seed_g * 2.0 ** (g_log_low + float(point[2]) * (g_log_high - g_log_low))
        return self._make_candidate(law, m=m, mu=mu, g=g)

    def _initial_candidates(self, law: str) -> tuple[tuple[ContactLawCandidate, ...], str]:
        points, backend = _unit_points(self._law_seeds[law], INITIAL_UNITS)
        result: list[ContactLawCandidate] = [self.seed_candidate(law)]
        seen = {result[0].key}
        for point in points:
            candidate = self._candidate_from_unit(law, point)
            if candidate.key in seen:
                continue
            result.append(candidate)
            seen.add(candidate.key)
            if len(result) == INITIAL_UNITS:
                return tuple(result), backend
        # This branch is practically unreachable with scrambled Sobol, but
        # deterministically refill if a future generator quantizes points.
        extra_points, extra_backend = _unit_points(self._law_seeds[law] + 1, 16)
        for point in extra_points:
            candidate = self._candidate_from_unit(law, point)
            if candidate.key in seen:
                continue
            result.append(candidate)
            seen.add(candidate.key)
            if len(result) == INITIAL_UNITS:
                return tuple(result), f"{backend}+{extra_backend}"
        raise ContactBenchmarkTunerError("initial Sobol lane could not produce eight unique candidates")

    def _parse_candidate(self, law: str, value: ContactLawCandidate | Mapping[str, Any]) -> ContactLawCandidate:
        if isinstance(value, ContactLawCandidate):
            if value.law != law:
                raise ContactBenchmarkTunerError("observation candidate law differs")
            payload: Mapping[str, Any] = value.as_dict()
        elif isinstance(value, Mapping):
            payload = value
        else:
            raise ContactBenchmarkTunerError("observation candidate must be an object")
        embedded_law = payload.get("law")
        if embedded_law is not None and embedded_law != law:
            raise ContactBenchmarkTunerError("observation candidate law differs")
        if "parameters" in payload:
            extra_metadata = set(payload) - {"law", "parameters"}
            if extra_metadata:
                raise ContactBenchmarkTunerError(
                    f"observation candidate metadata fields are unsupported: {sorted(extra_metadata)}"
                )
            nested = payload["parameters"]
            if not isinstance(nested, Mapping):
                raise ContactBenchmarkTunerError("observation candidate parameters must be an object")
            candidate_values = dict(nested)
        else:
            candidate_values = {str(name): raw for name, raw in payload.items() if name != "law"}
        expected_names = set(PARAMETER_ORDER[law])
        if set(candidate_values) != expected_names:
            missing = sorted(expected_names - set(candidate_values))
            extra = sorted(set(candidate_values) - expected_names)
            raise ContactBenchmarkTunerError(
                f"{law} observation candidate fields differ (missing={missing}, extra={extra})"
            )
        expected_fixed = self._fixed_parameters[law]
        for name, expected in expected_fixed:
            actual = _finite(candidate_values[name], f"{law} fixed {name}")
            if actual != expected:
                raise ContactBenchmarkTunerError(
                    f"{law} fixed parameter {name} differs from config seed"
                )
        return self._make_candidate(
            law,
            m=_finite(candidate_values["m"], f"{law} m"),
            mu=_finite(candidate_values["mu"], f"{law} mu"),
            g=_finite(candidate_values["g"], f"{law} g"),
        )

    def _parse_observation(
        self,
        law: str,
        value: TrainingObservation | Mapping[str, Any],
    ) -> _ParsedObservation:
        if isinstance(value, TrainingObservation):
            row_law = value.law
            candidate_value = value.candidate
            status = value.status
            nominal_feasible = value.nominal_feasible
            objective = value.objective
            split = value.split
        elif isinstance(value, Mapping):
            row_law = value.get("law", law)
            candidate_value = value.get("candidate")
            status = value.get("status")
            nominal_feasible = value.get("nominal_feasible")
            objective = value.get("objective")
            split = value.get("split", "training")
        else:
            raise ContactBenchmarkTunerError("training observation must be an object")
        if row_law != law:
            raise ContactBenchmarkTunerError("training observation law differs")
        if split != "training":
            raise ContactBenchmarkTunerError("holdout or non-training observations are not accepted")
        if status not in _STATUSES:
            raise ContactBenchmarkTunerError("training observation status must be completed or failed")
        if type(nominal_feasible) is not bool:
            raise ContactBenchmarkTunerError("training observation nominal_feasible must be bool")
        candidate = self._parse_candidate(law, candidate_value)
        if status == "failed":
            if objective is not None:
                raise ContactBenchmarkTunerError("failed training observation cannot carry an objective")
            parsed_objective = None
        else:
            if objective is None:
                raise ContactBenchmarkTunerError("completed training observation requires caller objective")
            parsed_objective = _finite(objective, "training disturbed objective")
            if parsed_objective < 0.0:
                raise ContactBenchmarkTunerError("training disturbed objective cannot be negative")
        return _ParsedObservation(candidate, status, nominal_feasible, parsed_objective, split)

    def _features(self, law: str, candidate: ContactLawCandidate) -> tuple[float, float, float]:
        seed_mu = self._seed_parameters[law]["mu"]
        seed_g = self._seed_parameters[law]["g"]
        return (
            (candidate.m - M[0]) / (M[1] - M[0]),
            (math.log2(candidate.mu / seed_mu) - math.log2(MU_RELATIVE[0])) /
            (math.log2(MU_RELATIVE[1]) - math.log2(MU_RELATIVE[0])),
            (math.log2(candidate.g / seed_g) - math.log2(G_RELATIVE[0])) /
            (math.log2(G_RELATIVE[1]) - math.log2(G_RELATIVE[0])),
        )

    def _bindings(
        self,
        law: str,
        *,
        phase: str,
        backend: str,
        bo_used: bool,
    ) -> dict[str, Any]:
        fixed = dict(self._fixed_parameters[law])
        return {
            "schema": BINDING_SCHEMA,
            "version": 1,
            "config_path": str(self.config_path),
            "config_sha256": self.config_sha256,
            "law": law,
            "base_seed": self.seed,
            "law_seed": self._law_seeds[law],
            "seed_parameter_sha256": _sha256_bytes(_canonical(self._seed_parameters[law])),
            "fixed_parameter_sha256": _sha256_bytes(_canonical(fixed)),
            "fixed_parameters": fixed,
            "parameter_status": self._specs[law].get("parameter_status", "unspecified_offline"),
            "tuned_parameters": list(_TUNED),
            "provisional_search_bounds": {
                "m": list(M),
                "mu_relative_to_config_seed": list(MU_RELATIVE),
                "g_relative_to_config_seed": list(G_RELATIVE),
            },
            "selection": "nominal_feasible_then_caller_supplied_disturbed_objective",
            "training_only": True,
            "holdout_used": False,
            "hardware_qualified": False,
            "phase": phase,
            "backend": backend,
            "bo_used": bo_used,
            "schedule": {
                "total_units": TOTAL_UNITS,
                "initial_sobol_units": INITIAL_UNITS,
                "bayesian_ei_units": EI_UNITS,
                "repeat_incumbent_units": REPEAT_UNITS,
            },
        }

    def _fit_ei(
        self,
        law: str,
        observations: Sequence[_ParsedObservation],
        excluded_keys: set[str],
    ) -> tuple[ContactLawCandidate, float, str]:
        usable = [
            row for row in observations
            if row.status == "completed" and row.nominal_feasible and row.objective is not None
        ]
        if not usable:
            raise ContactBenchmarkTunerError(
                "no completed nominally feasible training objective is available for Bayesian EI"
            )
        try:
            import numpy as np
        except (ImportError, ModuleNotFoundError) as error:
            raise ContactBenchmarkTunerError("numpy is unavailable for Bayesian EI") from error
        pool_points, pool_backend = _unit_points(self._law_seeds[law] + 1009, 512)
        pool: list[ContactLawCandidate] = []
        seen = set(excluded_keys)
        for point in pool_points:
            candidate = self._candidate_from_unit(law, point)
            if candidate.key in seen:
                continue
            pool.append(candidate)
            seen.add(candidate.key)
        if not pool:
            raise ContactBenchmarkTunerError("Bayesian EI candidate pool is exhausted")
        train_x = np.asarray([self._features(law, row.candidate) for row in usable], dtype=float)
        train_y = np.asarray([float(row.objective) for row in usable], dtype=float)
        if train_x.ndim != 2 or train_x.shape[1] != 3 or not np.all(np.isfinite(train_x)):
            raise ContactBenchmarkTunerError("Bayesian EI training features are invalid")
        if not np.all(np.isfinite(train_y)):
            raise ContactBenchmarkTunerError("Bayesian EI training objectives are invalid")
        candidate_x = np.asarray([self._features(law, candidate) for candidate in pool], dtype=float)
        ei = matern52_expected_improvement(train_x, train_y, candidate_x)
        selected_index = max(
            range(len(pool)),
            key=lambda index: (float(ei[index]), tuple(-value for value in candidate_x[index]), pool[index].key),
        )
        return pool[selected_index], float(ei[selected_index]), f"numpy_{pool_backend}_fixed_matern52_ei"

    def propose(
        self,
        law: str,
        observations: Sequence[TrainingObservation | Mapping[str, Any]],
        unit_count: int,
    ) -> TuningProposal:
        """Return the next 0-based unit for ``law``.

        ``unit_count`` is the number of already completed training units for
        this law.  It must equal ``len(observations)``; failed rows therefore
        advance the fixed 24-unit schedule while contributing no objective.
        Holdout rows are rejected at the input boundary.
        """

        selected = self._normalise_law(law)
        if type(unit_count) is not int or not 0 <= unit_count < TOTAL_UNITS:
            raise ContactBenchmarkTunerError("unit_count must be an integer in [0, 23]")
        if isinstance(observations, (str, bytes, bytearray)):
            raise ContactBenchmarkTunerError("observations must be a sequence of training rows")
        try:
            raw_observations = tuple(observations)
        except TypeError as error:
            raise ContactBenchmarkTunerError("observations must be a sequence of training rows") from error
        if len(raw_observations) != unit_count:
            raise ContactBenchmarkTunerError(
                "unit_count must equal the number of completed training observations, including failed units"
            )
        parsed = tuple(self._parse_observation(selected, row) for row in raw_observations)
        initial, initial_backend = self._initial_candidates(selected)
        observed_keys = {row.candidate.key for row in parsed}

        if unit_count < INITIAL_UNITS:
            candidate = initial[unit_count]
            reason = "initial_seed_clamped_to_bounds" if unit_count == 0 else "initial_sobol"
            return TuningProposal(
                law=selected,
                candidate=candidate,
                unit_index=unit_count,
                phase="initial_sobol",
                reason=reason,
                nominal_feasible=None,
                bo_used=False,
                backend=initial_backend,
                bindings=self._bindings(
                    selected,
                    phase="initial_sobol",
                    backend=initial_backend,
                    bo_used=False,
                ),
            )

        if unit_count < INITIAL_UNITS + EI_UNITS:
            excluded = observed_keys | {candidate.key for candidate in initial}
            try:
                candidate, acquisition, backend = self._fit_ei(selected, parsed, excluded)
                reason = "bayesian_expected_improvement_over_nominally_feasible_training"
                bo_used = True
                incumbent_objective = None
            except ContactBenchmarkTunerError as error:
                pool_points, pool_backend = _unit_points(self._law_seeds[selected] + 1009, 512)
                candidate = None
                for point in pool_points:
                    proposed = self._candidate_from_unit(selected, point)
                    if proposed.key not in excluded:
                        candidate = proposed
                        break
                if candidate is None:
                    raise ContactBenchmarkTunerError("deterministic EI fallback candidate pool is exhausted") from error
                acquisition = None
                backend = f"deterministic_space_filling_after_ei_unavailable:{pool_backend}"
                reason = f"bayesian_ei_unavailable:{error}"
                bo_used = False
                incumbent_objective = None
            return TuningProposal(
                law=selected,
                candidate=candidate,
                unit_index=unit_count,
                phase="bayesian_ei",
                reason=reason,
                nominal_feasible=None,
                bo_used=bo_used,
                backend=backend,
                acquisition_value=acquisition,
                incumbent_objective=incumbent_objective,
                bindings=self._bindings(
                    selected,
                    phase="bayesian_ei",
                    backend=backend,
                    bo_used=bo_used,
                ),
            )

        # Select once from the discovery block.  Rows from the repeated-
        # incumbent block cannot change which candidate the final block is
        # repeating, even when a repeat happens to obtain a lower objective.
        discovery_observations = parsed[: INITIAL_UNITS + EI_UNITS]
        usable = [
            row for row in discovery_observations
            if row.status == "completed" and row.nominal_feasible and row.objective is not None
        ]
        if usable:
            incumbent = min(
                usable,
                key=lambda row: (float(row.objective), row.candidate.key),
            )
            candidate = incumbent.candidate
            incumbent_objective = float(incumbent.objective)
            reason = "repeat_observed_nominally_feasible_incumbent"
        else:
            candidate = self.seed_candidate(selected)
            incumbent_objective = None
            reason = "repeat_provisional_seed_no_completed_nominally_feasible_incumbent"
        backend = "fixed_incumbent_replay"
        return TuningProposal(
            law=selected,
            candidate=candidate,
            unit_index=unit_count,
            phase="repeat_incumbent",
            reason=reason,
            nominal_feasible=True if usable else None,
            bo_used=False,
            backend=backend,
            incumbent_objective=incumbent_objective,
            bindings=self._bindings(
                selected,
                phase="repeat_incumbent",
                backend=backend,
                bo_used=False,
            ),
        )


__all__ = [
    "BINDING_SCHEMA",
    "ContactBenchmarkTuner",
    "ContactBenchmarkTunerError",
    "ContactLawCandidate",
    "EI_UNITS",
    "G",
    "INITIAL_UNITS",
    "M",
    "MU_RELATIVE",
    "PUBLIC_LAWS",
    "REPEAT_UNITS",
    "TOTAL_UNITS",
    "TrainingObservation",
    "TUNER_SCHEMA",
    "TuningProposal",
    "matern52_expected_improvement",
]
