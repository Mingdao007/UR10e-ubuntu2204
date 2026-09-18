from pathlib import Path
import csv
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from analyze_contact_static_capture import analyze  # noqa: E402
from contact_benchmark_freshness import replay_freshness_sensitivity  # noqa: E402
from contact_benchmark_protocol import (  # noqa: E402
    FRESH_AGE_S,
    STALE_AGE_S,
    SensorFreshnessTracker,
    classify_sensor_age,
    protocol,
)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (0.0, "fresh"),
        (0.019999999, "fresh"),
        (0.020, "held"),
        (0.079999999, "held"),
        (0.080, "stale"),
    ],
)
def test_sensor_age_boundaries_are_explicit(age, expected):
    assert classify_sensor_age(age) == expected


def test_future_or_nonfinite_age_fails_closed():
    for age in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite and non-negative"):
            classify_sensor_age(age)


def test_tracker_reports_held_and_stop_evidence():
    tracker = SensorFreshnessTracker()
    tracker.observe(0.001)
    tracker.observe(0.020)
    tracker.observe(0.050)
    tracker.stale_stop(0.080)
    tracker.geometric_latency_reject()
    summary = tracker.as_dict()
    assert summary["fresh_count"] == 1
    assert summary["held_count"] == 2
    assert summary["stale_count"] == 1
    assert summary["held_fraction"] == pytest.approx(.5)
    assert summary["stale_stop_count"] == 1
    assert summary["geometric_latency_reject_count"] == 1
    assert summary["age_max_s"] == pytest.approx(.08)


def test_protocol_serializes_contact_freshness_policy():
    policy = protocol()
    assert policy["freshness"]["fresh_age_s"] == FRESH_AGE_S
    assert policy["freshness"]["stale_age_s"] == STALE_AGE_S
    assert policy["freshness"]["bands"]["held"] == "0.020 <= age < 0.080"
    assert policy["freshness_sensitivity_cutoffs_s"] == [.02, .04, .06, .08]


def test_sensitivity_replay_censors_rows_and_only_ranks_complete_controllers():
    rows = [
        {"controller": "LAC", "observation_age_s": .001, "force_error_n": 1.0,
         "path_error_m": .01, "vibration_metric": .1},
        {"controller": "LAC", "observation_age_s": .050, "force_error_n": 2.0,
         "path_error_m": .02, "vibration_metric": .2},
        {"controller": "MSFC", "observation_age_s": .001, "force_error_n": .5,
         "path_error_m": .01, "vibration_metric": .05},
        {"controller": "MSFC", "observation_age_s": .010, "force_error_n": .6,
         "path_error_m": .01, "vibration_metric": .06},
    ]
    result = replay_freshness_sensitivity(rows)
    assert result["cutoffs_s"] == [.02, .04, .06, .08]
    at_40 = result["comparisons"][1]
    lac = next(item for item in at_40["controllers"] if item["controller"] == "LAC")
    msfc = next(item for item in at_40["controllers"] if item["controller"] == "MSFC")
    assert lac["censored"] is True
    assert lac["objective_eligible"] is False
    assert msfc["objective_eligible"] is True
    assert at_40["ranking_is_complete"] is False


def test_static_capture_reports_delivery_gap_bands_without_claiming_sensor_age(tmp_path):
    capture = tmp_path / "capture"
    capture.mkdir()
    times = [0.0, .010, .031, .032, .1121]
    with (capture / "data.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["t_monotonic_s", "Fx_N", "Fy_N", "Fz_N", "Mx_Nm", "My_Nm", "Mz_Nm"])
        writer.writeheader()
        for index, time_s in enumerate(times):
            writer.writerow({"t_monotonic_s": time_s, "Fx_N": 0, "Fy_N": 0, "Fz_N": 0,
                             "Mx_Nm": 0, "My_Nm": 0, "Mz_Nm": 0})
    (capture / "raw_frames.bin").write_bytes(b"raw")
    (capture / "metadata.json").write_text(json.dumps({"args": {"tcp_quickack": False}}))
    (capture / "summary.json").write_text(json.dumps({"parse_errors": 0, "dropped_sync_bytes": 0}))
    result = analyze(capture)
    assert result["observation_age_source"] == "host_delivery_gap_only"
    assert result["delivery_gap_bands"]["held"]["count"] == 1
    assert result["delivery_gap_bands"]["stale"]["count"] == 1
    assert result["delivery_gap_cutoff_bands"]["20_to_40ms"]["count"] == 1
    assert result["delivery_gap_cutoff_bands"]["ge_80ms"]["count"] == 1
    assert result["stale_stop_count"] == 1
    assert result["fresh_gap_diagnostic_pass"] is False
    assert "sensor-internal" in result["claim_scope"]
