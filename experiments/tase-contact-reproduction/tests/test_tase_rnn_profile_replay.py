from __future__ import annotations

import pytest

from tase_rnn_profile_replay import (
    FINITE_TIME_R08,
    LEGACY_R1,
    ProfileReplayError,
    compare_profile_replays,
    run_profile_replay,
)


def _step(profile, sample, state):
    previous = 0.0 if state is None else float(state)
    value = float(sample["value"]) + profile.r + previous * 0.1
    return {
        "output": [value, value / 2.0],
        "state": [value],
        "next_state": value,
        "residual": abs(value) * 0.01,
        "converged": profile.iterations >= 1,
        "compute_ms": 0.4 if profile is LEGACY_R1 else 0.8,
        "control_interval_s": 0.002,
        "qdot": [0.01, -0.01],
        "qp_feasible": True,
        "slew_ok": True,
        "safety_ok": True,
    }


def test_fixed_input_replay_reports_both_profiles_and_shared_digest() -> None:
    samples = [{"value": 0.1 * index} for index in range(4)]
    legacy = run_profile_replay(samples, LEGACY_R1, _step)
    finite = run_profile_replay(samples, FINITE_TIME_R08, _step)
    comparison = compare_profile_replays(legacy, finite)
    assert comparison["samples"] == 4
    assert comparison["input_digest"] == legacy.input_digest == finite.input_digest
    assert comparison["convergence_difference_count"] == 0
    assert comparison["qdot_bounds_ok"] is True
    assert comparison["promotion"] == "offline_comparison_only"


def test_profile_replay_rejects_different_inputs() -> None:
    left = run_profile_replay([{"value": 1.0}], LEGACY_R1, _step)
    right = run_profile_replay([{"value": 2.0}], FINITE_TIME_R08, _step)
    with pytest.raises(ProfileReplayError, match="different fixed inputs"):
        compare_profile_replays(left, right)


def test_optional_latency_fields_are_compared_per_profile_when_present() -> None:
    def step_with_latency(profile, sample, state):
        result = _step(profile, sample, state)
        result.update(
            first_sample_latency_ms=0.20 + sample["value"],
            send_wrapper_latency_ms=0.05 + sample["value"] / 2.0,
        )
        return result

    samples = [{"value": 0.1 * index} for index in range(4)]
    left = run_profile_replay(samples, LEGACY_R1, step_with_latency)
    right = run_profile_replay(samples, FINITE_TIME_R08, step_with_latency)
    comparison = compare_profile_replays(left, right)

    assert left.first_sample_latency_stats["status"] == "complete"
    assert left.first_sample_latency_stats["missing_count"] == 0
    assert left.first_sample_latency_stats["p99"] == pytest.approx(0.5)
    assert right.send_wrapper_latency_stats["mean"] == pytest.approx(0.125)
    assert comparison["per_profile"][LEGACY_R1.profile_id]["residual"]["status"] == "complete"
    assert comparison["per_profile"][FINITE_TIME_R08.profile_id]["convergence"]["converged_count"] == 4
    assert comparison["deterministic_shared_input"] == {
        "status": "pass",
        "same_input_digest": True,
        "input_digest": left.input_digest,
        "sample_count_equal": True,
        "offline_only": True,
    }


def test_missing_optional_latency_fields_are_reported_honestly() -> None:
    def step_partial(profile, sample, state):
        result = _step(profile, sample, state)
        # Deliberately expose only one optional field for one profile.  The
        # replay runner must not time or infer the absent send-wrapper field.
        if profile is LEGACY_R1:
            result["first_sample_latency_ms"] = 0.3
        return result

    samples = [{"value": 0.1 * index} for index in range(3)]
    legacy = run_profile_replay(samples, LEGACY_R1, step_partial)
    finite = run_profile_replay(samples, FINITE_TIME_R08, step_partial)
    comparison = compare_profile_replays(legacy, finite)

    assert legacy.first_sample_latency_stats["status"] == "complete"
    assert legacy.send_wrapper_latency_stats == {
        "status": "missing",
        "count": 0,
        "missing_count": 3,
        "min": None,
        "max": None,
        "mean": None,
        "p99": None,
    }
    assert finite.first_sample_latency_stats["status"] == "missing"
    assert comparison["send_wrapper_latency_ms"][LEGACY_R1.profile_id]["status"] == "missing"
    assert comparison["send_wrapper_latency_ms"][FINITE_TIME_R08.profile_id]["missing_count"] == 3


def test_qdot_violation_cannot_be_reported_as_safe() -> None:
    def unsafe_step(profile, sample, state):
        result = _step(profile, sample, state)
        result["qdot"] = [profile.qdot_limit_rad_s * 2.0, 0.0]
        return result

    result = run_profile_replay([{"value": 0.0}], LEGACY_R1, unsafe_step)
    assert result.constraint_stats()["qdot"]["bound_violation_count"] == 1
    assert result.safety_ok is False
    assert compare_profile_replays(result, result)["qdot_bounds_ok"] is False
