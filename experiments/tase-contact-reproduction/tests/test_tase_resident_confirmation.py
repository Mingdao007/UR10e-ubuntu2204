import json
from pathlib import Path

import pytest

import tase_resident_confirmation as confirmation


def _write_tuning_summary(path):
    path.mkdir()
    (path / "summary.json").write_text(json.dumps({
        "schema": "tase.resident-autotuner-summary-v1",
        "protocol_id": confirmation.PROTOCOL,
        "status": "complete", "resident_sessions_closed": True,
        "attempted": 24,
        "best": {"candidate_id": "bo-10", "Md_scalar": 8.592659656558919,
                 "Bd_scalar": 772.1473715259434},
    }))


def _write_attempt(session: Path, sequence: int, parameter: dict, mae: float):
    folder = session / "attempts" / f"{sequence:04d}"
    folder.mkdir(parents=True)
    (folder / "seal.json").write_text("{}")
    (folder / "attempt-result.json").write_text(json.dumps({
        "physical_dispatched": True,
        "parameter_binding": parameter,
        "applied_runtime_parameters": {
            "Md_scalar": parameter["Md_scalar"],
            "Bd_scalar": parameter["Bd_scalar"],
            "force_integral_policy": parameter["frozen"]["force_integral_policy"],
            "force_integral_limit_n_s": parameter["frozen"]["force_integral_limit_n_s"],
            "force_integral_authority_error_n": 0.5,
        },
        "evidence_eligible": True,
        "lifecycle": {"path_complete": True, "home_verified": True},
        "evidence": {"metrics": {
            "complete_bins": 550,
            "protocol_id": confirmation.PROTOCOL,
            "timing_gate_passed": True,
            "timing_evidence": {"acceptance_protocol_id": confirmation.PROTOCOL},
            "normal_force_mae_n": mae,
        }},
    }))


def test_five_pairs_keep_distinct_a_and_b_integral_identity(monkeypatch, tmp_path):
    tuning = tmp_path / "tuning"
    _write_tuning_summary(tuning)

    def fake_run(command, **_kwargs):
        manifest = Path(command[command.index("--resident-parameter-manifest") + 1])
        session = Path(command[command.index("--run-dir") + 1])
        files = json.loads(manifest.read_text())["parameter_files"]
        parameters = [json.loads((manifest.parent / name).read_text()) for name in files]
        for sequence, parameter in enumerate(parameters, 1):
            arm = parameter["candidate_id"][-1]
            _write_attempt(session, sequence, parameter, 1.5 if arm == "A" else 1.0)
        (session / "dispatch_receipt.json").write_text(json.dumps({
            "attempts": [{"sequence": i} for i in range(1, len(files) + 1)],
            "stop": {"home_verified": True, "program_stopped": True},
        }))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(confirmation.subprocess, "run", fake_run)
    result = confirmation.run_once(confirmation.DEFAULT_CONFIG, tuning)
    assert result["status"] == "complete"
    assert result["physical_attempts"] == 10
    assert result["valid_pairs"] == 5
    assert result["mean_improvement_n"] == 0.5
    assert result["improvement_supported"] is True
    plan = json.loads((tuning / "confirmation-rate400-b-v1/session-01-plan.json").read_text())
    assert {row["parameter"]["frozen"]["force_integral_limit_n_s"]
            for row in plan["rows"] if row["arm"] == "A"} == {1.0}
    assert {row["parameter"]["frozen"]["force_integral_limit_n_s"]
            for row in plan["rows"] if row["arm"] == "B"} == {0.5}


def test_failed_half_pair_is_retained_and_whole_pair_retried(monkeypatch, tmp_path):
    tuning = tmp_path / "tuning"
    _write_tuning_summary(tuning)
    counts = []

    def fake_run(command, **_kwargs):
        manifest = Path(command[command.index("--resident-parameter-manifest") + 1])
        session = Path(command[command.index("--run-dir") + 1])
        files = json.loads(manifest.read_text())["parameter_files"]
        counts.append(len(files))
        physical = 3 if len(counts) == 1 else len(files)
        for sequence, name in enumerate(files[:physical], 1):
            parameter = json.loads((manifest.parent / name).read_text())
            _write_attempt(session, sequence, parameter,
                           1.5 if parameter["candidate_id"].endswith("A") else 1.0)
        (session / "dispatch_receipt.json").write_text(json.dumps({
            "attempts": [{"sequence": i} for i in range(1, physical + 1)],
            "stop": {"home_verified": True, "program_stopped": True},
        }))
        return type("Completed", (), {"returncode": 1 if len(counts) == 1 else 0})()

    monkeypatch.setattr(confirmation.subprocess, "run", fake_run)
    first = confirmation.run_once(confirmation.DEFAULT_CONFIG, tuning)
    assert first["status"] == "incomplete"
    assert first["valid_pairs"] == 1
    second = confirmation.run_once(confirmation.DEFAULT_CONFIG, tuning, resume=True)
    assert counts == [10, 8]
    assert second["status"] == "complete"
    assert second["physical_attempts"] == 11
    assert second["valid_pairs"] == 5


def test_resume_rejects_changed_frozen_comparison(monkeypatch, tmp_path):
    tuning = tmp_path / "tuning"
    _write_tuning_summary(tuning)
    monkeypatch.setattr(confirmation.subprocess, "run", lambda *_args, **_kwargs: None)
    confirmation.run_once(confirmation.DEFAULT_CONFIG, tuning)
    source = tuning / "summary.json"
    data = json.loads(source.read_text())
    data["best"]["Md_scalar"] += 0.01
    source.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="comparison changed"):
        confirmation.run_once(confirmation.DEFAULT_CONFIG, tuning, resume=True)


def test_prelaunch_failure_uses_new_session_number_on_resume(monkeypatch, tmp_path):
    tuning = tmp_path / "tuning"
    _write_tuning_summary(tuning)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(Path(command[command.index("--run-dir") + 1]).name)
        if len(calls) == 1:
            raise OSError("prelaunch failure")
        return None

    monkeypatch.setattr(confirmation.subprocess, "run", fake_run)
    with pytest.raises(OSError, match="prelaunch failure"):
        confirmation.run_once(confirmation.DEFAULT_CONFIG, tuning)
    folder = tuning / "confirmation-rate400-b-v1"
    (folder / "session-01-plan.json").unlink()
    (folder / "session-01-manifest.json").unlink()
    confirmation.run_once(confirmation.DEFAULT_CONFIG, tuning, resume=True)
    assert calls == ["session-01", "session-02"]
    assert list((folder / "candidates").glob("session-01-*.json"))
    assert (folder / "session-02-plan.json").is_file()
