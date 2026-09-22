import json

from tase_integral_screening import (
    build_schedule, execute_campaign, load_screening_config, prepare_campaign,
    score_campaign,
)
import pytest


def test_screening_schedule_has_five_randomized_blocks_and_five_arms():
    rows = build_schedule(load_screening_config())
    assert len(rows) == 25
    assert [sum(row["arm_id"] == arm for row in rows) for arm in "ABCDE"] == [5] * 5
    for block in range(5):
        assert sorted(row["arm_id"] for row in rows if row["block"] == block) == list("ABCDE")
    assert len({(row["block"], row["position"]) for row in rows}) == 25


def test_screening_schedule_binds_incumbent_and_strategy_identity():
    rows = build_schedule(load_screening_config())
    assert {(row["Md_scalar"], row["Bd_scalar"]) for row in rows} == {
        (9.565272137974492, 693.6559295653944)
    }
    d = next(row for row in rows if row["arm_id"] == "D")
    assert d["frozen"]["force_integral_policy"] == "conditional-double-clamp-v1"
    assert d["frozen"]["force_integral_limit_n_s"] == 1.0
    e = next(row for row in rows if row["arm_id"] == "E")
    assert e["frozen"]["force_integral_policy"] == "integral-off-v1"


def test_screening_campaign_freezes_distinct_home_loaded_candidates(tmp_path):
    manifest_path = prepare_campaign(tmp_path / "screen")
    manifest = json.loads(manifest_path.read_text())
    files = manifest["parameter_files"]
    assert len(files) == len(set(files)) == 25
    candidates = [json.loads((manifest_path.parent / name).read_text()) for name in files]
    assert {item["frozen"]["force_integral_policy"] for item in candidates} == {
        "legacy-clamp-v1", "conditional-double-clamp-v1", "integral-off-v1",
    }
    assert {item["protocol_id"] for item in candidates} == {
        "figure8_window60_r013_rate400_v1",
    }


def test_screening_score_keeps_failed_diagnostic_out_of_selection(tmp_path):
    manifest_path = prepare_campaign(tmp_path / "screen")
    campaign = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    for ordinal, eligible in ((1, True), (2, False)):
        parameter = json.loads((campaign / manifest["parameter_files"][ordinal - 1]).read_text())
        folder = campaign / "session-01" / "attempts" / f"{ordinal:04d}"
        folder.mkdir(parents=True)
        (folder / "seal.json").write_text("{}")
        (folder / "attempt-result.json").write_text(json.dumps({
            "sequence": ordinal,
            "physical_dispatched": True,
            "parameter_binding": parameter,
            "applied_runtime_parameters": {
                "Md_scalar": parameter["Md_scalar"],
                "Bd_scalar": parameter["Bd_scalar"],
                "force_integral_limit_n_s": parameter["frozen"]["force_integral_limit_n_s"],
                "force_integral_policy": parameter["frozen"]["force_integral_policy"],
                "force_integral_authority_error_n": parameter["frozen"]["force_integral_authority_error_n"],
            },
            "evidence_eligible": eligible,
            "lifecycle": {"path_complete": True, "home_verified": True},
            "evidence": {"metrics": {
                "complete_bins": 550,
                "protocol_id": parameter["protocol_id"],
                "timing_gate_passed": eligible,
                "timing_evidence": {"acceptance_protocol_id": parameter["protocol_id"]},
                "normal_force_mae_n": 1.0 if eligible else 0.1,
            }},
        }))
    result = score_campaign(campaign)
    assert result["attempted"] == 2 and result["selected_arm"] is None
    assert [row["status"] for row in result["attempts"]] == ["complete", "failed"]
    assert result["attempts"][1]["mae_n"] is None
    assert result["attempts"][1]["diagnostic_mae_n"] == 0.1


def test_screening_execute_uses_one_resident_manifest(monkeypatch, tmp_path):
    campaign = tmp_path / "screen"
    prepare_campaign(campaign)
    called = []

    def fake_run(command, **kwargs):
        called.append((command, kwargs))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("tase_integral_screening.subprocess.run", fake_run)
    result = execute_campaign(campaign)
    assert result["attempted"] == 0
    assert len(called) == 1
    command, kwargs = called[0]
    assert command[command.index("--resident-attempts") + 1] == "25"
    assert command[command.index("--duration") + 1] == "r013_60_rate400"
    manifest = json.loads((campaign / "session-01-manifest.json").read_text())
    assert len(manifest["parameter_files"]) == 25


def test_screening_resume_rejects_unaccounted_physical_attempt(tmp_path):
    campaign = tmp_path / "screen"
    prepare_campaign(campaign)
    session = campaign / "session-01"
    session.mkdir()
    (session / "dispatch_receipt.json").write_text(json.dumps({
        "attempts": [{"sequence": 1}],
        "stop": {"home_verified": True, "program_stopped": True},
    }))
    with pytest.raises(RuntimeError, match="unaccounted physical attempt"):
        execute_campaign(campaign, resume=True)


def test_failed_after_collector_retains_diagnostic_but_cannot_score(tmp_path):
    campaign = tmp_path / "screen"
    manifest = prepare_campaign(campaign)
    first = json.loads((campaign / json.loads(manifest.read_text())["parameter_files"][0]).read_text())
    folder = campaign / "session-01" / "attempts" / "0001"
    folder.mkdir(parents=True)
    (folder / "seal.json").write_text("{}")
    (folder / "attempt-result.json").write_text(json.dumps({
        "parameter_binding": first,
        "physical_dispatched": True,
        "partial": False,
        "failed_after_collector": "transport closed",
        "evidence_eligible": False,
        "lifecycle": {"path_complete": True, "home_verified": True},
        "evidence": {"metrics": {
            "complete_bins": 550,
            "protocol_id": first["protocol_id"],
            "timing_gate_passed": True,
            "timing_evidence": {"acceptance_protocol_id": first["protocol_id"]},
            "normal_force_mae_n": 1.2,
        }},
    }))
    result = score_campaign(campaign)
    assert result["attempted"] == 1
    assert result["attempts"][0]["status"] == "failed"
    assert result["attempts"][0]["diagnostic_mae_n"] == 1.2


def test_full_budget_cannot_select_without_verified_home_and_stop(tmp_path):
    campaign = tmp_path / "screen"
    manifest = json.loads(prepare_campaign(campaign).read_text())
    for ordinal, name in enumerate(manifest["parameter_files"], 1):
        parameter = json.loads((campaign / name).read_text())
        folder = campaign / "session-01" / "attempts" / f"{ordinal:04d}"
        folder.mkdir(parents=True)
        (folder / "seal.json").write_text("{}")
        (folder / "attempt-result.json").write_text(json.dumps({
            "physical_dispatched": True,
            "parameter_binding": parameter,
            "applied_runtime_parameters": {
                "Md_scalar": parameter["Md_scalar"],
                "Bd_scalar": parameter["Bd_scalar"],
                "force_integral_limit_n_s": parameter["frozen"]["force_integral_limit_n_s"],
                "force_integral_policy": parameter["frozen"]["force_integral_policy"],
                "force_integral_authority_error_n": parameter["frozen"]["force_integral_authority_error_n"],
            },
            "evidence_eligible": True,
            "lifecycle": {"path_complete": True, "home_verified": True},
            "evidence": {"metrics": {
                "complete_bins": 550,
                "protocol_id": parameter["protocol_id"],
                "timing_gate_passed": True,
                "timing_evidence": {"acceptance_protocol_id": parameter["protocol_id"]},
                "normal_force_mae_n": 1.0,
            }},
        }))
    result = score_campaign(campaign)
    assert result["attempted"] == 25
    assert result["status"] == "awaiting_verified_close"
    assert result["selected_arm"] is None
    (campaign / "session-01" / "dispatch_receipt.json").write_text(json.dumps({
        "attempts": [{"sequence": index} for index in range(1, 26)],
        "stop": {"home_verified": True, "program_stopped": True},
    }))
    result = score_campaign(campaign)
    assert result["status"] == "complete"
    assert result["selected_arm"] == "A"


def test_pre_arm_failure_does_not_consume_screening_ordinal(tmp_path):
    campaign = tmp_path / "screen"
    prepare_campaign(campaign)
    session = campaign / "session-01"
    session.mkdir()
    (session / "dispatch_receipt.json").write_text(json.dumps({
        "attempts": [],
        "pre_arm_failure": {"candidate_id": "screen-A-00", "physical_dispatched": False},
        "stop": {"home_verified": True, "program_stopped": True},
    }))
    result = score_campaign(campaign)
    assert result["attempted"] == 0
    assert result["status"] == "incomplete"


def test_later_blocked_recovery_overrides_earlier_home_stop(tmp_path):
    campaign = tmp_path / "screen"
    prepare_campaign(campaign)
    session = campaign / "session-01"
    session.mkdir()
    (session / "dispatch_receipt.json").write_text(json.dumps({
        "attempts": [],
        "stop": {"home_verified": True, "program_stopped": True},
    }))
    (session / "supervisor-result.json").write_text(json.dumps({
        "autonomous_home_recovery": {"success": False, "state": "BLOCKED"},
    }))
    with pytest.raises(RuntimeError, match="verified joint Home and STOPPED"):
        execute_campaign(campaign, resume=True)


def test_recovery_evidence_directories_are_not_candidate_sessions(monkeypatch, tmp_path):
    campaign = tmp_path / "screen"
    prepare_campaign(campaign)
    session = campaign / "session-01"
    session.mkdir()
    (session / "dispatch_receipt.json").write_text(json.dumps({
        "attempts": [],
        "stop": {"home_verified": True, "program_stopped": True},
    }))
    (campaign / "session-01-autonomous-home").mkdir()
    commands = []
    def fake_run(command, **kwargs):
        commands.append(command)
        return type("Completed", (), {"returncode": 0})()
    monkeypatch.setattr("tase_integral_screening.subprocess.run", fake_run)
    execute_campaign(campaign, resume=True)
    assert commands[0][commands[0].index("--run-dir") + 1] == str(campaign / "session-02")
