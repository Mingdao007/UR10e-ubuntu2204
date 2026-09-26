import hashlib
import json
import pytest

from analyze_tase_resident_evidence import (
    _controller_path_force_diagnostics,
    _controller_trace_diagnostics,
    reduce_attempt,
    seal_session_diagnostic,
)


def test_sealed_attempt_reduction_separates_return_and_force_windows(tmp_path):
    attempt = tmp_path / "campaign/session-01/attempts/0001"
    attempt.mkdir(parents=True)
    segments = {}
    data = {
        "robot_frames": [
            {"received_monotonic_s": 1.0, "timestamp": 1.0,
             "consumed_packet_sequence": -1, "integer_echoes": {"26": 78}},
            {"received_monotonic_s": 2.0, "timestamp": 2.0,
             "consumed_packet_sequence": -1, "integer_echoes": {"26": 20}},
            {"received_monotonic_s": 6.0, "timestamp": 6.0,
             "consumed_packet_sequence": -1, "integer_echoes": {"26": 21}},
            {"received_monotonic_s": 14.0, "timestamp": 14.0,
             "consumed_packet_sequence": 100, "integer_echoes": {"26": 25}},
            {"received_monotonic_s": 16.0, "timestamp": 16.0,
             "consumed_packet_sequence": 100, "integer_echoes": {"26": 25}},
            {"received_monotonic_s": 50.0, "timestamp": 50.0,
             "consumed_packet_sequence": 101, "integer_echoes": {"26": 25}},
            {"received_monotonic_s": 73.999, "timestamp": 73.999,
             "consumed_packet_sequence": 102, "integer_echoes": {"26": 25}},
            {"received_monotonic_s": 75.0, "timestamp": 75.0,
             "consumed_packet_sequence": 102, "integer_echoes": {"26": 40}},
        ],
        "raw_sensor": [
            {"host_use_monotonic_s": 3.0, "corrected_wrench_n_nm": [0, 0, -0.4, 0, 0, 0], "sensor_fresh": True},
            {"host_use_monotonic_s": 5.0, "corrected_wrench_n_nm": [0, 0, -0.7, 0, 0, 0], "sensor_fresh": True},
            {"host_use_monotonic_s": 7.0, "corrected_wrench_n_nm": [0, 0, -2.0, 0, 0, 0], "sensor_fresh": True},
            {"host_use_monotonic_s": 10.0, "corrected_wrench_n_nm": [0, 0, -4.0, 0, 0, 0], "sensor_fresh": True},
            {"host_use_monotonic_s": 14.5, "corrected_wrench_n_nm": [0, 0, -5.0, 0, 0, 0], "sensor_fresh": True},
            {"host_use_monotonic_s": 15.0, "corrected_wrench_n_nm": [0, 0, -4, 0, 0, 0]},
            {"host_use_monotonic_s": 17.0, "corrected_wrench_n_nm": [0, 0, -6, 0, 0, 0]},
            {"host_use_monotonic_s": 21.0, "corrected_wrench_n_nm": [0, 0, -5, 0, 0, 0]},
            {"host_use_monotonic_s": 22.0, "corrected_wrench_n_nm": [0, 0, -7, 0, 0, 0]},
        ],
        "published_packets": [[15.0, {}], [15.002, {}], [15.006, {}]],
        "command_timeline": [
            {"packet_sequence": 100, "reference_phase": "path", "reference_time_s": 0.0,
             "host_monotonic_s": 15.0},
            {"packet_sequence": 101, "reference_phase": "path", "reference_time_s": 30.0,
             "host_monotonic_s": 45.0},
            {"packet_sequence": 102, "reference_phase": "path", "reference_time_s": 59.99,
             "host_monotonic_s": 74.99},
        ],
    }
    for name, rows in data.items():
        path = attempt / f"{name}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        content = path.read_bytes()
        segments[name] = {"path": str(path), "count": len(rows),
                          "sha256": hashlib.sha256(content).hexdigest()}
    service_path = attempt / "service_observations.jsonl"
    service_path.write_text(json.dumps({
        "label": "robot_frames",
        "row": {"received_monotonic_s": 78.6, "integer_echoes": {"26": 78}},
    }) + "\n")
    service = {"path": str(service_path), "count": 1,
               "sha256": hashlib.sha256(service_path.read_bytes()).hexdigest()}
    seal = {"segments": segments, "service_observations": service,
            "lifecycle": {"sealed": True}}
    (attempt / "seal.json").write_text(json.dumps(seal))
    item = {
        "sequence": 1,
        "physical_dispatched": True,
        "parameter_binding": {"candidate_id": "one",
                              "protocol_id": "figure8_window60_r013_rate400_v1"},
        "timing": {"started_monotonic_s": 1.0,
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
    assert row["path_start_source"] == "sealed_command_timeline"
    assert row["path_complete"] is True
    assert row["timing"]["physical_return_s"] == pytest.approx(3.6)
    assert row["timing"]["ready_home_to_receipt_s"] == pytest.approx(3.4)
    assert row["timing"]["stationary_home_verified_s"] == 79.1
    assert row["formal_bin_mean_mae_n"] == .25
    assert row["force"]["window_0_60_s"]["sample_mae_n"] == 1.0
    assert row["force"]["window_0_60_s"]["samples"] == 4
    assert row["force"]["window_0_5_s"]["sample_mae_n"] == 1.0
    assert row["force"]["window_5_60_s"]["sample_mae_n"] == 1.0
    binned = row["force"]["raw_sensor_bin_mean_diagnostics"]
    assert binned["window_0_60_s"]["nonempty_bins"] == 4
    assert binned["window_0_60_s"]["required_bins"] == 600
    assert binned["window_0_60_s"]["bin_mean_mae_n"] == pytest.approx(1.0)
    assert binned["window_0_5_s"]["nonempty_bins"] == 2
    assert binned["window_5_60_s"]["nonempty_bins"] == 2
    assert binned["receipt_normal_force_mae_n"] == pytest.approx(.25)
    assert binned["receipt_metric_eligible"] is True
    assert binned["raw_formal_bin_mean_minus_receipt_mae_n"] == pytest.approx(.75)
    pre_path = row["pre_path_contact_diagnostics"]
    assert pre_path["force_source"] == "Kunwei KWR75 corrected_wrench_n_nm"
    assert pre_path["stages"]["state20_contact_search"]["sensor_sample_count"] == 2
    assert pre_path["stages"]["state20_contact_search"]["below_1n_fraction"] == 1.0
    assert pre_path["stages"]["state21_latch_and_ramp"]["sensor_sample_count"] == 2
    assert pre_path["stages"]["entry"]["sensor_sample_count"] == 1
    assert "mae_n" not in pre_path["stages"]["state21_latch_and_ramp"]
    assert row["controller_trace"]["per_frame_diagnostics_complete"] is False
    assert row["cadence"]["classification"] == "rtde_below_460"
    raw_rates = row["raw_layer_measurements"]
    assert raw_rates["counts"]["formal_host_path_reference_sequences"] == 3
    assert raw_rates["counts"]["raw_rtde_frames"] == 4
    assert raw_rates["counts"]["tp_consumed_formal_echoes"] == 3
    assert "accepted, fully synchronized PathSample rows" in raw_rates[
        "synchronized_timing_evidence"
    ]["scope"]
    bundle = seal_session_diagnostic(attempt.parent.parent, events=events)
    assert bundle["attempt_count"] == 1
    assert bundle["protocol_id"] == "figure8_window60_r013_rate400_v1"
    sidecar = json.loads((attempt.parent.parent / "resident-evidence-diagnostic.json").read_text())
    assert sidecar["attempts"][0]["timing"]["physical_return_s"] == pytest.approx(3.6)


@pytest.mark.parametrize("schema", [
    "tase.qp-replay-command-timeline-v2",
    "tase.qp-replay-command-timeline-v3",
])
def test_v2_and_v3_controller_trace_report_integral_saturation_motion_and_timing(
    tmp_path, schema
):
    booleans = (
        "integral_state_clamped", "integral_authority_clamped",
        "integral_conditional_frozen", "integral_final_output_frozen",
        "integral_velocity_saturated", "integral_saturated",
        "integral_contact_gated", "integral_explicit_velocity_limit_active",
        "force_rise_guard", "force_preempt_warm_start",
        "force_preempt_direction_retry", "baseline_normal_projection_applied",
        "force_rise_inward_cap_applied", "host_slew_limited",
        "published_feedback_present", "published_task_space_saturated",
        "published_realization_saturated", "published_explicit_realization_limit",
    )
    row = {
        "schema": schema,
        "normal_force_measured_n": 4.0,
        "normal_force_filtered_n": 4.5,
        "normal_force_control_n": 4.5,
        "force_target_n": 5.0,
        "force_error_n": 0.5,
        "integral_state_n_s": 0.99,
        "integral_raw_state_n_s": 1.1,
        "integral_effective_limit_n_s": 1.0,
        "integral_i_term": 0.1,
        "integral_raw_normal_velocity_m_s": 0.001,
        "integral_applied_normal_velocity_m_s": 0.0005,
        "measured_force_norm_n": 5.0,
        "published_same_direction_freeze_sign": 1,
        "published_total_directional_shortfall_m_s": 0.0005,
        "published_requested_normal_m_s": 0.001,
        "published_task_normal_m_s": 0.0007,
        "published_realized_normal_m_s": 0.0005,
        "publish_latency_s": 0.0001,
        "host_monotonic_s": 10.0,
        "published_monotonic_s": 10.0001,
        "actual_dt_s": 0.002,
        "solver_elapsed_s": 0.0001,
        "provider_elapsed_s": 0.0007,
        "actual_q_rad": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
        "actual_qd_rad_s": [0.01, -0.02, 0.03, -0.04, 0.05, -0.06],
        "published_packet_qdot_rad_s": [0.02, -0.03, 0.04, -0.05, 0.06, -0.07],
    }
    row.update({name: False for name in booleans})
    row["integral_state_clamped"] = True
    row["integral_saturated"] = True
    row["published_feedback_present"] = True
    trace = tmp_path / "command_timeline.jsonl"
    trace.write_text(json.dumps(row) + "\n", encoding="utf-8")

    result = _controller_trace_diagnostics(trace)
    assert result["per_frame_diagnostics_complete"] is True
    assert result["integral_at_or_above_98pct_limit_ticks"] == 1
    assert result["saturation_and_transition_ticks"]["integral_saturated"] == 1
    assert result["actual_q_joint_range_rad"][0] == {"min": 0.0, "max": 0.0}
    assert result["actual_qd_max_abs_rad_s"] == pytest.approx(0.06)
    assert result["timing_s"]["publish_latency_s"]["mean"] == pytest.approx(0.0001)


def test_controller_force_diagnostics_separate_raw_filtered_sample_and_bin_errors():
    rows = [
        {
            "reference_phase": "path",
            "reference_time_s": index / 10 + 0.01,
            "normal_force_measured_n": 5.5,
            "normal_force_filtered_n": 5.1,
            "normal_force_control_n": 5.2,
        }
        for index in range(600)
    ]
    result = _controller_path_force_diagnostics(rows)
    measured = result["signals"]["normal_force_measured_n"]["windows"]
    filtered = result["signals"]["normal_force_filtered_n"]["windows"]
    assert result["available"] is True
    assert result["invalid_sample_or_channel_count"] == 0
    assert measured["window_0_60_s"]["per_sample"]["samples"] == 600
    assert measured["window_0_60_s"]["equal_weight_100ms_bins"]["required_bins"] == 600
    assert measured["window_0_60_s"]["equal_weight_100ms_bins"]["complete_coverage"] is True
    assert measured["window_5_60_s"]["per_sample"]["sample_mae_n"] == pytest.approx(0.5)
    assert measured["window_5_60_s"]["equal_weight_100ms_bins"]["required_bins"] == 550
    assert measured["window_5_60_s"]["equal_weight_100ms_bins"]["bin_mean_mae_n"] == pytest.approx(0.5)
    assert filtered["window_5_60_s"]["per_sample"]["sample_mae_n"] == pytest.approx(0.1)
    assert filtered["window_5_60_s"]["equal_weight_100ms_bins"]["bin_mean_mae_n"] == pytest.approx(0.1)
