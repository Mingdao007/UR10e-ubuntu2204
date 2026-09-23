"""Event-aligned apparatus scoring keeps historical force and human claims separate."""
import copy

import pytest

from tase_collision_trial import (CollisionTrialError, PROTOCOL_ID, TRIAL_SCHEMA,
                                  compare_paired_results, score_trial)


def _trial():
    # Include one pre-impact sample and all three complete event windows.
    samples = []
    for index in range(-10, 5001):
        t = index * .002
        samples.append({
            "monotonic_s": 100.0 + t,
            "pusher_force_n": 10.0 if 0 <= t < .5 else 0.0,
            "normal_force_n": 5.0,
            "tangent_speed_m_s": .002,
            "lateral_error_m": .0001,
            "orientation_error_rad": .001,
        })
    return {
        "schema": TRIAL_SCHEMA, "protocol_id": PROTOCOL_ID,
        "trial_id": "apparatus-link-tangent-001",
        "pair_id": "matched-pulse-001",
        "source_kind": "instrumented_apparatus", "human_present": False,
        "contact_site": "link", "direction": "tangent", "controller_arm": "dual",
        "apparatus": {"calibration_sha256": "b" * 64, "waveform_id": "pulse-01",
                      "clock_alignment_error_s": .0005, "trigger_threshold_n": 5.0},
        "event_at_monotonic_s": 100.0, "trial_started_at_s": 1001.0,
        "samples": samples,
        "cadence": {"rtde_path_hz": 480.0, "tp_path_hz": 475.0,
                    "rtde_event_hz": 480.0, "tp_event_hz": 475.0},
        "lifecycle": {"single_writer": True, "home_verified": True,
                      "guard_trip": False, "recovery_exhausted": False,
                      "task_restored_at_monotonic_s": 102.0,
                      "home_verified_at_monotonic_s": 120.0},
        "mark_evidence": {"schema_version": "contact-board-marks.v1",
                          "qualified": True, "registration": {"qualified": True},
                          "detectability": {"qualified": True},
                          "corridor_mask_sha256": "a" * 64,
                          "corridor_mask_sha256_basis": "raw_file_bytes",
                          "outside_corridor_area_px2": 0,
                          "max_outside_distance_px": None},
        "corridor_admission": {"schema": "tase.board-corridor-admission-v1",
                               "unperturbed_runs": 10, "detectable": True,
                               "frozen_before_candidate": True,
                               "mask_sha256": "a" * 64, "admitted_at_s": 1000.0},
    }


def test_complete_trial_reports_event_windows_and_no_extra_mark_only_with_visual_proof():
    result = score_trial(_trial())
    assert result["performance_eligible"] is True
    assert result["human_safety_claim"] is False
    assert result["visible_no_extra_mark_finding"] is True
    assert result["windows"]["impact"]["pusher_peak_n"] == 10.0
    assert result["windows"]["impact"]["pusher_impulse_n_s"] == pytest.approx(4.98, abs=.03)
    assert result["windows"]["swing"]["normal_force_mae_n"] == 0.0
    assert result["windows"]["swing"]["contact_loss_duration_s"] == 0.0
    assert result["windows"]["swing"]["loaded_tangent_slip_n_m"] == pytest.approx(.045, abs=.001)
    assert result["task_recovery_after_event_s"] == 2.0
    assert result["home_verified_after_event_s"] == 20.0
    assert result["legacy_5_60_mae_scope"] == "separate no-disturbance protocol only"


def test_guard_and_missing_surface_or_rate_preserve_safety_outcome_but_withhold_claims():
    trial = _trial()
    trial["lifecycle"]["guard_trip"] = True
    trial["lifecycle"]["home_verified"] = False
    trial["mark_evidence"] = {"qualified": False, "reason": "old_marks_saturated"}
    trial["corridor_admission"]["detectable"] = False
    trial["cadence"]["rtde_event_hz"] = 395.0
    result = score_trial(trial)
    assert result["safety_outcome"]["guard_trip"] is True
    assert result["windows"]["impact"]["pusher_peak_n"] == 10.0
    assert result["performance_eligible"] is False
    assert result["visible_no_extra_mark_finding"] is False
    assert {"guard_trip", "home_recovery_unverified", "surface_mark_not_detectable",
            "corridor_baseline_unqualified", "rate460_not_observed"} <= set(result["ineligible_reasons"])


def test_missing_recovery_window_and_human_identity_fail_closed():
    trial = _trial()
    trial["samples"] = [row for row in trial["samples"]
                        if row["monotonic_s"] < 105.1]
    result = score_trial(trial)
    assert result["windows"]["recovery"]["covered"] is False
    assert "event_window_incomplete" in result["ineligible_reasons"]
    human = copy.deepcopy(trial)
    human["human_present"] = True
    with pytest.raises(CollisionTrialError, match="cannot claim human"):
        score_trial(human)


def test_corridor_digest_and_admission_time_bind_visible_finding():
    trial = _trial()
    trial["mark_evidence"]["corridor_mask_sha256"] = "c" * 64
    result = score_trial(trial)
    assert "corridor_mask_identity_differs" in result["ineligible_reasons"]
    assert result["visible_no_extra_mark_finding"] is False
    trial["mark_evidence"]["corridor_mask_sha256"] = "a" * 64
    trial["trial_started_at_s"] = 999.0
    result = score_trial(trial)
    assert "corridor_baseline_unqualified" in result["ineligible_reasons"]


def test_missing_applied_pulse_does_not_count_as_a_collision_trial():
    trial = _trial()
    for sample in trial["samples"]:
        sample["pusher_force_n"] = 0.0
    result = score_trial(trial)
    assert "perturbation_not_observed" in result["ineligible_reasons"]
    assert result["performance_eligible"] is False


def test_ten_matched_pulses_support_only_the_apparatus_damage_comparison():
    rows = []
    for index in range(10):
        baseline = _trial()
        baseline["trial_id"] = f"baseline-{index}"
        baseline["pair_id"] = f"pair-{index}"
        baseline["controller_arm"] = "tase_baseline"
        baseline["mark_evidence"]["outside_corridor_area_px2"] = 5
        baseline["mark_evidence"]["max_outside_distance_px"] = 3
        candidate = copy.deepcopy(baseline)
        candidate["trial_id"] = f"dual-{index}"
        candidate["controller_arm"] = "dual"
        candidate["mark_evidence"]["outside_corridor_area_px2"] = 0
        candidate["mark_evidence"]["max_outside_distance_px"] = 0
        for sample in candidate["samples"]:
            sample["pusher_force_n"] *= .8
        rows.extend((score_trial(baseline), score_trial(candidate)))
    result = compare_paired_results(rows, candidate_arm="dual")
    assert result["matched_pairs"] == 10
    assert result["comparison_eligible"] is True
    assert result["damage_improvement_supported"] is True
    assert result["human_safety_claim"] is False
    assert result["metrics"]["outside_corridor_area_px2"]["median_difference"] == -5
    assert result["metrics"]["impact_pusher_peak_n"]["bootstrap_95_high"] < 0
    rows[-1]["apparatus_waveform_id"] = "different-waveform"
    invalid = compare_paired_results(rows, candidate_arm="dual")
    assert invalid["matched_pairs"] == 9
    assert invalid["comparison_eligible"] is False
    assert invalid["excluded_pairs"][0]["reason"] == "perturbation_identity_differs"
    rows[-1]["apparatus_waveform_id"] = "pulse-01"
    rows[-1]["contact_site"] = "tool"
    rows[-2]["contact_site"] = "tool"
    with pytest.raises(CollisionTrialError, match="mixes contact site"):
        compare_paired_results(rows, candidate_arm="dual")
