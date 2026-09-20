"""Pure paired-trial evaluator for the yield-fair campaign.

This module does not run the simulator, debit a ledger, or launch a campaign.
It rejects incomplete or mismatched evidence, recomputes metrics from rows,
and reports nominal feasibility separately from disturbed guards and pair
feasibility. Old reported metrics/recovery are preserved unchanged.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
import json
import math

import numpy as np

from contact_laws import SNAPSHOT_SIZE
from contact_yield_math import (
    YieldMathError,
    optional_finite_time,
    require_bool,
    require_rotation,
    require_unit_vector,
)
from contact_yield_metrics import compare_pair, summarize_trial
from contact_yield_protocol import PERIOD_S, parse_scenario


SCHEMA = "ur10e.yield-fair-selection-v1"
CONFIG_SCHEMA = "ur10e.yield-fair-campaign-v1"
METHODS = ("SFC", "DSFC", "MSFC")
FULLSTATE_LIMIT = (
    "full-state schema validation only; exact forward replay is not performed"
)
RECOVERY_REMAINING_S = 0.1
MSFC_MEMORY_SLICE = slice(7, 19)
OBSERVER_V3 = {
    "motion_gain": 0.3,
    "force_correction_gain": 0.0,
    "motion_normalization_floor_m_s": 0.002,
    "motion_rate_cap_rad_s": 0.05,
    "coplanarity_gain_s_inv": 0.3,
    "coplanarity_cross_floor_n_m_s": 1e-9,
}
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "yield_fair_campaign_v1.json"


class YieldFairSelectionError(ValueError):
    """Invalid, mismatched, or incomplete pair evidence."""


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise YieldFairSelectionError(f"{role} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise YieldFairSelectionError(f"{role} must be a finite number")
    return parsed


def _bool(value: Any, role: str) -> bool:
    if type(value) is not bool:
        raise YieldFairSelectionError(f"{role} must be bool")
    return value


def _nonempty(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise YieldFairSelectionError(f"{role} must be a nonempty string")
    return value


def _mapping(value: Any, role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise YieldFairSelectionError(f"{role} must be an object")
    return value


def path_sample_count(duration_s: float, dt_s: float) -> int:
    duration = _finite(duration_s, "duration_s")
    dt = _finite(dt_s, "dt_s")
    if duration <= 0.0 or dt <= 0.0:
        raise YieldFairSelectionError("duration_s and dt_s must be positive")
    return int(math.ceil(duration / dt))


def cell_weight(t: float, dt: float, window_start: float, window_end: float, period: float) -> float:
    """Left sample/hold weight: |[t, t+dt) ∩ [window_start, window_end] ∩ (-∞, period]|."""
    start = max(t, window_start)
    end = min(t + dt, window_end, period)
    return max(0.0, end - start)


def _vector3(value: Any, role: str) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float).reshape(3)
    except (TypeError, ValueError) as error:
        raise YieldFairSelectionError(f"{role} must be a finite 3-vector") from error
    if array.shape != (3,) or not np.all(np.isfinite(array)):
        raise YieldFairSelectionError(f"{role} must be a finite 3-vector")
    if any(isinstance(item, bool) for item in np.atleast_1d(value)):
        raise YieldFairSelectionError(f"{role} must be a finite 3-vector")
    return array


@dataclass(frozen=True)
class YieldFairContract:
    """Declared pair-evaluation bindings. Thresholds are development bands only."""

    material: str
    kappa_xx: float
    kappa_yy: float
    prior: str
    preparation: str
    nominal_scenario: str
    disturbed_scenario: str
    timeline: str
    duration_s: float
    dt_s: float
    period_s: float
    plant_substeps: int
    campaign_kind: str
    record_fullstate: bool
    require_ur10e: bool
    observer_parameters: Mapping[str, Any]
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
    source_hashes: Mapping[str, str] | None = None
    protocol_sha256: str | None = None
    candidate_parameters: Mapping[str, float] | None = None
    method: str | None = None
    kinematics_kind: str = "ur10e_calibrated_pinocchio"
    law_frame: str = "fixed_base"

    def __post_init__(self) -> None:
        object.__setattr__(self, "observer_parameters", dict(self.observer_parameters))
        if self.source_hashes is not None:
            object.__setattr__(self, "source_hashes", dict(self.source_hashes))
        if self.candidate_parameters is not None:
            object.__setattr__(
                self,
                "candidate_parameters",
                {str(name): _finite(value, name) for name, value in self.candidate_parameters.items()},
            )
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
                raise YieldFairSelectionError(f"{name} must be positive")
            object.__setattr__(self, name, parsed)
        _finite(self.compliance_stiffness_n_per_m, "compliance_stiffness_n_per_m")
        if type(self.plant_substeps) is not int or self.plant_substeps <= 0:
            raise YieldFairSelectionError("plant_substeps must be a positive integer")
        if type(self.record_fullstate) is not bool or type(self.require_ur10e) is not bool:
            raise YieldFairSelectionError("record_fullstate and require_ur10e must be bool")
        if self.campaign_kind == "holdout":
            raise YieldFairSelectionError("holdout rows/config are refused as training")
        if self.nominal_scenario == self.disturbed_scenario:
            raise YieldFairSelectionError("nominal and disturbed scenarios must differ")
        if self.prior != "approach":
            raise YieldFairSelectionError("contract prior must be approach")


@dataclass(frozen=True)
class YieldFairMemberResult:
    condition: str
    valid: bool
    failed: bool
    full_cycle: bool
    nominal_feasible: bool | None
    disturbed_guards_ok: bool | None
    checks: Mapping[str, Any]
    recomputed_metrics: Mapping[str, Any]
    reported_metrics: Mapping[str, Any]
    reason: str | None = None


@dataclass(frozen=True)
class YieldFairPairResult:
    schema: str = SCHEMA
    version: int = 1
    valid: bool = False
    nominal_feasible: bool = False
    disturbed_guards_ok: bool = False
    pair_feasible: bool = False
    objective: float | None = None
    objective_components: Mapping[str, float] | None = None
    objective_eligible: bool = False
    reported_metrics_nominal: Mapping[str, Any] | None = None
    reported_metrics_disturbed: Mapping[str, Any] | None = None
    reported_recovery: Mapping[str, Any] | None = None
    recomputed_metrics_nominal: Mapping[str, Any] | None = None
    recomputed_metrics_disturbed: Mapping[str, Any] | None = None
    nominal_checks: Mapping[str, Any] = field(default_factory=dict)
    disturbed_checks: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None
    claim_scope: str = (
        "prospective DEVELOPMENT-model comparison; no real precise-contact, "
        "safety, or physical qualification claim"
    )


def load_contract(path: Path | str = DEFAULT_CONFIG_PATH) -> YieldFairContract:
    root = json.loads(Path(path).read_text(encoding="utf-8"))
    return contract_from_config(root)


def contract_from_config(root: Mapping[str, Any]) -> YieldFairContract:
    if not isinstance(root, Mapping):
        raise YieldFairSelectionError("campaign config must be an object")
    if root.get("schema") != CONFIG_SCHEMA or root.get("version") != 1:
        raise YieldFairSelectionError("campaign config schema/version differs")
    if root.get("offline_only") is not True or root.get("hardware_qualified") is not False:
        raise YieldFairSelectionError("campaign config is not explicitly offline and unqualified")
    if root.get("does_not_implement_holdout") is not True or root.get("holdout_used_for_tuning") is not False:
        raise YieldFairSelectionError("holdout rows/config are refused as training")
    if root.get("campaign_kind") != "training" or root.get("formal_campaign_complete") is not False:
        raise YieldFairSelectionError("campaign config must be training plumbing, not a completed campaign")
    if tuple(root.get("methods") or ()) != METHODS:
        raise YieldFairSelectionError("campaign methods must be exactly SFC, DSFC, MSFC")
    cell = _mapping(root.get("training_cell"), "training_cell")
    if cell.get("material") != "stiff_low_mu" or cell.get("id") != "stiff_low_mu":
        raise YieldFairSelectionError("training cell must be stiff_low_mu")
    if cell.get("all_pairs_same_cell") is not True:
        raise YieldFairSelectionError("all 24 pairs must share the training cell")
    surface = _mapping(cell.get("surface"), "surface")
    kappa_xx = _finite(surface.get("kappa_xx"), "kappa_xx")
    kappa_yy = _finite(surface.get("kappa_yy"), "kappa_yy")
    if (kappa_xx, kappa_yy) != (0.8, 0.4):
        raise YieldFairSelectionError("surface curvature must be the mild default 0.8/0.4")
    duration = _finite(cell.get("duration_s"), "duration_s")
    dt = _finite(cell.get("dt_s"), "dt_s")
    if duration != PERIOD_S or not math.isclose(duration, 2.0 * math.pi / 0.1, rel_tol=0.0, abs_tol=0.0):
        raise YieldFairSelectionError("duration_s must be the full unwrapped period")
    if dt != 0.002:
        raise YieldFairSelectionError("dt_s must be 0.002")
    if cell.get("timeline") != "full_cycle" or cell.get("preparation") != "cold":
        raise YieldFairSelectionError("timeline/preparation must be full_cycle/cold")
    if cell.get("prior") != "approach":
        raise YieldFairSelectionError("contract prior must be approach")
    if cell.get("nominal_scenario") != "nominal" or cell.get("disturbed_scenario") != "sustained_release_oblique":
        raise YieldFairSelectionError("pair scenarios must be nominal and sustained_release_oblique")
    if cell.get("plant_substeps") != 8 or cell.get("record_fullstate") is not True or cell.get("require_ur10e") is not True:
        raise YieldFairSelectionError("runtime binding must be full-state UR10e plant_substeps=8")
    outer = _mapping(root.get("outer"), "outer")
    kp = _finite(outer.get("path_stiffness_n_per_m"), "path_stiffness_n_per_m")
    kz = _finite(outer.get("compliance_stiffness_n_per_m"), "compliance_stiffness_n_per_m")
    if kp != 120.0 or kz != 0.0:
        raise YieldFairSelectionError("shared outer must remain Kp=120, Kz=0")
    feasibility = _mapping(root.get("feasibility"), "feasibility")
    if feasibility.get("do_not_overload_nominal_feasible_with_pair_feasibility") is not True:
        raise YieldFairSelectionError("config must keep nominal_feasible distinct from pair feasibility")
    nominal = _mapping(feasibility.get("nominal"), "nominal feasibility")
    guards = _mapping(feasibility.get("disturbed_guards"), "disturbed guards")
    attitude_deg = _finite(nominal.get("attitude_rms_deg_max"), "attitude_rms_deg_max")
    if attitude_deg != 2.81:
        raise YieldFairSelectionError("nominal attitude band must be radians(2.81)")
    objective = _mapping(root.get("objective"), "objective")
    jload = _mapping(objective.get("Jload"), "Jload")
    jpath = _mapping(objective.get("Jpath"), "Jpath")
    jatt = _mapping(objective.get("Jatt"), "Jatt")
    disturbed_spec = parse_scenario("sustained_release_oblique", timeline="full_cycle")
    release = (
        float(disturbed_spec["start_s"])
        + float(disturbed_spec["width_s"])
        + float(disturbed_spec["hold_s"])
        + float(disturbed_spec["release_s"])
    )
    if _finite(jload.get("window_start_s"), "Jload window") != 20.0:
        raise YieldFairSelectionError("Jload onset must be 20 s")
    if _finite(jpath.get("window_start_s"), "Jpath window") != 35.5 or release != 35.5:
        raise YieldFairSelectionError("Jpath window must start at the 35.5 s release")
    if _finite(jatt.get("window_start_s"), "Jatt window") != 20.0:
        raise YieldFairSelectionError("Jatt onset must be 20 s")
    if _finite(jatt.get("scale_rad"), "Jatt scale") != 0.05:
        raise YieldFairSelectionError("Jatt scale is the declared 0.05 rad normalization")
    if objective.get("quadrature") != "left_sample_hold_cell_intersection":
        raise YieldFairSelectionError("objective quadrature must be left sample/hold cell intersection")
    schedule = _mapping(root.get("schedule"), "schedule")
    if schedule.get("stop_before_ei_if_initial_feasible_lt") != 3:
        raise YieldFairSelectionError("must stop before EI if fewer than 3 feasible of 8 initial")
    if schedule.get("literal_repeats") is not True or schedule.get("no_ci_from_identical_deterministic_repeats") is not True:
        raise YieldFairSelectionError("repeats are literal; no CI from identical deterministic repeats")
    runtime = _mapping(root.get("runtime"), "runtime")
    if runtime.get("campaign_kind") != "training" or runtime.get("record_fullstate") is not True:
        raise YieldFairSelectionError("runtime must request training full-state rows")
    return YieldFairContract(
        material=str(cell["material"]),
        kappa_xx=kappa_xx,
        kappa_yy=kappa_yy,
        prior=str(cell["prior"]),
        preparation=str(cell["preparation"]),
        nominal_scenario=str(cell["nominal_scenario"]),
        disturbed_scenario=str(cell["disturbed_scenario"]),
        timeline=str(cell["timeline"]),
        duration_s=duration,
        dt_s=dt,
        period_s=duration,
        plant_substeps=int(cell["plant_substeps"]),
        campaign_kind="training",
        record_fullstate=True,
        require_ur10e=True,
        observer_parameters=dict(OBSERVER_V3),
        path_stiffness_n_per_m=kp,
        compliance_stiffness_n_per_m=kz,
        load_onset_s=20.0,
        path_release_s=35.5,
        attitude_onset_s=20.0,
        jload_scale_n=_finite(jload.get("scale_n"), "Jload scale"),
        jpath_scale_m=_finite(jpath.get("scale_m"), "Jpath scale"),
        jatt_scale_rad=_finite(jatt.get("scale_rad"), "Jatt scale"),
        nominal_path_rms_m_max=_finite(nominal.get("path_rms_m_max"), "path_rms_m_max"),
        nominal_progress_ratio_min=_finite(nominal.get("progress_ratio_min"), "progress_ratio_min"),
        nominal_attitude_rms_rad_max=math.radians(attitude_deg),
        nominal_load_mae_n_max=_finite(nominal.get("load_mae_n_max"), "load_mae_n_max"),
        nominal_load_peak_n_max=_finite(nominal.get("load_peak_n_max"), "load_peak_n_max"),
        nominal_load_min_n_min=_finite(nominal.get("load_min_n_min"), "load_min_n_min"),
        disturbed_load_min_n_min=_finite(guards.get("load_min_n_min"), "disturbed load min"),
        disturbed_load_peak_n_max=_finite(guards.get("load_peak_n_max"), "disturbed load peak"),
        disturbed_progress_ratio_min=_finite(guards.get("progress_ratio_min"), "disturbed progress"),
        method=None,
    )


def expected_path_times(contract: YieldFairContract) -> np.ndarray:
    count = path_sample_count(contract.duration_s, contract.dt_s)
    return np.arange(count, dtype=float) * contract.dt_s


def expected_record_count(contract: YieldFairContract) -> int:
    return sum(expected_phase_counts(contract).values())


def expected_phase_counts(contract: YieldFairContract) -> dict[str, int]:
    return {
        "baseline": path_sample_count(2.0, contract.dt_s) if contract.preparation == "warm" else 0,
        "entry": path_sample_count(1.0, contract.dt_s),
        "path": path_sample_count(contract.duration_s, contract.dt_s),
    }


def _finite_vector(value: Any, size: int, name: str) -> np.ndarray:
    if value is None:
        raise YieldFairSelectionError(f"{name} is missing")
    try:
        items = list(value)
    except TypeError as error:
        raise YieldFairSelectionError(f"{name} must be a finite length-{size} vector") from error
    if len(items) != size or any(isinstance(item, bool) for item in items):
        raise YieldFairSelectionError(f"{name} must be a finite length-{size} vector")
    try:
        array = np.asarray([float(item) for item in items], dtype=float)
    except (TypeError, ValueError) as error:
        raise YieldFairSelectionError(f"{name} must be a finite length-{size} vector") from error
    if array.shape != (size,) or not np.all(np.isfinite(array)):
        raise YieldFairSelectionError(f"{name} must be a finite length-{size} vector")
    return array


def _as_mapping(value: Any, role: str) -> Mapping[str, Any]:
    if value is None:
        raise YieldFairSelectionError(f"{role} is missing")
    return _mapping(value, role)


def parse_controller_snapshot(
    state: Any,
    *,
    identity: str,
    approach: Sequence[float],
    role: str = "controller snapshot",
) -> dict[str, Any]:
    """Validate persistent controller state using the YieldController._parse_snapshot schema.

    This does not construct a controller, load QP, or replay ticks.
    """
    payload = _as_mapping(state, role)
    if payload.get("identity") != identity:
        raise YieldFairSelectionError(f"{role} identity differs")
    required = (
        "law22",
        "filter",
        "normal_estimate",
        "offset_base_m",
        "integral_n_s",
        "time_s",
        "path_time_s",
        "last_position_m",
        "last_velocity_base_m_s",
        "roll_anchor",
        "qp",
    )
    missing = [key for key in required if key not in payload]
    if missing:
        raise YieldFairSelectionError(f"{role} missing {missing}")
    law22 = _as_mapping(payload["law22"], f"{role} law22")
    if "values" not in law22:
        raise YieldFairSelectionError(f"{role} missing memory")
    values = law22["values"]
    if values is None or (isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and len(values) < SNAPSHOT_SIZE):
        raise YieldFairSelectionError(f"{role} missing memory")
    parsed_values = _finite_vector(values, SNAPSHOT_SIZE, f"{role} law22.values")
    _finite_vector(parsed_values[MSFC_MEMORY_SLICE], 12, f"{role} law22 memory")
    binding = law22.get("binding_id")
    if binding is not None and (not isinstance(binding, str) or len(binding) != 64):
        raise YieldFairSelectionError(f"{role} law22 binding_id is malformed")
    filt = _as_mapping(payload["filter"], f"{role} filter")
    _finite_vector(filt.get("filtered_force_base_n"), 3, f"{role} filtered_force_base_n")
    try:
        initialized = require_bool(filt.get("initialized"), "filter.initialized")
        estimate = _as_mapping(payload["normal_estimate"], f"{role} normal_estimate")
        inward = require_unit_vector(estimate.get("inward_normal_base"), "inward_normal_base")
        snap_approach = require_unit_vector(estimate.get("approach_inward_base"), "approach_inward_base")
        expected_approach = require_unit_vector(approach, "approach_inward_base")
        if not np.allclose(snap_approach, expected_approach, atol=1e-12, rtol=0):
            raise YieldFairSelectionError(f"{role} approach frame differs")
        _finite_vector(payload["offset_base_m"], 3, f"{role} offset_base_m")
        _finite_vector(payload["integral_n_s"], 3, f"{role} integral_n_s")
        time_s = optional_finite_time(payload["time_s"], "time_s")
        optional_finite_time(payload["path_time_s"], "path_time_s")
        _finite_vector(payload["last_position_m"], 3, f"{role} last_position_m")
        _finite_vector(payload["last_velocity_base_m_s"], 3, f"{role} last_velocity_base_m_s")
    except YieldMathError as error:
        raise YieldFairSelectionError(f"{role} invalid: {error}") from error
    if initialized != (time_s is not None):
        raise YieldFairSelectionError(f"{role} initialization clock differs")
    if initialized and payload["roll_anchor"] is None:
        raise YieldFairSelectionError(f"{role} initialized snapshot is missing roll_anchor")
    roll = payload["roll_anchor"]
    if roll is not None:
        try:
            require_rotation(roll, "roll_anchor")
        except YieldMathError as error:
            raise YieldFairSelectionError(f"{role} invalid: {error}") from error
    qp_state = _as_mapping(payload["qp"], f"{role} qp")
    if "x" not in qp_state or "y" not in qp_state:
        raise YieldFairSelectionError(f"{role} qp snapshot is malformed")
    _finite_vector(qp_state["x"], 6, f"{role} qp.x")
    _finite_vector(qp_state["y"], 12, f"{role} qp.y")
    return payload


def parse_simulator_snapshot(
    state: Any,
    *,
    identity: str,
    kinematics_kind: str,
    material: str,
    role: str = "simulator snapshot",
) -> dict[str, Any]:
    """Validate persistent plant state using the YieldSimulator.restore schema."""
    payload = _as_mapping(state, role)
    if payload.get("identity") != identity:
        raise YieldFairSelectionError(f"{role} identity differs")
    if payload.get("kinematics_kind") != kinematics_kind:
        raise YieldFairSelectionError(f"{role} kinematics identity differs")
    if payload.get("material") != material:
        raise YieldFairSelectionError(f"{role} material identity differs")
    for name, size in (
        ("q", 6),
        ("qdot", 6),
        ("qdot_cmd", 6),
        ("servo_integral", 6),
        ("sensed_force", 3),
        ("sensed_torque", 3),
        ("true_wrench", 6),
    ):
        _finite_vector(payload.get(name), size, f"{role} {name}")
    try:
        require_rotation(payload.get("rotation"), "rotation")
        time_s = optional_finite_time(payload.get("time_s"), "time_s")
    except YieldMathError as error:
        raise YieldFairSelectionError(f"{role} invalid: {error}") from error
    if time_s is None:
        raise YieldFairSelectionError(f"{role} time_s is missing")
    return payload


def _require_full_cycle_grid(rows: Sequence[Mapping[str, Any]], contract: YieldFairContract) -> np.ndarray:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise YieldFairSelectionError("incomplete evidence: rows missing")
    expected = expected_path_times(contract)
    if len(rows) != len(expected):
        raise YieldFairSelectionError("incomplete evidence: path sample grid does not cover the period")
    times = []
    dts = []
    for index, row in enumerate(rows):
        item = _mapping(row, f"row {index}")
        time_s = _finite(item.get("time_s"), f"row {index} time_s")
        dt_s = _finite(item.get("dt_s"), f"row {index} dt_s")
        times.append(time_s)
        dts.append(dt_s)
    times_a = np.asarray(times, dtype=float)
    if not np.allclose(times_a, expected, rtol=0.0, atol=1e-12):
        raise YieldFairSelectionError("time grid differs from the ceil sample grid")
    if any(dt != contract.dt_s for dt in dts):
        raise YieldFairSelectionError("time grid dt_s differs")
    last = float(expected[-1])
    if not (last < contract.period_s <= last + contract.dt_s + 1e-15):
        raise YieldFairSelectionError("ceil sample grid does not cover the unwrapped period")
    return times_a


def _record_clock(contract: YieldFairContract, index: int) -> dict[str, Any]:
    counts = expected_phase_counts(contract)
    baseline, entry, path = counts["baseline"], counts["entry"], counts["path"]
    if index < baseline:
        return {
            "phase": "baseline",
            "clock": index * contract.dt_s,
            "plant_path_time_s": 0.0,
            "scenario": "nominal",
        }
    entry_index = index - baseline
    if entry_index < entry:
        return {
            "phase": "entry",
            "clock": entry_index * contract.dt_s,
            "plant_path_time_s": 0.0,
            "scenario": "nominal",
        }
    path_index = entry_index - entry
    if path_index < path:
        clock = path_index * contract.dt_s
        return {
            "phase": "path",
            "clock": clock,
            "plant_path_time_s": clock,
            "scenario": None,
        }
    raise YieldFairSelectionError("record index exceeds entry/path count")


def _require_fullstate(artifact: Mapping[str, Any], contract: YieldFairContract) -> None:
    if contract.record_fullstate is not True:
        raise YieldFairSelectionError("full-state recording is required")
    records = artifact.get("records")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise YieldFairSelectionError("missing full-state records")
    counts = expected_phase_counts(contract)
    expected = counts["baseline"] + counts["entry"] + counts["path"]
    if len(records) != expected:
        raise YieldFairSelectionError("partial full-state evidence")
    identity = artifact.get("identity")
    if not isinstance(identity, str) or not identity:
        raise YieldFairSelectionError("controller identity missing")
    payload = _mapping(artifact.get("identity_payload"), "identity_payload")
    if "approach_inward_base" not in payload:
        raise YieldFairSelectionError("controller approach identity missing")
    _mapping(artifact.get("plant_identity_payload"), "plant_identity_payload")
    initial_c = artifact.get("initial_controller_snapshot")
    initial_p = artifact.get("initial_simulator_snapshot")
    final_c = artifact.get("final_controller_snapshot")
    final_p = artifact.get("final_simulator_snapshot")
    formal = artifact.get("formal_initial_snapshot")
    if initial_p is None:
        raise YieldFairSelectionError("missing initial simulator snapshot")
    plant_identity = _as_mapping(initial_p, "initial simulator snapshot").get("identity")
    if not isinstance(plant_identity, str) or not plant_identity:
        raise YieldFairSelectionError("plant identity missing")
    parse_controller_snapshot(
        initial_c, identity=identity, approach=payload["approach_inward_base"],
        role="initial controller snapshot",
    )
    parse_simulator_snapshot(
        initial_p, identity=plant_identity,
        kinematics_kind=str(artifact.get("kinematics_kind")),
        material=contract.material, role="initial simulator snapshot",
    )
    last_entry = counts["baseline"] + counts["entry"] - 1
    last_index = expected - 1
    for index, record in enumerate(records):
        item = _as_mapping(record, f"record {index}")
        clock = _record_clock(contract, index)
        if _finite(item.get("dt_s"), f"record {index} dt_s") != contract.dt_s:
            raise YieldFairSelectionError("record clocks differ")
        if _finite(item.get("plant_path_time_s"), f"record {index} plant_path_time_s") != clock["plant_path_time_s"]:
            raise YieldFairSelectionError("record clocks differ")
        reference = _as_mapping(item.get("reference"), f"record {index} reference")
        if reference.get("phase") != clock["phase"]:
            raise YieldFairSelectionError("entry/path record phase differs")
        expected_scenario = artifact.get("scenario") if clock["phase"] == "path" else clock["scenario"]
        if item.get("scenario") != expected_scenario:
            raise YieldFairSelectionError("record scenario differs")
        parse_controller_snapshot(
            item.get("controller_snapshot"), identity=identity,
            approach=payload["approach_inward_base"], role=f"record {index} controller snapshot",
        )
        parse_simulator_snapshot(
            item.get("simulator_snapshot"), identity=plant_identity,
            kinematics_kind=str(artifact.get("kinematics_kind")),
            material=contract.material, role=f"record {index} simulator snapshot",
        )
    last_entry_record = _as_mapping(records[last_entry], "last entry record")
    last_record = _as_mapping(records[last_index], "last record")
    formal_map = _as_mapping(formal, "formal_initial_snapshot")
    if formal_map.get("controller") != last_entry_record.get("controller_snapshot"):
        raise YieldFairSelectionError("formal initial controller snapshot differs from last entry")
    if formal_map.get("plant") != last_entry_record.get("simulator_snapshot"):
        raise YieldFairSelectionError("formal initial plant snapshot differs from last entry")
    parse_controller_snapshot(
        formal_map.get("controller"), identity=identity,
        approach=payload["approach_inward_base"], role="formal initial controller snapshot",
    )
    parse_simulator_snapshot(
        formal_map.get("plant"), identity=plant_identity,
        kinematics_kind=str(artifact.get("kinematics_kind")),
        material=contract.material, role="formal initial plant snapshot",
    )
    if final_c != last_record.get("controller_snapshot"):
        raise YieldFairSelectionError("final controller snapshot differs from last record")
    if final_p != last_record.get("simulator_snapshot"):
        raise YieldFairSelectionError("final plant snapshot differs from last record")
    parse_controller_snapshot(
        final_c, identity=identity, approach=payload["approach_inward_base"],
        role="final controller snapshot",
    )
    parse_simulator_snapshot(
        final_p, identity=plant_identity,
        kinematics_kind=str(artifact.get("kinematics_kind")),
        material=contract.material, role="final simulator snapshot",
    )


def _require_identities(artifact: Mapping[str, Any], contract: YieldFairContract, *, method: str) -> None:
    if artifact.get("method") != method:
        raise YieldFairSelectionError("candidate/controller method differs")
    if artifact.get("material") != contract.material:
        raise YieldFairSelectionError("material differs")
    if artifact.get("preparation") != contract.preparation:
        raise YieldFairSelectionError("preparation differs")
    if artifact.get("timeline") != contract.timeline:
        raise YieldFairSelectionError("timeline differs")
    if _finite(artifact.get("dt_s"), "artifact dt_s") != contract.dt_s:
        raise YieldFairSelectionError("dt_s differs")
    if not math.isclose(_finite(artifact.get("duration_s"), "artifact duration_s"), contract.duration_s, rel_tol=0.0, abs_tol=1e-12):
        raise YieldFairSelectionError("duration_s differs")
    if artifact.get("campaign_kind") == "holdout":
        raise YieldFairSelectionError("holdout rows/config are refused as training")
    if artifact.get("campaign_kind") != contract.campaign_kind:
        raise YieldFairSelectionError("campaign_kind differs")
    if contract.require_ur10e and artifact.get("kinematics_kind") != contract.kinematics_kind:
        raise YieldFairSelectionError("plant kinematics identity differs")
    payload = _mapping(artifact.get("identity_payload"), "identity_payload")
    plant = _mapping(artifact.get("plant_identity_payload"), "plant_identity_payload")
    if payload.get("law_frame") != contract.law_frame:
        raise YieldFairSelectionError("law frame differs")
    settings = _mapping(payload.get("settings"), "identity settings")
    if _finite(settings.get("path_stiffness_n_per_m"), "Kp") != contract.path_stiffness_n_per_m:
        raise YieldFairSelectionError("shared outer Kp differs")
    if _finite(settings.get("compliance_stiffness_n_per_m"), "Kz") != contract.compliance_stiffness_n_per_m:
        raise YieldFairSelectionError("shared outer Kz differs")
    estimator = _mapping(payload.get("estimator_parameters"), "estimator_parameters")
    if "initial_inward_normal_base" in estimator:
        raise YieldFairSelectionError("prior differs; approach prior required")
    for name, expected in contract.observer_parameters.items():
        actual = estimator.get(name)
        if _finite(actual, f"observer {name}") != _finite(expected, f"observer {name}"):
            raise YieldFairSelectionError("observer identity differs")
    parameters = payload.get("parameters")
    if not isinstance(parameters, Mapping):
        raise YieldFairSelectionError("actual law parameters missing")
    if contract.candidate_parameters is not None:
        expected_names = set(contract.candidate_parameters)
        actual_names = set(parameters)
        if expected_names != actual_names:
            raise YieldFairSelectionError("candidate law parameter fields differ")
        for name, expected in contract.candidate_parameters.items():
            if _finite(parameters.get(name), name) != expected:
                raise YieldFairSelectionError("actual law parameters differ from the candidate")
    if plant.get("integration_substeps") != contract.plant_substeps:
        raise YieldFairSelectionError("plant identity differs")
    surface = _mapping(plant.get("surface"), "plant surface")
    if _finite(surface.get("kappa_xx"), "plant kappa_xx") != contract.kappa_xx:
        raise YieldFairSelectionError("surface identity differs")
    if _finite(surface.get("kappa_yy"), "plant kappa_yy") != contract.kappa_yy:
        raise YieldFairSelectionError("surface identity differs")
    recorded_surface = artifact.get("surface_parameters")
    if recorded_surface is not None:
        recorded = _mapping(recorded_surface, "surface_parameters")
        if _finite(recorded.get("kappa_xx"), "surface kappa_xx") != contract.kappa_xx:
            raise YieldFairSelectionError("surface identity differs")
        if _finite(recorded.get("kappa_yy"), "surface kappa_yy") != contract.kappa_yy:
            raise YieldFairSelectionError("surface identity differs")
    if contract.source_hashes is not None:
        hashes = _mapping(artifact.get("source_hashes"), "source_hashes")
        if dict(hashes) != dict(contract.source_hashes):
            raise YieldFairSelectionError("source identity differs")
    if contract.protocol_sha256 is not None and artifact.get("protocol_sha256") != contract.protocol_sha256:
        raise YieldFairSelectionError("source/protocol identity differs")
    if not artifact.get("identity"):
        raise YieldFairSelectionError("controller identity missing")


def _load_series(rows: Sequence[Mapping[str, Any]], name: str) -> np.ndarray:
    values = []
    for index, row in enumerate(rows):
        values.append(_finite(row.get(name), f"row {index} {name}"))
    array = np.asarray(values, dtype=float)
    if not np.all(np.isfinite(array)):
        raise YieldFairSelectionError(f"NaN/non-finite {name}")
    return array


def _positions(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    values = [_vector3(row.get("position_m"), "position_m") for row in rows]
    return np.asarray(values, dtype=float)


def _native_failed(artifact: Mapping[str, Any]) -> bool:
    metrics = artifact.get("metrics")
    if isinstance(metrics, Mapping) and metrics.get("failed") is True:
        return True
    return False


def inspect_member(
    artifact: Mapping[str, Any],
    contract: YieldFairContract,
    *,
    condition: str,
) -> YieldFairMemberResult:
    payload = _mapping(artifact, "artifact")
    if condition not in {"nominal", "disturbed"}:
        raise YieldFairSelectionError("condition must be nominal or disturbed")
    expected_scenario = contract.nominal_scenario if condition == "nominal" else contract.disturbed_scenario
    if payload.get("scenario") != expected_scenario:
        raise YieldFairSelectionError("configured scenario differs")
    method = contract.method or payload.get("method")
    if method not in METHODS:
        raise YieldFairSelectionError("unknown candidate/controller")
    reported = dict(_mapping(payload.get("metrics"), "metrics")) if isinstance(payload.get("metrics"), Mapping) else {}
    if _native_failed(payload):
        return YieldFairMemberResult(
            condition=condition,
            valid=False,
            failed=True,
            full_cycle=False,
            nominal_feasible=False if condition == "nominal" else None,
            disturbed_guards_ok=False if condition == "disturbed" else None,
            checks={"failed": True},
            recomputed_metrics={},
            reported_metrics=reported,
            reason="failed member",
        )
    _require_identities(payload, contract, method=str(method))
    rows = payload.get("rows")
    times = _require_full_cycle_grid(rows, contract)
    _require_fullstate(payload, contract)
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
    recomputed = summarize_trial(
        list(rows),
        failed=False,
        failure_message=None,
        scenario=str(payload["scenario"]),
        material=contract.material,
        method=str(method),
        dt_s=contract.dt_s,
        kinematics_kind=str(payload.get("kinematics_kind")),
        campaign_kind=contract.campaign_kind,
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
        _finite(value, role)
    full_cycle = (
        len(times) == path_sample_count(contract.duration_s, contract.dt_s)
        and math.isclose(contract.duration_s, contract.period_s, rel_tol=0.0, abs_tol=1e-12)
        and contract.timeline == "full_cycle"
    )
    # Do not trust artifact['full_cycle']; only the recomputed coverage above.
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
        feasible = all(checks[name] is True for name in checks)
        return YieldFairMemberResult(
            condition=condition,
            valid=True,
            failed=False,
            full_cycle=full_cycle,
            nominal_feasible=bool(feasible),
            disturbed_guards_ok=None,
            checks=checks,
            recomputed_metrics=recomputed,
            reported_metrics=reported,
        )
    checks = {
        "load_min_ok": minimum >= contract.disturbed_load_min_n_min,
        "load_peak_ok": peak <= contract.disturbed_load_peak_n_max,
        "progress_ok": progress >= contract.disturbed_progress_ratio_min,
        "full_cycle": full_cycle,
        "no_failure": True,
    }
    guards = all(checks[name] is True for name in checks)
    return YieldFairMemberResult(
        condition=condition,
        valid=True,
        failed=False,
        full_cycle=full_cycle,
        nominal_feasible=None,
        disturbed_guards_ok=bool(guards),
        checks=checks,
        recomputed_metrics=recomputed,
        reported_metrics=reported,
    )


def _pair_identities_match(nominal: Mapping[str, Any], disturbed: Mapping[str, Any]) -> None:
    for key in ("method", "material", "dt_s", "duration_s", "preparation", "timeline", "identity"):
        if nominal.get(key) != disturbed.get(key):
            raise YieldFairSelectionError(f"mismatched pair {key}")
    n_payload = _mapping(nominal.get("identity_payload"), "nominal identity")
    d_payload = _mapping(disturbed.get("identity_payload"), "disturbed identity")
    if n_payload.get("parameters") != d_payload.get("parameters"):
        raise YieldFairSelectionError("mismatched pair candidate parameters")
    if n_payload.get("estimator_parameters") != d_payload.get("estimator_parameters"):
        raise YieldFairSelectionError("mismatched pair observer identity")
    if nominal.get("plant_identity_payload") != disturbed.get("plant_identity_payload"):
        raise YieldFairSelectionError("mismatched pair plant identity")
    if nominal.get("source_hashes") != disturbed.get("source_hashes"):
        raise YieldFairSelectionError("mismatched pair source identity")


def _objective_components(
    nominal_rows: Sequence[Mapping[str, Any]],
    disturbed_rows: Sequence[Mapping[str, Any]],
    contract: YieldFairContract,
) -> dict[str, float]:
    times = _require_full_cycle_grid(nominal_rows, contract)
    _require_full_cycle_grid(disturbed_rows, contract)
    load_n = _load_series(nominal_rows, "true_normal_load_n")
    load_d = _load_series(disturbed_rows, "true_normal_load_n")
    att_n = _load_series(nominal_rows, "orientation_error_rad")
    att_d = _load_series(disturbed_rows, "orientation_error_rad")
    pos_n = _positions(nominal_rows)
    pos_d = _positions(disturbed_rows)
    jload = 0.0
    jpath = 0.0
    jatt = 0.0
    dt = contract.dt_s
    period = contract.period_s
    for index, time_s in enumerate(times):
        t = float(time_s)
        load_term = abs(load_d[index] - load_n[index]) / contract.jload_scale_n
        path_term = float(np.linalg.norm(pos_d[index] - pos_n[index])) / contract.jpath_scale_m
        att_term = abs(att_d[index] - att_n[index]) / contract.jatt_scale_rad
        jload += load_term * cell_weight(t, dt, contract.load_onset_s, period, period)
        jpath += path_term * cell_weight(t, dt, contract.path_release_s, period, period)
        jatt += att_term * cell_weight(t, dt, contract.attitude_onset_s, period, period)
    for name, value in (("Jload", jload), ("Jpath", jpath), ("Jatt", jatt)):
        if not math.isfinite(value) or value < 0.0:
            raise YieldFairSelectionError(f"{name} is not a finite non-negative objective component")
    total = jload + jpath + jatt
    return {"Jload": float(jload), "Jpath": float(jpath), "Jatt": float(jatt), "J": float(total)}


def _recovery_window_covered(contract: YieldFairContract) -> bool:
    times = expected_path_times(contract)
    if len(times) == 0:
        return False
    last = float(times[-1])
    remaining = contract.period_s - contract.path_release_s
    return last >= contract.path_release_s and remaining >= RECOVERY_REMAINING_S


def evaluate_pair(
    nominal: Mapping[str, Any],
    disturbed: Mapping[str, Any],
    contract: YieldFairContract,
) -> YieldFairPairResult:
    nominal_art = _mapping(nominal, "nominal artifact")
    disturbed_art = _mapping(disturbed, "disturbed artifact")
    reported_n = dict(nominal_art.get("metrics") or {}) if isinstance(nominal_art.get("metrics"), Mapping) else {}
    reported_d = dict(disturbed_art.get("metrics") or {}) if isinstance(disturbed_art.get("metrics"), Mapping) else {}

    def _invalid(reason: str, **fields: Any) -> YieldFairPairResult:
        return YieldFairPairResult(
            valid=False,
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
        raise YieldFairSelectionError("pair scenarios must differ")
    _pair_identities_match(nominal_art, disturbed_art)
    bound = contract
    if contract.method is None:
        bound = replace(contract, method=str(nominal_art.get("method")))
    nominal_member = inspect_member(nominal_art, bound, condition="nominal")
    disturbed_member = inspect_member(disturbed_art, bound, condition="disturbed")
    if not nominal_member.valid or not disturbed_member.valid:
        return _invalid(
            "failed member; retained, no performance ranking",
            nominal_feasible=bool(nominal_member.nominal_feasible),
            disturbed_guards_ok=False,
            pair_feasible=False,
            recomputed_metrics_nominal=nominal_member.recomputed_metrics or None,
            recomputed_metrics_disturbed=disturbed_member.recomputed_metrics or None,
            nominal_checks=dict(nominal_member.checks),
            disturbed_checks=dict(disturbed_member.checks),
        )
    if _recovery_window_covered(bound):
        try:
            reported_recovery = compare_pair(nominal_art, disturbed_art)
        except (TypeError, ValueError, KeyError) as error:
            raise YieldFairSelectionError(
                f"recovery computation failed for complete pair: {error}"
            ) from error
    else:
        reported_recovery = {
            "eligible": False,
            "reason": "recovery unavailable: sample grid does not cover the disturbance window",
            "right_censored": True,
        }
    components = _objective_components(nominal_art["rows"], disturbed_art["rows"], bound)
    nominal_feasible = _bool(nominal_member.nominal_feasible, "nominal_feasible")
    guards = _bool(disturbed_member.disturbed_guards_ok, "disturbed_guards_ok")
    pair_feasible = bool(nominal_feasible and guards)
    return YieldFairPairResult(
        valid=True,
        nominal_feasible=nominal_feasible,
        disturbed_guards_ok=guards,
        pair_feasible=pair_feasible,
        objective=float(components["J"]),
        objective_components=components,
        objective_eligible=True,
        reported_metrics_nominal=reported_n,
        reported_metrics_disturbed=reported_d,
        reported_recovery=reported_recovery,
        recomputed_metrics_nominal=nominal_member.recomputed_metrics,
        recomputed_metrics_disturbed=disturbed_member.recomputed_metrics,
        nominal_checks=dict(nominal_member.checks),
        disturbed_checks=dict(disturbed_member.checks),
        claim_scope=(
            "prospective DEVELOPMENT-model comparison; no real precise-contact, "
            "safety, or physical qualification claim; " + FULLSTATE_LIMIT
        ),
    )


def bind_candidate(contract: YieldFairContract, *, method: str, parameters: Mapping[str, float],
                   source_hashes: Mapping[str, str] | None = None,
                   protocol_sha256: str | None = None) -> YieldFairContract:
    if method not in METHODS:
        raise YieldFairSelectionError("unknown candidate/controller")
    return replace(
        contract,
        method=method,
        candidate_parameters={str(name): _finite(value, name) for name, value in parameters.items()},
        source_hashes=source_hashes if source_hashes is not None else contract.source_hashes,
        protocol_sha256=protocol_sha256 if protocol_sha256 is not None else contract.protocol_sha256,
    )
