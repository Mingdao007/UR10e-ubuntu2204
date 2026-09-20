"""Pure holdout evaluator for the reserved simulator validation campaign.

This module does not run the simulator, debit training, or launch validation.
It reuses training full-state parsers and left-sample/hold integration helpers
without relabeling artifacts or monkeypatching the three-method training
evaluator. Nominal bands and disturbed guards are flags only.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
import json
import math

import numpy as np

from contact_yield_metrics import compare_pair, summarize_trial
from contact_yield_protocol import PERIOD_S, parse_scenario
from yield_fair_selection import (
    FULLSTATE_LIMIT,
    OBSERVER_V3,
    YieldFairSelectionError,
    cell_weight,
    expected_path_times,
    expected_phase_counts,
    expected_record_count,
    path_sample_count,
    _finite,
    _load_series,
    _mapping,
    _native_failed,
    _objective_components,
    _positions,
    _recovery_window_covered,
    _require_full_cycle_grid,
    _require_fullstate,
    _vector3,
)


SCHEMA = "ur10e.yield-validation-selection-v1"
RESERVATION_SCHEMA = "yield-validation-reservation-v2"
TRAINING_FREEZE_SCHEMA = "ur10e.yield-fair-campaign-freeze-v1"
ARMS = ("SFC", "DSFC", "MSFC", "MSFC_IDENTITY", "SFC_RADIAL")
ARM_ACTUAL_METHOD = {
    "SFC": "SFC",
    "DSFC": "DSFC",
    "MSFC": "MSFC",
    "MSFC_IDENTITY": "MSFC",
    "SFC_RADIAL": "SFC_RADIAL",
}
ACTUAL_METHODS = ("SFC", "DSFC", "MSFC", "SFC_RADIAL")
PRIOR_DIRECTIONS = ("approach", "along", "across")
PREPARATIONS = ("cold", "warm")
RESOLUTION_SETTINGS = (
    ("base", 0.002, 8),
    ("controller_refinement", 0.001, 4),
    ("plant_refinement", 0.002, 16),
)
MSFC_IDENTITY_METRIC = 1.0
LOAD_ONSET_S = 20.0
ATTITUDE_ONSET_S = 20.0
JLOAD_SCALE_N = 0.5
JPATH_SCALE_M = 0.002
JATT_SCALE_RAD = 0.05
DEFAULT_RESERVATION_PATH = (
    Path(__file__).resolve().parents[1] / "report" / "yield-validation-reservation-v2" / "protocol.json"
)
CLAIM_SCOPE = (
    "prospective simulator validation of reserved cells; no real precise-contact, "
    "safety, physical qualification, or pooled winner claim"
)


class YieldValidationSelectionError(ValueError):
    """Invalid, mismatched, incomplete, or untruthful validation evidence."""


def _bool(value: Any, role: str) -> bool:
    if type(value) is not bool:
        raise YieldValidationSelectionError(f"{role} must be bool")
    return value


def _nonempty(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise YieldValidationSelectionError(f"{role} must be a nonempty string")
    return value


def _wrap_fair(error: YieldFairSelectionError) -> YieldValidationSelectionError:
    return YieldValidationSelectionError(str(error))


def scenario_release_s(scenario: str, *, timeline: str = "full_cycle") -> float:
    spec = parse_scenario(scenario, timeline=timeline)
    if spec["kind"] == "nominal":
        raise YieldValidationSelectionError("nominal scenario has no disturbance release")
    return (
        float(spec["start_s"])
        + float(spec["width_s"])
        + float(spec["hold_s"])
        + float(spec["release_s"])
    )


def load_reservation(path: Path | str = DEFAULT_RESERVATION_PATH) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise YieldValidationSelectionError("reservation must be an object")
    if payload.get("schema") != RESERVATION_SCHEMA:
        raise YieldValidationSelectionError("reservation schema differs")
    if payload.get("status") != "reserved_not_executed":
        raise YieldValidationSelectionError("reservation is not the unexecuted v2 specification")
    if tuple(payload.get("arms") or ()) != ARMS:
        raise YieldValidationSelectionError("reservation arms must be the declared five")
    cells = payload.get("cells")
    if not isinstance(cells, list) or len(cells) != 6:
        raise YieldValidationSelectionError("reservation must declare exactly six cells")
    settings = payload.get("resolution_settings")
    if not isinstance(settings, list) or len(settings) != 3:
        raise YieldValidationSelectionError("reservation must declare exactly three resolution settings")
    expected = {item[0]: (item[1], item[2]) for item in RESOLUTION_SETTINGS}
    for row in settings:
        item = _mapping(row, "resolution setting")
        name = _nonempty(item.get("id"), "resolution id")
        if name not in expected:
            raise YieldValidationSelectionError(f"undeclared resolution {name}")
        dt = _finite(item.get("controller_dt_s"), "controller_dt_s")
        sub = item.get("plant_substeps")
        if (dt, sub) != expected[name]:
            raise YieldValidationSelectionError(f"resolution {name} dt/substeps differ from reservation")
    cost = _mapping(payload.get("cost"), "cost")
    if int(cost.get("maximum_total_trials")) != 180:
        raise YieldValidationSelectionError("reservation maximum_total_trials must remain 180")
    copied = []
    for cell in cells:
        item = _mapping(cell, "cell")
        surface = _mapping(item.get("surface_parameters"), "surface_parameters")
        copied.append({
            "id": _nonempty(item.get("id"), "cell id"),
            "material": _nonempty(item.get("material"), "material"),
            "surface_parameters": {
                "kappa_xx": _finite(surface.get("kappa_xx"), "kappa_xx"),
                "kappa_yy": _finite(surface.get("kappa_yy"), "kappa_yy"),
            },
            "prior_direction": _nonempty(item.get("prior_direction"), "prior_direction"),
            "prior_angle_deg": _finite(item.get("prior_angle_deg"), "prior_angle_deg"),
            "preparation": _nonempty(item.get("preparation"), "preparation"),
            "disturbed_scenario": _nonempty(item.get("disturbed_scenario"), "disturbed_scenario"),
            "nominal_scenario": _nonempty(item.get("nominal_scenario"), "nominal_scenario"),
        })
    if [cell["id"] for cell in copied] != ["V1", "V2", "V3", "V4", "V5", "V6"]:
        raise YieldValidationSelectionError("reservation cell ids must be V1..V6 in order")
    return {
        "schema": RESERVATION_SCHEMA,
        "cells": copied,
        "arms": list(ARMS),
        "resolution_settings": [
            {"id": name, "controller_dt_s": dt, "plant_substeps": sub}
            for name, dt, sub in RESOLUTION_SETTINGS
        ],
        "task": dict(_mapping(payload.get("task"), "task")),
        "observer": dict(_mapping(payload.get("observer"), "observer")),
        "cost": dict(cost),
        "reporting_rules": dict(payload.get("reporting_rules") or {}),
        "raw": dict(payload),
    }


def prior_inward_normal(
    *,
    approach: Sequence[float],
    along: Sequence[float],
    across: Sequence[float],
    direction: str,
    angle_deg: float,
) -> np.ndarray:
    if direction not in PRIOR_DIRECTIONS:
        raise YieldValidationSelectionError("prior_direction must be approach, along, or across")
    try:
        approach_u = np.asarray(approach, dtype=float).reshape(3)
        along_u = np.asarray(along, dtype=float).reshape(3)
        across_u = np.asarray(across, dtype=float).reshape(3)
    except (TypeError, ValueError) as error:
        raise YieldValidationSelectionError("prior basis must be finite 3-vectors") from error
    for name, vector in (("approach", approach_u), ("along", along_u), ("across", across_u)):
        if not np.all(np.isfinite(vector)) or float(np.linalg.norm(vector)) <= 0.0:
            raise YieldValidationSelectionError(f"{name} basis is not a finite nonzero vector")
        vector /= np.linalg.norm(vector)
    if abs(float(np.dot(approach_u, along_u))) > 1e-9:
        raise YieldValidationSelectionError("along must be orthogonal to approach")
    crossed = np.cross(approach_u, along_u)
    crossed /= np.linalg.norm(crossed)
    if float(np.dot(crossed, across_u)) < 0.99:
        raise YieldValidationSelectionError("across must be approach cross along")
    axis = {"approach": approach_u, "along": along_u, "across": across_u}[direction]
    angle = math.radians(_finite(angle_deg, "prior_angle_deg"))
    prior = math.cos(angle) * approach_u + math.sin(angle) * axis
    norm = float(np.linalg.norm(prior))
    if not math.isfinite(norm) or norm <= 0.0:
        raise YieldValidationSelectionError("prior inward normal is degenerate")
    return prior / norm


def derive_prior_basis(*, approach: Sequence[float], reference_velocity_m_s: Sequence[float]) -> dict[str, np.ndarray]:
    """Frame convention: along is velocity projected orthogonal to approach; across=approach×along."""
    approach_u = np.asarray(approach, dtype=float).reshape(3)
    velocity = np.asarray(reference_velocity_m_s, dtype=float).reshape(3)
    if not np.all(np.isfinite(approach_u)) or not np.all(np.isfinite(velocity)):
        raise YieldValidationSelectionError("approach and reference velocity must be finite")
    n_app = float(np.linalg.norm(approach_u))
    if n_app <= 0.0:
        raise YieldValidationSelectionError("approach must be nonzero")
    approach_u = approach_u / n_app
    along = velocity - approach_u * float(np.dot(velocity, approach_u))
    n_along = float(np.linalg.norm(along))
    if n_along <= 0.0:
        raise YieldValidationSelectionError("initial tangent reference direction is degenerate")
    along = along / n_along
    across = np.cross(approach_u, along)
    n_across = float(np.linalg.norm(across))
    if n_across <= 0.0:
        raise YieldValidationSelectionError("across basis is degenerate")
    across = across / n_across
    return {"approach": approach_u, "along": along, "across": across}


def candidate_parameters(candidate: Mapping[str, Any], *, method: str) -> dict[str, float]:
    payload = _mapping(candidate, f"{method} freeze candidate")
    if payload.get("method") not in (None, method):
        raise YieldValidationSelectionError(f"{method} freeze candidate method differs")
    if "parameters" in payload:
        values = _mapping(payload.get("parameters"), f"{method} parameters")
    else:
        values = {str(name): raw for name, raw in payload.items() if name not in {"method", "law"}}
    return {str(name): _finite(value, name) for name, value in values.items()}


def arm_law_parameters(arm: str, selected_candidates: Mapping[str, Any]) -> dict[str, float]:
    if arm not in ARMS:
        raise YieldValidationSelectionError("unknown validation arm")
    selected = _mapping(selected_candidates, "selected_candidates")
    if arm in ("SFC", "DSFC", "MSFC"):
        return candidate_parameters(selected[arm], method=arm)
    if arm == "MSFC_IDENTITY":
        params = dict(candidate_parameters(selected["MSFC"], method="MSFC"))
        if "minimum_metric_eigenvalue" not in params:
            raise YieldValidationSelectionError("MSFC freeze is missing minimum_metric_eigenvalue")
        params["minimum_metric_eigenvalue"] = MSFC_IDENTITY_METRIC
        return params
    sfc = candidate_parameters(selected["SFC"], method="SFC")
    names = ("m", "mu", "n", "g")
    missing = [name for name in names if name not in sfc]
    if missing:
        raise YieldValidationSelectionError(f"SFC freeze missing radial coefficients {missing}")
    return {name: sfc[name] for name in names}


def actual_method_for_arm(arm: str) -> str:
    if arm not in ARM_ACTUAL_METHOD:
        raise YieldValidationSelectionError("unknown validation arm")
    return ARM_ACTUAL_METHOD[arm]


@dataclass(frozen=True)
class YieldValidationContract:
    """Declared holdout pair bindings for one reserved cell/arm/resolution."""

    cell_id: str
    arm: str
    actual_method: str
    material: str
    kappa_xx: float
    kappa_yy: float
    prior_direction: str
    prior_angle_deg: float
    preparation: str
    nominal_scenario: str
    disturbed_scenario: str
    timeline: str
    duration_s: float
    dt_s: float
    period_s: float
    plant_substeps: int
    resolution_id: str
    campaign_kind: str
    record_fullstate: bool
    require_ur10e: bool
    observer_parameters: Mapping[str, Any]
    expected_prior: Sequence[float]
    approach_inward_base: Sequence[float]
    path_stiffness_n_per_m: float
    compliance_stiffness_n_per_m: float
    load_onset_s: float
    path_release_s: float
    attitude_onset_s: float
    jload_scale_n: float
    jpath_scale_m: float
    jatt_scale_rad: float
    nominal_path_rms_m_max: float
    nominal_progress_ratio_min: float
    nominal_attitude_rms_rad_max: float
    nominal_load_mae_n_max: float
    nominal_load_peak_n_max: float
    nominal_load_min_n_min: float
    disturbed_load_min_n_min: float
    disturbed_load_peak_n_max: float
    disturbed_progress_ratio_min: float
    candidate_parameters: Mapping[str, float]
    source_hashes: Mapping[str, str] | None = None
    protocol_sha256: str | None = None
    kinematics_kind: str = "ur10e_calibrated_pinocchio"
    law_frame: str = "fixed_base"

    def __post_init__(self) -> None:
        object.__setattr__(self, "observer_parameters", dict(self.observer_parameters))
        object.__setattr__(
            self,
            "candidate_parameters",
            {str(name): _finite(value, name) for name, value in self.candidate_parameters.items()},
        )
        object.__setattr__(self, "expected_prior", tuple(float(x) for x in np.asarray(self.expected_prior, dtype=float).reshape(3)))
        object.__setattr__(
            self,
            "approach_inward_base",
            tuple(float(x) for x in np.asarray(self.approach_inward_base, dtype=float).reshape(3)),
        )
        if self.source_hashes is not None:
            object.__setattr__(self, "source_hashes", dict(self.source_hashes))
        if self.arm not in ARMS:
            raise YieldValidationSelectionError("unknown validation arm")
        if self.actual_method != ARM_ACTUAL_METHOD[self.arm]:
            raise YieldValidationSelectionError("arm actual method mapping differs")
        if self.actual_method not in ACTUAL_METHODS:
            raise YieldValidationSelectionError("actual method is not an executable law")
        if self.arm == "MSFC_IDENTITY" and self.actual_method != "MSFC":
            raise YieldValidationSelectionError("identity ablation must execute MSFC")
        if self.campaign_kind != "holdout":
            raise YieldValidationSelectionError("validation contract campaign_kind must be holdout")
        if self.prior_direction not in PRIOR_DIRECTIONS:
            raise YieldValidationSelectionError("contract prior_direction differs")
        if self.preparation not in PREPARATIONS:
            raise YieldValidationSelectionError("preparation must be cold or warm")
        if self.timeline != "full_cycle":
            raise YieldValidationSelectionError("validation timeline must be full_cycle")
        if self.nominal_scenario == self.disturbed_scenario:
            raise YieldValidationSelectionError("nominal and disturbed scenarios must differ")
        if type(self.plant_substeps) is not int or self.plant_substeps <= 0:
            raise YieldValidationSelectionError("plant_substeps must be a positive integer")
        if type(self.record_fullstate) is not bool or type(self.require_ur10e) is not bool:
            raise YieldValidationSelectionError("record_fullstate and require_ur10e must be bool")
        for name in (
            "duration_s",
            "dt_s",
            "period_s",
            "kappa_xx",
            "kappa_yy",
            "path_stiffness_n_per_m",
            "jload_scale_n",
            "jpath_scale_m",
            "jatt_scale_rad",
        ):
            parsed = _finite(getattr(self, name), name)
            if parsed <= 0.0:
                raise YieldValidationSelectionError(f"{name} must be positive")
            object.__setattr__(self, name, parsed)
        _finite(self.compliance_stiffness_n_per_m, "compliance_stiffness_n_per_m")
        if self.arm == "MSFC_IDENTITY":
            metric = self.candidate_parameters.get("minimum_metric_eigenvalue")
            if metric != MSFC_IDENTITY_METRIC:
                raise YieldValidationSelectionError("MSFC identity must keep minimum_metric_eigenvalue=1")
        prior = np.asarray(self.expected_prior, dtype=float)
        if not np.all(np.isfinite(prior)) or abs(float(np.linalg.norm(prior)) - 1.0) > 1e-9:
            raise YieldValidationSelectionError("expected prior must be a finite unit vector")


@dataclass(frozen=True)
class YieldValidationMemberResult:
    condition: str
    valid: bool
    failed: bool
    full_cycle: bool
    nominal_band_ok: bool | None
    disturbed_guard_ok: bool | None
    checks: Mapping[str, Any]
    recomputed_metrics: Mapping[str, Any]
    reported_metrics: Mapping[str, Any]
    absolute_descriptors: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True)
class YieldValidationPairResult:
    schema: str = SCHEMA
    version: int = 1
    valid: bool = False
    failed_or_incomplete: bool = True
    nominal_band_ok: bool = False
    disturbed_guard_ok: bool = False
    pair_feasible_flag: bool = False
    skip_refinements: bool = True
    objective: float | None = None
    objective_components: Mapping[str, float] | None = None
    objective_eligible: bool = False
    reported_metrics_nominal: Mapping[str, Any] | None = None
    reported_metrics_disturbed: Mapping[str, Any] | None = None
    reported_recovery: Mapping[str, Any] | None = None
    recomputed_metrics_nominal: Mapping[str, Any] | None = None
    recomputed_metrics_disturbed: Mapping[str, Any] | None = None
    absolute_descriptors: Mapping[str, Any] = field(default_factory=dict)
    nominal_checks: Mapping[str, Any] = field(default_factory=dict)
    disturbed_checks: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None
    fullstate_limit: str = FULLSTATE_LIMIT
    claim_scope: str = CLAIM_SCOPE


def bind_arm(
    contract: YieldValidationContract,
    *,
    arm: str,
    parameters: Mapping[str, float],
    source_hashes: Mapping[str, str] | None = None,
    protocol_sha256: str | None = None,
) -> YieldValidationContract:
    if arm not in ARMS:
        raise YieldValidationSelectionError("unknown validation arm")
    actual = ARM_ACTUAL_METHOD[arm]
    parsed = {str(name): _finite(value, name) for name, value in parameters.items()}
    if arm == "MSFC_IDENTITY" and parsed.get("minimum_metric_eigenvalue") != MSFC_IDENTITY_METRIC:
        raise YieldValidationSelectionError("MSFC identity must keep minimum_metric_eigenvalue=1")
    return replace(
        contract,
        arm=arm,
        actual_method=actual,
        candidate_parameters=parsed,
        source_hashes=source_hashes if source_hashes is not None else contract.source_hashes,
        protocol_sha256=protocol_sha256 if protocol_sha256 is not None else contract.protocol_sha256,
    )


def _unit3(value: Any, role: str) -> np.ndarray:
    try:
        array = _vector3(value, role)
    except YieldFairSelectionError as error:
        raise _wrap_fair(error) from error
    norm = float(np.linalg.norm(array))
    if not math.isfinite(norm) or norm <= 0.0:
        raise YieldValidationSelectionError(f"{role} must be a finite unit vector")
    return array / norm


def _require_identities(artifact: Mapping[str, Any], contract: YieldValidationContract) -> None:
    if artifact.get("method") == "MSFC_IDENTITY":
        raise YieldValidationSelectionError("raw artifact method must not be relabeled MSFC_IDENTITY")
    if artifact.get("method") != contract.actual_method:
        raise YieldValidationSelectionError("artifact method differs from the arm's actual executable method")
    if contract.arm == "SFC_RADIAL" and artifact.get("method") != "SFC_RADIAL":
        raise YieldValidationSelectionError("SFC_RADIAL arm must keep actual method SFC_RADIAL")
    if contract.arm == "MSFC_IDENTITY" and artifact.get("method") != "MSFC":
        raise YieldValidationSelectionError("MSFC identity arm must keep actual method MSFC")
    if artifact.get("material") != contract.material:
        raise YieldValidationSelectionError("material differs")
    if artifact.get("preparation") != contract.preparation:
        raise YieldValidationSelectionError("preparation differs")
    if artifact.get("timeline") != contract.timeline:
        raise YieldValidationSelectionError("timeline differs")
    if _finite(artifact.get("dt_s"), "artifact dt_s") != contract.dt_s:
        raise YieldValidationSelectionError("dt_s differs")
    if not math.isclose(
        _finite(artifact.get("duration_s"), "artifact duration_s"),
        contract.duration_s,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise YieldValidationSelectionError("duration_s differs")
    if artifact.get("campaign_kind") == "training":
        raise YieldValidationSelectionError("training rows cannot enter validation")
    if artifact.get("campaign_kind") != "holdout":
        raise YieldValidationSelectionError("campaign_kind must be holdout")
    if contract.require_ur10e and artifact.get("kinematics_kind") != contract.kinematics_kind:
        raise YieldValidationSelectionError("plant kinematics identity differs")
    payload = _mapping(artifact.get("identity_payload"), "identity_payload")
    plant = _mapping(artifact.get("plant_identity_payload"), "plant_identity_payload")
    if payload.get("law_frame") != contract.law_frame:
        raise YieldValidationSelectionError("law frame differs")
    settings = _mapping(payload.get("settings"), "identity settings")
    if _finite(settings.get("path_stiffness_n_per_m"), "Kp") != contract.path_stiffness_n_per_m:
        raise YieldValidationSelectionError("shared outer Kp differs")
    if _finite(settings.get("compliance_stiffness_n_per_m"), "Kz") != contract.compliance_stiffness_n_per_m:
        raise YieldValidationSelectionError("shared outer Kz differs")
    estimator = _mapping(payload.get("estimator_parameters"), "estimator_parameters")
    if "initial_inward_normal_base" not in estimator:
        raise YieldValidationSelectionError("prior differs; initial_inward_normal_base required")
    extra = set(estimator) - set(contract.observer_parameters) - {"initial_inward_normal_base"}
    missing = set(contract.observer_parameters) - set(estimator)
    if extra or missing:
        raise YieldValidationSelectionError("observer identity differs")
    for name, expected in contract.observer_parameters.items():
        if _finite(estimator.get(name), f"observer {name}") != _finite(expected, f"observer {name}"):
            raise YieldValidationSelectionError("observer identity differs")
    prior = _unit3(estimator.get("initial_inward_normal_base"), "initial_inward_normal_base")
    expected_prior = _unit3(contract.expected_prior, "expected prior")
    if not np.allclose(prior, expected_prior, atol=1e-12, rtol=0.0):
        raise YieldValidationSelectionError("prior axis differs")
    approach = _unit3(payload.get("approach_inward_base"), "approach_inward_base")
    expected_approach = _unit3(contract.approach_inward_base, "contract approach")
    if not np.allclose(approach, expected_approach, atol=1e-12, rtol=0.0):
        raise YieldValidationSelectionError("approach frame differs")
    if np.allclose(prior, expected_approach, atol=1e-12, rtol=0.0) and not (
        contract.prior_direction == "approach" and abs(contract.prior_angle_deg) < 1e-12
    ):
        raise YieldValidationSelectionError("prior axis differs")
    parameters = payload.get("parameters")
    if not isinstance(parameters, Mapping):
        raise YieldValidationSelectionError("actual law parameters missing")
    expected_names = set(contract.candidate_parameters)
    if set(parameters) != expected_names:
        raise YieldValidationSelectionError("candidate law parameter fields differ")
    for name, expected in contract.candidate_parameters.items():
        if _finite(parameters.get(name), name) != expected:
            raise YieldValidationSelectionError("actual law parameters differ from the frozen arm coefficients")
    if contract.arm == "MSFC_IDENTITY":
        if _finite(parameters.get("minimum_metric_eigenvalue"), "minimum_metric_eigenvalue") != MSFC_IDENTITY_METRIC:
            raise YieldValidationSelectionError("MSFC identity coefficients differ")
    if plant.get("integration_substeps") != contract.plant_substeps:
        raise YieldValidationSelectionError("plant identity differs")
    surface = _mapping(plant.get("surface"), "plant surface")
    if _finite(surface.get("kappa_xx"), "plant kappa_xx") != contract.kappa_xx:
        raise YieldValidationSelectionError("surface identity differs")
    if _finite(surface.get("kappa_yy"), "plant kappa_yy") != contract.kappa_yy:
        raise YieldValidationSelectionError("surface identity differs")
    recorded_surface = artifact.get("surface_parameters")
    if recorded_surface is not None:
        recorded = _mapping(recorded_surface, "surface_parameters")
        if _finite(recorded.get("kappa_xx"), "surface kappa_xx") != contract.kappa_xx:
            raise YieldValidationSelectionError("surface identity differs")
        if _finite(recorded.get("kappa_yy"), "surface kappa_yy") != contract.kappa_yy:
            raise YieldValidationSelectionError("surface identity differs")
    if contract.source_hashes is not None:
        hashes = _mapping(artifact.get("source_hashes"), "source_hashes")
        if dict(hashes) != dict(contract.source_hashes):
            raise YieldValidationSelectionError("source identity differs")
    if contract.protocol_sha256 is not None and artifact.get("protocol_sha256") != contract.protocol_sha256:
        raise YieldValidationSelectionError("source/protocol identity differs")
    if not artifact.get("identity"):
        raise YieldValidationSelectionError("controller identity missing")


def _member_absolute(rows: Sequence[Mapping[str, Any]], recomputed: Mapping[str, Any]) -> dict[str, Any]:
    load = _load_series(rows, "true_normal_load_n")
    return {
        "force_mae_n": recomputed.get("force_mae_n"),
        "force_rmse_n": recomputed.get("force_rmse_n"),
        "force_peak_n": recomputed.get("force_peak_n"),
        "load_min_n": float(np.min(load)),
        "path_rmse_m": recomputed.get("path_rmse_m"),
        "path_peak_m": recomputed.get("path_peak_m"),
        "progress_ratio": recomputed.get("progress_ratio"),
        "orientation_rmse_rad": recomputed.get("orientation_rmse_rad"),
        "orientation_peak_rad": recomputed.get("orientation_peak_rad"),
        "orientation_rmse_deg": (
            None if recomputed.get("orientation_rmse_rad") is None
            else math.degrees(float(recomputed["orientation_rmse_rad"]))
        ),
        "normal_estimation_rmse_rad": recomputed.get("normal_estimation_rmse_rad"),
        "contact_loss_duration_s": recomputed.get("contact_loss_duration_s"),
        "residual_path_m": recomputed.get("residual_path_m"),
        "actual_progress_m": recomputed.get("actual_progress_m"),
        "reference_progress_m": recomputed.get("reference_progress_m"),
    }


def inspect_member(
    artifact: Mapping[str, Any],
    contract: YieldValidationContract,
    *,
    condition: str,
) -> YieldValidationMemberResult:
    payload = _mapping(artifact, "artifact")
    if condition not in {"nominal", "disturbed"}:
        raise YieldValidationSelectionError("condition must be nominal or disturbed")
    expected_scenario = contract.nominal_scenario if condition == "nominal" else contract.disturbed_scenario
    if payload.get("scenario") != expected_scenario:
        raise YieldValidationSelectionError("configured scenario differs")
    reported = dict(_mapping(payload.get("metrics"), "metrics")) if isinstance(payload.get("metrics"), Mapping) else {}
    if payload.get("method") == "MSFC_IDENTITY":
        raise YieldValidationSelectionError("raw artifact method must not be relabeled MSFC_IDENTITY")
    if payload.get("method") != contract.actual_method:
        raise YieldValidationSelectionError("artifact method differs from the arm's actual executable method")
    if payload.get("campaign_kind") == "training":
        raise YieldValidationSelectionError("training rows cannot enter validation")
    if _native_failed(payload):
        return YieldValidationMemberResult(
            condition=condition,
            valid=False,
            failed=True,
            full_cycle=False,
            nominal_band_ok=False if condition == "nominal" else None,
            disturbed_guard_ok=False if condition == "disturbed" else None,
            checks={"failed": True},
            recomputed_metrics={},
            reported_metrics=reported,
            reason="failed member",
        )
    try:
        _require_identities(payload, contract)
        rows = payload.get("rows")
        times = _require_full_cycle_grid(rows, contract)
        _require_fullstate(payload, contract)
        initial_c = _mapping(payload.get("initial_controller_snapshot"), "initial controller snapshot")
        estimate = _mapping(initial_c.get("normal_estimate"), "initial normal_estimate")
        inward = _unit3(estimate.get("inward_normal_base"), "initial inward_normal_base")
        if not np.allclose(inward, _unit3(contract.expected_prior, "expected prior"), atol=1e-12, rtol=0.0):
            raise YieldValidationSelectionError("prior snapshot differs")
        numeric_fields = (
            "true_normal_load_n",
            "force_error_n",
            "path_error_m",
            "orientation_error_rad",
            "normal_estimation_error_rad",
            "actual_progress_m_s",
            "reference_progress_m_s",
        )
        for index, row in enumerate(rows):
            for name in numeric_fields:
                _finite(row.get(name), f"row {index} {name}")
            _vector3(row.get("position_m"), f"row {index} position_m")
    except YieldFairSelectionError as error:
        raise _wrap_fair(error) from error
    recomputed = summarize_trial(
        list(rows),
        failed=False,
        failure_message=None,
        scenario=str(payload["scenario"]),
        material=contract.material,
        method=str(payload["method"]),
        dt_s=contract.dt_s,
        kinematics_kind=str(payload.get("kinematics_kind")),
        campaign_kind="holdout",
        timeline=contract.timeline,
    )
    load = _load_series(rows, "true_normal_load_n")
    path_rms = recomputed["path_rmse_m"]
    progress = recomputed["progress_ratio"]
    attitude = recomputed["orientation_rmse_rad"]
    load_mae = recomputed["force_mae_n"]
    peak = recomputed["force_peak_n"]
    minimum = float(np.min(load))
    for role, value in (
        ("path_rmse_m", path_rms),
        ("progress_ratio", progress),
        ("orientation_rmse_rad", attitude),
        ("force_mae_n", load_mae),
        ("force_peak_n", peak),
        ("load_min_n", minimum),
    ):
        try:
            _finite(value, role)
        except YieldFairSelectionError as error:
            raise _wrap_fair(error) from error
    full_cycle = (
        len(times) == path_sample_count(contract.duration_s, contract.dt_s)
        and math.isclose(contract.duration_s, contract.period_s, rel_tol=0.0, abs_tol=1e-12)
        and contract.timeline == "full_cycle"
    )
    if condition == "nominal":
        checks = {
            "path_rms_ok": path_rms <= contract.nominal_path_rms_m_max,
            "progress_ok": progress >= contract.nominal_progress_ratio_min,
            "attitude_rms_ok": attitude <= contract.nominal_attitude_rms_rad_max,
            "load_mae_ok": load_mae <= contract.nominal_load_mae_n_max,
            "load_peak_ok": peak <= contract.nominal_load_peak_n_max,
            "load_min_ok": minimum >= contract.nominal_load_min_n_min,
            "full_cycle": full_cycle,
            "no_failure": True,
        }
        band_ok = all(checks[name] is True for name in checks)
        return YieldValidationMemberResult(
            condition=condition,
            valid=True,
            failed=False,
            full_cycle=full_cycle,
            nominal_band_ok=bool(band_ok),
            disturbed_guard_ok=None,
            checks=checks,
            recomputed_metrics=recomputed,
            reported_metrics=reported,
            absolute_descriptors=_member_absolute(rows, recomputed),
        )
    checks = {
        "load_min_ok": minimum >= contract.disturbed_load_min_n_min,
        "load_peak_ok": peak <= contract.disturbed_load_peak_n_max,
        "progress_ok": progress >= contract.disturbed_progress_ratio_min,
        "full_cycle": full_cycle,
        "no_failure": True,
    }
    guards = all(checks[name] is True for name in checks)
    return YieldValidationMemberResult(
        condition=condition,
        valid=True,
        failed=False,
        full_cycle=full_cycle,
        nominal_band_ok=None,
        disturbed_guard_ok=bool(guards),
        checks=checks,
        recomputed_metrics=recomputed,
        reported_metrics=reported,
        absolute_descriptors=_member_absolute(rows, recomputed),
    )


def _pair_identities_match(nominal: Mapping[str, Any], disturbed: Mapping[str, Any]) -> None:
    for key in ("method", "material", "dt_s", "duration_s", "preparation", "timeline", "identity"):
        if nominal.get(key) != disturbed.get(key):
            raise YieldValidationSelectionError(f"mismatched pair {key}")
    n_payload = _mapping(nominal.get("identity_payload"), "nominal identity")
    d_payload = _mapping(disturbed.get("identity_payload"), "disturbed identity")
    if n_payload.get("parameters") != d_payload.get("parameters"):
        raise YieldValidationSelectionError("mismatched pair candidate parameters")
    if n_payload.get("estimator_parameters") != d_payload.get("estimator_parameters"):
        raise YieldValidationSelectionError("mismatched pair observer identity")
    if nominal.get("plant_identity_payload") != disturbed.get("plant_identity_payload"):
        raise YieldValidationSelectionError("mismatched pair plant identity")
    if nominal.get("source_hashes") != disturbed.get("source_hashes"):
        raise YieldValidationSelectionError("mismatched pair source identity")


def _window_rms(series: np.ndarray, times: np.ndarray, start: float, period: float) -> float | None:
    mask = (times >= start) & (times < period)
    if not np.any(mask):
        return None
    values = series[mask]
    return float(np.sqrt(np.mean(values * values)))


def _weighted_abs(series: np.ndarray, times: np.ndarray, dt: float, start: float, period: float) -> float:
    total = 0.0
    for index, time_s in enumerate(times):
        total += abs(float(series[index])) * cell_weight(float(time_s), dt, start, period, period)
    return float(total)


def pair_absolute_descriptors(
    nominal: Mapping[str, Any],
    disturbed: Mapping[str, Any],
    contract: YieldValidationContract,
    *,
    recovery: Mapping[str, Any] | None,
    components: Mapping[str, float] | None,
    nominal_member: YieldValidationMemberResult,
    disturbed_member: YieldValidationMemberResult,
) -> dict[str, Any]:
    times = expected_path_times(contract)
    att_n = _load_series(nominal["rows"], "orientation_error_rad")
    att_d = _load_series(disturbed["rows"], "orientation_error_rad")
    load_n = _load_series(nominal["rows"], "true_normal_load_n")
    load_d = _load_series(disturbed["rows"], "true_normal_load_n")
    pos_n = _positions(nominal["rows"])
    pos_d = _positions(disturbed["rows"])
    delta_pos = np.linalg.norm(pos_d - pos_n, axis=1)
    release = contract.path_release_s
    dt = contract.dt_s
    period = contract.period_s
    descriptors = {
        "cell_id": contract.cell_id,
        "arm": contract.arm,
        "actual_method": contract.actual_method,
        "resolution_id": contract.resolution_id,
        "J": None if components is None else components.get("J"),
        "Jload": None if components is None else components.get("Jload"),
        "Jpath": None if components is None else components.get("Jpath"),
        "Jatt": None if components is None else components.get("Jatt"),
        "J_att_absolute_onset_to_end_s": _weighted_abs(att_d, times, dt, contract.attitude_onset_s, period) / contract.jatt_scale_rad,
        "J_att_absolute_post_release_s": _weighted_abs(att_d, times, dt, release, period) / contract.jatt_scale_rad,
        "nominal_att_absolute_onset_to_end_s": _weighted_abs(att_n, times, dt, contract.attitude_onset_s, period) / contract.jatt_scale_rad,
        "disturbed_post_release_att_rms_deg": None if _window_rms(att_d, times, release, period) is None else math.degrees(_window_rms(att_d, times, release, period)),
        "nominal_post_release_att_rms_deg": None if _window_rms(att_n, times, release, period) is None else math.degrees(_window_rms(att_n, times, release, period)),
        "nominal_whole_path_att_rms_deg": math.degrees(float(nominal_member.recomputed_metrics["orientation_rmse_rad"])),
        "disturbed_whole_path_att_rms_deg": math.degrees(float(disturbed_member.recomputed_metrics["orientation_rmse_rad"])),
        "disturbed_max_att_deg": math.degrees(float(np.max(np.abs(att_d)))),
        "true_load_max_n": float(np.max(load_d)),
        "true_load_min_n": float(np.min(load_d)),
        "nominal_true_load_max_n": float(np.max(load_n)),
        "nominal_true_load_min_n": float(np.min(load_n)),
        "disturbed_progress_ratio": disturbed_member.recomputed_metrics.get("progress_ratio"),
        "nominal_progress_ratio": nominal_member.recomputed_metrics.get("progress_ratio"),
        "nominal_path_rmse_m": nominal_member.recomputed_metrics.get("path_rmse_m"),
        "disturbed_path_rmse_m": disturbed_member.recomputed_metrics.get("path_rmse_m"),
        "nominal_force_mae_n": nominal_member.recomputed_metrics.get("force_mae_n"),
        "disturbed_force_mae_n": disturbed_member.recomputed_metrics.get("force_mae_n"),
        "nominal_force_peak_n": nominal_member.recomputed_metrics.get("force_peak_n"),
        "disturbed_force_peak_n": disturbed_member.recomputed_metrics.get("force_peak_n"),
        "post_release_terminal_distance_m": float(delta_pos[-1]),
        "post_release_terminal_delta_load_n": float(load_d[-1] - load_n[-1]),
        "recovery": dict(recovery or {}),
        "nominal_bands_ok_flag": nominal_member.nominal_band_ok,
        "disturbed_guards_ok_flag": disturbed_member.disturbed_guard_ok,
        "path_release_s": release,
        "load_onset_s": contract.load_onset_s,
        "attitude_onset_s": contract.attitude_onset_s,
    }
    if recovery:
        for key in ("yield_peak_m", "residual_displacement_m", "recoil_m", "recovery_s", "eligible"):
            if key in recovery:
                descriptors[key] = recovery[key]
    return descriptors


def base_failed_or_incomplete(result: YieldValidationPairResult) -> bool:
    """Refinements skip only for an actually failed or incomplete base pair."""
    return (not result.valid) or result.failed_or_incomplete


def refinements_required(result: YieldValidationPairResult) -> bool:
    """Out-of-band complete pairs still receive refinements."""
    return result.valid is True and result.failed_or_incomplete is False


def evaluate_pair(
    nominal: Mapping[str, Any],
    disturbed: Mapping[str, Any],
    contract: YieldValidationContract,
) -> YieldValidationPairResult:
    nominal_art = _mapping(nominal, "nominal artifact")
    disturbed_art = _mapping(disturbed, "disturbed artifact")
    reported_n = dict(nominal_art.get("metrics") or {}) if isinstance(nominal_art.get("metrics"), Mapping) else {}
    reported_d = dict(disturbed_art.get("metrics") or {}) if isinstance(disturbed_art.get("metrics"), Mapping) else {}

    def _invalid(reason: str, **fields: Any) -> YieldValidationPairResult:
        return YieldValidationPairResult(
            valid=False,
            failed_or_incomplete=True,
            skip_refinements=True,
            objective=None,
            objective_eligible=False,
            reported_metrics_nominal=reported_n,
            reported_metrics_disturbed=reported_d,
            reported_recovery={
                "eligible": False,
                "reason": "recovery unavailable: pair is not a complete uncensored result",
            },
            reason=reason,
            **fields,
        )

    if nominal_art.get("scenario") == disturbed_art.get("scenario"):
        raise YieldValidationSelectionError("pair scenarios must differ")
    _pair_identities_match(nominal_art, disturbed_art)
    nominal_member = inspect_member(nominal_art, contract, condition="nominal")
    disturbed_member = inspect_member(disturbed_art, contract, condition="disturbed")
    if not nominal_member.valid or not disturbed_member.valid:
        return _invalid(
            "failed member; retained, no performance ranking",
            nominal_band_ok=bool(nominal_member.nominal_band_ok),
            disturbed_guard_ok=False,
            pair_feasible_flag=False,
            recomputed_metrics_nominal=nominal_member.recomputed_metrics or None,
            recomputed_metrics_disturbed=disturbed_member.recomputed_metrics or None,
            nominal_checks=dict(nominal_member.checks),
            disturbed_checks=dict(disturbed_member.checks),
        )
    try:
        if _recovery_window_covered(contract):
            try:
                reported_recovery = compare_pair(nominal_art, disturbed_art)
            except (TypeError, ValueError, KeyError) as error:
                raise YieldValidationSelectionError(
                    f"recovery computation failed for complete pair: {error}"
                ) from error
        else:
            reported_recovery = {
                "eligible": False,
                "reason": "recovery unavailable: sample grid does not cover the disturbance window",
                "right_censored": True,
            }
        components = _objective_components(nominal_art["rows"], disturbed_art["rows"], contract)
    except YieldFairSelectionError as error:
        raise _wrap_fair(error) from error
    band_ok = _bool(nominal_member.nominal_band_ok, "nominal_band_ok")
    guards = _bool(disturbed_member.disturbed_guard_ok, "disturbed_guard_ok")
    descriptors = pair_absolute_descriptors(
        nominal_art,
        disturbed_art,
        contract,
        recovery=reported_recovery,
        components=components,
        nominal_member=nominal_member,
        disturbed_member=disturbed_member,
    )
    return YieldValidationPairResult(
        valid=True,
        failed_or_incomplete=False,
        skip_refinements=False,
        nominal_band_ok=band_ok,
        disturbed_guard_ok=guards,
        pair_feasible_flag=bool(band_ok and guards),
        objective=float(components["J"]),
        objective_components=components,
        objective_eligible=True,
        reported_metrics_nominal=reported_n,
        reported_metrics_disturbed=reported_d,
        reported_recovery=reported_recovery,
        recomputed_metrics_nominal=nominal_member.recomputed_metrics,
        recomputed_metrics_disturbed=disturbed_member.recomputed_metrics,
        absolute_descriptors=descriptors,
        nominal_checks=dict(nominal_member.checks),
        disturbed_checks=dict(disturbed_member.checks),
    )


_LOWER_BETTER = {
    "J", "Jload", "Jpath", "Jatt",
    "J_att_absolute_onset_to_end_s", "J_att_absolute_post_release_s",
    "nominal_att_absolute_onset_to_end_s",
    "disturbed_post_release_att_rms_deg", "nominal_whole_path_att_rms_deg",
    "disturbed_whole_path_att_rms_deg", "disturbed_max_att_deg",
    "true_load_max_n", "nominal_path_rmse_m", "disturbed_path_rmse_m",
    "nominal_force_mae_n", "disturbed_force_mae_n", "nominal_force_peak_n",
    "disturbed_force_peak_n", "post_release_terminal_distance_m",
    "yield_peak_m", "residual_displacement_m",
}
_HIGHER_BETTER = {"disturbed_progress_ratio", "nominal_progress_ratio", "true_load_min_n"}


def _numeric_delta(left: Any, right: Any) -> float | None:
    if left is None or right is None:
        return None
    if isinstance(left, bool) or isinstance(right, bool):
        return None
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(a) or not math.isfinite(b):
        return None
    return a - b


def compare_resolution_settings(
    *,
    arm_a: str,
    arm_b: str,
    settings_a: Mapping[str, Mapping[str, Any]],
    settings_b: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Descriptor delta plus paired/unpaired sensitivity. No universal tolerance or winner."""
    names = [item[0] for item in RESOLUTION_SETTINGS]
    complete_a = all(name in settings_a and settings_a[name].get("valid") is True for name in names)
    complete_b = all(name in settings_b and settings_b[name].get("valid") is True for name in names)
    if not (complete_a and complete_b):
        return {
            "complete": False,
            "reason": "both arms must have all three complete settings",
            "equivalence": False,
            "winner": None,
        }
    keys = sorted(
        set(settings_a["base"].get("absolute_descriptors") or {})
        & set(settings_b["base"].get("absolute_descriptors") or {})
    )
    descriptor_delta = {}
    unpaired = {}
    for resolution in names:
        left = settings_a[resolution].get("absolute_descriptors") or {}
        right = settings_b[resolution].get("absolute_descriptors") or {}
        descriptor_delta[resolution] = {
            key: _numeric_delta(left.get(key), right.get(key)) for key in keys
        }
        unpaired[resolution] = descriptor_delta[resolution]
    paired = {}
    unresolved = []
    for arm, settings in ((arm_a, settings_a), (arm_b, settings_b)):
        base = settings["base"].get("absolute_descriptors") or {}
        controller = settings["controller_refinement"].get("absolute_descriptors") or {}
        plant = settings["plant_refinement"].get("absolute_descriptors") or {}
        controller_step = {key: _numeric_delta(controller.get(key), base.get(key)) for key in keys}
        plant_step = {key: _numeric_delta(plant.get(key), base.get(key)) for key in keys}
        arm_unresolved = []
        for key in keys:
            c_delta = controller_step.get(key)
            p_delta = plant_step.get(key)
            if c_delta is None or p_delta is None:
                continue
            if c_delta == 0.0 and p_delta == 0.0:
                continue
            if (c_delta > 0.0 and p_delta < 0.0) or (c_delta < 0.0 and p_delta > 0.0):
                arm_unresolved.append(key)
                unresolved.append({"arm": arm, "descriptor": key})
        paired[arm] = {
            "controller_refinement_minus_base": controller_step,
            "plant_refinement_minus_base": plant_step,
            "unresolved_descriptors": arm_unresolved,
        }
    j_a = settings_a["base"]["absolute_descriptors"].get("J")
    j_b = settings_b["base"]["absolute_descriptors"].get("J")
    j_delta = _numeric_delta(j_a, j_b)
    adverse = []
    lower_j_arm = None
    if j_delta is not None:
        lower_j_arm = arm_a if j_delta < 0.0 else arm_b if j_delta > 0.0 else None
        if lower_j_arm is not None:
            better = settings_a["base"]["absolute_descriptors"] if lower_j_arm == arm_a else settings_b["base"]["absolute_descriptors"]
            worse = settings_b["base"]["absolute_descriptors"] if lower_j_arm == arm_a else settings_a["base"]["absolute_descriptors"]
            for key in _LOWER_BETTER:
                delta = _numeric_delta(better.get(key), worse.get(key))
                if delta is not None and delta > 0.0:
                    adverse.append(key)
            for key in _HIGHER_BETTER:
                delta = _numeric_delta(better.get(key), worse.get(key))
                if delta is not None and delta < 0.0:
                    adverse.append(key)
    if unresolved:
        comparison = "unresolved_sensitivity"
    elif lower_j_arm is not None and adverse:
        comparison = "mixed"
    elif lower_j_arm is None:
        comparison = "j_tied_not_equivalence"
    else:
        comparison = "lower_j_with_reported_absolutes"
    return {
        "complete": True,
        "arm_a": arm_a,
        "arm_b": arm_b,
        "descriptor_delta": descriptor_delta,
        "paired_step_sensitivity": paired,
        "unpaired_sensitivity": unpaired,
        "unresolved_sensitivity": unresolved,
        "lower_j_arm": lower_j_arm,
        "adverse_absolute_changes_for_lower_j": adverse,
        "comparison": comparison,
        "equivalence": False,
        "winner": None,
        "note": "unresolved sensitivity is not equivalence; lower J with adverse absolute changes is mixed",
    }
