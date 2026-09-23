import json
import threading
import time

from tase_autotuner import _candidate_for
from tase_resident_autotuner import (
    PROTOCOL, _next_generation, _reconcile_sealed_sessions, _score_item,
    _screening_initial, candidate_payload,
    load_config, run_campaign,
)


def _initial_row(config):
    return {
        "ordinal": 0, "candidate_id": "initial-00", "stage": "initial", "index": 0,
        "Md_scalar": config["incumbent"]["Md_scalar"],
        "Bd_scalar": config["incumbent"]["Bd_scalar"],
        "status": "complete", "mae_n": 1.1,
        "protocol_id": PROTOCOL, "duration_token": "r013_60_rate400",
        "source_candidate_id": "screen-B-00",
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
            "Md_scalar": candidate.Md_scalar, "Bd_scalar": candidate.Bd_scalar,
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
                "protocol_id": PROTOCOL,
                "motion_gate_passed": True,
                "timing_gate_passed": True,
                "timing_evidence": {"acceptance_protocol_id": PROTOCOL},
                "normal_force_mae_n": 1.25,
            },
        },
    }))


def test_rate400_candidate_keeps_approved_b_policy_and_absolute_search_box():
    config = load_config()
    row = _initial_row(config)
    candidate = _candidate_for(config, [row], 1)
    payload = candidate_payload(candidate, config)
    assert payload["protocol_id"] == PROTOCOL
    assert payload["frozen"]["force_integral_limit_n_s"] == 0.5
    assert payload["frozen"]["force_integral_policy"] == "legacy-clamp-v1"
    assert 12 / 2**0.5 <= candidate.Md_scalar <= 12 * 2**0.5
    assert 550 / 2**0.5 <= candidate.Bd_scalar <= 550 * 2**0.5


def test_sealed_result_recovers_an_unwritten_ledger_row(tmp_path):
    config = load_config()
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
    config = load_config()
    candidate = _candidate_for(config, [_initial_row(config)], 1)
    result_path = tmp_path / "attempt-result.json"
    _write_result(result_path, candidate, config, eligible=False)
    row = _score_item(result_path, candidate, 1, config)
    assert row["status"] == "failed"
    assert row["mae_n"] is None
    assert row["diagnostic_mae_n"] == 1.25
    assert row["safe_to_continue"] is False


def test_rate_floor_failure_keeps_diagnostic_and_allows_safe_budget_continuation(tmp_path):
    config = load_config()
    candidate = _candidate_for(config, [_initial_row(config)], 1)
    result_path = tmp_path / "attempt-result.json"
    _write_result(result_path, candidate, config, eligible=False)
    item = json.loads(result_path.read_text())
    metrics = item["evidence"]["metrics"]
    metrics["timing_gate_passed"] = False
    metrics["timing_evidence"] = {
        "acceptance_protocol_id": PROTOCOL,
        "duration_s": 60.0,
        "minimum_rate_hz": 400.0,
        "layer_rates_hz": {
            "writer_publishes": 369.4,
            "rtde_frames": 369.4,
            "kunwei_frames": 999.95,
            "tp_consumed_packet_echoes": 369.4,
        },
        "max_fresh_gap_s": .0083,
        "max_fresh_gap_limit_s": .02,
        "runtime_stale_stop_s": .08,
        "feedback_age_p99_s": .009,
        "feedback_age_p99_max_s": .01,
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
    config = load_config()
    config["screening_campaign"] = "screen"
    screen = tmp_path / "screen"
    session = screen / "session-02"
    own = session / "attempts" / "0001" / "attempt-result.json"
    other = session / "attempts" / "0011" / "attempt-result.json"
    own.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    for path, candidate_id in ((own, "screen-B-00"), (other, "screen-D-02")):
        path.write_text(json.dumps({
            "evidence_eligible": True,
            "parameter_binding": {
                "candidate_id": candidate_id,
                "protocol_id": PROTOCOL,
                **config["incumbent"],
            },
            "applied_runtime_parameters": {
                "force_integral_policy": "legacy-clamp-v1",
                "force_integral_limit_n_s": 0.5,
            },
            "evidence": {"metrics": {"complete_bins": 550,
                                      "normal_force_mae_n": 1.2}},
        }))
    screen.mkdir(exist_ok=True)
    (screen / "screening-results.json").write_text(json.dumps({
        "status": "complete", "sessions_closed": True,
        "protocol_id": PROTOCOL, "selected_arm": "B", "attempted": 25,
        "attempts": [{"candidate_id": "screen-B-00", "status": "complete",
                      "ordinal": 10, "run_dir": str(session), "mae_n": 1.2}],
    }))
    row = _screening_initial(config)
    assert row["source_attempt_result"] == str(own)


def test_tuner_proposes_next_candidate_after_prior_seal(monkeypatch, tmp_path):
    import tase_resident_autotuner as tuner

    monkeypatch.setattr(tuner, "TOTAL", 3)  # reused initial-00 plus two physical candidates
    config = load_config()
    monkeypatch.setattr(tuner, "_screening_initial", lambda _: _initial_row(config))
    events = []

    class FakeOwner:
        def __init__(self, command, **_kwargs):
            self.command = command
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
    result = run_campaign(tuner.DEFAULT_CONFIG, tmp_path / "campaign")
    assert result["attempted"] == 3
    assert result["status"] == "complete"
    assert [row["candidate_id"] for row in events] == ["initial-01", "initial-02"]


def test_tuner_checkpoint_closes_owner_after_one_new_attempt(monkeypatch, tmp_path):
    import tase_resident_autotuner as tuner

    monkeypatch.setattr(tuner, "TOTAL", 3)
    config = load_config()
    monkeypatch.setattr(tuner, "_screening_initial", lambda _: _initial_row(config))

    class FakeOwner:
        def __init__(self, command, **_kwargs):
            assert command[command.index("--resident-attempts") + 1] == "1"
            self.returncode = None
            self.thread = threading.Thread(target=self.run)
            self.thread.start()

        def run(self):
            session = tmp_path / "campaign" / "session-01"
            source = tmp_path / "campaign" / "session-01-candidates"
            candidate_file = source / "candidate-0001.json"
            until = time.monotonic() + 3
            while not candidate_file.is_file():
                assert time.monotonic() < until
                time.sleep(0.005)
            candidate = _candidate_for(config, [_initial_row(config)], 1)
            _write_result(session / "attempts" / "0001" / "attempt-result.json", candidate, config)
            (session / "dispatch_receipt.json").write_text(json.dumps({
                "attempts": [{"sequence": 1}],
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
    result = run_campaign(tuner.DEFAULT_CONFIG, tmp_path / "campaign", max_new_attempts=1)
    assert result["attempted"] == 2  # initial-00 is the already sealed screening unit
    assert result["status"] == "incomplete"
    assert result["resident_sessions_closed"] is True
    assert not (tmp_path / "campaign" / "session-01-candidates" / "candidate-0002.json").exists()


def test_tuner_dispatches_next_candidate_after_sealed_rate_only_failure(monkeypatch, tmp_path):
    import tase_resident_autotuner as tuner

    monkeypatch.setattr(tuner, "TOTAL", 3)
    config = load_config()
    monkeypatch.setattr(tuner, "_screening_initial", lambda _: _initial_row(config))
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
                        "acceptance_protocol_id": PROTOCOL,
                        "duration_s": 60.0,
                        "minimum_rate_hz": 400.0,
                        "layer_rates_hz": {
                            "writer_publishes": 369.4,
                            "rtde_frames": 369.4,
                            "kunwei_frames": 999.95,
                            "tp_consumed_packet_echoes": 369.4,
                        },
                        "max_fresh_gap_s": .0083,
                        "max_fresh_gap_limit_s": .02,
                        "runtime_stale_stop_s": .08,
                        "feedback_age_p99_s": .009,
                        "feedback_age_p99_max_s": .01,
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
    result = run_campaign(tuner.DEFAULT_CONFIG, tmp_path / "campaign")
    assert result["attempted"] == 3
    assert result["failed"] == 1
    assert result["complete"] == 2
    assert [row["candidate_id"] for row in events] == ["initial-01", "initial-02"]
