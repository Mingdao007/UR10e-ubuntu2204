"""Offline three-method proposer for the full-task yield-fair campaign.

This module proposes SFC / DSFC / MSFC candidates only.  It does not run the
simulator, define the scalar objective, own a ledger, or freeze the rest of
the campaign.  Each method receives 24 units: eight shared mechanical
initial triples, twelve Bayesian expected-improvement points, and four
repeats of the discovery incumbent.  A unit is one sealed nominal/disturbed
pair.  The caller supplies ``objective`` and ``nominal_feasible``.

Only ``m``, ``mu`` and ``g`` are free.  Complete native parameters, including
active MSFC memory and the FT-v1 solver defaults, are bound from
``config/yield_fair_tuning_v1.json``.  Search bounds are offline ranges, not
hardware safety limits.  This is not the six-law 256-tick proxy tuner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any

from contact_benchmark_tuner import (
    ContactBenchmarkTunerError,
    _canonical,
    _finite as _benchmark_finite,
    _freeze,
    _sha256_bytes,
    _thaw,
    _unit_points,
    matern52_expected_improvement,
)


TUNER_SCHEMA = "ur10e.yield-fair-tuner-v1"
BINDING_SCHEMA = "ur10e.yield-fair-tuner-binding-v1"
CANDIDATE_SCHEMA = "ur10e.yield-fair-candidate-v1"
CONFIG_SCHEMA = "ur10e.yield-fair-tuning-v1"
METHODS = ("SFC", "DSFC", "MSFC")
TOTAL_UNITS = 24
INITIAL_UNITS = 8
EI_UNITS = 12
REPEAT_UNITS = 4
INITIAL_SOBOL_COUNT = 5
TUNED = ("m", "mu", "g")
STATUSES = frozenset(("completed", "failed"))
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "yield_fair_tuning_v1.json"
SHARED_SEED = 20260920
ACQUISITION_SEED_OFFSET = 1009
ACQUISITION_POOL_SIZE = 512
M_BOUNDS = (4.0, 16.0)
MU_BOUNDS = (40.0, 2500.0)
G_BOUNDS = (0.005, 0.2)
G50 = (4.0, 1228.1391393367705, 0.08583341909109758)
FT_V1_SEED_TRIPLES = {
    "SFC": (4.0, 393.0, 0.052126826121414976),
    "DSFC": (4.0, 310.66177089084647, 0.0642),
    "MSFC": G50,
}
MSFC_ACTIVE_METRIC_FLOOR = 0.026926929041248004
MSFC_IDENTITY_METRIC = 1.0
NATIVE_PARAMETER_ORDER = {
    "SFC": ("m", "mu", "n", "g"),
    "DSFC": (
        "m",
        "g",
        "p",
        "a",
        "n",
        "mu",
        "max_iterations",
        "residual_tolerance_n",
        "relative_radius_tolerance",
    ),
    "MSFC": (
        "m",
        "g",
        "p",
        "a",
        "n",
        "mu",
        "force_scale_n",
        "tau_force_s",
        "tau_recovery_s",
        "kappa_per_n2_s",
        "max_iterations",
        "residual_tolerance_n",
        "structure_tolerance",
        "minimum_metric_eigenvalue",
    ),
}
FT_V1_FIXED = {
    "SFC": {"n": 3.0},
    "DSFC": {
        "p": 0.5,
        "a": 0.05,
        "n": 3.0,
        "max_iterations": 32.0,
        "residual_tolerance_n": 1e-09,
        "relative_radius_tolerance": 1e-10,
    },
    "MSFC": {
        "p": 0.5,
        "a": 0.05,
        "n": 3.0,
        "force_scale_n": 2.1095928078033372,
        "tau_force_s": 0.19236795686399732,
        "tau_recovery_s": 0.20212720757006053,
        "kappa_per_n2_s": 0.6334129092862626,
        "max_iterations": 32.0,
        "residual_tolerance_n": 1e-09,
        "structure_tolerance": 1e-12,
        "minimum_metric_eigenvalue": MSFC_ACTIVE_METRIC_FLOOR,
    },
}


class YieldContactTunerError(ValueError):
    """Invalid yield-fair training input or an unavailable offline proposal."""


def _finite(value: Any, role: str) -> float:
    try:
        return _benchmark_finite(value, role)
    except ContactBenchmarkTunerError as error:
        raise YieldContactTunerError(str(error)) from error


def _positive(value: Any, role: str) -> float:
    parsed = _finite(value, role)
    if parsed <= 0.0:
        raise YieldContactTunerError(f"{role} must be positive")
    return parsed


def _nonempty_id(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise YieldContactTunerError(f"{role} must be a nonempty string")
    return value


def _mechanical_key(m: float, mu: float, g: float) -> str:
    return _sha256_bytes(_canonical({"m": float(m), "mu": float(mu), "g": float(g)}))


def _require_sobol(seed: int, count: int) -> tuple[tuple[tuple[float, ...], ...], str]:
    try:
        points, backend = _unit_points(seed, count)
    except ContactBenchmarkTunerError as error:
        raise YieldContactTunerError(f"SciPy Sobol is unavailable: {error}") from error
    if backend != "scipy_sobol":
        raise YieldContactTunerError("SciPy Sobol is unavailable")
    return points, backend


def _in_bounds(value: float, bounds: tuple[float, float]) -> bool:
    return bounds[0] <= value <= bounds[1]


def _from_unit(point: Sequence[float]) -> tuple[float, float, float]:
    if len(point) != 3:
        raise YieldContactTunerError("unit point must have three coordinates")
    clipped = tuple(min(1.0, max(0.0, _finite(value, "unit coordinate"))) for value in point)
    m = M_BOUNDS[0] + clipped[0] * (M_BOUNDS[1] - M_BOUNDS[0])
    mu = math.exp(math.log(MU_BOUNDS[0]) + clipped[1] * (math.log(MU_BOUNDS[1]) - math.log(MU_BOUNDS[0])))
    g = math.exp(math.log(G_BOUNDS[0]) + clipped[2] * (math.log(G_BOUNDS[1]) - math.log(G_BOUNDS[0])))
    return float(m), float(mu), float(g)


def _to_unit(m: float, mu: float, g: float) -> tuple[float, float, float]:
    return (
        (m - M_BOUNDS[0]) / (M_BOUNDS[1] - M_BOUNDS[0]),
        (math.log(mu) - math.log(MU_BOUNDS[0])) / (math.log(MU_BOUNDS[1]) - math.log(MU_BOUNDS[0])),
        (math.log(g) - math.log(G_BOUNDS[0])) / (math.log(G_BOUNDS[1]) - math.log(G_BOUNDS[0])),
    )


@dataclass(frozen=True)
class YieldMethodCandidate:
    """One complete native parameter set with only ``m``, ``mu`` and ``g`` free."""

    method: str
    m: float
    mu: float
    g: float
    fixed_parameters: tuple[tuple[str, float], ...] = ()
    config_sha256: str = ""

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise YieldContactTunerError(f"unknown yield method {self.method!r}")
        if not isinstance(self.config_sha256, str) or not self.config_sha256:
            raise YieldContactTunerError("candidate config_sha256 is missing")
        object.__setattr__(self, "m", _positive(self.m, f"{self.method} m"))
        object.__setattr__(self, "mu", _positive(self.mu, f"{self.method} mu"))
        object.__setattr__(self, "g", _positive(self.g, f"{self.method} g"))
        names: set[str] = set()
        fixed: list[tuple[str, float]] = []
        try:
            raw_fixed = tuple(self.fixed_parameters)
        except TypeError as error:
            raise YieldContactTunerError(f"{self.method} fixed parameters are invalid") from error
        for item in raw_fixed:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise YieldContactTunerError(f"{self.method} fixed parameters are invalid")
            name, value = item
            if not isinstance(name, str) or name in names or name in TUNED:
                raise YieldContactTunerError(f"{self.method} fixed parameter names are invalid")
            names.add(name)
            fixed.append((name, _positive(value, f"{self.method} {name}")))
        expected_fixed = {name for name in NATIVE_PARAMETER_ORDER[self.method] if name not in TUNED}
        if names != expected_fixed:
            raise YieldContactTunerError(
                f"{self.method} fixed parameter fields differ from its native parameter order"
            )
        fixed_by_name = dict(fixed)
        object.__setattr__(
            self,
            "fixed_parameters",
            tuple(
                (name, fixed_by_name[name])
                for name in NATIVE_PARAMETER_ORDER[self.method]
                if name not in TUNED
            ),
        )

    @property
    def parameters(self) -> dict[str, float]:
        result = {"m": float(self.m), "mu": float(self.mu), "g": float(self.g)}
        result.update({name: float(value) for name, value in self.fixed_parameters})
        return {name: result[name] for name in NATIVE_PARAMETER_ORDER[self.method]}

    @property
    def key(self) -> str:
        return _sha256_bytes(
            _canonical(
                {
                    "schema": CANDIDATE_SCHEMA,
                    "config_sha256": self.config_sha256,
                    "method": self.method,
                    "parameters": self.parameters,
                }
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {"method": self.method, **self.parameters}


@dataclass(frozen=True)
class YieldTrainingObservation:
    """Caller-supplied sealed pair row; failed rows carry no objective."""

    method: str
    candidate: YieldMethodCandidate | Mapping[str, Any]
    status: str
    nominal_feasible: bool
    unit_index: int
    training_cell_id: str
    selection_contract_id: str
    pair_complete: bool
    split: str
    objective: float | None = None
    pair: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class YieldTuningProposal:
    """Next-unit proposal and the binding needed to audit it."""

    method: str
    candidate: YieldMethodCandidate
    unit_index: int
    phase: str
    reason: str
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
            raise YieldContactTunerError("tuning proposal schema/version differs")
        if self.method not in METHODS or self.candidate.method != self.method:
            raise YieldContactTunerError("proposal method/candidate method differs")
        if type(self.unit_index) is not int or not 0 <= self.unit_index < TOTAL_UNITS:
            raise YieldContactTunerError("proposal unit_index must be in [0, 23]")
        if self.phase not in {"initial", "bayesian_ei", "repeat_incumbent"}:
            raise YieldContactTunerError("proposal phase is unsupported")
        if self.phase == "bayesian_ei":
            if self.bo_used is not True or self.acquisition_value is None:
                raise YieldContactTunerError("bayesian_ei proposals must report an actual EI value")
        elif self.bo_used:
            raise YieldContactTunerError("non-BO proposals cannot set bo_used")
        if not isinstance(self.reason, str) or not self.reason:
            raise YieldContactTunerError("proposal reason is missing")
        if self.nominal_feasible is not None and type(self.nominal_feasible) is not bool:
            raise YieldContactTunerError("proposal nominal_feasible must be bool or None")
        if type(self.bo_used) is not bool:
            raise YieldContactTunerError("proposal BO flag must be bool")
        if self.acquisition_value is not None:
            _finite(self.acquisition_value, "proposal acquisition_value")
        if self.incumbent_objective is not None:
            _finite(self.incumbent_objective, "proposal incumbent_objective")
        object.__setattr__(self, "bindings", _freeze(dict(self.bindings)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "method": self.method,
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


@dataclass(frozen=True)
class _ParsedObservation:
    candidate: YieldMethodCandidate
    status: str
    nominal_feasible: bool
    objective: float | None
    split: str
    unit_index: int
    training_cell_id: str
    selection_contract_id: str
    pair_complete: bool


class YieldContactTuner:
    """Deterministic offline SFC/DSFC/MSFC candidate proposer."""

    def __init__(
        self,
        config_path: Path | str = DEFAULT_CONFIG_PATH,
        *,
        training_cell_id: str,
        selection_contract_id: str,
    ) -> None:
        self.training_cell_id = _nonempty_id(training_cell_id, "training_cell_id")
        self.selection_contract_id = _nonempty_id(selection_contract_id, "selection_contract_id")
        supplied_config_path = Path(config_path).expanduser()
        if supplied_config_path.is_symlink():
            raise YieldContactTunerError(f"tuner config must not be a symlink: {supplied_config_path}")
        self.config_path = supplied_config_path.resolve()
        if not self.config_path.is_file():
            raise YieldContactTunerError(f"tuner config is not a regular file: {self.config_path}")
        try:
            raw_bytes = self.config_path.read_bytes()
            root = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise YieldContactTunerError(f"tuner config is unreadable: {error}") from error
        self._root = self._validate_config(root)
        self.config_sha256 = _sha256_bytes(raw_bytes)
        self.seed = SHARED_SEED
        self._seed_parameters: dict[str, dict[str, float]] = {}
        self._fixed_parameters: dict[str, tuple[tuple[str, float], ...]] = {}
        specs = self._root["methods_spec"]
        for method in METHODS:
            spec = specs[method]
            seed_parameters = {
                name: _positive(spec["seed_parameters"][name], f"{method} seed {name}")
                for name in NATIVE_PARAMETER_ORDER[method]
            }
            if tuple(seed_parameters[name] for name in TUNED) != FT_V1_SEED_TRIPLES[method]:
                raise YieldContactTunerError(f'{method} initial seed drifted from FT-v1')
            fixed = tuple(
                (name, seed_parameters[name]) for name in NATIVE_PARAMETER_ORDER[method] if name not in TUNED
            )
            expected_fixed = FT_V1_FIXED[method]
            actual_fixed = dict(fixed)
            if actual_fixed != expected_fixed:
                raise YieldContactTunerError(f"{method} fixed parameters drifted from FT-v1")
            if method == "DSFC" and (actual_fixed["a"] != 0.05 or actual_fixed["p"] != 0.5):
                raise YieldContactTunerError("DSFC must not inherit legacy tuner a=1.2, p=0.1")
            if method == "MSFC":
                metric = actual_fixed["minimum_metric_eigenvalue"]
                if metric == MSFC_IDENTITY_METRIC:
                    raise YieldContactTunerError("MSFC must remain active; identity metric is not accepted")
                if metric != MSFC_ACTIVE_METRIC_FLOOR:
                    raise YieldContactTunerError("MSFC metric floor drifted from FT-v1")
            self._seed_parameters[method] = seed_parameters
            self._fixed_parameters[method] = fixed
        self._mechanical_triples, self._initial_backend = self._build_shared_triples()
        if G50 not in self._mechanical_triples:
            raise YieldContactTunerError("shared initial triples must include the g50 point")

    def _validate_config(self, root: Any) -> dict[str, Any]:
        if not isinstance(root, dict):
            raise YieldContactTunerError("tuner config must be a JSON object")
        if root.get("schema") != CONFIG_SCHEMA or root.get("version") != 1:
            raise YieldContactTunerError("tuner config schema/version differs")
        if root.get("offline_only") is not True or root.get("hardware_qualified") is not False:
            raise YieldContactTunerError("tuner config is not explicitly offline and unqualified")
        if root.get("endpoint") != "none":
            raise YieldContactTunerError("tuner config endpoint must be none")
        if root.get("does_not_freeze_campaign") is not True or root.get("objective_definition") is not None:
            raise YieldContactTunerError("tuner config must not claim to freeze the campaign or objective")
        if tuple(root.get("methods", ())) != METHODS:
            raise YieldContactTunerError("tuner config methods must be exactly SFC, DSFC, MSFC")
        roles = {method: 'baseline' if method == 'SFC' else 'proposal' for method in METHODS}
        if root.get('method_roles') != roles or root.get('tuned_parameters') != list(TUNED):
            raise YieldContactTunerError('tuner config method roles or tuned parameters drifted')
        expected_schedule = {
            'total_units': TOTAL_UNITS, 'initial_units': INITIAL_UNITS,
            'bayesian_ei_units': EI_UNITS, 'repeat_incumbent_units': REPEAT_UNITS,
            'unit_definition': 'one_nominal_disturbed_pair',
            'failed_unit_consumes_ordinal': True, 'holdout_used_for_tuning': False,
            'repeat_rows_cannot_change_incumbent': True,
            'no_qualified_incumbent_if_no_feasible_discovery': True,
        }
        if root.get('schedule') != expected_schedule:
            raise YieldContactTunerError('tuner config schedule drifted')
        task = root.get('task_binding', {})
        if (task.get('normal_force_n') != 5.0
                or task.get('duration_s') != 2.0 * math.pi / 0.1
                or task.get('unknown_surface') is not True
                or task.get('attitude_compliant') is not True):
            raise YieldContactTunerError('tuner config task binding drifted')
        bounds = root.get("search_bounds")
        if not isinstance(bounds, Mapping):
            raise YieldContactTunerError("tuner config search_bounds are missing")
        for name, expected in (("m", M_BOUNDS), ("mu", MU_BOUNDS), ("g", G_BOUNDS)):
            item = bounds.get(name)
            if not isinstance(item, Mapping):
                raise YieldContactTunerError(f"tuner config {name} bounds are missing")
            low = _positive(item.get("low"), f"{name} bound low")
            high = _positive(item.get("high"), f"{name} bound high")
            if (low, high) != expected:
                raise YieldContactTunerError(f"tuner config {name} bounds drifted")
            if item.get('scale') != ('linear' if name == 'm' else 'log'):
                raise YieldContactTunerError(f'tuner config {name} scale drifted')
        g50 = root.get("included_points", {}).get("g50") if isinstance(root.get("included_points"), Mapping) else None
        if not isinstance(g50, Mapping):
            raise YieldContactTunerError("tuner config must include the g50 point")
        g50_triple = (
            _positive(g50.get("m"), "g50 m"),
            _positive(g50.get("mu"), "g50 mu"),
            _positive(g50.get("g"), "g50 g"),
        )
        if g50_triple != G50:
            raise YieldContactTunerError("tuner config g50 point drifted")
        if not _in_bounds(G50[0], M_BOUNDS) or not _in_bounds(G50[1], MU_BOUNDS) or not _in_bounds(G50[2], G_BOUNDS):
            raise YieldContactTunerError("g50 point is outside offline search bounds")
        initial = root.get("initial_design")
        if not isinstance(initial, Mapping) or initial.get("seed") != SHARED_SEED:
            raise YieldContactTunerError("tuner config initial seed must be 20260920")
        if initial.get("shared_mechanical_triples") is not True:
            raise YieldContactTunerError("tuner config must share mechanical triples across methods")
        if initial.get("no_method_specific_random_pools") is not True:
            raise YieldContactTunerError("tuner config must not use method-specific random pools")
        acquisition = root.get("acquisition")
        if (
            not isinstance(acquisition, Mapping)
            or acquisition.get("seed") != SHARED_SEED
            or acquisition.get("shared_across_methods") is not True
            or acquisition.get("require_scipy_sobol") is not True
            or acquisition.get("space_filling_fallback_is_not_bayesian_ei") is not True
            or acquisition.get('method') != 'bayesian_expected_improvement'
            or acquisition.get('seed_offset') != ACQUISITION_SEED_OFFSET
            or acquisition.get('pool_size') != ACQUISITION_POOL_SIZE
        ):
            raise YieldContactTunerError("tuner config acquisition contract drifted")
        msfc = root.get("msfc")
        if (
            not isinstance(msfc, Mapping)
            or msfc.get("mode") != "active"
            or _finite(msfc.get("minimum_metric_eigenvalue"), "msfc metric") != MSFC_ACTIVE_METRIC_FLOOR
        ):
            raise YieldContactTunerError("tuner config MSFC must remain active at the FT-v1 metric floor")
        specs = root.get("methods_spec")
        orders = root.get("parameter_order")
        if not isinstance(specs, Mapping) or set(specs) != set(METHODS):
            raise YieldContactTunerError("tuner config methods_spec must contain SFC, DSFC and MSFC")
        if not isinstance(orders, Mapping):
            raise YieldContactTunerError("tuner config parameter_order is missing")
        for method in METHODS:
            spec = specs[method]
            if not isinstance(spec, Mapping):
                raise YieldContactTunerError(f"tuner config {method} spec is missing")
            if spec.get('native_law') != method or spec.get('role') != roles[method]:
                raise YieldContactTunerError(f'tuner config {method} native role drifted')
            order = tuple(spec.get("parameter_order") or ())
            if order != NATIVE_PARAMETER_ORDER[method] or tuple(orders.get(method) or ()) != order:
                raise YieldContactTunerError(f"tuner config {method} parameter order drifted")
            seed_parameters = spec.get("seed_parameters")
            fixed_parameters = spec.get("fixed_parameters")
            if not isinstance(seed_parameters, Mapping) or set(seed_parameters) != set(order):
                raise YieldContactTunerError(f"tuner config {method} seed parameters are incomplete")
            if not isinstance(fixed_parameters, Mapping):
                raise YieldContactTunerError(f"tuner config {method} fixed parameters are missing")
            expected_fixed = {name for name in order if name not in TUNED}
            if set(fixed_parameters) != expected_fixed:
                raise YieldContactTunerError(f"tuner config {method} fixed parameter fields differ")
            for name in order:
                _positive(seed_parameters[name], f"{method} seed {name}")
            for name in expected_fixed:
                if _positive(fixed_parameters[name], f"{method} fixed {name}") != _positive(
                    seed_parameters[name], f"{method} seed {name}"
                ):
                    raise YieldContactTunerError(f"{method} fixed parameter {name} drifted from its seed")
        return root

    def _build_shared_triples(self) -> tuple[tuple[tuple[float, float, float], ...], str]:
        seeds = []
        seen: set[str] = set()
        for method in METHODS:
            parameters = self._seed_parameters[method]
            triple = (float(parameters["m"]), float(parameters["mu"]), float(parameters["g"]))
            if not self._bounds_admit(*triple):
                raise YieldContactTunerError(f"{method} FT-v1 seed triple is outside offline search bounds")
            key = _mechanical_key(*triple)
            if key in seen:
                raise YieldContactTunerError("FT-v1 seed triples must be unique")
            seen.add(key)
            seeds.append(triple)
        for count in (8, 16, 32):
            points, backend = _require_sobol(self.seed, count)
            extra = []
            extra_seen = set(seen)
            for point in points:
                triple = _from_unit(point)
                if not self._bounds_admit(*triple):
                    raise YieldContactTunerError("Sobol mechanical triple is outside offline search bounds")
                key = _mechanical_key(*triple)
                if key in extra_seen:
                    continue
                extra.append(triple)
                extra_seen.add(key)
                if len(extra) == INITIAL_SOBOL_COUNT:
                    return tuple(seeds + extra), backend
        raise YieldContactTunerError("shared Sobol lane could not produce five unique mechanical triples")

    def _bounds_admit(self, m: float, mu: float, g: float) -> bool:
        return _in_bounds(m, M_BOUNDS) and _in_bounds(mu, MU_BOUNDS) and _in_bounds(g, G_BOUNDS)

    @property
    def bounds(self) -> Mapping[str, tuple[float, float]]:
        return MappingProxyType({"m": M_BOUNDS, "mu": MU_BOUNDS, "g": G_BOUNDS})

    def shared_mechanical_triples(self) -> tuple[tuple[float, float, float], ...]:
        return self._mechanical_triples

    def initial_candidates(self, method: str) -> tuple[YieldMethodCandidate, ...]:
        selected = self._normalise_method(method)
        return tuple(self._make_candidate(selected, *triple) for triple in self._mechanical_triples)

    def seed_candidate(self, method: str) -> YieldMethodCandidate:
        selected = self._normalise_method(method)
        seed = self._seed_parameters[selected]
        return self._make_candidate(selected, seed["m"], seed["mu"], seed["g"])

    def _normalise_method(self, method: str) -> str:
        if not isinstance(method, str) or method not in METHODS:
            raise YieldContactTunerError(
                f"method must be one of {', '.join(METHODS)}; six-law proxy labels are unavailable"
            )
        return method

    def _make_candidate(self, method: str, m: float, mu: float, g: float) -> YieldMethodCandidate:
        if not self._bounds_admit(m, mu, g):
            raise YieldContactTunerError("candidate is outside offline search bounds")
        return YieldMethodCandidate(
            method=method,
            m=float(m),
            mu=float(mu),
            g=float(g),
            fixed_parameters=self._fixed_parameters[method],
            config_sha256=self.config_sha256,
        )

    def _parse_candidate(
        self,
        method: str,
        value: YieldMethodCandidate | Mapping[str, Any],
    ) -> YieldMethodCandidate:
        if isinstance(value, YieldMethodCandidate):
            if value.method != method:
                raise YieldContactTunerError("observation candidate method differs")
            if value.config_sha256 != self.config_sha256:
                raise YieldContactTunerError("observation candidate config_sha256 differs")
            payload: Mapping[str, Any] = value.as_dict()
        elif isinstance(value, Mapping):
            payload = value
        else:
            raise YieldContactTunerError("observation candidate must be an object")
        embedded_method = payload.get("method", payload.get("law"))
        if embedded_method is not None and embedded_method != method:
            raise YieldContactTunerError("observation candidate method differs")
        if "parameters" in payload:
            extra_metadata = set(payload) - {"method", "law", "parameters"}
            if extra_metadata:
                raise YieldContactTunerError(
                    f"observation candidate metadata fields are unsupported: {sorted(extra_metadata)}"
                )
            nested = payload["parameters"]
            if not isinstance(nested, Mapping):
                raise YieldContactTunerError("observation candidate parameters must be an object")
            candidate_values = dict(nested)
        else:
            candidate_values = {
                str(name): raw for name, raw in payload.items() if name not in {"method", "law"}
            }
        expected_names = set(NATIVE_PARAMETER_ORDER[method])
        if set(candidate_values) != expected_names:
            missing = sorted(expected_names - set(candidate_values))
            extra = sorted(set(candidate_values) - expected_names)
            raise YieldContactTunerError(
                f"{method} observation candidate fields differ (missing={missing}, extra={extra})"
            )
        for name, expected in self._fixed_parameters[method]:
            actual = _positive(candidate_values[name], f"{method} fixed {name}")
            if actual != expected:
                raise YieldContactTunerError(f"{method} fixed parameter {name} differs from FT-v1 config")
        return self._make_candidate(
            method,
            _positive(candidate_values["m"], f"{method} m"),
            _positive(candidate_values["mu"], f"{method} mu"),
            _positive(candidate_values["g"], f"{method} g"),
        )

    def _parse_pair(self, pair: Any, summary_status: str) -> None:
        if pair is None:
            return
        if not isinstance(pair, Mapping) or set(pair) != {"nominal", "disturbed"}:
            raise YieldContactTunerError("incomplete pair rows are not accepted")
        for condition in ("nominal", "disturbed"):
            side = pair[condition]
            if isinstance(side, Mapping):
                status = side.get("status")
            else:
                status = side
            if status not in {'complete', 'completed', 'failed', 'censored', 'interrupted'}:
                raise YieldContactTunerError("incomplete pair rows are not accepted")
            if summary_status == 'completed' and status not in {'complete', 'completed'}:
                raise YieldContactTunerError('completed pair summary contradicts terminal member status')

    def _parse_observation(
        self,
        method: str,
        value: YieldTrainingObservation | Mapping[str, Any],
        expected_unit_index: int,
    ) -> _ParsedObservation:
        if isinstance(value, YieldTrainingObservation):
            row_method = value.method
            candidate_value = value.candidate
            status = value.status
            nominal_feasible = value.nominal_feasible
            objective = value.objective
            split = value.split
            unit_index = value.unit_index
            training_cell_id = value.training_cell_id
            selection_contract_id = value.selection_contract_id
            pair_complete = value.pair_complete
            pair = value.pair
        elif isinstance(value, Mapping):
            row_method = value.get("method", value.get("law", method))
            candidate_value = value.get("candidate")
            status = value.get("status")
            nominal_feasible = value.get("nominal_feasible")
            objective = value.get("objective")
            split = value.get("split")
            unit_index = value.get("unit_index")
            training_cell_id = value.get("training_cell_id")
            selection_contract_id = value.get("selection_contract_id")
            pair_complete = value.get("pair_complete")
            pair = value.get("pair")
        else:
            raise YieldContactTunerError("training observation must be an object")
        if row_method != method:
            raise YieldContactTunerError("training observation method differs")
        if split != "training":
            raise YieldContactTunerError("holdout or non-training observations are not accepted")
        if training_cell_id != self.training_cell_id:
            raise YieldContactTunerError("training_cell_id differs from the proposer contract")
        if selection_contract_id != self.selection_contract_id:
            raise YieldContactTunerError("selection_contract_id differs from the proposer contract")
        if pair_complete is not True:
            raise YieldContactTunerError("incomplete pair rows are not accepted")
        self._parse_pair(pair, status)
        if type(unit_index) is not int:
            raise YieldContactTunerError("training observation unit_index must be an integer")
        if unit_index != expected_unit_index:
            raise YieldContactTunerError(
                "training history unit_index must be sequential without gaps, duplicates, or reordering"
            )
        if status not in STATUSES:
            raise YieldContactTunerError("training observation status must be completed or failed")
        if type(nominal_feasible) is not bool:
            raise YieldContactTunerError("training observation nominal_feasible must be bool")
        candidate = self._parse_candidate(method, candidate_value)
        if status == "failed":
            if objective is not None:
                raise YieldContactTunerError("failed training observation cannot carry an objective")
            parsed_objective = None
        else:
            if objective is None:
                raise YieldContactTunerError("completed training observation requires caller objective")
            parsed_objective = _finite(objective, "training scalar objective")
            if parsed_objective < 0.0:
                raise YieldContactTunerError("training scalar objective cannot be negative")
        return _ParsedObservation(
            candidate,
            status,
            nominal_feasible,
            parsed_objective,
            split,
            unit_index,
            training_cell_id,
            selection_contract_id,
            pair_complete,
        )

    def _bindings(
        self,
        method: str,
        *,
        phase: str,
        backend: str,
        bo_used: bool,
    ) -> dict[str, Any]:
        fixed = dict(self._fixed_parameters[method])
        return {
            "schema": BINDING_SCHEMA,
            "version": 1,
            "config_path": str(self.config_path),
            "config_sha256": self.config_sha256,
            "method": method,
            "role": self._root["method_roles"][method],
            "training_cell_id": self.training_cell_id,
            "selection_contract_id": self.selection_contract_id,
            "shared_seed": self.seed,
            "fixed_parameter_sha256": _sha256_bytes(_canonical(fixed)),
            "fixed_parameters": fixed,
            "tuned_parameters": list(TUNED),
            "search_bounds": {
                "kind": "offline_search_ranges_not_hardware_safety_limits",
                "m": list(M_BOUNDS),
                "mu": list(MU_BOUNDS),
                "g": list(G_BOUNDS),
            },
            "included_point_g50": {"m": G50[0], "mu": G50[1], "g": G50[2]},
            "msfc_mode": "active" if method == "MSFC" else None,
            "msfc_minimum_metric_eigenvalue": (
                MSFC_ACTIVE_METRIC_FLOOR if method == "MSFC" else None
            ),
            "selection": "nominal_feasible_then_caller_supplied_scalar_objective",
            "training_only": True,
            "holdout_used": False,
            "hardware_qualified": False,
            "does_not_freeze_campaign": True,
            "unit_definition": "one_nominal_disturbed_pair",
            "pair_completion_required": True,
            "phase": phase,
            "backend": backend,
            "bo_used": bo_used,
            "schedule": {
                "total_units": TOTAL_UNITS,
                "initial_units": INITIAL_UNITS,
                "bayesian_ei_units": EI_UNITS,
                "repeat_incumbent_units": REPEAT_UNITS,
            },
        }

    def _fit_ei(
        self,
        method: str,
        observations: Sequence[_ParsedObservation],
        excluded_keys: set[str],
    ) -> tuple[YieldMethodCandidate, float, str]:
        usable = [
            row
            for row in observations
            if row.status == "completed" and row.nominal_feasible and row.objective is not None
        ]
        if not usable:
            raise YieldContactTunerError(
                "Bayesian EI unavailable: no completed nominally feasible training objective is available"
            )
        try:
            import numpy as np
        except (ImportError, ModuleNotFoundError) as error:
            raise YieldContactTunerError("Bayesian EI unavailable: numpy is unavailable") from error
        pool_points, pool_backend = _require_sobol(self.seed + ACQUISITION_SEED_OFFSET, ACQUISITION_POOL_SIZE)
        pool: list[YieldMethodCandidate] = []
        seen = set(excluded_keys)
        for point in pool_points:
            candidate = self._make_candidate(method, *_from_unit(point))
            if candidate.key in seen:
                continue
            pool.append(candidate)
            seen.add(candidate.key)
        if not pool:
            raise YieldContactTunerError("Bayesian EI unavailable: candidate pool is exhausted")
        train_x = np.asarray([_to_unit(row.candidate.m, row.candidate.mu, row.candidate.g) for row in usable], dtype=float)
        train_y = np.asarray([float(row.objective) for row in usable], dtype=float)
        candidate_x = np.asarray(
            [_to_unit(candidate.m, candidate.mu, candidate.g) for candidate in pool],
            dtype=float,
        )
        try:
            ei = matern52_expected_improvement(train_x, train_y, candidate_x)
        except ContactBenchmarkTunerError as error:
            raise YieldContactTunerError(f"Bayesian EI unavailable: {error}") from error
        selected_index = max(
            range(len(pool)),
            key=lambda index: (
                float(ei[index]),
                tuple(-value for value in candidate_x[index]),
                pool[index].key,
            ),
        )
        return pool[selected_index], float(ei[selected_index]), f"numpy_{pool_backend}_fixed_matern52_ei"

    def propose(
        self,
        method: str,
        observations: Sequence[YieldTrainingObservation | Mapping[str, Any]],
        unit_count: int,
    ) -> YieldTuningProposal:
        """Return the next 0-based unit for ``method``.

        ``unit_count`` is the number of already completed sealed pair rows for
        this method.  It must equal ``len(observations)``.  Failed rows advance
        the 24-unit schedule and never fabricate an objective.  Dry proposals
        do not consume a unit.  Holdout rows, mixed cells/contracts, incomplete
        pairs, and ordinal gaps/duplicates/reorders are rejected.
        """

        selected = self._normalise_method(method)
        if type(unit_count) is not int or not 0 <= unit_count < TOTAL_UNITS:
            raise YieldContactTunerError("unit_count must be an integer in [0, 23]")
        if isinstance(observations, (str, bytes, bytearray)):
            raise YieldContactTunerError("observations must be a sequence of training rows")
        try:
            raw_observations = tuple(observations)
        except TypeError as error:
            raise YieldContactTunerError("observations must be a sequence of training rows") from error
        if len(raw_observations) != unit_count:
            raise YieldContactTunerError(
                "unit_count must equal the number of completed training observations, including failed units"
            )
        parsed = tuple(
            self._parse_observation(selected, row, expected_unit_index=index)
            for index, row in enumerate(raw_observations)
        )
        initial = self.initial_candidates(selected)
        observed_keys = {row.candidate.key for row in parsed}

        if unit_count < INITIAL_UNITS:
            candidate = initial[unit_count]
            if unit_count < 3:
                reason = f"initial_ft_v1_seed_triple_{METHODS[unit_count]}"
            else:
                reason = "initial_shared_scrambled_sobol"
            return YieldTuningProposal(
                method=selected,
                candidate=candidate,
                unit_index=unit_count,
                phase="initial",
                reason=reason,
                nominal_feasible=None,
                bo_used=False,
                backend=self._initial_backend,
                bindings=self._bindings(
                    selected,
                    phase="initial",
                    backend=self._initial_backend,
                    bo_used=False,
                ),
            )

        if unit_count < INITIAL_UNITS + EI_UNITS:
            excluded = observed_keys | {candidate.key for candidate in initial}
            candidate, acquisition, backend = self._fit_ei(selected, parsed, excluded)
            return YieldTuningProposal(
                method=selected,
                candidate=candidate,
                unit_index=unit_count,
                phase="bayesian_ei",
                reason="bayesian_expected_improvement_over_nominally_feasible_training",
                nominal_feasible=None,
                bo_used=True,
                backend=backend,
                acquisition_value=acquisition,
                bindings=self._bindings(
                    selected,
                    phase="bayesian_ei",
                    backend=backend,
                    bo_used=True,
                ),
            )

        discovery_observations = parsed[: INITIAL_UNITS + EI_UNITS]
        usable = [
            row
            for row in discovery_observations
            if row.status == "completed" and row.nominal_feasible and row.objective is not None
        ]
        if not usable:
            raise YieldContactTunerError("no qualified incumbent")
        incumbent = min(usable, key=lambda row: (float(row.objective), row.candidate.key))
        backend = "fixed_incumbent_replay"
        return YieldTuningProposal(
            method=selected,
            candidate=incumbent.candidate,
            unit_index=unit_count,
            phase="repeat_incumbent",
            reason="repeat_observed_nominally_feasible_incumbent",
            nominal_feasible=True,
            bo_used=False,
            backend=backend,
            incumbent_objective=float(incumbent.objective),
            bindings=self._bindings(
                selected,
                phase="repeat_incumbent",
                backend=backend,
                bo_used=False,
            ),
        )


__all__ = [
    "BINDING_SCHEMA",
    "CANDIDATE_SCHEMA",
    "CONFIG_SCHEMA",
    "DEFAULT_CONFIG_PATH",
    "EI_UNITS",
    "G50",
    "G_BOUNDS",
    "INITIAL_UNITS",
    "METHODS",
    "MSFC_ACTIVE_METRIC_FLOOR",
    "M_BOUNDS",
    "MU_BOUNDS",
    "NATIVE_PARAMETER_ORDER",
    "REPEAT_UNITS",
    "SHARED_SEED",
    "TOTAL_UNITS",
    "TUNER_SCHEMA",
    "YieldContactTuner",
    "YieldContactTunerError",
    "YieldMethodCandidate",
    "YieldTrainingObservation",
    "YieldTuningProposal",
]
