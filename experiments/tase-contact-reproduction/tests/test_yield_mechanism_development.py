"""Development mechanism diagnostic tests. No full-cycle or reserved-budget launch."""
from __future__ import annotations

import hashlib
import inspect as pyinspect
import json
from pathlib import Path

import pytest

from yield_contact_tuner import MSFC_ACTIVE_METRIC_FLOOR, MSFC_IDENTITY_METRIC, YieldContactTuner
from yield_fair_selection import YieldFairMemberResult, YieldFairPairResult
from yield_mechanism_development import (
    ARMS,
    SELECTED_UNITS,
    TOTAL_TRIALS,
    YieldMechanismDevelopmentError,
    _artifact_path,
    _atomic_write_json,
    _load_manifest,
    _load_state,
    _run_kwargs,
    _slot_contract,
    bound_arm_parameters,
    campaign_report,
    campaign_status,
    create_campaign,
    inspect_member,
    main,
    run_next,
    selected_triples,
)


ROOT = Path(__file__).resolve().parents[1]


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _tuner():
    return YieldContactTuner(
        ROOT / "config/yield_fair_tuning_v1.json",
        training_cell_id="stiff_low_mu",
        selection_contract_id="yield-fair-selection-contract-v1",
    )


def _binary_fixture(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    qp = tmp_path / "libcontact_qp.so"
    qp.write_bytes(b"yield-mechanism-development-identity-fixture")
    laws = tmp_path / "contact-six-laws"
    laws.mkdir()
    (laws / "libcontact_laws.so").write_bytes(b"yield-mechanism-development-laws-fixture")
    return qp, laws


def _create(tmp_path: Path) -> Path:
    qp, laws = _binary_fixture(tmp_path / "binaries")
    output = tmp_path / "diagnostic"
    create_campaign(output, experiment_root=ROOT, qp_library=qp, native_laws_root=laws)
    return output


def _inspect_ok(artifact, contract, *, condition, arm=None, actual_method=None):
    return YieldFairMemberResult(
        condition=condition,
        valid=True,
        failed=False,
        full_cycle=True,
        nominal_feasible=True if condition == "nominal" else None,
        disturbed_guards_ok=True if condition == "disturbed" else None,
        checks={"ok": True},
        recomputed_metrics={
            "path_rmse_m": 0.001,
            "orientation_rmse_rad": 0.01,
            "progress_ratio": 0.9,
            "force_mae_n": 0.05,
            "force_peak_n": 5.2,
        },
        reported_metrics=dict(artifact.get("metrics") or {}),
    )


def _inspect_failed(artifact, contract, *, condition, arm=None, actual_method=None):
    return YieldFairMemberResult(
        condition=condition,
        valid=False,
        failed=True,
        full_cycle=False,
        nominal_feasible=False if condition == "nominal" else None,
        disturbed_guards_ok=False if condition == "disturbed" else None,
        checks={"failed": True},
        recomputed_metrics={},
        reported_metrics=dict(artifact.get("metrics") or {}),
        reason="failed member",
    )


def _inspect_out_of_band(artifact, contract, *, condition, arm=None, actual_method=None):
    member = _inspect_ok(artifact, contract, condition=condition, arm=arm, actual_method=actual_method)
    if condition == "nominal":
        return YieldFairMemberResult(
            condition=condition,
            valid=True,
            failed=False,
            full_cycle=True,
            nominal_feasible=False,
            disturbed_guards_ok=None,
            checks={"ok": True, "path_rms_ok": False},
            recomputed_metrics=dict(member.recomputed_metrics),
            reported_metrics=dict(member.reported_metrics),
        )
    return member


def _pair_ok(nominal, disturbed, contract, *, arm=None, actual_method=None, **fields):
    payload = dict(
        valid=True,
        nominal_feasible=True,
        disturbed_guards_ok=True,
        pair_feasible=True,
        objective=12.0,
        objective_components={"J": 12.0, "Jload": 4.0, "Jpath": 5.0, "Jatt": 3.0},
        objective_eligible=True,
        reported_recovery={"eligible": True, "recovery_s": 3.5},
        recomputed_metrics_nominal={"force_peak_n": 5.2, "path_rmse_m": 0.001},
        recomputed_metrics_disturbed={"force_peak_n": 6.1, "path_rmse_m": 0.002},
    )
    payload.update(fields)
    return YieldFairPairResult(**payload)


def _pair_out_of_band(nominal, disturbed, contract, *, arm=None, actual_method=None):
    return _pair_ok(
        nominal, disturbed, contract, arm=arm, actual_method=actual_method,
        pair_feasible=False, nominal_feasible=False, disturbed_guards_ok=True,
        objective=40.0, objective_components={"J": 40.0, "Jload": 20.0, "Jpath": 15.0, "Jatt": 5.0},
    )


def _fail_artifact(**kwargs):
    return {
        "method": kwargs["method"],
        "scenario": kwargs["scenario"],
        "metrics": {"failed": True, "failure_message": "plant failed"},
        "campaign_kind": "diagnostic_seed",
        "identity_payload": {"parameters": kwargs.get("law_parameters")},
    }


def _ok_artifact(**kwargs):
    return {
        "method": kwargs["method"],
        "scenario": kwargs["scenario"],
        "metrics": {"failed": False, "force_peak_n": 5.2, "path_rmse_m": 0.001},
        "campaign_kind": "diagnostic_seed",
        "identity_payload": {"parameters": kwargs.get("law_parameters")},
        "material": "stiff_low_mu",
        "dt_s": kwargs.get("dt_s"),
        "duration_s": kwargs.get("duration_s"),
        "preparation": kwargs.get("preparation"),
        "timeline": kwargs.get("timeline"),
    }


def test_help_create_status_report_do_not_launch(tmp_path):
    assert main([]) == 0
    calls = []

    def boom(**kwargs):
        calls.append(kwargs)
        raise AssertionError("create/status/report must not execute")

    output = _create(tmp_path)
    status = campaign_status(output)
    report = campaign_report(output)
    assert status["formal_validation_complete"] is False
    assert status["executed_trials"] == 0
    assert status["training_budget"] == 0
    assert status["validation_budget"] == 0
    assert status["inflight"] is None
    assert status["next"]["slot_id"] == "u0-SFC-base-nominal"
    assert report["winner"] is None
    assert report["not_validation"] is True
    assert report["data_selection"]["not_holdout"] is True
    assert "memory adversity" in report["data_selection"]["unit4"]
    assert (output / "attempts").exists() is False
    assert calls == []
    state = json.loads((output / "campaign.json").read_text())
    assert "freeze" not in state
    assert state["campaign_protocol"]["not_validation"] is True
    assert "freeze_path" not in create_campaign.__code__.co_varnames
    assert "freeze" not in create_campaign.__code__.co_varnames


def test_schedule_is_exactly_sixty_fresh_diagnostic_slots(tmp_path):
    output = _create(tmp_path)
    manifest = json.loads((output / "manifest.json").read_text())
    slots = manifest["slots"]
    assert manifest["count"] == TOTAL_TRIALS == 60
    assert len(slots) == 60
    ids = [slot["slot_id"] for slot in slots]
    assert len(set(ids)) == 60
    assert slots[0]["slot_id"] == "u0-SFC-base-nominal"
    assert slots[-1]["slot_id"] == "u4-MSFC_IDENTITY-plant_refinement-disturbed"
    combos = {
        (slot["unit_index"], slot["arm"], slot["resolution_id"], slot["condition"])
        for slot in slots
    }
    expected = {
        (unit, arm, resolution, condition)
        for unit in SELECTED_UNITS
        for arm in ARMS
        for resolution, _, _ in (("base", 0.002, 8), ("controller_refinement", 0.001, 4), ("plant_refinement", 0.002, 16))
        for condition in ("nominal", "disturbed")
    }
    assert combos == expected
    assert all(slot["actual_method"] == "MSFC" for slot in slots if slot["arm"] == "MSFC_IDENTITY")
    assert all(slot["actual_method"] == "SFC_RADIAL" for slot in slots if slot["arm"] == "SFC_RADIAL")
    assert all(slot["scenario"] == "sustained_release_oblique" for slot in slots if slot["condition"] == "disturbed")
    tuner = _tuner()
    triples = selected_triples(tuner)
    assert set(triples) == {0, 4}
    for slot in slots:
        assert (slot["m"], slot["mu"], slot["g"]) == triples[slot["unit_index"]]


def test_shared_coefficients_and_identity_routing(tmp_path):
    output = _create(tmp_path)
    state = _load_state(output)
    slots = {slot["slot_id"]: slot for slot in _load_manifest(output)}
    tuner = _tuner()
    triples = selected_triples(tuner)
    for unit in SELECTED_UNITS:
        mechanical = []
        for arm in ARMS:
            slot = slots[f"u{unit}-{arm}-base-nominal"]
            kwargs = _run_kwargs(state, slot, _slot_contract(state, slot))
            assert kwargs["campaign_kind"] == "diagnostic_seed"
            assert kwargs["timeline"] == "full_cycle"
            assert kwargs["record_fullstate"] is True
            assert kwargs["estimator_parameters"]["motion_gain"] == 0.3
            assert "initial_inward_normal_base" not in kwargs["estimator_parameters"]
            assert kwargs["surface_parameters"] == {"kappa_xx": 0.8, "kappa_yy": 0.4}
            mechanical.append((kwargs["law_parameters"]["m"], kwargs["law_parameters"]["mu"], kwargs["law_parameters"]["g"]))
            expected = bound_arm_parameters(tuner, arm, triples[unit])
            assert kwargs["law_parameters"] == expected
            if arm == "MSFC_IDENTITY":
                assert kwargs["method"] == "MSFC"
                assert kwargs["law_parameters"]["minimum_metric_eigenvalue"] == MSFC_IDENTITY_METRIC
            elif arm == "SFC_RADIAL":
                assert kwargs["method"] == "SFC_RADIAL"
                assert set(kwargs["law_parameters"]) == {"m", "mu", "n", "g"}
            elif arm == "MSFC":
                assert kwargs["method"] == "MSFC"
                assert kwargs["law_parameters"]["minimum_metric_eigenvalue"] == MSFC_ACTIVE_METRIC_FLOOR
            else:
                assert kwargs["method"] == arm
        assert len(set(mechanical)) == 1
        assert mechanical[0] == triples[unit]
    ident = _run_kwargs(state, slots["u0-MSFC_IDENTITY-base-nominal"], _slot_contract(state, slots["u0-MSFC_IDENTITY-base-nominal"]))
    msfc = _run_kwargs(state, slots["u0-MSFC-base-nominal"], _slot_contract(state, slots["u0-MSFC-base-nominal"]))
    assert ident["law_parameters"]["m"] == msfc["law_parameters"]["m"]
    assert ident["law_parameters"]["minimum_metric_eigenvalue"] != msfc["law_parameters"]["minimum_metric_eigenvalue"]
    radial = _run_kwargs(state, slots["u4-SFC_RADIAL-controller_refinement-disturbed"], _slot_contract(state, slots["u4-SFC_RADIAL-controller_refinement-disturbed"]))
    assert radial["dt_s"] == 0.001 and radial["plant_substeps"] == 4
    assert radial["scenario"] == "sustained_release_oblique"
    plant = _run_kwargs(state, slots["u4-DSFC-plant_refinement-nominal"], _slot_contract(state, slots["u4-DSFC-plant_refinement-nominal"]))
    assert plant["dt_s"] == 0.002 and plant["plant_substeps"] == 16
    assert ident["settings"].path_stiffness_n_per_m == 120.0
    assert ident["settings"].compliance_stiffness_n_per_m == 0.0


def test_coefficient_mismatch_is_refused(tmp_path):
    output = _create(tmp_path)

    def mismatch(**kwargs):
        parameters = dict(kwargs["law_parameters"])
        parameters["m"] = float(parameters["m"]) + 1.0
        return _ok_artifact(**{**kwargs, "law_parameters": parameters})

    with pytest.raises(YieldMechanismDevelopmentError, match="actual law parameters differ"):
        run_next(output, runner=mismatch, inspect=_inspect_ok, pair_eval=_pair_ok)
    status = campaign_status(output)
    assert status["inflight"]["slot_id"] == "u0-SFC-base-nominal"
    assert not _artifact_path(output, "u0-SFC-base-nominal").exists()

    identity = _ok_artifact(method="SFC", scenario="nominal", law_parameters={"m": 4.0, "mu": 393.0, "n": 3.0, "g": 0.05})
    identity["identity_payload"]["parameters"]["m"] = 99.0
    contract = _slot_contract(_load_state(output), _load_manifest(output)[0])
    with pytest.raises(YieldMechanismDevelopmentError, match="actual law parameters differ"):
        inspect_member(identity, contract, condition="nominal", arm="SFC", actual_method="SFC")


def test_identity_relabel_is_refused(tmp_path):
    output = _create(tmp_path)

    def relabel(**kwargs):
        artifact = _ok_artifact(**kwargs)
        artifact["method"] = "MSFC_IDENTITY" if kwargs["method"] == "SFC" else kwargs["method"]
        return artifact

    with pytest.raises(YieldMechanismDevelopmentError, match="not relabeling"):
        run_next(output, runner=relabel, inspect=_inspect_ok, pair_eval=_pair_ok)
    with pytest.raises(YieldMechanismDevelopmentError, match="inflight"):
        run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)

    output2 = _create(tmp_path / "radial")
    launched = []

    def as_sfc(**kwargs):
        launched.append((kwargs["method"], kwargs["scenario"]))
        artifact = _ok_artifact(**kwargs)
        if kwargs["method"] == "SFC_RADIAL":
            artifact["method"] = "SFC"
        return artifact

    for _ in range(6):
        run_next(output2, runner=as_sfc, inspect=_inspect_ok, pair_eval=_pair_ok)
    with pytest.raises(YieldMechanismDevelopmentError, match="not relabeling"):
        run_next(output2, runner=as_sfc, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert launched[-1] == ("SFC_RADIAL", "nominal")


def test_overwrite_and_inflight_are_refused(tmp_path):
    output = _create(tmp_path)
    first = run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    digest = first["artifact_sha256"]
    path = Path(first["artifact_path"])
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        _atomic_write_json(path, {"method": "SFC"})
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest

    def crash(**kwargs):
        raise RuntimeError("crash before artifact")

    with pytest.raises(RuntimeError, match="crash before artifact"):
        run_next(output, runner=crash, inspect=_inspect_ok, pair_eval=_pair_ok)
    with pytest.raises(YieldMechanismDevelopmentError, match="inflight"):
        run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    status = campaign_status(output)
    assert status["executed_trials"] == 1
    assert status["inflight"]["slot_id"] == "u0-SFC-base-disturbed"


def test_pair_association_uses_own_nominal(tmp_path):
    output = _create(tmp_path)
    seen = []

    def capture(nominal, disturbed, contract, *, arm=None, actual_method=None):
        seen.append({
            "nominal_scenario": nominal["scenario"],
            "disturbed_scenario": disturbed["scenario"],
            "nominal_method": nominal["method"],
            "disturbed_method": disturbed["method"],
            "arm": arm,
            "dt_s": contract.dt_s,
            "parameters": (nominal.get("identity_payload") or {}).get("parameters"),
        })
        return _pair_ok(nominal, disturbed, contract, arm=arm, actual_method=actual_method)

    first = run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=capture)
    assert first["condition"] == "nominal"
    assert seen == []
    second = run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=capture)
    assert second["condition"] == "disturbed"
    assert second["pair_id"] == first["pair_id"] == "u0-SFC-base"
    assert len(seen) == 1
    assert seen[0]["nominal_scenario"] == "nominal"
    assert seen[0]["disturbed_scenario"] == "sustained_release_oblique"
    assert seen[0]["nominal_method"] == seen[0]["disturbed_method"] == "SFC"
    assert seen[0]["arm"] == "SFC"
    assert seen[0]["dt_s"] == 0.002
    third = run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=capture)
    assert third["slot_id"] == "u0-SFC-controller_refinement-nominal"
    assert len(seen) == 1


def test_failed_member_is_retained_and_does_not_skip(tmp_path):
    output = _create(tmp_path)
    failed = run_next(output, runner=_fail_artifact, inspect=_inspect_failed, pair_eval=_pair_ok)
    assert failed["status"] == "failed"
    assert failed["retained"] is True
    assert _artifact_path(output, failed["slot_id"]).is_file()
    partner = run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert partner["pair_id"] == failed["pair_id"]
    assert partner["condition"] == "disturbed"
    assert partner["pair_complete"] is False or partner.get("failed_or_incomplete") is True
    assert partner["retained"] is True
    third = run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert third["slot_id"] == "u0-SFC-controller_refinement-nominal"
    status = campaign_status(output)
    assert status["executed_trials"] == 3
    assert status["failed"] >= 1
    report = campaign_report(output)
    retained = [row for row in report["members"] if row["outcome"] is not None]
    assert len(retained) == 3
    assert all(row["retained"] for row in retained)


def test_out_of_band_complete_is_not_skipped(tmp_path):
    output = _create(tmp_path)
    run_next(output, runner=_ok_artifact, inspect=_inspect_out_of_band, pair_eval=_pair_out_of_band)
    disturbed = run_next(output, runner=_ok_artifact, inspect=_inspect_out_of_band, pair_eval=_pair_out_of_band)
    assert disturbed["status"] == "complete"
    assert disturbed["out_of_band"] is True
    assert disturbed["pair_feasible"] is False
    assert disturbed["objective"] == 40.0
    third = run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert third["slot_id"] == "u0-SFC-controller_refinement-nominal"
    report = campaign_report(output)
    assert report["winner"] is None
    assert report["causal_inference"] is False
    assert report["no_cross_resolution_pooling"] is True
    matched = [row for row in report["pairs"] if row["pair_id"] == "u0-SFC-base"]
    assert matched[0]["out_of_band"] is True
    assert matched[0]["objective"] == 40.0


def test_report_identifies_matches_without_winner(tmp_path):
    output = _create(tmp_path)
    objectives = {"SFC": 10.0, "SFC_RADIAL": 11.0}

    def pair_for(nominal, disturbed, contract, *, arm=None, actual_method=None):
        return _pair_ok(
            nominal, disturbed, contract, arm=arm, actual_method=actual_method,
            objective=objectives.get(arm, 12.0),
            objective_components={"J": objectives.get(arm, 12.0), "Jload": 1.0, "Jpath": 1.0, "Jatt": 1.0},
        )

    for _ in range(8):
        run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=pair_for)
    report = campaign_report(output)
    assert report["winner"] is None
    assert any(row["kind"] == "matched_arm" for row in report["matched_comparisons"])
    sfc_vs_radial = [
        row for row in report["matched_comparisons"]
        if row["arm_a"] == "SFC" and row["arm_b"] == "SFC_RADIAL" and row["resolution_id"] == "base"
    ]
    assert sfc_vs_radial
    assert sfc_vs_radial[0]["winner"] is None
    assert sfc_vs_radial[0]["descriptor_delta_a_minus_b"]["J"] == pytest.approx(-1.0)
    sfc_dt = [row for row in report["resolution_dt_differences"] if row["arm"] == "SFC" and row["unit_index"] == 0]
    assert sfc_dt
    assert sfc_dt[0]["winner"] is None
    assert sfc_dt[0]["dt_s"]["base"] == 0.002
    assert sfc_dt[0]["dt_s"]["controller_refinement"] == 0.001
    assert main(["status", "--output", str(output)]) == 0
    assert main(["report", "--output", str(output)]) == 0


def test_mutated_manifest_is_rejected_by_status_and_run_next(tmp_path):
    output = _create(tmp_path)
    path = output / "manifest.json"
    original = path.read_bytes()
    payload = json.loads(original.decode("utf-8"))
    payload["slots"][0]["law_parameters"]["g"] = 0.199
    path.write_text(_canonical(payload), encoding="utf-8")
    with pytest.raises(YieldMechanismDevelopmentError, match="manifest identity|schedule"):
        campaign_status(output)
    with pytest.raises(YieldMechanismDevelopmentError, match="manifest identity|schedule"):
        run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert not (output / "inflight.json").exists()

    path.write_bytes(original)
    payload = json.loads(original.decode("utf-8"))
    payload["slots"][0]["slot_id"] = "mutated-id"
    payload["slots"][0]["pair_id"] = "mutated-pair"
    payload["slots"][0]["actual_method"] = "DSFC"
    payload["slots"][0]["scenario"] = "sustained_release_normal"
    payload["slots"][0]["controller_dt_s"] = 0.003
    path.write_text(_canonical(payload), encoding="utf-8")
    with pytest.raises(YieldMechanismDevelopmentError, match="manifest identity|schedule"):
        campaign_status(output)
    payload = json.loads(original.decode("utf-8"))
    payload["slots"] = list(reversed(payload["slots"]))
    path.write_text(_canonical(payload), encoding="utf-8")
    with pytest.raises(YieldMechanismDevelopmentError, match="manifest identity|schedule"):
        run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)


def test_protocol_digest_drift_is_rejected(tmp_path):
    output = _create(tmp_path)
    state_path = output / "campaign.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["campaign_protocol"]["duration_s"] = 1.0
    state_path.write_text(_canonical(state), encoding="utf-8")
    with pytest.raises(YieldMechanismDevelopmentError, match="protocol digest"):
        campaign_status(output)
    with pytest.raises(YieldMechanismDevelopmentError, match="protocol digest"):
        run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)


def test_run_next_supplies_frozen_outer_settings_through_real_api(tmp_path):
    from contact_yield_controller import YieldSettings
    from contact_yield_runner import run_closed_loop

    signature = pyinspect.signature(run_closed_loop)
    assert "settings" in signature.parameters
    assert "qp_library" in signature.parameters
    assert "build_root" in signature.parameters
    fields = getattr(YieldSettings, "__dataclass_fields__", {})
    assert "path_stiffness_n_per_m" in fields
    assert "compliance_stiffness_n_per_m" in fields

    output = _create(tmp_path)
    captured = []

    def runner(**kwargs):
        captured.append(kwargs)
        return _ok_artifact(**kwargs)

    run_next(output, runner=runner, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert captured
    settings = captured[0]["settings"]
    assert isinstance(settings, YieldSettings)
    assert settings.path_stiffness_n_per_m == 120.0
    assert settings.compliance_stiffness_n_per_m == 0.0
    assert captured[0]["qp_library"].is_file()
    assert captured[0]["build_root"].is_dir()


def test_missing_binaries_fail_before_inflight(tmp_path):
    output = _create(tmp_path)
    qp = Path(json.loads((output / "campaign.json").read_text(encoding="utf-8"))["qp_library"])
    qp.unlink()
    with pytest.raises(YieldMechanismDevelopmentError, match="identity drifted|unbound|missing"):
        run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert not (output / "inflight.json").exists()


def test_create_refuses_unbound_default_binaries(tmp_path):
    with pytest.raises(YieldMechanismDevelopmentError, match="missing|unbound"):
        create_campaign(tmp_path / "unbound", experiment_root=ROOT)
