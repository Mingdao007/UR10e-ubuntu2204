import hashlib
import json
import pytest

from analyze_tase_resident_evidence import reduce_attempt, seal_session_diagnostic


def test_sealed_attempt_reduction_separates_return_and_force_windows(tmp_path):
    attempt = tmp_path / "campaign/session-01/attempts/0001"
    attempt.mkdir(parents=True)
    segments = {}
    data = {
        "robot_frames": [
            {"received_monotonic_s": 1.0, "integer_echoes": {"26": 78}},
            {"received_monotonic_s": 2.0, "integer_echoes": {"26": 20}},
            {"received_monotonic_s": 6.0, "integer_echoes": {"26": 21}},
            {"received_monotonic_s": 14.0, "integer_echoes": {"26": 25}},
            {"received_monotonic_s": 75.0, "integer_echoes": {"26": 40}},
            {"received_monotonic_s": 78.6, "integer_echoes": {"26": 78}},
        ],
        "raw_sensor": [
            {"host_use_monotonic_s": 15.0, "corrected_wrench_n_nm": [0, 0, -4, 0, 0, 0]},
            {"host_use_monotonic_s": 17.0, "corrected_wrench_n_nm": [0, 0, -6, 0, 0, 0]},
            {"host_use_monotonic_s": 21.0, "corrected_wrench_n_nm": [0, 0, -5, 0, 0, 0]},
            {"host_use_monotonic_s": 22.0, "corrected_wrench_n_nm": [0, 0, -7, 0, 0, 0]},
        ],
        "published_packets": [[15.0, {}], [15.002, {}], [15.006, {}]],
    }
    for name, rows in data.items():
        path = attempt / f"{name}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        content = path.read_bytes()
        segments[name] = {"path": str(path), "count": len(rows),
                          "sha256": hashlib.sha256(content).hexdigest()}
    seal = {"segments": segments, "lifecycle": {"sealed": True}}
    (attempt / "seal.json").write_text(json.dumps(seal))
    item = {
        "sequence": 1,
        "physical_dispatched": True,
        "parameter_binding": {"candidate_id": "one",
                              "protocol_id": "figure8_window60_r013_rate400_v1"},
        "timing": {"started_monotonic_s": 1.0,
                   "path_started_monotonic_s": 15.0,
                   "home_verified_monotonic_s": 82.0},
        "lifecycle": {"path_complete": True, "home_verified": True, "sealed": True},
        "evidence_eligible": True,
        "evidence": {"metrics": {"complete_bins": 550, "normal_force_mae_n": .25,
                                 "timing_evidence": {"layer_rates_hz": {
                                     "rtde_frames": 430., "tp_consumed_packet_echoes": 430.,
                                 }}}},
        "timing_attribution": {"host_path_publish_rate_hz": 480.},
        "sealed_evidence": seal,
    }
    (attempt / "attempt-result.json").write_text(json.dumps(item))
    events = [
        {"stage": "HOME_SETTLE", "event": "verified", "sequence": 1,
         "monotonic_s": 79.1},
        {"stage": "SEAL", "event": "complete", "sequence": 1,
         "monotonic_s": 84.0},
    ]
    row = reduce_attempt(attempt, events=events)
    assert row["timing"]["physical_return_s"] == pytest.approx(3.6)
    assert row["timing"]["ready_home_to_receipt_s"] == pytest.approx(3.4)
    assert row["timing"]["stationary_home_verified_s"] == 79.1
    assert row["formal_bin_mean_mae_n"] == .25
    assert row["force"]["window_0_5_s"]["sample_mae_n"] == 1.0
    assert row["force"]["window_5_60_s"]["sample_mae_n"] == 1.0
    assert row["cadence"]["classification"] == "rtde_below_460"
    bundle = seal_session_diagnostic(attempt.parent.parent, events=events)
    assert bundle["attempt_count"] == 1
    assert bundle["protocol_id"] == "figure8_window60_r013_rate400_v1"
    sidecar = json.loads((attempt.parent.parent / "resident-evidence-diagnostic.json").read_text())
    assert sidecar["attempts"][0]["timing"]["physical_return_s"] == pytest.approx(3.6)
