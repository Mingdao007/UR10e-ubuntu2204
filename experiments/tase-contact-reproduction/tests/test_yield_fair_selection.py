"""Falsify pair evaluation; synthetic artifacts only except where noted."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from contact_yield_protocol import PERIOD_S
from yield_fair_selection import (
    OBSERVER_V3,
    SNAPSHOT_SIZE,
    YieldFairContract,
    YieldFairSelectionError,
    bind_candidate,
    cell_weight,
    contract_from_config,
    evaluate_pair,
    expected_phase_counts,
    expected_record_count,
    inspect_member,
    load_contract,
    path_sample_count,
)


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_IDENTITY = "a" * 64
PLANT_IDENTITY = "b" * 64
LAW_BINDING = "c" * 64
APPROACH = [0.0, 0.0, -1.0]


def _contract(**overrides) -> YieldFairContract:
    values = dict(
        material="stiff_low_mu",
        kappa_xx=0.8,
        kappa_yy=0.4,
        prior="approach",
        preparation="cold",
        nominal_scenario="nominal",
        disturbed_scenario="sustained_release_oblique",
        timeline="full_cycle",
        duration_s=0.009,
        dt_s=0.002,
        period_s=0.009,
        plant_substeps=8,
        campaign_kind="training",
        record_fullstate=True,
        require_ur10e=True,
        observer_parameters=dict(OBSERVER_V3),
        path_stiffness_n_per_m=120.0,
        compliance_stiffness_n_per_m=0.0,
        load_onset_s=0.004,
        path_release_s=0.006,
        attitude_onset_s=0.004,
        jload_scale_n=0.5,
        jpath_scale_m=0.002,
        jatt_scale_rad=0.05,
        nominal_path_rms_m_max=0.00548,
        nominal_progress_ratio_min=0.850,
        nominal_attitude_rms_rad_max=math.radians(2.81),
        nominal_load_mae_n_max=0.127,
        nominal_load_peak_n_max=6.27,
        nominal_load_min_n_min=1.0,
        disturbed_load_min_n_min=1.0,
        disturbed_load_peak_n_max=8.0,
        disturbed_progress_ratio_min=0.850,
        method="SFC",
        candidate_parameters={"m": 4.0, "mu": 393.0, "n": 3.0, "g": 0.05},
    )
    values.update(overrides)
    return YieldFairContract(**values)


def _rows(contract: YieldFairContract, *, load=5.0, path=0.0, attitude=0.0, progress=1.0, position=None):
    count = path_sample_count(contract.duration_s, contract.dt_s)
    pos = [0.0, 0.0, 0.0] if position is None else list(position)
    rows = []
    for index in range(count):
        rows.append({
            "time_s": index * contract.dt_s,
            "dt_s": contract.dt_s,
            "true_normal_load_n": load,
            "force_error_n": load - 5.0,
            "path_error_m": path,
            "orientation_error_rad": attitude,
            "normal_estimation_error_rad": 0.0,
            "actual_progress_m_s": progress,
            "reference_progress_m_s": 1.0,
            "position_m": list(pos),
            "saturated": False,
            "qp_intervention": False,
        })
    return rows


def _controller_snapshot(*, initialized: bool, time_s, path_time_s, law_values=None):
    return {
        "identity": CONTROLLER_IDENTITY,
        "law22": {
            "values": list(law_values if law_values is not None else [0.0] * SNAPSHOT_SIZE),
            "binding_id": LAW_BINDING,
        },
        "filter": {"filtered_force_base_n": [0.0, 0.0, 0.0], "initialized": initialized},
        "normal_estimate": {
            "inward_normal_base": list(APPROACH),
            "approach_inward_base": list(APPROACH),
        },
        "offset_base_m": [0.0, 0.0, 0.0],
        "integral_n_s": [0.0, 0.0, 0.0],
        "time_s": time_s,
        "path_time_s": path_time_s,
        "last_position_m": [0.0, 0.0, 0.0],
        "last_velocity_base_m_s": [0.0, 0.0, 0.0],
        "roll_anchor": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]] if initialized else None,
        "qp": {"x": [0.0] * 6, "y": [0.0] * 12},
    }


def _plant_snapshot(*, time_s: float):
    return {
        "identity": PLANT_IDENTITY,
        "q": [0.0] * 6,
        "qdot": [0.0] * 6,
        "qdot_cmd": [0.0] * 6,
        "servo_integral": [0.0] * 6,
        "rotation": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        "sensed_force": [0.0, 0.0, 5.0],
        "sensed_torque": [0.0, 0.0, 0.0],
        "true_wrench": [0.0, 0.0, 5.0, 0.0, 0.0, 0.0],
        "time_s": time_s,
        "kinematics_kind": "ur10e_calibrated_pinocchio",
        "material": "stiff_low_mu",
    }


def _fullstate_records(contract: YieldFairContract, *, scenario: str):
    counts = expected_phase_counts(contract)
    records = []
    phases = (
        [("baseline", counts["baseline"])] +
        [("entry", counts["entry"])] +
        [("path", counts["path"])]
    )
    for phase, count in phases:
        for index in range(count):
            clock = index * contract.dt_s
            path_time = clock if phase == "path" else 0.0
            controller = _controller_snapshot(
                initialized=True, time_s=clock + contract.dt_s, path_time_s=path_time if phase == "path" else None,
            )
            plant = _plant_snapshot(time_s=clock + contract.dt_s)
            records.append({
                "dt_s": contract.dt_s,
                "scenario": scenario if phase == "path" else "nominal",
                "plant_path_time_s": path_time,
                "reference": {
                    "phase": phase,
                    "path_time_s": clock if phase == "path" else None,
                },
                "controller_snapshot": controller,
                "simulator_snapshot": plant,
            })
    last_entry = counts["baseline"] + counts["entry"] - 1
    return {
        "records": records,
        "initial_controller_snapshot": _controller_snapshot(initialized=False, time_s=None, path_time_s=None),
        "initial_simulator_snapshot": _plant_snapshot(time_s=0.0),
        "formal_initial_snapshot": {
            "controller": copy.deepcopy(records[last_entry]["controller_snapshot"]),
            "plant": copy.deepcopy(records[last_entry]["simulator_snapshot"]),
        },
        "final_controller_snapshot": copy.deepcopy(records[-1]["controller_snapshot"]),
        "final_simulator_snapshot": copy.deepcopy(records[-1]["simulator_snapshot"]),
    }


def _artifact(contract: YieldFairContract, *, scenario, rows=None, **extra):
    rows = _rows(contract) if rows is None else rows
    fullstate = _fullstate_records(contract, scenario=scenario)
    payload = {
        "method": contract.method or "SFC",
        "scenario": scenario,
        "material": contract.material,
        "dt_s": contract.dt_s,
        "duration_s": contract.duration_s,
        "preparation": contract.preparation,
        "timeline": contract.timeline,
        "identity": CONTROLLER_IDENTITY,
        "campaign_kind": contract.campaign_kind,
        "kinematics_kind": contract.kinematics_kind,
        "protocol_sha256": contract.protocol_sha256,
        "source_hashes": dict(contract.source_hashes or {}),
        "metrics": {"failed": False, "sentinel": "keep-reported", "force_peak_n": 5.0},
        "rows": rows,
        "identity_payload": {
            "law_frame": contract.law_frame,
            "parameters": dict(contract.candidate_parameters or {}),
            "estimator_parameters": dict(contract.observer_parameters),
            "approach_inward_base": list(APPROACH),
            "settings": {
                "path_stiffness_n_per_m": contract.path_stiffness_n_per_m,
                "compliance_stiffness_n_per_m": contract.compliance_stiffness_n_per_m,
            },
        },
        "plant_identity_payload": {
            "integration_substeps": contract.plant_substeps,
            "surface": {"kappa_xx": contract.kappa_xx, "kappa_yy": contract.kappa_yy},
        },
        "surface_parameters": {"kappa_xx": contract.kappa_xx, "kappa_yy": contract.kappa_yy},
        "full_cycle": True,
        **fullstate,
    }
    payload.update(extra)
    return payload


def _pair(contract=None, *, disturbed_rows=None, nominal_rows=None):
    contract = contract or _contract()
    nominal = _artifact(contract, scenario="nominal", rows=nominal_rows)
    disturbed = _artifact(contract, scenario="sustained_release_oblique", rows=disturbed_rows)
    return contract, nominal, disturbed


def test_v1_config_is_development_comparison_not_qualification():
    contract = load_contract(ROOT / "config" / "yield_fair_campaign_v1.json")
    assert contract.duration_s == PERIOD_S
    assert contract.duration_s == 2.0 * math.pi / 0.1
    assert contract.material == "stiff_low_mu"
    assert (contract.kappa_xx, contract.kappa_yy) == (0.8, 0.4)
    assert contract.prior == "approach"
    assert contract.disturbed_scenario == "sustained_release_oblique"
    assert contract.plant_substeps == 8
    assert contract.path_stiffness_n_per_m == 120.0
    assert contract.compliance_stiffness_n_per_m == 0.0
    assert contract.nominal_attitude_rms_rad_max == math.radians(2.81)
    assert contract.jatt_scale_rad == 0.05
    assert contract.path_release_s == 35.5
    assert contract.campaign_kind == "training"
    raw = json.loads((ROOT / "config" / "yield_fair_campaign_v1.json").read_text())
    raw["does_not_implement_holdout"] = False
    with pytest.raises(YieldFairSelectionError, match="holdout"):
        contract_from_config(raw)


def test_rejects_mismatched_pair_candidate_frame_grid_incomplete_and_nan():
    contract, nominal, disturbed = _pair()
    evaluate_pair(nominal, disturbed, contract)

    other = copy.deepcopy(disturbed)
    other["method"] = "DSFC"
    other["identity"] = "other"
    with pytest.raises(YieldFairSelectionError, match="mismatched pair"):
        evaluate_pair(nominal, other, contract)

    bad_candidate = copy.deepcopy(nominal)
    bad_candidate["identity_payload"]["parameters"]["m"] = 9.0
    with pytest.raises(YieldFairSelectionError, match="actual law parameters differ"):
        inspect_member(bad_candidate, contract, condition="nominal")

    bad_frame = copy.deepcopy(nominal)
    bad_frame["identity_payload"]["law_frame"] = "tool"
    with pytest.raises(YieldFairSelectionError, match="law frame"):
        inspect_member(bad_frame, contract, condition="nominal")

    short = copy.deepcopy(nominal)
    short["rows"] = short["rows"][:-1]
    with pytest.raises(YieldFairSelectionError, match="incomplete"):
        inspect_member(short, contract, condition="nominal")

    grid = copy.deepcopy(nominal)
    grid["rows"][1]["time_s"] = 0.003
    with pytest.raises(YieldFairSelectionError, match="time grid"):
        inspect_member(grid, contract, condition="nominal")

    missing_state = copy.deepcopy(nominal)
    missing_state["records"] = []
    with pytest.raises(YieldFairSelectionError, match="full-state"):
        inspect_member(missing_state, contract, condition="nominal")

    nan_rows = _rows(contract)
    nan_rows[0]["true_normal_load_n"] = float("nan")
    nan_art = _artifact(contract, scenario="nominal", rows=nan_rows)
    with pytest.raises(YieldFairSelectionError, match="finite"):
        inspect_member(nan_art, contract, condition="nominal")

    bool_rows = _rows(contract)
    bool_rows[0]["actual_progress_m_s"] = True
    bool_art = _artifact(contract, scenario="nominal", rows=bool_rows)
    with pytest.raises(YieldFairSelectionError, match="finite"):
        inspect_member(bool_art, contract, condition="nominal")


def test_does_not_trust_full_cycle_boolean_or_reported_metrics_alone():
    contract, nominal, disturbed = _pair()
    nominal["full_cycle"] = True
    nominal["metrics"] = {"failed": False, "path_rmse_m": 0.0, "progress_ratio": 1.0, "sentinel": "keep-reported"}
    nominal["rows"] = []
    with pytest.raises(YieldFairSelectionError, match="incomplete"):
        inspect_member(nominal, contract, condition="nominal")


def test_progress_freezing_disturbed_is_not_pair_feasible_and_keeps_true_nominal():
    contract = _contract()
    nominal_rows = _rows(contract, progress=1.0, load=5.0)
    disturbed_rows = _rows(contract, progress=0.0, load=5.0)
    result = evaluate_pair(
        _artifact(contract, scenario="nominal", rows=nominal_rows),
        _artifact(contract, scenario="sustained_release_oblique", rows=disturbed_rows),
        contract,
    )
    assert result.valid is True
    assert result.nominal_feasible is True
    assert result.disturbed_guards_ok is False
    assert result.pair_feasible is False
    assert result.objective_eligible is True
    assert result.objective is not None
    assert result.reported_recovery["reason"].startswith("recovery unavailable")
    assert result.reported_metrics_nominal["sentinel"] == "keep-reported"
    assert "sentinel" not in result.recomputed_metrics_nominal


def test_infeasible_disturbed_load_guard_does_not_overwrite_nominal_field():
    contract = _contract()
    result = evaluate_pair(
        _artifact(contract, scenario="nominal", rows=_rows(contract, load=5.0)),
        _artifact(contract, scenario="sustained_release_oblique", rows=_rows(contract, load=9.0)),
        contract,
    )
    assert result.nominal_feasible is True
    assert result.disturbed_guards_ok is False
    assert result.pair_feasible is False
    assert result.objective is not None


def test_exact_final_cell_weights_and_known_analytic_objective():
    contract = _contract()
    assert path_sample_count(0.009, 0.002) == 5
    assert cell_weight(0.008, 0.002, 0.0, 0.009, 0.009) == pytest.approx(0.001)
    assert cell_weight(0.008, 0.002, 0.0, 0.009, 0.009) != pytest.approx(0.002)
    nominal_rows = _rows(contract, load=5.0, attitude=0.0, position=(0.0, 0.0, 0.0))
    disturbed_rows = _rows(contract, load=5.5, attitude=0.05, position=(0.002, 0.0, 0.0))
    result = evaluate_pair(
        _artifact(contract, scenario="nominal", rows=nominal_rows),
        _artifact(contract, scenario="sustained_release_oblique", rows=disturbed_rows),
        contract,
    )
    assert result.pair_feasible is True
    assert result.objective_eligible is True
    assert result.objective_components["Jload"] == pytest.approx(0.005)
    assert result.objective_components["Jpath"] == pytest.approx(0.003)
    assert result.objective_components["Jatt"] == pytest.approx(0.005)
    assert result.objective == pytest.approx(0.013)
    assert result.reported_recovery["eligible"] is False
    assert "recovery unavailable" in result.reported_recovery["reason"]


def test_failed_member_has_no_objective():
    contract, nominal, disturbed = _pair()
    disturbed["metrics"] = {"failed": True, "sentinel": "keep-reported"}
    result = evaluate_pair(nominal, disturbed, contract)
    assert result.valid is False
    assert result.objective is None
    assert result.objective_eligible is False
    assert result.pair_feasible is False
    assert result.reported_metrics_disturbed["sentinel"] == "keep-reported"


def test_bind_candidate_rejects_holdout_kind():
    with pytest.raises(YieldFairSelectionError, match="holdout"):
        _contract(campaign_kind="holdout")


def test_null_empty_nan_missing_memory_and_mismatched_final_snapshots_are_rejected():
    contract = _contract()
    nominal = _artifact(contract, scenario="nominal")

    null_art = copy.deepcopy(nominal)
    null_art["records"][0]["controller_snapshot"] = None
    with pytest.raises(YieldFairSelectionError, match="missing"):
        inspect_member(null_art, contract, condition="nominal")

    empty_art = copy.deepcopy(nominal)
    empty_art["records"][0]["controller_snapshot"] = {}
    with pytest.raises(YieldFairSelectionError, match="identity"):
        inspect_member(empty_art, contract, condition="nominal")

    nan_art = copy.deepcopy(nominal)
    nan_art["records"][1]["controller_snapshot"]["law22"]["values"][0] = float("nan")
    with pytest.raises(YieldFairSelectionError, match="finite"):
        inspect_member(nan_art, contract, condition="nominal")

    memory_art = copy.deepcopy(nominal)
    memory_art["records"][2]["controller_snapshot"]["law22"]["values"] = [0.0] * 7
    with pytest.raises(YieldFairSelectionError, match="missing memory"):
        inspect_member(memory_art, contract, condition="nominal")

    missing_memory = copy.deepcopy(nominal)
    missing_memory["records"][2]["controller_snapshot"]["law22"]["values"] = None
    with pytest.raises(YieldFairSelectionError, match="missing memory"):
        inspect_member(missing_memory, contract, condition="nominal")

    mismatch = copy.deepcopy(nominal)
    mismatch["final_controller_snapshot"] = _controller_snapshot(
        initialized=True, time_s=1.0, path_time_s=0.0,
    )
    mismatch["final_controller_snapshot"]["identity"] = "f" * 64
    with pytest.raises(YieldFairSelectionError, match="final controller snapshot differs"):
        inspect_member(mismatch, contract, condition="nominal")


def test_complete_pair_does_not_swallow_recovery_failure_when_window_is_covered():
    contract = _contract(
        duration_s=0.3,
        period_s=0.3,
        dt_s=0.1,
        load_onset_s=0.0,
        path_release_s=0.1,
        attitude_onset_s=0.0,
    )
    nominal = _artifact(contract, scenario="nominal")
    disturbed = _artifact(contract, scenario="sustained_release_oblique")
    del nominal["metrics"]["force_peak_n"]
    with pytest.raises(YieldFairSelectionError, match="recovery computation failed"):
        evaluate_pair(nominal, disturbed, contract)
