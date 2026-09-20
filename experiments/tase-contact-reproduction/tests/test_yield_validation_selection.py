"""Falsify reserved-cell holdout evaluation; synthetic artifacts only."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import numpy as np
import pytest

from contact_yield_protocol import PERIOD_S, parse_scenario
from yield_fair_selection import (
    OBSERVER_V3,
    SNAPSHOT_SIZE,
    YieldFairSelectionError,
    cell_weight,
    contract_from_config,
    evaluate_pair as training_evaluate_pair,
    expected_phase_counts,
    load_contract,
    path_sample_count,
)
from yield_validation_selection import (
    ARMS,
    MSFC_IDENTITY_METRIC,
    YieldValidationContract,
    YieldValidationSelectionError,
    arm_law_parameters,
    base_failed_or_incomplete,
    cell_weight as reused_cell_weight,
    compare_resolution_settings,
    derive_prior_basis,
    evaluate_pair,
    inspect_member,
    load_reservation,
    prior_inward_normal,
    refinements_required,
    scenario_release_s,
)


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_IDENTITY = "a" * 64
PLANT_IDENTITY = "b" * 64
LAW_BINDING = "c" * 64
APPROACH = [0.0, 0.0, -1.0]
ALONG = [1.0, 0.0, 0.0]
ACROSS = [0.0, -1.0, 0.0]


def _prior(direction="approach", angle_deg=0.0):
    return prior_inward_normal(
        approach=APPROACH, along=ALONG, across=ACROSS,
        direction=direction, angle_deg=angle_deg,
    ).tolist()


def _contract(**overrides) -> YieldValidationContract:
    values = dict(
        cell_id="V1",
        arm="SFC",
        actual_method="SFC",
        material="stiff_low_mu",
        kappa_xx=1.2,
        kappa_yy=0.7,
        prior_direction="approach",
        prior_angle_deg=0.0,
        preparation="cold",
        nominal_scenario="nominal",
        disturbed_scenario="sustained_release_normal",
        timeline="full_cycle",
        duration_s=0.009,
        dt_s=0.002,
        period_s=0.009,
        plant_substeps=8,
        resolution_id="base",
        campaign_kind="holdout",
        record_fullstate=True,
        require_ur10e=True,
        observer_parameters=dict(OBSERVER_V3),
        expected_prior=_prior(),
        approach_inward_base=list(APPROACH),
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
        candidate_parameters={"m": 4.0, "mu": 393.0, "n": 3.0, "g": 0.05},
    )
    values.update(overrides)
    return YieldValidationContract(**values)


def _rows(contract: YieldValidationContract, *, load=5.0, path=0.0, attitude=0.0, progress=1.0, position=None):
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


def _law_values(contract: YieldValidationContract):
    values = [0.0] * SNAPSHOT_SIZE
    if contract.actual_method == "SFC_RADIAL":
        values[0] = 20260920.0
        values[1] = 3.0
        values[2] = 3.0
        values[3] = float(contract.dt_s)
    return values


def _controller_snapshot(*, initialized: bool, time_s, path_time_s, law_values=None, inward=None):
    inward_normal = list(APPROACH) if inward is None else list(inward)
    return {
        "identity": CONTROLLER_IDENTITY,
        "law22": {
            "values": list(law_values if law_values is not None else [0.0] * SNAPSHOT_SIZE),
            "binding_id": LAW_BINDING,
        },
        "filter": {"filtered_force_base_n": [0.0, 0.0, 0.0], "initialized": initialized},
        "normal_estimate": {
            "inward_normal_base": inward_normal,
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


def _plant_snapshot(*, time_s: float, material="stiff_low_mu"):
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
        "material": material,
    }


def _fullstate_records(contract: YieldValidationContract, *, scenario: str):
    counts = expected_phase_counts(contract)
    records = []
    phases = [("baseline", counts["baseline"]), ("entry", counts["entry"]), ("path", counts["path"])]
    for phase, count in phases:
        for index in range(count):
            clock = index * contract.dt_s
            path_time = clock if phase == "path" else 0.0
            controller = _controller_snapshot(
                initialized=True, time_s=clock + contract.dt_s, path_time_s=path_time if phase == "path" else None,
                law_values=_law_values(contract), inward=contract.expected_prior,
            )
            plant = _plant_snapshot(time_s=clock + contract.dt_s, material=contract.material)
            records.append({
                "dt_s": contract.dt_s,
                "scenario": scenario if phase == "path" else "nominal",
                "plant_path_time_s": path_time,
                "reference": {"phase": phase, "path_time_s": clock if phase == "path" else None},
                "controller_snapshot": controller,
                "simulator_snapshot": plant,
            })
    last_entry = counts["baseline"] + counts["entry"] - 1
    return {
        "records": records,
        "initial_controller_snapshot": _controller_snapshot(
            initialized=False, time_s=None, path_time_s=None,
            law_values=_law_values(contract), inward=contract.expected_prior,
        ),
        "initial_simulator_snapshot": _plant_snapshot(time_s=0.0, material=contract.material),
        "formal_initial_snapshot": {
            "controller": copy.deepcopy(records[last_entry]["controller_snapshot"]),
            "plant": copy.deepcopy(records[last_entry]["simulator_snapshot"]),
        },
        "final_controller_snapshot": copy.deepcopy(records[-1]["controller_snapshot"]),
        "final_simulator_snapshot": copy.deepcopy(records[-1]["simulator_snapshot"]),
    }


def _artifact(contract: YieldValidationContract, *, scenario, rows=None, **extra):
    rows = _rows(contract) if rows is None else rows
    fullstate = _fullstate_records(contract, scenario=scenario)
    estimator = {**dict(contract.observer_parameters), "initial_inward_normal_base": list(contract.expected_prior)}
    payload = {
        "method": contract.actual_method,
        "scenario": scenario,
        "material": contract.material,
        "dt_s": contract.dt_s,
        "duration_s": contract.duration_s,
        "preparation": contract.preparation,
        "timeline": contract.timeline,
        "identity": CONTROLLER_IDENTITY,
        "campaign_kind": "holdout",
        "kinematics_kind": contract.kinematics_kind,
        "protocol_sha256": contract.protocol_sha256,
        "source_hashes": dict(contract.source_hashes or {}),
        "metrics": {"failed": False, "sentinel": "keep-reported", "force_peak_n": 5.0},
        "rows": rows,
        "identity_payload": {
            "law_frame": contract.law_frame,
            "parameters": dict(contract.candidate_parameters),
            "estimator_parameters": estimator,
            "approach_inward_base": list(contract.approach_inward_base),
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


def test_reservation_copies_exact_six_cells_and_three_settings():
    reservation = load_reservation(ROOT / "report/yield-validation-reservation-v2/protocol.json")
    raw = json.loads((ROOT / "report/yield-validation-reservation-v2/protocol.json").read_text())
    assert reservation["cells"] == [
        {
            "id": cell["id"],
            "material": cell["material"],
            "surface_parameters": {
                "kappa_xx": cell["surface_parameters"]["kappa_xx"],
                "kappa_yy": cell["surface_parameters"]["kappa_yy"],
            },
            "prior_direction": cell["prior_direction"],
            "prior_angle_deg": cell["prior_angle_deg"],
            "preparation": cell["preparation"],
            "disturbed_scenario": cell["disturbed_scenario"],
            "nominal_scenario": cell["nominal_scenario"],
        }
        for cell in raw["cells"]
    ]
    assert reservation["arms"] == list(ARMS)
    assert (reservation["cells"][0]["kappa_xx"] if False else reservation["cells"][0]["surface_parameters"]) == {"kappa_xx": 1.2, "kappa_yy": 0.7}
    assert reservation["cells"][2]["surface_parameters"] == {"kappa_xx": 3.0, "kappa_yy": 4.0}
    assert reservation["cells"][4]["preparation"] == "warm"
    assert reservation["cost"]["maximum_total_trials"] == 180


def test_scenario_release_windows_are_parse_scenario_not_training_35_5():
    assert scenario_release_s("short_pulse_tangent", timeline="full_cycle") == pytest.approx(20.5)
    assert scenario_release_s("short_pulse_oblique", timeline="full_cycle") == pytest.approx(20.5)
    assert scenario_release_s("sustained_release_normal", timeline="full_cycle") == pytest.approx(35.5)
    spec = parse_scenario("short_pulse_normal", timeline="full_cycle")
    assert spec["start_s"] + spec["width_s"] + spec["hold_s"] + spec["release_s"] == 20.5
    pulse = _contract(disturbed_scenario="short_pulse_normal", path_release_s=scenario_release_s("short_pulse_normal"))
    result = evaluate_pair(
        _artifact(pulse, scenario="nominal"),
        _artifact(pulse, scenario="short_pulse_normal", rows=_rows(pulse, load=5.5, position=(0.002, 0.0, 0.0))),
        pulse,
    )
    assert result.valid is True
    assert result.objective_components["Jpath"] == pytest.approx(0.0)


def test_exact_final_cell_weights_and_known_analytic_objective():
    contract = _contract()
    assert path_sample_count(0.009, 0.002) == 5
    assert cell_weight(0.008, 0.002, 0.0, 0.009, 0.009) == pytest.approx(0.001)
    assert reused_cell_weight(0.008, 0.002, 0.0, 0.009, 0.009) == pytest.approx(0.001)
    result = evaluate_pair(
        _artifact(contract, scenario="nominal", rows=_rows(contract, load=5.0, attitude=0.0, position=(0.0, 0.0, 0.0))),
        _artifact(contract, scenario="sustained_release_normal", rows=_rows(contract, load=5.5, attitude=0.05, position=(0.002, 0.0, 0.0))),
        contract,
    )
    assert result.valid is True
    assert result.objective_components["Jload"] == pytest.approx(0.005)
    assert result.objective_components["Jpath"] == pytest.approx(0.003)
    assert result.objective_components["Jatt"] == pytest.approx(0.005)
    assert result.objective == pytest.approx(0.013)
    assert "disturbed_force_peak_n" in result.absolute_descriptors
    assert result.reported_metrics_nominal["sentinel"] == "keep-reported"


def test_wrong_arm_coefficients_prior_axis_scenario_dt_and_refinement_are_rejected():
    contract, nominal = _contract(), None
    contract = _contract()
    nominal = _artifact(contract, scenario="nominal")

    bad_coeff = copy.deepcopy(nominal)
    bad_coeff["identity_payload"]["parameters"]["m"] = 9.0
    with pytest.raises(YieldValidationSelectionError, match="actual law parameters differ"):
        inspect_member(bad_coeff, contract, condition="nominal")

    identity = _contract(
        arm="MSFC_IDENTITY", actual_method="MSFC",
        candidate_parameters={"m": 4.0, "g": 0.08, "p": 0.5, "a": 0.05, "n": 3.0, "mu": 100.0,
                              "force_scale_n": 2.1, "tau_force_s": 0.19, "tau_recovery_s": 0.20,
                              "kappa_per_n2_s": 0.63, "max_iterations": 32.0, "residual_tolerance_n": 1e-9,
                              "structure_tolerance": 1e-12, "minimum_metric_eigenvalue": 1.0},
    )
    ident_art = _artifact(identity, scenario="nominal")
    ident_art["identity_payload"]["parameters"]["minimum_metric_eigenvalue"] = 0.026926929041248004
    with pytest.raises(YieldValidationSelectionError, match="actual law parameters differ"):
        inspect_member(ident_art, identity, condition="nominal")

    tilted = _contract(prior_direction="along", prior_angle_deg=7.0, expected_prior=_prior("along", 7.0))
    wrong_prior = _artifact(tilted, scenario="nominal")
    wrong_prior["identity_payload"]["estimator_parameters"]["initial_inward_normal_base"] = list(APPROACH)
    with pytest.raises(YieldValidationSelectionError, match="prior axis"):
        inspect_member(wrong_prior, tilted, condition="nominal")

    with pytest.raises(YieldValidationSelectionError, match="configured scenario"):
        inspect_member(_artifact(contract, scenario="nominal"), contract, condition="disturbed")

    dt = copy.deepcopy(nominal)
    dt["dt_s"] = 0.001
    with pytest.raises(YieldValidationSelectionError, match="dt_s differs"):
        inspect_member(dt, contract, condition="nominal")

    refinement = _contract(resolution_id="controller_refinement", dt_s=0.001, plant_substeps=4)
    planted = _artifact(refinement, scenario="nominal")
    planted["plant_identity_payload"]["integration_substeps"] = 8
    with pytest.raises(YieldValidationSelectionError, match="plant identity"):
        inspect_member(planted, refinement, condition="nominal")


def test_msfc_identity_and_radial_labels_stay_truthful():
    identity = _contract(
        arm="MSFC_IDENTITY", actual_method="MSFC",
        candidate_parameters={"m": 4.0, "g": 0.08, "p": 0.5, "a": 0.05, "n": 3.0, "mu": 100.0,
                              "force_scale_n": 2.1, "tau_force_s": 0.19, "tau_recovery_s": 0.20,
                              "kappa_per_n2_s": 0.63, "max_iterations": 32.0, "residual_tolerance_n": 1e-9,
                              "structure_tolerance": 1e-12, "minimum_metric_eigenvalue": 1.0},
    )
    relabeled = _artifact(identity, scenario="nominal")
    relabeled["method"] = "MSFC_IDENTITY"
    with pytest.raises(YieldValidationSelectionError, match="not be relabeled"):
        inspect_member(relabeled, identity, condition="nominal")
    honest = _artifact(identity, scenario="nominal")
    assert honest["method"] == "MSFC"
    inspect_member(honest, identity, condition="nominal")

    radial = _contract(arm="SFC_RADIAL", actual_method="SFC_RADIAL")
    as_sfc = _artifact(radial, scenario="nominal")
    as_sfc["method"] = "SFC"
    with pytest.raises(YieldValidationSelectionError, match="actual executable method"):
        inspect_member(as_sfc, radial, condition="nominal")
    inspect_member(_artifact(radial, scenario="nominal"), radial, condition="nominal")

    selected = {"SFC": {"method": "SFC", "m": 4.0, "mu": 1.0, "n": 3.0, "g": 0.05},
                "MSFC": {"method": "MSFC", "m": 4.0, "g": 0.08, "p": 0.5, "a": 0.05, "n": 3.0, "mu": 100.0,
                         "force_scale_n": 2.1, "tau_force_s": 0.19, "tau_recovery_s": 0.20,
                         "kappa_per_n2_s": 0.63, "max_iterations": 32.0, "residual_tolerance_n": 1e-9,
                         "structure_tolerance": 1e-12, "minimum_metric_eigenvalue": 0.0269}}
    ident_params = arm_law_parameters("MSFC_IDENTITY", selected)
    assert ident_params["minimum_metric_eigenvalue"] == MSFC_IDENTITY_METRIC
    assert ident_params["m"] == selected["MSFC"]["m"]
    assert arm_law_parameters("SFC_RADIAL", selected) == {"m": 4.0, "mu": 1.0, "n": 3.0, "g": 0.05}


def test_out_of_band_complete_pair_is_not_dropped_failed_base_skips_refinements():
    contract = _contract()
    out_of_band = evaluate_pair(
        _artifact(contract, scenario="nominal", rows=_rows(contract, progress=1.0, load=5.0)),
        _artifact(contract, scenario="sustained_release_normal", rows=_rows(contract, progress=0.0, load=5.0)),
        contract,
    )
    assert out_of_band.valid is True
    assert out_of_band.failed_or_incomplete is False
    assert out_of_band.skip_refinements is False
    assert out_of_band.nominal_band_ok is True
    assert out_of_band.disturbed_guard_ok is False
    assert out_of_band.pair_feasible_flag is False
    assert out_of_band.objective is not None
    assert refinements_required(out_of_band) is True
    assert base_failed_or_incomplete(out_of_band) is False

    disturbed = _artifact(contract, scenario="sustained_release_normal")
    disturbed["metrics"] = {"failed": True, "sentinel": "keep-reported"}
    failed = evaluate_pair(_artifact(contract, scenario="nominal"), disturbed, contract)
    assert failed.valid is False
    assert failed.skip_refinements is True
    assert failed.objective is None
    assert failed.reported_metrics_disturbed["sentinel"] == "keep-reported"
    assert refinements_required(failed) is False
    assert base_failed_or_incomplete(failed) is True


def test_null_missing_partial_grid_and_nan_are_rejected():
    contract = _contract()
    nominal = _artifact(contract, scenario="nominal")

    null_art = copy.deepcopy(nominal)
    null_art["records"][0]["controller_snapshot"] = None
    with pytest.raises(YieldValidationSelectionError, match="missing"):
        inspect_member(null_art, contract, condition="nominal")

    missing_state = copy.deepcopy(nominal)
    missing_state["records"] = []
    with pytest.raises(YieldValidationSelectionError, match="full-state"):
        inspect_member(missing_state, contract, condition="nominal")

    short = copy.deepcopy(nominal)
    short["rows"] = short["rows"][:-1]
    with pytest.raises(YieldValidationSelectionError, match="incomplete"):
        inspect_member(short, contract, condition="nominal")

    nan_rows = _rows(contract)
    nan_rows[0]["true_normal_load_n"] = float("nan")
    with pytest.raises(YieldValidationSelectionError, match="finite"):
        inspect_member(_artifact(contract, scenario="nominal", rows=nan_rows), contract, condition="nominal")

    boolean = copy.deepcopy(nominal)
    boolean["full_cycle"] = True
    boolean["rows"] = []
    with pytest.raises(YieldValidationSelectionError, match="incomplete"):
        inspect_member(boolean, contract, condition="nominal")


def test_does_not_trick_training_evaluator_with_holdout_or_relabeled_method():
    holdout = _contract()
    nominal = _artifact(holdout, scenario="nominal")
    disturbed = _artifact(holdout, scenario="sustained_release_normal")
    training = load_contract(ROOT / "config/yield_fair_campaign_v1.json")
    with pytest.raises(YieldFairSelectionError):
        training_evaluate_pair(nominal, disturbed, training)
    labeled = copy.deepcopy(nominal)
    labeled["campaign_kind"] = "training"
    with pytest.raises(YieldValidationSelectionError, match="training rows"):
        inspect_member(labeled, holdout, condition="nominal")
    raw = json.loads((ROOT / "config/yield_fair_campaign_v1.json").read_text())
    with pytest.raises(YieldFairSelectionError):
        contract_from_config({**raw, "campaign_kind": "holdout"})
    with pytest.raises(YieldValidationSelectionError, match="holdout"):
        _contract(campaign_kind="training")


def test_nonzero_prior_warm_and_radial_fullstate_schema():
    tilted = _prior("along", 7.0)
    warm = _contract(
        preparation="warm", dt_s=1.0, duration_s=1.0, period_s=1.0,
        load_onset_s=0.0, path_release_s=0.5, attitude_onset_s=0.0,
        prior_direction="along", prior_angle_deg=7.0, expected_prior=tilted,
    )
    inspect_member(_artifact(warm, scenario="nominal"), warm, condition="nominal")
    radial = _contract(arm="SFC_RADIAL", actual_method="SFC_RADIAL", expected_prior=tilted,
                       prior_direction="along", prior_angle_deg=7.0)
    inspect_member(_artifact(radial, scenario="nominal"), radial, condition="nominal")
    mismatch = _artifact(radial, scenario="nominal")
    mismatch["initial_controller_snapshot"]["normal_estimate"]["inward_normal_base"] = list(APPROACH)
    with pytest.raises(YieldValidationSelectionError, match="prior snapshot"):
        inspect_member(mismatch, radial, condition="nominal")


def test_warm_entry_counts_and_source_hashes_are_checked():
    warm = _contract(preparation="warm", dt_s=1.0, duration_s=1.0, period_s=1.0,
                     load_onset_s=0.0, path_release_s=0.5, attitude_onset_s=0.0)
    assert expected_phase_counts(warm)["baseline"] == path_sample_count(2.0, warm.dt_s)
    inspect_member(_artifact(warm, scenario="nominal"), warm, condition="nominal")
    hashed = _contract(source_hashes={"contact_yield_runner.py": "d" * 64})
    art = _artifact(hashed, scenario="nominal")
    art["source_hashes"] = {"contact_yield_runner.py": "e" * 64}
    with pytest.raises(YieldValidationSelectionError, match="source identity"):
        inspect_member(art, hashed, condition="nominal")


def test_prior_basis_preserves_approach_cross_along_frame():
    basis = derive_prior_basis(approach=APPROACH, reference_velocity_m_s=[0.004, 0.0, 0.0])
    np.testing.assert_allclose(basis["approach"], APPROACH)
    np.testing.assert_allclose(basis["along"], ALONG)
    np.testing.assert_allclose(basis["across"], ACROSS)
    tilted = prior_inward_normal(approach=APPROACH, along=ALONG, across=ACROSS, direction="along", angle_deg=7.0)
    assert abs(float(np.linalg.norm(tilted)) - 1.0) < 1e-12
    assert not np.allclose(tilted, APPROACH)


def test_unresolved_sensitivity_is_not_equivalence_and_lower_j_with_adverse_is_mixed():
    def setting(j, peak, progress, valid=True):
        return {
            "valid": valid,
            "absolute_descriptors": {"J": j, "disturbed_force_peak_n": peak, "disturbed_progress_ratio": progress},
        }
    mixed = compare_resolution_settings(
        arm_a="SFC", arm_b="DSFC",
        settings_a={
            "base": setting(1.0, 9.0, 0.9),
            "controller_refinement": setting(1.01, 9.0, 0.9),
            "plant_refinement": setting(0.99, 9.0, 0.9),
        },
        settings_b={
            "base": setting(2.0, 6.0, 0.95),
            "controller_refinement": setting(2.0, 6.0, 0.95),
            "plant_refinement": setting(2.0, 6.0, 0.95),
        },
    )
    assert mixed["complete"] is True
    assert mixed["winner"] is None
    assert mixed["equivalence"] is False
    assert mixed["comparison"] in {"mixed", "unresolved_sensitivity"}
    assert "disturbed_force_peak_n" in mixed["adverse_absolute_changes_for_lower_j"] or mixed["comparison"] == "unresolved_sensitivity"

    unresolved = compare_resolution_settings(
        arm_a="SFC", arm_b="MSFC",
        settings_a={
            "base": setting(1.0, 5.0, 1.0),
            "controller_refinement": setting(1.2, 5.0, 1.0),
            "plant_refinement": setting(0.8, 5.0, 1.0),
        },
        settings_b={
            "base": setting(1.1, 5.1, 1.0),
            "controller_refinement": setting(1.1, 5.1, 1.0),
            "plant_refinement": setting(1.1, 5.1, 1.0),
        },
    )
    assert unresolved["equivalence"] is False
    assert unresolved["winner"] is None
    assert unresolved["unresolved_sensitivity"] or unresolved["comparison"] == "unresolved_sensitivity"
