import json

from tase_integral_screening import (
    _path_duration_matches, _session_verified_closed, build_schedule, execute_campaign,
    load_screening_config, prepare_campaign, score_campaign,
)
import pytest


TEST_TRIPLET = {"script": "a" * 64, "txt": "b" * 64, "urp": "c" * 64}


def _write_dispatch(campaign, *, session_name="session-01", attempt_count=0, stopped=False, triplet=None):
    session = campaign / session_name
    session.mkdir(exist_ok=True)
    schedule = json.loads((campaign / "screening-schedule.json").read_text())
    method = "TASE_RNN_MATURE"
    dispatch = {
        "controller_readback_triplet": TEST_TRIPLET if triplet is None else triplet,
        "method": method,
        "controller_identity": {"method": method},
        "live_path": {"protocol_id": schedule["protocol_id"], "path_duration_s": 60.0},
        "attempts": [{"sequence": index} for index in range(1, attempt_count + 1)],
    }
    if stopped:
        dispatch["stop"] = {"home_verified": True, "program_stopped": True}
    (session / "dispatch_receipt.json").write_text(json.dumps(dispatch))


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
    assert {item["protocol_id"] for item in candidates} == {"figure8_window60_r013_rate400_v1"}
    assert (manifest_path.parent / "config-frozen.json").is_file()
    schedule = json.loads((manifest_path.parent / "screening-schedule.json").read_text())
    assert schedule["seed"] == 20260922
    assert schedule["config_sha256"]


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
                "path_duration_s": 60.00000000001455 if ordinal == 1 else 60.0,
                "path_duration_rounding_bound_s": 2.9118042178810644e-11,
                "path_coverage_interval_s": 0.0020000000076834112,
                "protocol_id": parameter["protocol_id"],
                "timing_gate_passed": eligible,
                "timing_evidence": {
                    "acceptance_protocol_id": parameter["protocol_id"],
                    "successful": eligible,
                    "minimum_rate_hz": 400.0,
                    "layer_rates_hz": {
                        "writer_publishes": 500.0 if eligible else 369.4,
                        "rtde_frames": 500.0 if eligible else 369.4,
                        "kunwei_frames": 999.95,
                        "tp_consumed_packet_echoes": 500.0 if eligible else 369.4,
                    },
                },
                "normal_force_mae_n": 1.0 if eligible else 0.1,
            }},
        }))
    _write_dispatch(campaign, attempt_count=2)
    result = score_campaign(campaign)
    assert result["attempted"] == 2 and result["selected_arm"] is None
    assert [row["status"] for row in result["attempts"]] == ["complete", "failed"]
    assert result["attempts"][1]["mae_n"] is None
    assert result["attempts"][1]["diagnostic_mae_n"] == 0.1
    assert result["attempts"][1]["failure"].startswith(
        "rate_evidence_mismatch:timing_receipt_not_successful,"
    )


def test_path_duration_accepts_only_collector_derived_rounding_error():
    evidence = {
        "path_duration_s": 60.00000000001455,
        "path_duration_rounding_bound_s": 2.9118042178810644e-11,
        "path_coverage_interval_s": 0.0020000000076834112,
    }
    assert _path_duration_matches(evidence)
    assert not _path_duration_matches({**evidence, "path_duration_s": 60.002})
    assert not _path_duration_matches({
        **evidence,
        "path_duration_s": 60.00000000001455 + 1.01 * evidence["path_duration_rounding_bound_s"],
    })
    assert not _path_duration_matches({
        **evidence,
        "path_duration_rounding_bound_s": evidence["path_coverage_interval_s"] / 2.0,
    })
    assert not _path_duration_matches({
        "path_duration_s": 60.00000000001455,
        "path_coverage_interval_s": 0.002,
    })


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


def test_r31_screening_config_uses_rate460_and_freezes_parameters(tmp_path):
    from pathlib import Path

    config_path = Path(__file__).resolve().parents[1] / "config" / "tase_integral_screening_r31_rate460_v1.json"
    manifest_path = prepare_campaign(tmp_path / "r31", config_path)
    campaign = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    schedule = json.loads((campaign / "screening-schedule.json").read_text())
    assert schedule["protocol_id"] == "figure8_window60_r013_rate460_v1"
    assert schedule["duration_token"] == "r013_60_rate460"
    assert schedule["expected_controller_readback_triplet_sha256"] == {
        "script": "ba062d293d800cc2c7a081fcf256e6e6f076f30eae5b86021fa63c52f367370e",
        "txt": "f20271234adb59eea648e07304af283bebc4ca4014e3a63bdf02d8e3e1eee901",
        "urp": "495a98b352f0e5881dca4709de043791f4775ecbc01086088ee1d5eddbb63f6c",
    }
    candidates = [json.loads((campaign / path).read_text()) for path in manifest["parameter_files"]]
    assert {item["protocol_id"] for item in candidates} == {schedule["protocol_id"]}
    assert sum(item["frozen"]["force_integral_policy"] == "integral-off-v1" for item in candidates) == 5


def test_r31_screening_executor_passes_rate460_duration(monkeypatch, tmp_path):
    from pathlib import Path

    config_path = Path(__file__).resolve().parents[1] / "config" / "tase_integral_screening_r31_rate460_v1.json"
    campaign = tmp_path / "r31"
    prepare_campaign(campaign, config_path)
    called = []

    def fake_run(command, **kwargs):
        called.append((command, kwargs))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("tase_integral_screening.subprocess.run", fake_run)
    execute_campaign(campaign)
    command, _kwargs = called[0]
    assert command[command.index("--duration") + 1] == "r013_60_rate460"


def test_r31_screening_score_rejects_rate400_receipt_even_if_attempt_claims_eligible(tmp_path):
    from pathlib import Path

    config_path = Path(__file__).resolve().parents[1] / "config" / "tase_integral_screening_r31_rate460_v1.json"
    manifest_path = prepare_campaign(tmp_path / "r31", config_path)
    campaign = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    parameter = json.loads((campaign / manifest["parameter_files"][0]).read_text())
    folder = campaign / "session-01" / "attempts" / "0001"
    folder.mkdir(parents=True)
    (folder / "seal.json").write_text("{}")
    (folder / "attempt-result.json").write_text(json.dumps({
        "physical_dispatched": True,
        "parameter_binding": parameter,
        "applied_runtime_parameters": {
            "Md_scalar": parameter["Md_scalar"],
            "Bd_scalar": parameter["Bd_scalar"],
            **{key: parameter["frozen"][key] for key in (
                "force_integral_limit_n_s", "force_integral_policy", "force_integral_authority_error_n"
            )},
        },
        "evidence_eligible": True,
        "lifecycle": {"path_complete": True, "home_verified": True},
        "evidence": {"metrics": {
            "complete_bins": 550,
            "path_duration_s": 60.0,
            "protocol_id": parameter["protocol_id"],
            "timing_gate_passed": True,
            "timing_evidence": {
                "acceptance_protocol_id": parameter["protocol_id"],
                "successful": True,
                "minimum_rate_hz": 400.0,
                "layer_rates_hz": {"writer_publishes": 500.0, "rtde_frames": 500.0,
                                   "kunwei_frames": 999.0, "tp_consumed_packet_echoes": 500.0},
            },
            "normal_force_mae_n": 1.0,
        }},
    }))
    _write_dispatch(campaign, attempt_count=1,
                    triplet=json.loads((campaign / "screening-schedule.json").read_text())[
                        "expected_controller_readback_triplet_sha256"])
    result = score_campaign(campaign)
    assert result["attempts"][0]["status"] == "failed"
    assert result["attempts"][0]["mae_n"] is None
    assert result["attempts"][0]["failure"].startswith("rate_evidence_mismatch:")


def test_r31_screening_score_rejects_changed_tp_triplet(tmp_path):
    from pathlib import Path

    config_path = Path(__file__).resolve().parents[1] / "config" / "tase_integral_screening_r31_rate460_v1.json"
    manifest_path = prepare_campaign(tmp_path / "r31", config_path)
    campaign = manifest_path.parent
    manifest = json.loads(manifest_path.read_text())
    parameter = json.loads((campaign / manifest["parameter_files"][0]).read_text())
    folder = campaign / "session-01" / "attempts" / "0001"
    folder.mkdir(parents=True)
    (folder / "seal.json").write_text("{}")
    (folder / "attempt-result.json").write_text(json.dumps({
        "physical_dispatched": True,
        "parameter_binding": parameter,
        "applied_runtime_parameters": {
            "Md_scalar": parameter["Md_scalar"],
            "Bd_scalar": parameter["Bd_scalar"],
            **{key: parameter["frozen"][key] for key in (
                "force_integral_limit_n_s", "force_integral_policy", "force_integral_authority_error_n"
            )},
        },
        "evidence_eligible": True,
        "lifecycle": {"path_complete": True, "home_verified": True},
        "evidence": {"metrics": {
            "complete_bins": 550,
            "path_duration_s": 60.0,
            "protocol_id": parameter["protocol_id"],
            "timing_gate_passed": True,
            "timing_evidence": {
                "acceptance_protocol_id": parameter["protocol_id"],
                "successful": True,
                "minimum_rate_hz": 460.0,
                "layer_rates_hz": {"writer_publishes": 480.0, "rtde_frames": 480.0,
                                   "kunwei_frames": 999.0, "tp_consumed_packet_echoes": 480.0},
            },
            "normal_force_mae_n": 1.0,
        }},
    }))
    _write_dispatch(campaign, attempt_count=1)
    result = score_campaign(campaign)
    assert result["attempts"][0]["status"] == "failed"
    assert result["attempts"][0]["failure"] == "controller_readback_triplet_missing_or_mismatched"


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
                "path_duration_s": 60.0,
                "protocol_id": parameter["protocol_id"],
                "timing_gate_passed": True,
                "timing_evidence": {
                    "acceptance_protocol_id": parameter["protocol_id"],
                    "successful": True,
                    "minimum_rate_hz": 400.0,
                    "layer_rates_hz": {"writer_publishes": 500.0, "rtde_frames": 500.0,
                                       "kunwei_frames": 999.0, "tp_consumed_packet_echoes": 500.0},
                },
                "normal_force_mae_n": 1.0,
            }},
        }))
    result = score_campaign(campaign)
    assert result["attempted"] == 25
    assert result["status"] == "awaiting_verified_close"
    assert result["selected_arm"] is None
    _write_dispatch(campaign, attempt_count=25, stopped=True)
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


def test_current_recovery_receipt_accepts_fresh_home_and_dashboard_stop(tmp_path):
    session = tmp_path / "session-01"
    attempt = session / "attempts" / "0001"
    attempt.mkdir(parents=True)
    (attempt / "attempt-result.json").write_text("{}\n")
    (session / "dispatch_receipt.json").write_text(json.dumps({
        "attempts": [{}],
        "stop": {
            "home_verified": True,
            "observed_stationary": True,
            "program_stopped": True,
            "safety_mode": 1,
            "dashboard_stop": {
                "program_stopped": True,
                "dashboard": {
                    "running": "Program running: false",
                    "programState": "STOPPED step5d_contact_six_qp_v1.urp",
                    "safetymode": "Safetymode: NORMAL",
                },
            },
        },
    }), encoding="utf-8")
    (session / "supervisor-result.json").write_text(json.dumps({
        "autonomous_home_recovery": {"success": True, "home_verified": True},
    }), encoding="utf-8")

    assert _session_verified_closed(session) is True


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
