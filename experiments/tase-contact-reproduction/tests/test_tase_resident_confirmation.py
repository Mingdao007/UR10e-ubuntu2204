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


def _write_e_config(path):
    config = json.loads(confirmation.DEFAULT_CONFIG.read_text())
    config.update({
        "selected_screening_arm": "E",
        "initial_source": "selected_screening_candidate",
        "force_integral_policy": "integral-off-v1",
        "force_integral_limit_n_s": 1.0,
        "screening_campaign": "runs/tase-r31-integral-screening-test",
    })
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    return path


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


def test_five_pairs_match_rate460_a_baseline_to_selected_e_policy(monkeypatch, tmp_path):
    tuning = tmp_path / "tuning"
    _write_tuning_summary(tuning)
    config = _write_e_config(tmp_path / "rate460-e.json")

    def fake_run(command, **_kwargs):
        assert command[command.index("--duration") + 1] == "r013_60_rate460"
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
    result = confirmation.run_once(config, tuning)
    assert result["status"] == "complete"
    assert result["physical_attempts"] == 10
    assert result["valid_pairs"] == 5
    assert result["mean_improvement_n"] == 0.5
    assert result["improvement_supported"] is True
    plan = json.loads((tuning / "confirmation-rate460-e-v1/session-01-plan.json").read_text())
    assert {row["parameter"]["frozen"]["force_integral_limit_n_s"]
            for row in plan["rows"] if row["arm"] == "A"} == {1.0}
    assert {row["parameter"]["frozen"]["force_integral_limit_n_s"]
            for row in plan["rows"] if row["arm"] == "B"} == {1.0}
    assert {row["parameter"]["frozen"]["force_integral_policy"]
            for row in plan["rows"] if row["arm"] == "A"} == {"legacy-clamp-v1"}
    assert {row["parameter"]["frozen"]["force_integral_policy"]
            for row in plan["rows"] if row["arm"] == "B"} == {"integral-off-v1"}


def test_failed_half_pair_is_retained_and_whole_pair_retried(monkeypatch, tmp_path):
    tuning = tmp_path / "tuning"
    _write_tuning_summary(tuning)
    config = _write_e_config(tmp_path / "rate460-e.json")
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
    first = confirmation.run_once(config, tuning)
    assert first["status"] == "incomplete"
    assert first["valid_pairs"] == 1
    second = confirmation.run_once(config, tuning, resume=True)
    assert counts == [10, 8]
    assert second["status"] == "complete"
    assert second["physical_attempts"] == 11
    assert second["valid_pairs"] == 5


def test_resume_rejects_changed_frozen_comparison(monkeypatch, tmp_path):
    tuning = tmp_path / "tuning"
    _write_tuning_summary(tuning)
    config = _write_e_config(tmp_path / "rate460-e.json")
    monkeypatch.setattr(confirmation.subprocess, "run", lambda *_args, **_kwargs: None)
    confirmation.run_once(config, tuning)
    source = tuning / "summary.json"
    data = json.loads(source.read_text())
    data["best"]["Md_scalar"] += 0.01
    source.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="comparison changed"):
        confirmation.run_once(config, tuning, resume=True)


def test_prelaunch_failure_uses_new_session_number_on_resume(monkeypatch, tmp_path):
    tuning = tmp_path / "tuning"
    _write_tuning_summary(tuning)
    config = _write_e_config(tmp_path / "rate460-e.json")
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(Path(command[command.index("--run-dir") + 1]).name)
        if len(calls) == 1:
            raise OSError("prelaunch failure")
        return None

    monkeypatch.setattr(confirmation.subprocess, "run", fake_run)
    with pytest.raises(OSError, match="prelaunch failure"):
        confirmation.run_once(config, tuning)
    folder = tuning / "confirmation-rate460-e-v1"
    (folder / "session-01-plan.json").unlink()
    (folder / "session-01-manifest.json").unlink()
    confirmation.run_once(config, tuning, resume=True)
    assert calls == ["session-01", "session-02"]
    assert list((folder / "candidates").glob("session-01-*.json"))
    assert (folder / "session-02-plan.json").is_file()


def test_resume_reconciles_interrupted_arm_only_after_verified_home(tmp_path):
    tuning = tmp_path / "tuning"
    _write_tuning_summary(tuning)
    config_path = _write_e_config(tmp_path / "rate460-e.json")
    config = confirmation.load_config(config_path)
    best = {"Md_scalar": 9.4, "Bd_scalar": 777.0}
    confirmation_dir = tuning / "confirmation-rate460-e-v1"
    session = confirmation_dir / "session-02"
    session.mkdir(parents=True)
    (confirmation_dir / "comparison-frozen.json").write_text(json.dumps({
        "config": config, "tuning_best": best, "tuning_source": str(tuning / "summary.json"),
    }))
    planned = [
        {"block": 0, "arm": arm, "pair_attempt_id": "session-02-block-00",
         "parameter": confirmation._parameter(arm, 0, 2, config, best)}
        for arm in ("B", "A")
    ]
    (confirmation_dir / "session-02-plan.json").write_text(
        json.dumps({"rows": planned}, indent=2)
    )
    _write_attempt(session, 1, planned[0]["parameter"], 1.8)
    (session / "owner.json").write_text(json.dumps({
        "pid": 99999999, "process_start": 1, "entry": "contact_yield_live.py",
        "run_dir": str(session), "attempt_id": "interrupted", "active": True,
    }))
    (session / "supervisor-rtde.jsonl").write_text("\n".join(json.dumps(row) for row in (
        # Retained TP registers can show the previous session's ordinal at
        # STOP; it must not be mistaken for a new attempt in this session.
        {"output_int_register_25": 10, "output_int_register_26": 90,
         "timestamp": 9.9, "observed_at_s": 99.9},
        {"output_int_register_25": 1, "output_int_register_26": 25,
         "timestamp": 10.0, "observed_at_s": 100.0},
        {"output_int_register_25": 2, "output_int_register_26": 20,
         "timestamp": 10.4, "observed_at_s": 100.4,
         "actual_TCP_pose": [0, 0, 0, 0, 0, 0], "safety_mode": 1, "robot_mode": 7},
    )) + "\n")
    recovery_dir = session / "recovery-after-orchestrator-exit"
    recovery_dir.mkdir()
    recovery = {
        "success": True, "state": "HOME_RECOVERED", "source_attempt": str(session),
        "home_commandable": True, "home_attempted": True,
        "home_motion_dispatched": True, "home_blocked": False,
        "home": {
            "success": True, "final_joint_max_error_rad": 0.00008,
            "final_q": [0, 0, 0, 0, 0, 0],
            "dashboard_after": {
                "running": "Program running: false", "programState": "STOPPED home.urp",
                "safetymode": "Safetymode: NORMAL", "is in remote control": "true",
            },
        },
    }
    recovery_path = recovery_dir / "result.json"
    recovery_path.write_text(json.dumps(recovery))

    assert confirmation._reconcile_recovered_orphan(session, confirmation_dir) is True
    partial = json.loads((session / "attempts/0002/attempt-result.json").read_text())
    assert partial["partial"] is True
    assert partial["physical_dispatched"] is True
    assert partial["parameter_binding"]["candidate_id"] == planned[1]["parameter"]["candidate_id"]
    assert partial["applied_runtime_parameters"] is None
    assert partial["evidence"]["metrics"]["normal_force_mae_n"] is None
    assert partial["lifecycle"]["path_complete"] is False
    assert json.loads((session / "owner.json").read_text())["active"] is False
    summary = confirmation.score(confirmation_dir)
    assert summary["physical_attempts"] == 2
    assert summary["failed_attempts"] == 1
    assert summary["all_sessions_closed"] is True


def test_orphan_reconciliation_requires_verified_recovery(tmp_path):
    confirmation_dir = tmp_path / "confirmation-rate460-e-v1"
    session = confirmation_dir / "session-02"
    session.mkdir(parents=True)
    (session / "owner.json").write_text(json.dumps({
        "pid": 99999999, "process_start": 1, "entry": "contact_yield_live.py",
        "run_dir": str(session), "active": True,
    }))
    (confirmation_dir / "session-02-plan.json").write_text(json.dumps({"rows": []}))
    recovery_dir = session / "recovery-after-orchestrator-exit"
    recovery_dir.mkdir()
    (recovery_dir / "result.json").write_text(json.dumps({
        "success": False, "state": "BLOCKED", "source_attempt": str(session),
    }))

    assert confirmation._reconcile_recovered_orphan(session, confirmation_dir) is False
    assert not (session / "dispatch_receipt.json").exists()
    assert json.loads((session / "owner.json").read_text())["active"] is True
