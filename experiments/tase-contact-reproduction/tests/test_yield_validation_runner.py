"""Validation campaign plumbing tests. No full-cycle or formal-budget launch."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from yield_contact_tuner import FT_V1_FIXED, FT_V1_SEED_TRIPLES, METHODS, YieldContactTuner
from yield_validation_runner import (
    YieldValidationCampaignError,
    campaign_report,
    campaign_status,
    create_campaign,
    load_training_freeze,
    main,
    reconcile,
    run_next,
)
from yield_validation_selection import (
    MSFC_IDENTITY_METRIC,
    YieldValidationMemberResult,
    YieldValidationPairResult,
    arm_law_parameters,
)


ROOT = Path(__file__).resolve().parents[1]
APPROACH = [0.0, 0.0, -1.0]
ALONG = [1.0, 0.0, 0.0]
ACROSS = [0.0, -1.0, 0.0]
BASIS = {"approach": APPROACH, "along": ALONG, "across": ACROSS}
PLANT = {
    "kinematics_kind": "ur10e_calibrated_pinocchio",
    "calibration_hash": "d" * 64,
    "claim_scope": "test fixture; not qualification",
    "home_path": "report/contact-six-qp-20260917/preserved-home.json",
    "home_sha256": "e" * 64,
}


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _selected_candidates():
    tuner = YieldContactTuner(
        ROOT / "config/yield_fair_tuning_v1.json",
        training_cell_id="stiff_low_mu",
        selection_contract_id="yield-fair-selection-contract-v1",
    )
    sfc = {"method": "SFC", "m": FT_V1_SEED_TRIPLES["SFC"][0], "mu": FT_V1_SEED_TRIPLES["SFC"][1],
           "g": FT_V1_SEED_TRIPLES["SFC"][2], **FT_V1_FIXED["SFC"]}
    dsfc = {"method": "DSFC", "m": FT_V1_SEED_TRIPLES["DSFC"][0], "mu": FT_V1_SEED_TRIPLES["DSFC"][1],
            "g": FT_V1_SEED_TRIPLES["DSFC"][2], **FT_V1_FIXED["DSFC"]}
    msfc = {"method": "MSFC", "m": FT_V1_SEED_TRIPLES["MSFC"][0], "mu": FT_V1_SEED_TRIPLES["MSFC"][1],
            "g": FT_V1_SEED_TRIPLES["MSFC"][2], **FT_V1_FIXED["MSFC"]}
    return {
        "SFC": tuner._parse_candidate("SFC", sfc).as_dict(),
        "DSFC": tuner._parse_candidate("DSFC", dsfc).as_dict(),
        "MSFC": tuner._parse_candidate("MSFC", msfc).as_dict(),
    }


def _freeze_payload(**overrides):
    """Adversarial self-hashed freeze. Not a production helper and not a training export."""
    selected = _selected_candidates()
    payload = {
        "schema": "ur10e.yield-fair-campaign-freeze-v1",
        "version": 1,
        "selected_candidates": selected,
        "freeze_sha256": hashlib.sha256(_canonical(selected).encode("utf-8")).hexdigest(),
        "formal_campaign_complete": False,
        "holdout_implemented": False,
        "hardware_qualified": False,
        "training_cell_id": "stiff_low_mu",
        "selection_contract_id": "yield-fair-selection-contract-v1",
    }
    payload.update(overrides)
    if "selected_candidates" in overrides and "freeze_sha256" not in overrides:
        payload["freeze_sha256"] = hashlib.sha256(
            _canonical(overrides["selected_candidates"]).encode("utf-8")
        ).hexdigest()
    return payload


def _write_freeze(path: Path, payload=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(payload or _freeze_payload()), encoding="utf-8")
    return path


def _seal_training_pair(ledger, method, index, proposal, *, failed=False, objective=1.0):
    candidate = proposal.candidate
    encoded = b'{"synthetic_fixture":true}'
    digest = hashlib.sha256(encoded).hexdigest()
    evidence = {
        **ledger.bindings,
        "artifact_sha256": digest,
        "objective_eligible": not failed,
        "nominal_feasible": not failed,
        "objective": None if failed else objective,
        "pair_feasible": not failed,
        "disturbed_guards_ok": not failed,
    }
    output = Path(ledger.db.execute("PRAGMA database_list").fetchone()[2]).parent
    for suffix in ("n", "d"):
        path = output / "attempts" / f"{method}-{index}-{suffix}" / "artifact.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
    unit = ledger.begin(
        attempt_id=f"{method}-{index}-n", controller=method,
        candidate=candidate, condition="nominal",
    )
    ledger.seal(f"{method}-{index}-n", status="failed" if failed else "complete", evidence=evidence)
    ledger.begin(
        attempt_id=f"{method}-{index}-d", controller=method,
        candidate=candidate, condition="disturbed", unit=unit,
    )
    ledger.seal(f"{method}-{index}-d", status="failed" if failed else "complete", evidence=evidence)


def _materialize_bound_training_freeze(root: Path) -> Path:
    from yield_contact_ledger import YieldContactLedger
    from yield_fair_campaign import create_campaign as create_training
    from yield_fair_campaign import freeze_campaign

    create_training(root, experiment_root=ROOT)
    state = json.loads((root / "campaign.json").read_text(encoding="utf-8"))
    tuner = YieldContactTuner(
        ROOT / "config/yield_fair_tuning_v1.json",
        training_cell_id=state["campaign_protocol"]["training_cell_id"],
        selection_contract_id=state["campaign_protocol"]["selection_contract_id"],
    )
    ledger = YieldContactLedger(
        root / "campaign.sqlite",
        tuner=tuner,
        campaign_protocol_sha256=state["campaign_protocol_sha256"],
    )
    try:
        for method in METHODS:
            for index in range(24):
                rows = ledger.training_observations(method)
                proposal = ledger.tuner.propose(method, rows, index)
                _seal_training_pair(ledger, method, index, proposal, objective=float(index + 1))
    finally:
        ledger.close()
    freeze_campaign(root)
    return root / "freeze.json"


def _clone_training(bound_freeze: Path, dest: Path) -> Path:
    shutil.copytree(bound_freeze.parent, dest)
    freeze = dest / "freeze.json"
    state_path = dest / "campaign.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["output_root"] = str(dest.resolve())
    state["ledger_path"] = str((dest / "campaign.sqlite").resolve())
    state_path.write_text(_canonical(state), encoding="utf-8")
    return freeze


@pytest.fixture(scope="module")
def bound_freeze(tmp_path_factory):
    return _materialize_bound_training_freeze(tmp_path_factory.mktemp("bound-training"))


def _create(tmp_path, freeze: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "campaign"
    create_campaign(
        output,
        freeze_path=freeze,
        experiment_root=ROOT,
        prior_basis=BASIS,
        plant_identity=PLANT,
    )
    return output


def _inspect_ok(artifact, contract, *, condition):
    return YieldValidationMemberResult(
        condition=condition, valid=True, failed=False, full_cycle=True,
        nominal_band_ok=True, disturbed_guard_ok=True if condition == "disturbed" else None,
        checks={"ok": True}, recomputed_metrics={"path_rmse_m": 0.0, "orientation_rmse_rad": 0.0, "progress_ratio": 1.0,
                                                 "force_mae_n": 0.0, "force_peak_n": 5.0},
        reported_metrics=dict(artifact.get("metrics") or {}),
        absolute_descriptors={"J": 1.0} if condition == "disturbed" else {},
    )


def _inspect_failed(artifact, contract, *, condition):
    return YieldValidationMemberResult(
        condition=condition, valid=False, failed=True, full_cycle=False,
        nominal_band_ok=False if condition == "nominal" else None,
        disturbed_guard_ok=False if condition == "disturbed" else None,
        checks={"failed": True}, recomputed_metrics={}, reported_metrics=dict(artifact.get("metrics") or {}),
        reason="failed member",
    )


def _pair_ok(nominal, disturbed, contract, **fields):
    payload = dict(
        valid=True, failed_or_incomplete=False, skip_refinements=False,
        nominal_band_ok=True, disturbed_guard_ok=True, pair_feasible_flag=True,
        objective=1.0, objective_components={"J": 1.0, "Jload": 0.0, "Jpath": 0.0, "Jatt": 0.0},
        objective_eligible=True,
        absolute_descriptors={"J": 1.0, "disturbed_force_peak_n": 5.0, "disturbed_progress_ratio": 1.0},
    )
    payload.update(fields)
    return YieldValidationPairResult(**payload)


def _pair_out_of_band(nominal, disturbed, contract):
    return _pair_ok(
        nominal, disturbed, contract,
        pair_feasible_flag=False, nominal_band_ok=False, disturbed_guard_ok=False,
        absolute_descriptors={"J": 1.0, "disturbed_force_peak_n": 9.0, "disturbed_progress_ratio": 0.5},
    )


def _fail_artifact(**kwargs):
    return {
        "method": kwargs["method"],
        "scenario": kwargs["scenario"],
        "metrics": {"failed": True},
        "campaign_kind": "holdout",
    }


def _ok_artifact(**kwargs):
    return {
        "method": kwargs["method"],
        "scenario": kwargs["scenario"],
        "metrics": {"failed": False},
        "campaign_kind": "holdout",
        "identity_payload": {"parameters": kwargs.get("law_parameters")},
    }


def test_import_help_create_status_do_not_launch(tmp_path, bound_freeze):
    assert main([]) == 0
    calls = []

    def boom(**kwargs):
        calls.append(kwargs)
        raise AssertionError("create/status must not execute")

    output = _create(tmp_path, bound_freeze)
    status = campaign_status(output)
    assert status["formal_validation_complete"] is False
    assert status["executed_trials"] == 0
    assert status["inflight"] is None
    assert status["next"]["slot_id"] == "V1-SFC-base-nominal"
    assert (output / "attempts").exists() is False
    assert calls == []
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["count"] == 180
    assert manifest["slots"][0]["slot_id"] == "V1-SFC-base-nominal"
    assert manifest["slots"][-1]["slot_id"] == "V6-SFC_RADIAL-plant_refinement-disturbed"
    state = json.loads((output / "campaign.json").read_text())
    assert "yield_validation_runner.py" not in state["campaign_protocol"]["source_hashes"]
    assert "contact_yield_runner.py" in state["campaign_protocol"]["source_hashes"]
    assert "tools/yield_validation_runner.py" in state["campaign_protocol"]["execution_identities"]["scientific_files"]
    assert state["campaign_protocol"]["training_execution_identities"]
    assert "inspect_ledger" not in create_campaign.__code__.co_varnames


def test_boolean_or_self_hashed_freeze_without_training_cannot_pass(tmp_path):
    with pytest.raises(YieldValidationCampaignError, match="boolean"):
        load_training_freeze(_write_freeze(tmp_path / "bool.json", {"valid": True, "complete": True}), experiment_root=ROOT)
    orphan = _write_freeze(tmp_path / "orphan.json")
    with pytest.raises(YieldValidationCampaignError, match="missing bound identities|ledger is missing|campaign export is missing"):
        load_training_freeze(orphan, experiment_root=ROOT)
    with pytest.raises(YieldValidationCampaignError, match="missing bound identities|ledger is missing|campaign export is missing"):
        create_campaign(
            tmp_path / "campaign",
            freeze_path=orphan,
            experiment_root=ROOT,
            prior_basis=BASIS,
            plant_identity=PLANT,
        )


def test_missing_ledger_default_create_path_is_rejected(tmp_path, bound_freeze):
    clone = tmp_path / "no-sqlite"
    freeze = _clone_training(bound_freeze, clone)
    (clone / "campaign.sqlite").unlink()
    with pytest.raises(YieldValidationCampaignError, match="ledger is missing"):
        load_training_freeze(freeze, experiment_root=ROOT)
    with pytest.raises(YieldValidationCampaignError, match="ledger is missing"):
        create_campaign(
            tmp_path / "campaign",
            freeze_path=freeze,
            experiment_root=ROOT,
            prior_basis=BASIS,
            plant_identity=PLANT,
        )
    identities_only = clone / "identities-only"
    identities_only.mkdir()
    payload = json.loads(bound_freeze.read_text(encoding="utf-8"))
    _write_freeze(identities_only / "freeze.json", payload)
    with pytest.raises(YieldValidationCampaignError, match="campaign export is missing|ledger is missing"):
        load_training_freeze(identities_only / "freeze.json", experiment_root=ROOT)


def test_illegal_status_missing_pair_duplicate_and_mismatched_evidence_are_rejected(tmp_path, bound_freeze):
    missing = tmp_path / "missing-pair"
    freeze = _clone_training(bound_freeze, missing)
    conn = sqlite3.connect(missing / "campaign.sqlite")
    conn.execute("DELETE FROM attempts WHERE controller='SFC' AND unit=3 AND condition='disturbed'")
    conn.commit()
    conn.close()
    with pytest.raises(YieldValidationCampaignError, match="missing pair"):
        load_training_freeze(freeze, experiment_root=ROOT)

    illegal = tmp_path / "illegal-status"
    freeze = _clone_training(bound_freeze, illegal)
    conn = sqlite3.connect(illegal / "campaign.sqlite")
    conn.execute("UPDATE attempts SET status='running' WHERE controller='DSFC' AND unit=0 AND condition='nominal'")
    conn.commit()
    conn.close()
    with pytest.raises(YieldValidationCampaignError, match="inflight|illegal status"):
        load_training_freeze(freeze, experiment_root=ROOT)

    mismatched = tmp_path / "mismatched-evidence"
    freeze = _clone_training(bound_freeze, mismatched)
    conn = sqlite3.connect(mismatched / "campaign.sqlite")
    row = conn.execute("SELECT id,evidence FROM attempts WHERE controller='MSFC' AND unit=1 AND condition='nominal'").fetchone()
    evidence = json.loads(row[1])
    evidence["campaign_protocol_sha256"] = "0" * 64
    conn.execute("UPDATE attempts SET evidence=? WHERE id=?", (_canonical(evidence), row[0]))
    conn.commit()
    conn.close()
    with pytest.raises(YieldValidationCampaignError, match="mismatched evidence"):
        load_training_freeze(freeze, experiment_root=ROOT)


def test_arm_kwargs_keep_identity_and_radial_truthful(tmp_path, bound_freeze):
    from yield_validation_runner import _load_manifest, _load_state, _run_kwargs, _slot_contract

    output = _create(tmp_path, bound_freeze)
    state = _load_state(output)
    slots = {slot["slot_id"]: slot for slot in _load_manifest(output)}
    ident = _run_kwargs(state, slots["V1-MSFC_IDENTITY-base-nominal"], _slot_contract(state, slots["V1-MSFC_IDENTITY-base-nominal"]))
    assert ident["method"] == "MSFC"
    assert ident["campaign_kind"] == "holdout"
    assert ident["timeline"] == "full_cycle"
    assert ident["duration_s"] == pytest.approx(62.83185307179586)
    assert ident["law_parameters"]["minimum_metric_eigenvalue"] == MSFC_IDENTITY_METRIC
    msfc = arm_law_parameters("MSFC", state["selected_candidates"])
    assert ident["law_parameters"]["m"] == msfc["m"]
    assert ident["estimator_parameters"]["initial_inward_normal_base"]
    assert ident["record_fullstate"] is True
    radial = _run_kwargs(state, slots["V1-SFC_RADIAL-base-disturbed"], _slot_contract(state, slots["V1-SFC_RADIAL-base-disturbed"]))
    assert radial["method"] == "SFC_RADIAL"
    assert radial["law_parameters"] == arm_law_parameters("SFC_RADIAL", state["selected_candidates"])
    assert radial["scenario"] == "sustained_release_normal"
    v3 = _slot_contract(state, slots["V3-SFC-base-nominal"])
    assert v3.prior_direction == "along" and v3.prior_angle_deg == 7
    assert list(v3.expected_prior) != APPROACH
    plant = _run_kwargs(state, slots["V1-SFC-plant_refinement-nominal"], _slot_contract(state, slots["V1-SFC-plant_refinement-nominal"]))
    assert plant["dt_s"] == 0.002 and plant["plant_substeps"] == 16
    ctrl = _run_kwargs(state, slots["V1-SFC-controller_refinement-nominal"], _slot_contract(state, slots["V1-SFC-controller_refinement-nominal"]))
    assert ctrl["dt_s"] == 0.001 and ctrl["plant_substeps"] == 4
    warm = _slot_contract(state, slots["V5-SFC-base-nominal"])
    assert warm.preparation == "warm"
    assert slots["V1-SFC-base-nominal"]["slot_id"] != slots["V1-DSFC-base-nominal"]["slot_id"]


def test_fail_debits_and_restart_before_artifact_does_not_rerun(tmp_path, bound_freeze):
    output = _create(tmp_path, bound_freeze)
    calls = []

    def crash(**kwargs):
        calls.append((kwargs["method"], kwargs["scenario"]))
        raise RuntimeError("crash before artifact")

    with pytest.raises(RuntimeError, match="crash before artifact"):
        run_next(output, runner=crash, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert calls == [("SFC", "nominal")]
    with pytest.raises(YieldValidationCampaignError, match="inflight"):
        run_next(output, runner=crash, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert calls == [("SFC", "nominal")]
    reconciled = reconcile(output, interrupt=True, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert reconciled["status"] == "interrupted"
    again = reconcile(output, interrupt=True)
    assert again["reconciled"] is False
    result = run_next(output, runner=_fail_artifact, inspect=_inspect_failed, pair_eval=_pair_ok)
    assert result["condition"] == "disturbed"
    assert calls == [("SFC", "nominal")]
    status = campaign_status(output)
    assert status["executed_trials"] == 2
    assert status["interrupted"] == 1


def test_reconcile_after_artifact_before_seal_is_idempotent(tmp_path, bound_freeze):
    from yield_validation_runner import _artifact_path, _atomic_write_json, _inflight_path

    output = _create(tmp_path, bound_freeze)
    slot = json.loads((output / "manifest.json").read_text())["slots"][0]
    _atomic_write_json(_inflight_path(output), {"slot_id": slot["slot_id"], "arm": "SFC", "actual_method": "SFC",
                                               "condition": "nominal", "index": 0, "registered": True})
    _atomic_write_json(_artifact_path(output, slot["slot_id"]), _ok_artifact(method="SFC", scenario="nominal"))
    launched = []

    def boom(**kwargs):
        launched.append(1)
        raise AssertionError("duplicate native invocation")

    with pytest.raises(YieldValidationCampaignError, match="inflight"):
        run_next(output, runner=boom, inspect=_inspect_ok, pair_eval=_pair_ok)
    first = reconcile(output, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert first["status"] == "complete"
    second = reconcile(output, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert second["reconciled"] is False
    assert launched == []


def test_out_of_band_base_still_gets_refinements_failed_base_skips(tmp_path, bound_freeze):
    output = _create(tmp_path, bound_freeze)
    run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_out_of_band)
    run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_out_of_band)
    third = run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert third["slot_id"] == "V1-SFC-controller_refinement-nominal"
    assert third.get("status") != "skipped_refinement"

    output2 = _create(tmp_path / "failed", bound_freeze)
    run_next(output2, runner=_fail_artifact, inspect=_inspect_failed, pair_eval=_pair_ok)
    run_next(output2, runner=_fail_artifact, inspect=_inspect_failed, pair_eval=_pair_ok)
    skipped = run_next(output2, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert skipped["slot_id"] == "V1-DSFC-base-nominal"
    assert any(item["status"] == "skipped_refinement" for item in skipped["skipped_before"])
    assert len(skipped["skipped_before"]) == 4
    assert {item["slot_id"] for item in skipped["skipped_before"]} == {
        "V1-SFC-controller_refinement-nominal",
        "V1-SFC-controller_refinement-disturbed",
        "V1-SFC-plant_refinement-nominal",
        "V1-SFC-plant_refinement-disturbed",
    }


def test_source_drift_and_artifact_tamper_are_rejected(tmp_path, bound_freeze):
    clone = tmp_path / "drift-training"
    freeze = _clone_training(bound_freeze, clone)
    output = _create(tmp_path / "drift-campaign", freeze)
    payload = json.loads(freeze.read_text(encoding="utf-8"))
    payload["claim_scope"] = "tampered"
    freeze.write_text(_canonical(payload), encoding="utf-8")
    with pytest.raises(YieldValidationCampaignError, match="drifted"):
        campaign_status(output)

    output2 = _create(tmp_path / "tamper", bound_freeze)
    run_next(output2, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    path = output2 / "attempts" / "V1-SFC-base-nominal" / "artifact.json"
    original = path.read_bytes()
    path.write_bytes(b'{"tampered":true}')
    assert path.read_bytes() != original
    with pytest.raises(YieldValidationCampaignError, match="digest differs"):
        run_next(output2, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)


def test_finite_schedule_has_no_auto_launch_and_presumes_one_attempt(tmp_path, bound_freeze):
    output = _create(tmp_path, bound_freeze)
    seen = []

    def record(**kwargs):
        seen.append((kwargs["method"], kwargs["scenario"], kwargs["dt_s"], kwargs["plant_substeps"], kwargs["campaign_kind"]))
        return _ok_artifact(**kwargs)

    first = run_next(output, runner=record, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert first["slot_id"] == "V1-SFC-base-nominal"
    assert seen == [("SFC", "nominal", 0.002, 8, "holdout")]
    assert campaign_status(output)["inflight"] is None
    second = run_next(output, runner=record, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert second["slot_id"] == "V1-SFC-base-disturbed"
    assert seen[1][1] == "sustained_release_normal"
    assert main(["status", "--output", str(output)]) == 0


def test_interrupted_nominal_does_not_strand_its_pair(tmp_path, bound_freeze):
    output = _create(tmp_path, bound_freeze)

    def boom(**kwargs):
        raise RuntimeError("interrupted process")

    with pytest.raises(RuntimeError):
        run_next(output, runner=boom, inspect=_inspect_ok, pair_eval=_pair_ok)
    reconcile(output, interrupt=True)
    result = run_next(output, runner=_fail_artifact, inspect=_inspect_failed, pair_eval=_pair_ok)
    assert result["slot_id"] == "V1-SFC-base-disturbed"
    assert campaign_status(output)["executed_trials"] == 2


def test_report_does_not_pool_j_or_name_a_winner(tmp_path, bound_freeze):
    output = _create(tmp_path, bound_freeze)
    run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    run_next(output, runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    report = campaign_report(output)
    assert report["no_j_pooling"] is True
    assert report["no_ci"] is True
    assert report["winner"] is None
    assert report["formal_validation_complete"] is False
    assert "V1" in report["cells"]


def test_concurrent_run_next_does_not_execute_the_same_attempt_twice(tmp_path, bound_freeze):
    output = _create(tmp_path, bound_freeze)
    calls = []
    barrier = threading.Barrier(2)

    def crash(**kwargs):
        calls.append(kwargs["scenario"])
        time.sleep(0.05)
        raise RuntimeError("crash")

    errors = []

    def worker():
        barrier.wait()
        try:
            run_next(output, runner=crash, inspect=_inspect_ok, pair_eval=_pair_ok)
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == ["nominal"]
    assert any("inflight" in str(error) for error in errors)
    assert any("crash" in str(error) for error in errors)


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    from build_contact_qp import build
    return build(tmp_path_factory.mktemp("yield-validation-qp"))


def test_path_diagnostic_native_smoke_does_not_spend_validation_budget(library):
    from contact_yield_runner import run_closed_loop

    observer = dict(json.loads((ROOT / "config/yield_normal_observer_v3.json").read_text()))
    observer["initial_inward_normal_base"] = [0.0, 0.0, -1.0]
    result = run_closed_loop(
        method="SFC",
        duration_s=0.02,
        qp_library=library,
        estimator_parameters=observer,
        plant_substeps=8,
        surface_parameters={"kappa_xx": 1.2, "kappa_yy": 0.7},
        campaign_kind="diagnostic_seed",
        record_fullstate=True,
        require_ur10e=True,
        preparation="cold",
        timeline="diagnostic",
    )
    assert result["campaign_kind"] == "diagnostic_seed"
    assert result["formal_campaign_complete"] is False
    assert result["kinematics_kind"] == "ur10e_calibrated_pinocchio"
    assert result["records"]
    assert result["identity_payload"]["estimator_parameters"]["initial_inward_normal_base"]
    assert result["campaign_kind"] != "holdout"


def test_native_execution_drift_from_training_is_rejected(bound_freeze, monkeypatch):
    import yield_validation_runner as module
    original = module.training_execution_identities
    def changed(root, config):
        result = original(root, config)
        result["qp_library_sha256"] = "0" * 64
        return result
    monkeypatch.setattr(module, "training_execution_identities", changed)
    with pytest.raises(YieldValidationCampaignError, match="training execution identity drifted"):
        load_training_freeze(bound_freeze, experiment_root=ROOT)
