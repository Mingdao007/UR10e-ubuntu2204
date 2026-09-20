"""Campaign plumbing tests. No full-cycle or formal-budget launch."""
from __future__ import annotations

import json
import hashlib
import threading
import time
from pathlib import Path

import pytest

from yield_contact_ledger import YieldContactLedger
from yield_contact_tuner import METHODS, YieldContactTuner
from yield_fair_campaign import (
    YieldFairCampaignError,
    campaign_status,
    create_campaign,
    freeze_campaign,
    main,
    reconcile,
    run_next,
)
from yield_fair_selection import YieldFairMemberResult, YieldFairPairResult, load_contract


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "yield_fair_campaign_v1.json"


def _create(tmp_path, *, config=None):
    cfg = config or CONFIG
    output = tmp_path / "campaign"
    create_campaign(output, config_path=cfg, experiment_root=ROOT)
    return output


def _ledger(output):
    state = json.loads((output / "campaign.json").read_text())
    tuner = YieldContactTuner(
        ROOT / "config" / "yield_fair_tuning_v1.json",
        training_cell_id=state["campaign_protocol"]["training_cell_id"],
        selection_contract_id=state["campaign_protocol"]["selection_contract_id"],
    )
    return YieldContactLedger(
        output / "campaign.sqlite",
        tuner=tuner,
        campaign_protocol_sha256=state["campaign_protocol_sha256"],
    )


def _evidence(ledger, *, success=True, objective=1.0, pair_feasible=True, nominal_feasible=None):
    feasible = success if nominal_feasible is None else nominal_feasible
    return {
        **ledger.bindings,
        "artifact_sha256": "b" * 64,
        "objective_eligible": success,
        "nominal_feasible": feasible,
        "objective": objective if success else None,
        "pair_feasible": pair_feasible if success else False,
        "disturbed_guards_ok": pair_feasible if success else False,
    }


def _seal_pair(ledger, method, index, proposal, *, failed=False, objective=1.0,
               pair_feasible=True, nominal_feasible=None):
    candidate = proposal.candidate
    evidence = _evidence(
        ledger, success=not failed, objective=objective,
        pair_feasible=pair_feasible, nominal_feasible=nominal_feasible,
    )
    # Synthetic lifecycle evidence is still persisted and digest-bound.
    output = Path(ledger.db.execute("PRAGMA database_list").fetchone()[2]).parent
    encoded = b'{"synthetic_fixture":true}'
    evidence["artifact_sha256"] = hashlib.sha256(encoded).hexdigest()
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


def _fail_artifact(**kwargs):
    return {
        "method": kwargs["method"],
        "scenario": kwargs["scenario"],
        "metrics": {"failed": True},
        "campaign_kind": "training",
    }


def _ok_artifact(**kwargs):
    return {
        "method": kwargs["method"],
        "scenario": kwargs["scenario"],
        "metrics": {"failed": False},
        "campaign_kind": "training",
        "identity_payload": {"parameters": kwargs.get("law_parameters")},
    }


def _inspect_ok(artifact, contract, *, condition):
    return YieldFairMemberResult(
        condition=condition, valid=True, failed=False, full_cycle=True,
        nominal_feasible=True, disturbed_guards_ok=True if condition == "disturbed" else None,
        checks={"ok": True}, recomputed_metrics={}, reported_metrics={},
    )


def _pair_ok(nominal, disturbed, contract, objective=1.0, pair_feasible=True, nominal_feasible=True,
             guards=True):
    return YieldFairPairResult(
        valid=True, nominal_feasible=nominal_feasible, disturbed_guards_ok=guards,
        pair_feasible=pair_feasible, objective=objective,
        objective_components={"J": objective, "Jload": 0.0, "Jpath": 0.0, "Jatt": 0.0},
        objective_eligible=pair_feasible,
    )


def test_import_help_create_status_do_not_launch(tmp_path):
    assert main([]) == 0
    output = _create(tmp_path)
    status = campaign_status(output)
    assert status["formal_campaign_complete"] is False
    assert status["holdout_implemented"] is False
    assert status["inflight"] is None
    assert (output / "attempts").exists() is False


def test_fail_debits_budget_and_restart_before_artifact_does_not_rerun(tmp_path):
    output = _create(tmp_path)
    calls = []

    def crash(**kwargs):
        calls.append(kwargs["scenario"])
        raise RuntimeError("crash before artifact")

    with pytest.raises(RuntimeError, match="crash before artifact"):
        run_next(output, method="SFC", runner=crash, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert calls == ["nominal"]
    with pytest.raises(YieldFairCampaignError, match="inflight"):
        run_next(output, method="SFC", runner=crash, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert calls == ["nominal"]
    reconciled = reconcile(output, interrupt=True, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert reconciled["status"] == "interrupted"
    again = reconcile(output, interrupt=True)
    assert again["reconciled"] is False
    result = run_next(output, method="SFC", runner=_fail_artifact)
    assert result["status"] == "failed"
    assert calls == ["nominal"]
    assert reconcile(output, interrupt=True)["reconciled"] is False
    ledger = _ledger(output)
    rows = ledger.training_observations("SFC")
    assert len(rows) == 1 and rows[0].status == "failed" and rows[0].objective is None
    assert ledger.progress()["SFC"]["used_units"] == 1
    ledger.close()


def test_reconcile_after_artifact_before_seal_is_idempotent(tmp_path):
    from yield_fair_campaign import (
        _attempt_id,
        _atomic_write_json,
        _artifact_path,
        _load_state,
        _next_slot,
        _open_bound,
    )

    output = _create(tmp_path)
    state = _load_state(output)
    config, contract, tuner, ledger = _open_bound(state)
    try:
        slot = _next_slot(ledger, "DSFC")
        proposal = tuner.propose("DSFC", [], 0)
        attempt_id = _attempt_id("DSFC", 0, "nominal")
        ledger.begin(
            attempt_id=attempt_id, controller="DSFC",
            candidate=proposal.candidate.as_dict(), condition="nominal",
        )
        _atomic_write_json(
            _artifact_path(output, attempt_id),
            _ok_artifact(method="DSFC", scenario="nominal", law_parameters=proposal.candidate.parameters),
        )
    finally:
        ledger.close()
    launched = []

    def boom(**kwargs):
        launched.append(1)
        raise AssertionError("duplicate native invocation")

    with pytest.raises(YieldFairCampaignError, match="inflight"):
        run_next(output, method="DSFC", runner=boom, inspect=_inspect_ok, pair_eval=_pair_ok)
    first = reconcile(output, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert first["status"] == "complete"
    second = reconcile(output, inspect=_inspect_ok, pair_eval=_pair_ok)
    assert second["reconciled"] is False
    assert launched == []


def test_adversarial_pair_feasible_cannot_override_rejected_nominal(tmp_path):
    output = _create(tmp_path)
    ledger = _ledger(output)
    proposal = ledger.tuner.propose("SFC", [], 0)
    candidate = proposal.candidate
    unit = ledger.begin(
        attempt_id="SFC-0-n", controller="SFC", candidate=candidate, condition="nominal",
    )
    ledger.seal("SFC-0-n", status="complete", evidence={
        **ledger.bindings, "artifact_sha256": "b" * 64, "objective_eligible": True,
        "nominal_feasible": False, "objective": 1.0,
    })
    ledger.begin(
        attempt_id="SFC-0-d", controller="SFC", candidate=candidate, condition="disturbed", unit=unit,
    )
    ledger.seal("SFC-0-d", status="complete", evidence={
        **ledger.bindings, "artifact_sha256": "c" * 64, "objective_eligible": True,
        "objective": 1.0, "pair_feasible": True,
    })
    with pytest.raises(ValueError, match="disturbed_guards_ok must be bool"):
        ledger.training_observations("SFC")
    ledger.close()


def test_pair_feasible_adapter_keeps_true_nominal_out_of_ei(tmp_path):
    output = _create(tmp_path)
    ledger = _ledger(output)
    proposal = ledger.tuner.propose("SFC", [], 0)
    _seal_pair(
        ledger, "SFC", 0, proposal, objective=1.5,
        pair_feasible=False, nominal_feasible=True,
    )
    rows = ledger.training_observations("SFC")
    assert rows[0].nominal_feasible is False
    assert rows[0].status == "completed"
    assert rows[0].objective == 1.5
    evidence = json.loads(ledger.db.execute(
        "SELECT evidence FROM attempts WHERE id='SFC-0-n'").fetchone()[0])
    assert evidence["nominal_feasible"] is True
    assert evidence["pair_feasible"] is False
    ledger.close()


def test_config_drift_is_rejected(tmp_path):
    cfg = tmp_path / "yield_fair_campaign_v1.json"
    cfg.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    output = tmp_path / "campaign"
    create_campaign(output, config_path=cfg, experiment_root=ROOT)
    payload = json.loads(cfg.read_text(encoding="utf-8"))
    payload["claim_scope"] = "tampered"
    cfg.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(YieldFairCampaignError, match="drifted"):
        campaign_status(output)


def test_stop_before_ei_if_fewer_than_three_feasible_initial(tmp_path):
    output = _create(tmp_path)
    ledger = _ledger(output)
    for index in range(8):
        rows = ledger.training_observations("MSFC")
        proposal = ledger.tuner.propose("MSFC", rows, index)
        _seal_pair(
            ledger, "MSFC", index, proposal,
            failed=index >= 2, objective=float(index + 1),
            pair_feasible=index < 2,
        )
    ledger.close()
    with pytest.raises(YieldFairCampaignError, match="stopping before EI"):
        run_next(output, method="MSFC", runner=_fail_artifact)
    ledger = _ledger(output)
    assert ledger.progress()["MSFC"]["used_units"] == 8
    ledger.close()


def test_synthetic_full_24_schedule_freeze_exports_identities(tmp_path):
    output = _create(tmp_path)
    ledger = _ledger(output)
    selected = {}
    for method in METHODS:
        for index in range(24):
            rows = ledger.training_observations(method)
            proposal = ledger.tuner.propose(method, rows, index)
            _seal_pair(ledger, method, index, proposal, objective=float(index + 1))
            if index == 20:
                selected[method] = proposal.candidate.as_dict()
    ledger.close()
    frozen = freeze_campaign(output)
    assert frozen["formal_campaign_complete"] is False
    assert frozen["holdout_implemented"] is False
    assert set(frozen["selected_candidates"]) == set(METHODS)
    assert frozen["selected_candidates"] == selected
    assert frozen["source_hashes"]
    assert frozen["execution_identities"]["scientific_files"]
    assert frozen["execution_identities"]["native_files"]
    assert "tools/yield_fair_selection.py" in frozen["execution_identities"]["scientific_files"]
    assert frozen["selection_contract_id"] == "yield-fair-selection-contract-v1"
    again = freeze_campaign(output)
    assert again["freeze_sha256"] == frozen["freeze_sha256"]
    with pytest.raises(YieldFairCampaignError, match="frozen"):
        run_next(output, method="SFC", runner=_fail_artifact)


def test_tampered_nominal_artifact_does_not_enter_ei(tmp_path):
    output = _create(tmp_path)
    run_next(output, method="SFC", runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    path = output / "attempts" / "SFC-00-nominal" / "artifact.json"
    original = path.read_bytes()
    path.write_bytes(b'{"tampered":true}')
    assert path.read_bytes() != original
    with pytest.raises(YieldFairCampaignError, match="digest differs"):
        run_next(output, method="SFC", runner=_ok_artifact, inspect=_inspect_ok, pair_eval=_pair_ok)
    ledger = _ledger(output)
    inflight = ledger.inflight()
    assert inflight is not None and inflight["condition"] == "disturbed"
    with pytest.raises(ValueError, match="not sealed"):
        ledger.training_observations("SFC")
    ledger.close()
    reconcile(output, interrupt=True)
    ledger = _ledger(output)
    rows = ledger.training_observations("SFC")
    assert rows[0].status == "failed"
    assert rows[0].objective is None
    assert rows[0].nominal_feasible is False
    ledger.close()


def test_mutated_sealed_artifact_cannot_freeze(tmp_path):
    output = _create(tmp_path)
    ledger = _ledger(output)
    for method in METHODS:
        for index in range(24):
            rows = ledger.training_observations(method)
            proposal = ledger.tuner.propose(method, rows, index)
            _seal_pair(ledger, method, index, proposal, objective=float(index + 1))
    ledger.close()
    tampered = output / "attempts" / "SFC-0-n" / "artifact.json"
    tampered.parent.mkdir(parents=True, exist_ok=True)
    tampered.write_text("{}", encoding="utf-8")
    with pytest.raises(YieldFairCampaignError, match="mutated"):
        freeze_campaign(output)


def test_concurrent_run_next_does_not_execute_the_same_attempt_twice(tmp_path):
    output = _create(tmp_path)
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
            run_next(output, method="SFC", runner=crash, inspect=_inspect_ok, pair_eval=_pair_ok)
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


def test_holdout_config_is_refused_as_training(tmp_path):
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    payload["does_not_implement_holdout"] = False
    payload["holdout_used_for_tuning"] = True
    cfg = tmp_path / "holdout.json"
    cfg.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception, match="holdout"):
        create_campaign(tmp_path / "out", config_path=cfg, experiment_root=ROOT)


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    from build_contact_qp import build
    return build(tmp_path_factory.mktemp("yield-fair-qp"))


def test_development_native_diagnostic_smoke_does_not_spend_formal_budget(library):
    from contact_yield_runner import run_closed_loop

    observer = json.loads((ROOT / "config" / "yield_normal_observer_v3.json").read_text())
    result = run_closed_loop(
        method="SFC",
        duration_s=0.08,
        qp_library=library,
        estimator_parameters=observer,
        plant_substeps=8,
        surface_parameters={"kappa_xx": 0.8, "kappa_yy": 0.4},
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
    assert result["identity"]
    assert result["plant_identity_payload"].get("integration_substeps") == 8
    assert result["identity_payload"]["estimator_parameters"]["motion_gain"] == 0.3
    assert "initial_inward_normal_base" not in result["identity_payload"]["estimator_parameters"]
    contract = load_contract(CONFIG)
    assert contract.campaign_kind != result["campaign_kind"]


def test_missing_sealed_artifact_cannot_freeze(tmp_path):
    output = _create(tmp_path)
    run_next(output, method="SFC", runner=_ok_artifact, inspect=_inspect_ok)
    (output / "attempts/SFC-00-nominal/artifact.json").unlink()
    with pytest.raises(YieldFairCampaignError, match="missing"):
        freeze_campaign(output)


def test_interrupted_nominal_retains_debit_and_can_finish_failed_pair(tmp_path):
    output = _create(tmp_path)
    def boom(**kwargs):
        raise RuntimeError("interrupted process")
    with pytest.raises(RuntimeError):
        run_next(output, method="SFC", runner=boom)
    reconcile(output, interrupt=True)
    result = run_next(output, method="SFC", runner=_fail_artifact)
    assert result["status"] == "failed"
    ledger = _ledger(output)
    try:
        assert ledger.progress()["SFC"]["used_units"] == 1
        assert ledger.training_observations("SFC")[0].status == "failed"
    finally:
        ledger.close()


def test_null_new_pair_fields_do_not_use_legacy_fallback(tmp_path):
    output = _create(tmp_path)
    ledger = _ledger(output)
    proposal = ledger.tuner.propose("SFC", [], 0)
    _seal_pair(ledger, "SFC", 0, proposal)
    row = ledger.db.execute("SELECT evidence FROM attempts WHERE id='SFC-0-d'").fetchone()
    evidence = json.loads(row[0])
    evidence.update(pair_feasible=None, disturbed_guards_ok=None)
    # Corrupt temporary fixture only; production seal stays immutable.
    ledger.db.execute("UPDATE attempts SET evidence=? WHERE id='SFC-0-d'", (json.dumps(evidence),))
    ledger.db.commit()
    try:
        with pytest.raises(ValueError, match="disturbed_guards_ok must be bool"):
            ledger.training_observations("SFC")
    finally:
        ledger.close()
