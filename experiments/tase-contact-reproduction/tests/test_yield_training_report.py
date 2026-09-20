"""Synthetic falsifiers for the read-only training-result report. No live campaign."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import yield_training_report as report_module
from yield_training_report import (
    YieldTrainingReportError,
    build_report_from_snapshot,
    main,
    read_readonly_snapshot,
    write_training_report,
)


CLAIM = (
    "prospective DEVELOPMENT-model comparison; no real precise-contact, "
    "safety, or physical qualification claim"
)
BINDINGS = {
    "schema": "yield-contact-training-ledger-v1",
    "split": "training",
    "campaign_protocol_sha256": "aa" * 32,
    "tuner_config_sha256": "bb" * 32,
    "training_cell_id": "stiff_low_mu",
    "selection_contract_id": "yield-fair-selection-contract-v1",
}
LEDGER_SQL = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE units(
    controller TEXT NOT NULL,
    number INTEGER NOT NULL,
    candidate TEXT NOT NULL,
    PRIMARY KEY(controller, number)
);
CREATE TABLE attempts(
    id TEXT PRIMARY KEY,
    controller TEXT NOT NULL,
    unit INTEGER NOT NULL,
    condition TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence TEXT
);
"""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _candidate(method: str, m=4.0, mu=393.0, g=0.052126826121414976) -> dict:
    payload = {"method": method, "m": m, "mu": mu, "g": g, "n": 3.0}
    if method != "SFC":
        payload.update(
            {
                "p": 0.5,
                "a": 0.05,
                "max_iterations": 32.0,
                "residual_tolerance_n": 1e-09,
            }
        )
    return payload


def _rows(*, load=5.0, path=0.001, attitude=0.01, progress=1.0, offset=0.0, low=False):
    rows = []
    for index in range(8):
        time_s = index * 0.1
        load_n = 0.4 if low and index in {4, 5} else load
        rows.append(
            {
                "time_s": time_s,
                "true_normal_load_n": load_n,
                "force_error_n": load_n - 5.0,
                "path_error_m": path,
                "orientation_error_rad": attitude,
                "normal_estimation_error_rad": 0.0,
                "actual_progress_m_s": progress,
                "reference_progress_m_s": 1.0,
                "position_m": [offset, 0.0, 0.0],
                "saturated": index == 7,
                "qp_intervention": index == 6,
            }
        )
    return rows


def _artifact(method: str, scenario: str, parameters: dict, *, load=5.0, path=0.001,
              attitude=0.01, progress=1.0, offset=0.0, low=False, failed=False):
    return {
        "method": method,
        "scenario": scenario,
        "material": "stiff_low_mu",
        "dt_s": 0.1,
        "duration_s": 0.7,
        "preparation": "cold",
        "timeline": "diagnostic",
        "identity": "ab" * 32,
        "campaign_kind": "training",
        "kinematics_kind": "ur10e_calibrated_pinocchio",
        "metrics": {"failed": failed, "force_peak_n": load},
        "identity_payload": {"parameters": parameters},
        "rows": _rows(
            load=load, path=path, attitude=attitude, progress=progress, offset=offset, low=low
        ),
    }


def _write_artifact(root: Path, attempt_id: str, payload: dict) -> tuple[bytes, str]:
    path = root / "attempts" / attempt_id / "artifact.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    path.write_bytes(encoded)
    return encoded, _sha256(encoded)


def _state(root: Path) -> dict:
    protocol = {
        "schema": "ur10e.yield-fair-campaign-protocol-v1",
        "campaign_config_sha256": "cc" * 32,
        "tuner_config_sha256": "bb" * 32,
        "observer_config_sha256": "dd" * 32,
        "yield_protocol_sha256": "ee" * 32,
        "training_cell_id": "stiff_low_mu",
        "selection_contract_id": "yield-fair-selection-contract-v1",
        "source_hashes": {"contact_yield_metrics.py": "11" * 32},
        "execution_identities": {
            "scientific_files": {"tools/yield_contact_tuner.py": "22" * 32}
        },
    }
    return {
        "schema": "ur10e.yield-fair-campaign-state-v1",
        "version": 1,
        "config_path": "/recorded/not/opened/config/yield_fair_campaign_v1.json",
        "config_sha256": "cc" * 32,
        "experiment_root": "/recorded/not/opened",
        "output_root": str(root),
        "ledger_path": str(root / "campaign.sqlite"),
        "campaign_protocol": protocol,
        "campaign_protocol_sha256": "aa" * 32,
        "ledger_bindings": dict(BINDINGS),
        "formal_campaign_complete": False,
        "holdout_implemented": False,
        "hardware_qualified": False,
        "claim_scope": CLAIM,
    }


def _open_campaign(tmp_path: Path) -> Path:
    root = tmp_path / "campaign"
    root.mkdir()
    connection = sqlite3.connect(root / "campaign.sqlite")
    connection.executescript(LEDGER_SQL)
    connection.execute("INSERT INTO metadata VALUES ('protocol', ?)", ("ff" * 32,))
    connection.execute(
        "INSERT INTO metadata VALUES ('controllers', ?)",
        (_canonical(["SFC", "DSFC", "MSFC"]),),
    )
    connection.commit()
    connection.close()
    (root / "campaign.json").write_text(_canonical(_state(root)) + "\n", encoding="utf-8")
    return root


def _insert_unit(root: Path, method: str, unit: int, candidate: dict) -> None:
    connection = sqlite3.connect(root / "campaign.sqlite")
    connection.execute(
        "INSERT INTO units VALUES (?, ?, ?)",
        (method, unit, _canonical(candidate)),
    )
    connection.commit()
    connection.close()


def _insert_attempt(root: Path, method: str, unit: int, condition: str, status: str, evidence=None) -> None:
    connection = sqlite3.connect(root / "campaign.sqlite")
    connection.execute(
        "INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?)",
        (
            f"{method}-{unit:02d}-{condition}",
            method,
            unit,
            condition,
            status,
            None if evidence is None else _canonical(evidence),
        ),
    )
    connection.commit()
    connection.close()


def _complete_pair(
    root: Path,
    method: str,
    unit: int,
    *,
    objective: float,
    pair_feasible: bool,
    nominal_feasible: bool,
    disturbed_guards_ok: bool,
    candidate=None,
    disturbed_offset=0.002,
    low=False,
):
    candidate = candidate or _candidate(method)
    _insert_unit(root, method, unit, candidate)
    nominal_art = _artifact(method, "nominal", candidate)
    disturbed_art = _artifact(
        method,
        "sustained_release_oblique",
        candidate,
        offset=disturbed_offset,
        low=low,
        path=0.002,
    )
    _, n_digest = _write_artifact(root, f"{method}-{unit:02d}-nominal", nominal_art)
    _, d_digest = _write_artifact(root, f"{method}-{unit:02d}-disturbed", disturbed_art)
    nominal_evidence = {
        **BINDINGS,
        "artifact_sha256": n_digest,
        "objective_eligible": True,
        "nominal_feasible": nominal_feasible,
        "nominal_checks": {
            "path_rms_ok": nominal_feasible,
            "progress_ok": True,
            "attitude_rms_ok": True,
            "load_mae_ok": True,
            "load_peak_ok": True,
            "load_min_ok": True,
            "full_cycle": True,
            "no_failure": True,
        },
    }
    disturbed_evidence = {
        **BINDINGS,
        "artifact_sha256": d_digest,
        "objective_eligible": True,
        "objective": objective,
        "objective_components": {
            "J": objective,
            "Jload": objective * 0.5,
            "Jpath": objective * 0.3,
            "Jatt": objective * 0.2,
        },
        "pair_feasible": pair_feasible,
        "disturbed_guards_ok": disturbed_guards_ok,
        "nominal_feasible_reported": nominal_feasible,
        "disturbed_checks": {
            "load_min_ok": disturbed_guards_ok,
            "load_peak_ok": True,
            "progress_ok": True,
            "full_cycle": True,
            "no_failure": True,
        },
    }
    _insert_attempt(root, method, unit, "nominal", "complete", nominal_evidence)
    _insert_attempt(root, method, unit, "disturbed", "complete", disturbed_evidence)


def _failed_pair(root: Path, method: str, unit: int, *, reason="native failed"):
    candidate = _candidate(method, mu=100.0 + unit)
    _insert_unit(root, method, unit, candidate)
    encoded = b'{"interrupted":true,"metrics":{"failed":true}}'
    digest = _sha256(encoded)
    path = root / "attempts" / f"{method}-{unit:02d}-nominal" / "artifact.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    evidence = {
        **BINDINGS,
        "artifact_sha256": digest,
        "objective_eligible": False,
        "nominal_feasible": False,
        "reason": reason,
    }
    _insert_attempt(root, method, unit, "nominal", "failed", evidence)
    _insert_attempt(
        root,
        method,
        unit,
        "disturbed",
        "interrupted",
        {**evidence, "pair_feasible": False, "disturbed_guards_ok": False},
    )


def test_import_and_help_are_inert(tmp_path, capsys):
    assert main([]) == 0
    captured = capsys.readouterr()
    assert "Does not launch" in captured.out
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    assert list(tmp_path.iterdir()) == []


def test_source_never_instantiates_writer_ledger_or_runs_native():
    text = (Path(__file__).resolve().parents[1] / "tools/yield_training_report.py").read_text(
        encoding="utf-8"
    )
    assert "YieldContactLedger" not in text
    assert "run_closed_loop" not in text
    assert "create_campaign" not in text
    assert "freeze_campaign" not in text
    assert "mode=ro" in text
    assert "_FONT_5X7" not in text
    assert "zlib.compress" not in text
    assert "class _Scene" not in text
    assert "def require_matplotlib" in text


def test_missing_complete_raw_digest_fails(tmp_path):
    root = _open_campaign(tmp_path)
    candidate = _candidate("SFC")
    _insert_unit(root, "SFC", 0, candidate)
    art = _artifact("SFC", "nominal", candidate)
    _write_artifact(root, "SFC-00-nominal", art)
    _insert_attempt(
        root,
        "SFC",
        0,
        "nominal",
        "complete",
        {
            **BINDINGS,
            "objective_eligible": True,
            "nominal_feasible": True,
            "nominal_checks": {"path_rms_ok": True, "no_failure": True},
        },
    )
    _insert_attempt(
        root,
        "SFC",
        0,
        "disturbed",
        "complete",
        {
            **BINDINGS,
            "artifact_sha256": "ab" * 32,
            "objective_eligible": True,
            "pair_feasible": True,
            "disturbed_guards_ok": True,
            "disturbed_checks": {"load_min_ok": True, "no_failure": True},
        },
    )
    with pytest.raises(YieldTrainingReportError, match="missing a 64-hex artifact_sha256"):
        write_training_report(root, tmp_path / "out")


def test_mismatched_complete_raw_digest_fails(tmp_path):
    root = _open_campaign(tmp_path)
    candidate = _candidate("SFC")
    _insert_unit(root, "SFC", 0, candidate)
    art = _artifact("SFC", "nominal", candidate)
    _, digest = _write_artifact(root, "SFC-00-nominal", art)
    _write_artifact(root, "SFC-00-disturbed", _artifact("SFC", "sustained_release_oblique", candidate))
    (root / "attempts/SFC-00-disturbed/artifact.json").write_text('{"tampered":true}\n')
    _insert_attempt(
        root, "SFC", 0, "nominal", "complete",
        {
            **BINDINGS, "artifact_sha256": digest, "objective_eligible": True,
            "nominal_feasible": True, "nominal_checks": {"path_rms_ok": True, "no_failure": True},
        },
    )
    _insert_attempt(
        root, "SFC", 0, "disturbed", "complete",
        {
            **BINDINGS, "artifact_sha256": digest, "objective_eligible": True,
            "pair_feasible": True, "disturbed_guards_ok": True,
            "disturbed_checks": {"load_min_ok": True, "no_failure": True},
        },
    )
    with pytest.raises(YieldTrainingReportError, match="raw digest differs"):
        write_training_report(root, tmp_path / "out")


def test_running_member_artifact_is_never_read(tmp_path):
    root = _open_campaign(tmp_path)
    _complete_pair(
        root, "DSFC", 0, objective=9.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True,
    )
    candidate = _candidate("SFC")
    _insert_unit(root, "SFC", 0, candidate)
    poison = root / "attempts/SFC-00-nominal/artifact.json"
    poison.parent.mkdir(parents=True, exist_ok=True)
    poison.write_text("NOT-JSON-SHOULD-NEVER-BE-PARSED", encoding="utf-8")
    _insert_attempt(root, "SFC", 0, "nominal", "running", None)
    report = write_training_report(root, tmp_path / "out")
    slot = report["methods"]["SFC"]["slots"][0]
    assert slot["slot_state"] == "incomplete_running_or_pending"
    assert slot["members"]["nominal"]["artifact_parsed"] is False
    assert slot["objective"] is None
    assert all(item["attempt_id"] != "SFC-00-nominal" for item in report["artifact_digest_manifest"])
    assert "SFC-00-nominal" in report["snapshot"]["inflight"]


def test_infeasible_low_j_is_not_selected(tmp_path):
    root = _open_campaign(tmp_path)
    _complete_pair(
        root, "SFC", 0, objective=10.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True,
    )
    _complete_pair(
        root, "SFC", 1, objective=1.0, pair_feasible=False,
        nominal_feasible=False, disturbed_guards_ok=True, candidate=_candidate("SFC", mu=80.0),
    )
    _complete_pair(
        root, "SFC", 2, objective=8.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True, candidate=_candidate("SFC", mu=90.0),
    )
    report = write_training_report(root, tmp_path / "out")
    series = report["best_so_far"]["SFC"]
    assert series[0]["best_so_far_j"] == 10.0
    assert series[1]["infeasible_objective_sample"] is True
    assert series[1]["observed_objective"] == 1.0
    assert series[1]["best_so_far_j"] == 10.0
    assert series[2]["best_so_far_j"] == 8.0
    assert report["methods"]["SFC"]["slots"][1]["slot_state"] == "completed_nominal_infeasible"
    assert report["interpretation"]["finite_objective_is_not_feasible_incumbent"] is True


def test_stopped_sfc_rows_are_not_carried_as_full_budget(tmp_path):
    root = _open_campaign(tmp_path)
    for unit in range(8):
        feasible = unit < 2
        _complete_pair(
            root, "SFC", unit,
            objective=20.0 - unit,
            pair_feasible=feasible,
            nominal_feasible=feasible,
            disturbed_guards_ok=True if unit != 7 else False,
            candidate=_candidate("SFC", mu=40.0 + unit),
        )
    report = write_training_report(root, tmp_path / "out")
    sfc = report["methods"]["SFC"]
    assert sfc["budget_spent_pairs"] == 8
    assert sfc["feasible_pairs"] == 2
    assert sfc["stopped_before_ei"] is True
    assert sfc["full_24_budget_spent"] is False
    assert report["equal_budget_freeze"] is False
    assert report["full_budget_three_method_freeze"] is False
    states = [slot["slot_state"] for slot in sfc["slots"]]
    assert states[8:] == ["stopped_before_ei"] * 16
    assert [row["unit"] for row in report["best_so_far"]["SFC"]] == list(range(8))
    assert sfc["slots"][7]["slot_state"] == "completed_nominal_and_disturbed_guard_infeasible"


def test_incomplete_and_failed_pairs_consume_budget_without_fake_j(tmp_path):
    root = _open_campaign(tmp_path)
    _failed_pair(root, "MSFC", 0)
    candidate = _candidate("MSFC", mu=200.0)
    _insert_unit(root, "MSFC", 1, candidate)
    art = _artifact("MSFC", "nominal", candidate)
    _, digest = _write_artifact(root, "MSFC-01-nominal", art)
    _insert_attempt(
        root, "MSFC", 1, "nominal", "complete",
        {**BINDINGS, "artifact_sha256": digest, "objective_eligible": True, "nominal_feasible": True},
    )
    report = write_training_report(root, tmp_path / "out")
    msfc = report["methods"]["MSFC"]
    assert msfc["slots"][0]["slot_state"] == "failed"
    assert msfc["slots"][0]["budget_consumed"] is True
    assert msfc["slots"][0]["objective"] is None
    assert msfc["slots"][0]["members"]["nominal"]["artifact_parsed"] is False
    assert msfc["slots"][1]["slot_state"] == "incomplete_running_or_pending"
    assert msfc["budget_spent_pairs"] == 2
    series = report["best_so_far"]["MSFC"]
    assert [row["unit"] for row in series] == [0]
    assert series[0]["slot_state"] == "failed"
    assert series[0]["observed_objective"] is None
    assert series[0]["best_so_far_j"] is None
    assert all(row["unit"] != 1 for row in series)
    best_csv = (tmp_path / "out" / "best_so_far.csv").read_text(encoding="utf-8").strip().splitlines()
    header = best_csv[0].split(",")
    observed_i = header.index("observed_objective")
    best_i = header.index("best_so_far_j")
    for line in best_csv[1:]:
        cols = line.split(",")
        if cols[0] == "MSFC":
            assert cols[observed_i] == ""
            assert cols[best_i] == ""


def test_output_is_immutable_and_does_not_write_campaign(tmp_path):
    root = _open_campaign(tmp_path)
    _complete_pair(
        root, "DSFC", 0, objective=4.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True,
    )
    sqlite_before = _sha256((root / "campaign.sqlite").read_bytes())
    state_before = (root / "campaign.json").read_bytes()
    first = tmp_path / "report-out"
    write_training_report(root, first)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_training_report(root, first)
    assert _sha256((root / "campaign.sqlite").read_bytes()) == sqlite_before
    assert (root / "campaign.json").read_bytes() == state_before
    with pytest.raises(YieldTrainingReportError, match="inside the campaign root"):
        write_training_report(root, root / "nested-report")


def test_budget_and_parameter_matching_for_initial_not_ei(tmp_path):
    root = _open_campaign(tmp_path)
    shared = {"m": 4.0, "mu": 310.66177089084647, "g": 0.0642}
    for method in ("SFC", "DSFC", "MSFC"):
        _complete_pair(
            root, method, 0, objective=12.0, pair_feasible=True,
            nominal_feasible=True, disturbed_guards_ok=True,
            candidate=_candidate(method, **shared),
        )
    _complete_pair(
        root, "SFC", 1, objective=15.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True,
        candidate=_candidate("SFC", m=5.0, mu=100.0, g=0.01),
    )
    _complete_pair(
        root, "DSFC", 1, objective=16.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True,
        candidate=_candidate("DSFC", m=6.0, mu=200.0, g=0.02),
    )
    _complete_pair(
        root, "MSFC", 1, objective=17.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True,
        candidate=_candidate("MSFC", m=7.0, mu=300.0, g=0.03),
    )
    _complete_pair(
        root, "SFC", 8, objective=3.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True,
        candidate=_candidate("SFC", **shared),
    )
    report = write_training_report(root, tmp_path / "out")
    assert report["methods"]["SFC"]["slots"][0]["shared_initial_triple"] is True
    assert report["methods"]["DSFC"]["slots"][0]["shared_initial_triple"] is True
    assert report["initial_matched_triples"][0]["shared_mechanical_triple"] is True
    assert report["methods"]["SFC"]["slots"][1]["shared_initial_triple"] is False
    ei = report["methods"]["SFC"]["slots"][8]
    assert ei["scheduled_phase"] == "bayesian_ei"
    assert ei["coefficient_matched_pair"] is False
    assert ei["ei_is_method_specific_trajectory"] is True
    assert ei["parameters"]["m"] == 4.0
    assert report["methods"]["SFC"]["slots"][20]["scheduled_phase"] == "repeat_incumbent"
    assert report["methods"]["SFC"]["slots"][20]["literal_repeat"] is True
    assert report["methods"]["SFC"]["slots"][20]["slot_state"] == "not_run"


def test_snapshot_does_not_requery_sqlite(tmp_path):
    root = _open_campaign(tmp_path)
    _complete_pair(
        root, "SFC", 0, objective=7.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True,
    )
    snapshot = read_readonly_snapshot(root)
    (root / "campaign.sqlite").unlink()
    report = build_report_from_snapshot(snapshot, root)
    assert report["methods"]["SFC"]["slots"][0]["slot_state"] == "completed_feasible"
    assert report["methods"]["SFC"]["slots"][0]["members"]["nominal"]["descriptors"]["force_mae_n"] is not None


def test_end_to_end_report_writes_plots_and_keeps_infeasible_triples(tmp_path):
    root = _open_campaign(tmp_path)
    shared = {"m": 4.0, "mu": 393.0, "g": 0.052126826121414976}
    for method in ("SFC", "DSFC", "MSFC"):
        _complete_pair(
            root, method, 0, objective=11.0 + {"SFC": 0, "DSFC": 1, "MSFC": 2}[method],
            pair_feasible=True, nominal_feasible=True, disturbed_guards_ok=True,
            candidate=_candidate(method, **shared), low=True,
        )
    for unit in range(1, 8):
        _complete_pair(
            root, "SFC", unit, objective=30.0 + unit, pair_feasible=False,
            nominal_feasible=False, disturbed_guards_ok=unit != 3,
            candidate=_candidate("SFC", mu=50.0 + unit),
        )
    _complete_pair(
        root, "DSFC", 1, objective=5.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True,
        candidate=_candidate("DSFC", mu=12.0),
    )
    _failed_pair(root, "DSFC", 2)
    candidate = _candidate("MSFC", mu=12.0)
    _insert_unit(root, "MSFC", 1, candidate)
    _insert_attempt(root, "MSFC", 1, "nominal", "running", None)
    poison = root / "attempts/MSFC-01-nominal/artifact.json"
    poison.parent.mkdir(parents=True, exist_ok=True)
    poison.write_bytes(b"running-must-not-parse")
    output = tmp_path / "report-out"
    report = write_training_report(root, output)
    assert (output / "report.json").is_file()
    assert (output / "report.md").is_file()
    for name in ("slots.csv", "members.csv", "initial_triples.csv", "best_so_far.csv", "artifact_digest_manifest.csv"):
        assert (output / name).is_file()
    png = output / "plots/best_so_far_j.png"
    pdf = output / "plots/best_so_far_j.pdf"
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert pdf.read_bytes()[:4] == b"%PDF"
    for name in (
        "initial_triples_nominal.png",
        "initial_triples_nominal.pdf",
        "initial_triples_disturbed.png",
        "initial_triples_disturbed.pdf",
    ):
        assert (output / "plots" / name).is_file()
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "not independent validation" in markdown
    assert "no superiority" in markdown.lower() or "not" in markdown
    assert "load<1N" in markdown or "load < 1 N" in markdown or "< 1 N" in markdown
    assert report["interpretation"]["no_ci_winner_p_value_pooled_validation_or_superiority_claim"] is True
    assert report["snapshot"]["validation_executed"] is False
    infeasible = report["initial_matched_triples"][3]["methods"]["SFC"]
    assert infeasible["dropped_because_infeasible"] is False
    assert infeasible["slot_state"] == "completed_nominal_and_disturbed_guard_infeasible"
    descriptors = report["methods"]["SFC"]["slots"][0]["members"]["disturbed"]["descriptors"]
    assert descriptors["low_load_duration_s"] == pytest.approx(0.2)
    assert "not geometric" in descriptors["low_load_duration_definition"]
    recovery = report["methods"]["SFC"]["slots"][0]["recovery"]
    assert recovery["eligible"] is True
    assert recovery["postrelease_max_offset_m"] == pytest.approx(0.002)
    assert report["methods"]["SFC"]["stopped_before_ei"] is True
    assert len(report["methods"]["SFC"]["slots"]) == 24
    assert len(report["methods"]["DSFC"]["slots"]) == 24
    assert len(report["methods"]["MSFC"]["slots"]) == 24
    assert report["methods"]["MSFC"]["slots"][1]["slot_state"] == "incomplete_running_or_pending"
    assert [row["unit"] for row in report["best_so_far"]["MSFC"]] == [0]
    assert b"matplotlib" in pdf.read_bytes().lower()
    assert main(["--campaign", str(root), "--output", str(tmp_path / "report-cli")]) == 0


def test_contradictory_pair_feasible_does_not_select_or_count_stop(tmp_path):
    root = _open_campaign(tmp_path)
    _complete_pair(
        root, "SFC", 0, objective=1.0, pair_feasible=True,
        nominal_feasible=False, disturbed_guards_ok=True,
    )
    with pytest.raises(YieldTrainingReportError, match="pair_feasible contradicts"):
        write_training_report(root, tmp_path / "out")
    assert (tmp_path / "out").exists() is False


def test_missing_complete_flags_are_not_inferred_valid(tmp_path):
    root = _open_campaign(tmp_path)
    candidate = _candidate("SFC")
    _insert_unit(root, "SFC", 0, candidate)
    _, n_digest = _write_artifact(root, "SFC-00-nominal", _artifact("SFC", "nominal", candidate))
    _, d_digest = _write_artifact(
        root, "SFC-00-disturbed", _artifact("SFC", "sustained_release_oblique", candidate)
    )
    _insert_attempt(
        root, "SFC", 0, "nominal", "complete",
        {
            **BINDINGS,
            "artifact_sha256": n_digest,
            "objective_eligible": True,
            "nominal_feasible": True,
            "nominal_checks": {"path_rms_ok": True, "no_failure": True},
        },
    )
    _insert_attempt(
        root, "SFC", 0, "disturbed", "complete",
        {
            **BINDINGS,
            "artifact_sha256": d_digest,
            "objective_eligible": True,
            "objective": 2.0,
            "disturbed_guards_ok": True,
            "disturbed_checks": {"load_min_ok": True, "no_failure": True},
        },
    )
    with pytest.raises(YieldTrainingReportError, match="pair_feasible must be bool"):
        write_training_report(root, tmp_path / "out")
    assert (tmp_path / "out").exists() is False


def test_checks_contradicting_flags_fail_closed(tmp_path):
    root = _open_campaign(tmp_path)
    candidate = _candidate("SFC")
    _insert_unit(root, "SFC", 0, candidate)
    _, n_digest = _write_artifact(root, "SFC-00-nominal", _artifact("SFC", "nominal", candidate))
    _, d_digest = _write_artifact(
        root, "SFC-00-disturbed", _artifact("SFC", "sustained_release_oblique", candidate)
    )
    _insert_attempt(
        root, "SFC", 0, "nominal", "complete",
        {
            **BINDINGS,
            "artifact_sha256": n_digest,
            "objective_eligible": True,
            "nominal_feasible": True,
            "nominal_checks": {"path_rms_ok": False, "no_failure": True},
        },
    )
    _insert_attempt(
        root, "SFC", 0, "disturbed", "complete",
        {
            **BINDINGS,
            "artifact_sha256": d_digest,
            "objective_eligible": True,
            "objective": 2.0,
            "pair_feasible": True,
            "disturbed_guards_ok": True,
            "disturbed_checks": {"load_min_ok": True, "no_failure": True},
        },
    )
    with pytest.raises(YieldTrainingReportError, match="nominal_checks contradict"):
        write_training_report(root, tmp_path / "out")
    assert (tmp_path / "out").exists() is False


def test_running_unit_does_not_extend_best_so_far_curve(tmp_path):
    root = _open_campaign(tmp_path)
    _complete_pair(
        root, "SFC", 0, objective=1.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True,
    )
    candidate = _candidate("SFC", mu=80.0)
    _insert_unit(root, "SFC", 1, candidate)
    poison = root / "attempts/SFC-01-nominal/artifact.json"
    poison.parent.mkdir(parents=True, exist_ok=True)
    poison.write_text("NOT-JSON", encoding="utf-8")
    _insert_attempt(root, "SFC", 1, "nominal", "running", None)
    report = write_training_report(root, tmp_path / "out")
    assert report["methods"]["SFC"]["slots"][0]["slot_state"] == "completed_feasible"
    assert report["methods"]["SFC"]["slots"][1]["slot_state"] == "incomplete_running_or_pending"
    series = report["best_so_far"]["SFC"]
    assert [row["unit"] for row in series] == [0]
    assert series[0]["best_so_far_j"] == 1.0
    assert all(row["slot_state"] != "incomplete_running_or_pending" for row in series)
    csv_text = (tmp_path / "out" / "best_so_far.csv").read_text(encoding="utf-8")
    assert "incomplete_running_or_pending" not in csv_text
    slots_csv = (tmp_path / "out" / "slots.csv").read_text(encoding="utf-8")
    assert "incomplete_running_or_pending" in slots_csv


def test_missing_matplotlib_fails_before_writing(tmp_path, monkeypatch):
    root = _open_campaign(tmp_path)
    _complete_pair(
        root, "SFC", 0, objective=4.0, pair_feasible=True,
        nominal_feasible=True, disturbed_guards_ok=True,
    )

    def unavailable():
        raise YieldTrainingReportError(
            "matplotlib and numpy are required for PNG/PDF output; unavailable: missing"
        )

    monkeypatch.setattr(report_module, "require_matplotlib", unavailable)
    output = tmp_path / "out"
    with pytest.raises(YieldTrainingReportError, match="matplotlib and numpy are required"):
        write_training_report(root, output)
    assert output.exists() is False
