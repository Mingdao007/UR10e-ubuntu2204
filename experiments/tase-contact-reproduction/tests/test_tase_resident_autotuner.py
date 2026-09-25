import json
import threading
import time

from tase_autotuner import _candidate_for
from tase_resident_autotuner import (
    DEFAULT_CONFIG,
    LEGACY_RATE400_PROTOCOL,
    PROTOCOL,
    _baseline_initial,
    _next_generation,
    _reconcile_sealed_sessions,
    _score_item,
    _screening_initial,
    candidate_payload,
    load_config,
    run_campaign,
)

ROOT = DEFAULT_CONFIG.parents[1]
LEGACY_CONFIG = ROOT / "config/tase_resident_autotuner_rate400_b_v1.json"
RATE460_E_CONFIG = ROOT / "config/tase_resident_autotuner_rate460_e_v1.json"
DUMMY_TRIPLET = {"script": "a" * 64, "txt": "b" * 64, "urp": "c" * 64}


def _legacy_config():
    return load_config(LEGACY_CONFIG)


def _initial_row(config):
    legacy = config["protocol_id"] == LEGACY_RATE400_PROTOCOL
    return {
        "ordinal": 0,
        "candidate_id": "initial-00",
        "stage": "initial",
        "index": 0,
        "Md_scalar": config["incumbent"]["Md_scalar"],
        "Bd_scalar": config["incumbent"]["Bd_scalar"],
        "status": "complete",
        "mae_n": 1.1,
        "protocol_id": config["protocol_id"],
        "duration_token": config["duration_token"],
        "source_candidate_id": "screen-B-00" if legacy else "a-baseline-fixture",
    }


def _write_result(path, candidate, config, *, eligible=True):
    payload = candidate_payload(candidate, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_name("seal.json").write_text("{}")
    path.write_text(json.dumps({
        "physical_dispatched": True,
        "parameter_binding": {
            **payload,
            "force_integral_policy": payload["frozen"]["force_integral_policy"],
            "force_integral_limit_n_s": payload["frozen"]["force_integral_limit_n_s"],
        },
        "applied_runtime_parameters": {
            "Md_scalar": candidate.Md_scalar,
            "Bd_scalar": candidate.Bd_scalar,
            "force_integral_policy": config["force_integral_policy"],
            "force_integral_limit_n_s": config["force_integral_limit_n_s"],
            "force_integral_authority_error_n": config["force_integral_authority_error_n"],
        },
        "evidence_eligible": eligible,
        "lifecycle": {"path_complete": True, "home_verified": True,
                      "ready_for_next": True, "sealed": True},
        "evidence": {
            "safety_gate_passed": True,
            "contact_gate_passed": True,
            "return_gate_passed": True,
            "metrics": {
                "complete_bins": 550,
                "path_duration_s": 60.0,
                "protocol_id": config["protocol_id"],
                "motion_gate_passed": True,
                "timing_gate_passed": True,
                "timing_evidence": {
                    "acceptance_protocol_id": config["protocol_id"],
                    "minimum_rate_hz": config["minimum_data_rate_hz"],
                    "layer_rates_hz": {
                        "writer_publishes": 480.0,
                        "rtde_frames": 480.0,
                        "kunwei_frames": 999.0,
                        "tp_consumed_packet_echoes": 480.0,
                    },
                },
                "normal_force_mae_n": 1.25,
            },
        },
    }))


def _write_rate460_baseline(tmp_path, *, minimum_echo_hz=480.0):
    config = load_config()
    attempt_path = tmp_path / "baseline-session" / "attempts" / "0001" / "attempt-result.json"
    attempt_path.parent.mkdir(parents=True)
    session = attempt_path.parent.parent.parent
    rates = {
        "writer_publishes": 480.0,
        "rtde_frames": 480.0,
        "kunwei_frames": 999.0,
        "tp_consumed_packet_echoes": minimum_echo_hz,
    }
    binding = {
        "candidate_id": "figure8-rate460-integral-1p0-A-baseline",
        "protocol_id": PROTOCOL,
        "duration_token": "r013_60_rate460",
        "Md_scalar": config["incumbent"]["Md_scalar"],
        "Bd_scalar": config["incumbent"]["Bd_scalar"],
        "force_integral_policy": "legacy-clamp-v1",
        "force_integral_limit_n_s": 1.0,
        "force_integral_authority_error_n": 0.5,
        "frozen": {"force_integral_policy": "legacy-clamp-v1", "force_integral_limit_n_s": 1.0},
    }
    lifecycle = {
        "path_complete": True,
        "home_verified": True,
        "ready_for_next": True,
        "program_stopped": True,
        "sealed": True,
    }
    metrics = {
        "protocol_id": PROTOCOL,
        "path_duration_s": 60.0,
        "formal_window_s": [5.0, 60.0],
        "complete_bins": 550,
        "complete": True,
        "timing_gate_passed": True,
        "normal_force_mae_n": 1.2,
        "timing_evidence": {
            "acceptance_protocol_id": PROTOCOL,
            "minimum_rate_hz": 460.0,
            "duration_s": 60.0,
            "layer_rates_hz": rates,
        },
    }
    attempt_path.write_text(json.dumps({
        "physical_dispatched": True,
        "evidence_eligible": True,
        "parameter_binding": binding,
        "lifecycle": lifecycle,
        "evidence": {"metrics": metrics},
    }))
    attempt_path.with_name("seal.json").write_text(json.dumps({
        "schema": "yield-live-entry/attempt-seal-v1",
        "lifecycle": lifecycle,
    }))
    (session / "dispatch_receipt.json").write_text(json.dumps({
        "method": "TASE_RNN_MATURE",
        "executed": True,
        "controller_readback_triplet": DUMMY_TRIPLET,
    }))
    return attempt_path


def test_default_tuner_profile_is_rate460_a_and_keeps_absolute_search_box():
    config = load_config()
    assert config["protocol_id"] == PROTOCOL
    assert config["duration_token"] == "r013_60_rate460"
    assert config["selected_screening_arm"] == "A"
    assert config["force_integral_limit_n_s"] == 1.0
    assert config["minimum_data_rate_hz"] == 460.0
    candidate = _candidate_for(config, [_initial_row(config)], 1)
    payload = candidate_payload(candidate, config)
    assert payload["protocol_id"] == PROTOCOL
    assert payload["duration_token"] == "r013_60_rate460"
    assert payload["frozen"]["force_integral_limit_n_s"] == 1.0
    assert 12 / 2**0.5 <= candidate.Md_scalar <= 12 * 2**0.5
    assert 550 / 2**0.5 <= candidate.Bd_scalar <= 550 * 2**0.5


def test_rate460_e_tuner_reuses_the_selected_integral_off_screening_unit(monkeypatch, tmp_path):
    import tase_resident_autotuner as tuner

    config = load_config(RATE460_E_CONFIG)
    assert config["selected_screening_arm"] == "E"
    assert config["force_integral_policy"] == "integral-off-v1"
    assert config["force_integral_limit_n_s"] == 1.0
    candidate_id = "screen-E-00"
    screen = tmp_path / "screen"
    session = screen / "session-01"
    attempt = session / "attempts" / "0002" / "attempt-result.json"
    attempt.parent.mkdir(parents=True)
    mae = 1.7976558912800982
    triplet = {"script": "a" * 64, "txt": "b" * 64, "urp": "c" * 64}
    parameter = candidate_payload(
        _candidate_for(config, [], 0), config,
    )
    parameter.update({"candidate_id": candidate_id, "stage": "integral_screening", "index": 1})
    parameter["frozen"]["force_integral_policy"] = "integral-off-v1"
    parameter["frozen"]["force_integral_limit_n_s"] = 1.0
    timing = {
        "acceptance_protocol_id": PROTOCOL,
        "successful": True,
        "minimum_rate_hz": 460.0,
        "layer_rates_hz": {
            "writer_publishes": 481.1,
            "rtde_frames": 481.1,
            "kunwei_frames": 999.9,
            "tp_consumed_packet_echoes": 481.1,
        },
    }
    attempt.write_text(json.dumps({
        "physical_dispatched": True,
        "evidence_eligible": True,
        "parameter_binding": {
            **parameter,
            "force_integral_policy": "integral-off-v1",
            "force_integral_limit_n_s": 1.0,
        },
        "applied_runtime_parameters": {
            "Md_scalar": config["incumbent"]["Md_scalar"],
            "Bd_scalar": config["incumbent"]["Bd_scalar"],
            "force_integral_policy": "integral-off-v1",
            "force_integral_limit_n_s": 1.0,
            "force_integral_authority_error_n": 0.5,
        },
        "lifecycle": {"path_complete": True, "home_verified": True, "ready_for_next": True},
        "evidence": {"metrics": {
            "path_duration_s": 60.00000000001455,
            "path_duration_rounding_bound_s": 2.9118042178810644e-11,
            "path_coverage_interval_s": 0.0020000000076834112,
            "complete_bins": 550,
            "protocol_id": PROTOCOL,
            "timing_gate_passed": True,
            "timing_evidence": timing,
            "normal_force_mae_n": mae,
        }},
    }))
    attempt.with_name("seal.json").write_text("{}\n")
    (session / "dispatch_receipt.json").write_text(json.dumps({
        "method": "TASE_RNN_MATURE",
        "controller_identity": {"method": "TASE_RNN_MATURE"},
        "live_path": {"protocol_id": PROTOCOL, "path_duration_s": 60.0},
        "controller_readback_triplet": triplet,
    }))
    (screen / "screening-results.json").write_text(json.dumps({
        "status": "complete",
        "sessions_closed": True,
        "protocol_id": PROTOCOL,
        "selected_arm": "E",
        "attempted": 25,
        "expected_controller_readback_triplet_sha256": triplet,
        "attempts": [{
            "candidate_id": candidate_id,
            "status": "complete",
            "run_dir": str(session),
            "mae_n": mae,
            "controller_readback_triplet_sha256": triplet,
        }],
    }))
    config["screening_campaign"] = "screen"
    config["controller_readback_triplet"] = triplet
    monkeypatch.setattr(tuner, "ROOT", tmp_path)
    row = tuner._initial_row(config)
    assert row["source_candidate_id"] == candidate_id
    assert row["mae_n"] == mae
    assert row["force_integral_policy"] == "integral-off-v1"
    assert row["controller_readback_triplet"] == triplet


def test_rate460_e_candidate_scoring_preserves_integral_off_identity(tmp_path):
    config = load_config(RATE460_E_CONFIG)
    config["controller_readback_triplet"] = DUMMY_TRIPLET
    records = [_initial_row(config)]
    candidate = _candidate_for(config, records, 1)
    result_path = tmp_path / "session-01" / "attempts" / "0001" / "attempt-result.json"
    _write_result(result_path, candidate, config)
    (result_path.parent.parent.parent / "dispatch_receipt.json").write_text(json.dumps({
        "method": "TASE_RNN_MATURE",
        "controller_identity": {"method": "TASE_RNN_MATURE"},
        "live_path": {"protocol_id": PROTOCOL, "path_duration_s": 60.0},
        "controller_readback_triplet": DUMMY_TRIPLET,
    }))
    row = _score_item(result_path, candidate, 1, config)
    assert row["status"] == "complete"
    assert candidate_payload(candidate, config)["frozen"]["force_integral_policy"] == "integral-off-v1"
    assert row["controller_readback_triplet"] == DUMMY_TRIPLET


def test_resident_candidate_uses_fresh_readback_before_session_dispatch_is_written(tmp_path):
    import hashlib

    config = load_config(RATE460_E_CONFIG)
    records = [_initial_row(config)]
    candidate = _candidate_for(config, records, 1)
    result_path = tmp_path / "session-01" / "attempts" / "0001" / "attempt-result.json"
    _write_result(result_path, candidate, config)
    session = result_path.parents[2]
    program = "step5d_contact_six_qp_v1"
    readback_dir = session / "readback" / program
    readback_dir.mkdir(parents=True)
    triplet = {}
    for extension in ("script", "txt", "urp"):
        payload = f"verified-{program}-{extension}".encode()
        (readback_dir / f"{program}.{extension}").write_bytes(payload)
        triplet[extension] = hashlib.sha256(payload).hexdigest()
    config["controller_readback_triplet"] = triplet
    (session / "controller_receipt.json").write_text(json.dumps({
        "program": program,
        "controller_target": f"/programs/andyl/kunwei/step5/{program}.urp",
        "script_sha256": triplet["script"],
        "txt_sha256": triplet["txt"],
        "urp_sha256": triplet["urp"],
        "safety_mode": "NORMAL",
        "provenance": {"triplet": "readback-results.json"},
    }))
    (session / "readback-results.json").write_text(json.dumps({
        "pass": True,
        "scope": "fresh GET of installed packages",
        "basenames": [program, "step5d_contact_home_v1"],
    }))

    assert not (session / "dispatch_receipt.json").exists()
    row = _score_item(result_path, candidate, 1, config)
    assert row["status"] == "complete"
    assert row["safe_to_continue"] is True
    assert row["controller_readback_triplet"] == triplet


def test_closed_session_reconciles_package_identity_scoring_race(tmp_path):
    import tase_resident_autotuner as tuner

    config = load_config(RATE460_E_CONFIG)
    config["controller_readback_triplet"] = DUMMY_TRIPLET
    campaign = tmp_path / "campaign"
    session = campaign / "session-01"
    attempt = session / "attempts" / "0001" / "attempt-result.json"
    rows = [_initial_row(config)]
    candidate = _candidate_for(config, rows, 1)
    _write_result(attempt, candidate, config)

    # This is the score produced while the resident supervisor was still open
    # and had not written its final dispatch receipt.
    provisional = _score_item(attempt, candidate, 1, config)
    assert provisional["status"] == "failed"
    assert provisional["failure"] == "evidence_ineligible"
    rows.append(provisional)
    ledger = campaign / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))

    # The session closes and supplies its immutable package triplet and Home/STOP proof.
    (session / "dispatch_receipt.json").write_text(json.dumps({
        "method": "TASE_RNN_MATURE",
        "controller_identity": {"method": "TASE_RNN_MATURE"},
        "live_path": {"protocol_id": PROTOCOL, "path_duration_s": 60.0},
        "controller_readback_triplet": DUMMY_TRIPLET,
        "attempts": [{"sequence": 1}],
        "stop": {"home_verified": True, "program_stopped": True},
    }))

    reconciled = _reconcile_sealed_sessions(campaign, config, ledger, rows)
    assert reconciled[1]["status"] == "complete"
    assert reconciled[1]["mae_n"] == 1.25
    assert reconciled[1]["score_correction"]["previous_status"] == "failed"
    correction = json.loads((campaign / "ledger-corrections.jsonl").read_text().splitlines()[0])
    assert correction["previous"]["status"] == "failed"
    assert correction["replacement"]["status"] == "complete"
    effective_rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert effective_rows[1]["status"] == "complete"


def test_legacy_rate400_b_remains_explicit_and_separate():
    config = _legacy_config()
    assert config["protocol_id"] == LEGACY_RATE400_PROTOCOL
    assert config["duration_token"] == "r013_60_rate400"
    assert config["selected_screening_arm"] == "B"
    assert config["force_integral_limit_n_s"] == 0.5


def test_rate460_initial00_requires_sealed_complete_baseline_and_readback_identity(tmp_path):
    attempt_path = _write_rate460_baseline(tmp_path)
    config = load_config(initial_attempt_result=attempt_path)
    row = _baseline_initial(config)
    assert row["candidate_id"] == "initial-00"
    assert row["source_attempt_result"] == str(attempt_path.resolve())
    assert row["reused_first_live_A_baseline"] is True
    assert row["controller_readback_triplet"] == DUMMY_TRIPLET
    assert row["force_integral_limit_n_s"] == 1.0


def test_rate460_initial00_rejects_sub460_layer_rate(tmp_path):
    attempt_path = _write_rate460_baseline(tmp_path, minimum_echo_hz=459.9)
    config = load_config(initial_attempt_result=attempt_path)
    try:
        _baseline_initial(config)
    except ValueError as exc:
        assert "timing" in str(exc)
    else:
        raise AssertionError("sub-460 Hz baseline was admitted")


def test_sealed_result_recovers_an_unwritten_ledger_row(tmp_path):
    config = _legacy_config()
    rows = [_initial_row(config)]
    candidate = _candidate_for(config, rows, 1)
    result_path = tmp_path / "session-01" / "attempts" / "0001" / "attempt-result.json"
    _write_result(result_path, candidate, config)
    ledger = tmp_path / "ledger.jsonl"
    recovered = _reconcile_sealed_sessions(tmp_path, config, ledger, rows)
    assert len(recovered) == 2
    assert recovered[-1]["candidate_id"] == "initial-01"
    assert recovered[-1]["status"] == "complete"
    assert recovered[-1]["mae_n"] == 1.25
    assert len(ledger.read_text().splitlines()) == 1


def test_ineligible_full_path_keeps_diagnostic_out_of_bo_and_pauses_unknown_failure(tmp_path):
    config = _legacy_config()
    candidate = _candidate_for(config, [_initial_row(config)], 1)
    result_path = tmp_path / "attempt-result.json"
    _write_result(result_path, candidate, config, eligible=False)
    row = _score_item(result_path, candidate, 1, config)
    assert row["status"] == "failed"
    assert row["mae_n"] is None
    assert row["diagnostic_mae_n"] == 1.25
    assert row["safe_to_continue"] is False


def test_rate_floor_failure_keeps_diagnostic_and_allows_safe_budget_continuation(tmp_path):
    config = _legacy_config()
    candidate = _candidate_for(config, [_initial_row(config)], 1)
    result_path = tmp_path / "attempt-result.json"
    _write_result(result_path, candidate, config, eligible=False)
    item = json.loads(result_path.read_text())
    metrics = item["evidence"]["metrics"]
    metrics["timing_gate_passed"] = False
    metrics["timing_evidence"] = {
        "acceptance_protocol_id": LEGACY_RATE400_PROTOCOL,
        "minimum_rate_hz": 400.0,
        "duration_s": 60.0,
        "layer_rates_hz": {
            "writer_publishes": 369.4,
            "rtde_frames": 369.4,
            "kunwei_frames": 999.95,
            "tp_consumed_packet_echoes": 369.4,
        },
        "max_fresh_gap_s": 0.0083,
        "max_fresh_gap_limit_s": 0.02,
        "runtime_stale_stop_s": 0.08,
        "feedback_age_p99_s": 0.009,
        "feedback_age_p99_max_s": 0.01,
    }
    result_path.write_text(json.dumps(item))
    row = _score_item(result_path, candidate, 1, config)
    assert row["status"] == "failed"
    assert row["mae_n"] is None
    assert row["diagnostic_mae_n"] == 1.25
    assert row["safe_to_continue"] is True


def test_failed_launch_candidate_directory_is_not_reused(tmp_path):
    (tmp_path / "session-01-candidates").mkdir()
    assert _next_generation(tmp_path) == 2


def test_screening_reuse_resolves_candidate_not_global_ordinal(monkeypatch, tmp_path):
    import tase_resident_autotuner as tuner
    monkeypatch.setattr(tuner, "ROOT", tmp_path)
    config = _legacy_config()
    config["screening_campaign"] = "screen"
    screen = tmp_path / "screen"
    session = screen / "session-02"
    own = session / "attempts" / "0001" / "attempt-result.json"
    other = session / "attempts" / "0011" / "attempt-result.json"
    own.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    for path, candidate_id in ((own, "screen-B-00"), (other, "screen-D-02")):
        path.write_text(json.dumps({
            "physical_dispatched": True,
            "evidence_eligible": True,
            "lifecycle": {"path_complete": True, "home_verified": True},
            "parameter_binding": {
                "candidate_id": candidate_id,
                "protocol_id": LEGACY_RATE400_PROTOCOL,
                **config["incumbent"],
                "force_integral_policy": "legacy-clamp-v1",
                "force_integral_limit_n_s": 0.5,
            },
            "applied_runtime_parameters": {
                "force_integral_policy": "legacy-clamp-v1",
                "force_integral_limit_n_s": 0.5,
                "force_integral_authority_error_n": 0.5,
            },
            "evidence": {"metrics": {
                "path_duration_s": 60.0,
                "complete_bins": 550,
                "protocol_id": LEGACY_RATE400_PROTOCOL,
                "timing_gate_passed": True,
                "timing_evidence": {
                    "acceptance_protocol_id": LEGACY_RATE400_PROTOCOL,
                    "successful": True,
                    "minimum_rate_hz": 400.0,
                    "layer_rates_hz": {
                        "writer_publishes": 480.0,
                        "rtde_frames": 480.0,
                        "kunwei_frames": 999.0,
                        "tp_consumed_packet_echoes": 480.0,
                    },
                },
                "normal_force_mae_n": 1.2,
            }},
        }))
    own.with_name("seal.json").write_text("{}\n")
    (session / "dispatch_receipt.json").write_text(json.dumps({
        "method": "TASE_RNN_MATURE",
        "controller_identity": {"method": "TASE_RNN_MATURE"},
        "live_path": {"protocol_id": LEGACY_RATE400_PROTOCOL, "path_duration_s": 60.0},
    }))
    screen.mkdir(exist_ok=True)
    (screen / "screening-results.json").write_text(json.dumps({
        "status": "complete", "sessions_closed": True,
        "protocol_id": LEGACY_RATE400_PROTOCOL, "selected_arm": "B", "attempted": 25,
        "attempts": [{"candidate_id": "screen-B-00", "status": "complete",
                      "ordinal": 10, "run_dir": str(session), "mae_n": 1.2}],
    }))
    row = _screening_initial(config)
    assert row["source_attempt_result"] == str(own.resolve())


def test_tuner_proposes_next_candidate_after_prior_seal(monkeypatch, tmp_path):
    import tase_resident_autotuner as tuner
    monkeypatch.setattr(tuner, "TOTAL", 3)
    config = _legacy_config()
    monkeypatch.setattr(tuner, "_initial_row", lambda _: _initial_row(config))
    events = []

    class FakeOwner:
        def __init__(self, command, **_kwargs):
            self.returncode = None
            self.thread = threading.Thread(target=self.run)
            self.thread.start()

        def run(self):
            session = tmp_path / "campaign" / "session-01"
            source = tmp_path / "campaign" / "session-01-candidates"
            for sequence in (1, 2):
                candidate_file = source / f"candidate-{sequence:04d}.json"
                until = time.monotonic() + 3
                while not candidate_file.is_file():
                    assert time.monotonic() < until
                    time.sleep(0.005)
                payload = json.loads(candidate_file.read_text())
                candidate = _candidate_for(config, [_initial_row(config)] + events, sequence)
                assert payload["candidate_id"] == candidate.candidate_id
                result_path = session / "attempts" / f"{sequence:04d}" / "attempt-result.json"
                _write_result(result_path, candidate, config)
                events.append({"candidate_id": candidate.candidate_id,
                               "Md_scalar": candidate.Md_scalar,
                               "Bd_scalar": candidate.Bd_scalar,
                               "status": "complete", "mae_n": 1.25,
                               "ordinal": sequence, "stage": candidate.stage,
                               "index": candidate.index})
            (session / "dispatch_receipt.json").write_text(json.dumps({
                "attempts": [{"sequence": 1}, {"sequence": 2}],
                "stop": {"home_verified": True, "program_stopped": True},
            }))
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self):
            self.thread.join(timeout=4)
            assert not self.thread.is_alive()
            return self.returncode

    monkeypatch.setattr(tuner.subprocess, "Popen", FakeOwner)
    result = run_campaign(LEGACY_CONFIG, tmp_path / "campaign")
    assert result["attempted"] == 3
    assert result["status"] == "complete"
    assert [row["candidate_id"] for row in events] == ["initial-01", "initial-02"]


def test_tuner_dispatches_next_candidate_after_sealed_rate_only_failure(monkeypatch, tmp_path):
    import tase_resident_autotuner as tuner
    monkeypatch.setattr(tuner, "TOTAL", 3)
    config = _legacy_config()
    monkeypatch.setattr(tuner, "_initial_row", lambda _: _initial_row(config))
    events = []

    class FakeOwner:
        def __init__(self, command, **_kwargs):
            self.returncode = None
            self.thread = threading.Thread(target=self.run)
            self.thread.start()

        def run(self):
            session = tmp_path / "campaign" / "session-01"
            source = tmp_path / "campaign" / "session-01-candidates"
            for sequence in (1, 2):
                candidate_file = source / f"candidate-{sequence:04d}.json"
                until = time.monotonic() + 3
                while not candidate_file.is_file():
                    assert time.monotonic() < until
                    time.sleep(0.005)
                payload = json.loads(candidate_file.read_text())
                candidate = _candidate_for(config, [_initial_row(config)] + events, sequence)
                assert payload["candidate_id"] == candidate.candidate_id
                result_path = session / "attempts" / f"{sequence:04d}" / "attempt-result.json"
                _write_result(result_path, candidate, config, eligible=sequence != 1)
                if sequence == 1:
                    item = json.loads(result_path.read_text())
                    metrics = item["evidence"]["metrics"]
                    metrics["timing_gate_passed"] = False
                    metrics["timing_evidence"] = {
                        "acceptance_protocol_id": LEGACY_RATE400_PROTOCOL,
                        "minimum_rate_hz": 400.0,
                        "duration_s": 60.0,
                        "layer_rates_hz": {
                            "writer_publishes": 369.4,
                            "rtde_frames": 369.4,
                            "kunwei_frames": 999.95,
                            "tp_consumed_packet_echoes": 369.4,
                        },
                        "max_fresh_gap_s": 0.0083,
                        "max_fresh_gap_limit_s": 0.02,
                        "runtime_stale_stop_s": 0.08,
                        "feedback_age_p99_s": 0.009,
                        "feedback_age_p99_max_s": 0.01,
                    }
                    result_path.write_text(json.dumps(item))
                events.append({
                    "candidate_id": candidate.candidate_id,
                    "Md_scalar": candidate.Md_scalar,
                    "Bd_scalar": candidate.Bd_scalar,
                    "status": "failed" if sequence == 1 else "complete",
                    "mae_n": None if sequence == 1 else 1.2,
                    "ordinal": sequence,
                    "stage": candidate.stage,
                    "index": candidate.index,
                })
            (session / "dispatch_receipt.json").write_text(json.dumps({
                "attempts": [{"sequence": 1}, {"sequence": 2}],
                "stop": {"home_verified": True, "program_stopped": True},
            }))
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self):
            self.thread.join(timeout=4)
            assert not self.thread.is_alive()
            return self.returncode

    monkeypatch.setattr(tuner.subprocess, "Popen", FakeOwner)
    result = run_campaign(LEGACY_CONFIG, tmp_path / "campaign")
    assert result["attempted"] == 3
    assert result["failed"] == 1
    assert result["complete"] == 2
    assert [row["candidate_id"] for row in events] == ["initial-01", "initial-02"]
