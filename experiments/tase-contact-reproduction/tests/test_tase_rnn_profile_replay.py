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
