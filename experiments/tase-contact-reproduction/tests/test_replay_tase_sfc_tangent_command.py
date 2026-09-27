from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from replay_tase_sfc_tangent_command import (  # noqa: E402
    _apply_last_recorded_transition,
    _compose_replay_twist,
    _force_metrics,
)
from tase_sfc_tangent_augmentation import TaseSfcTangentAugmentationRuntime  # noqa: E402


class _Solver:
    def __init__(self):
        self.state = None

    def restore(self, state):
        self.state = state


class _Runtime:
    def __init__(self):
        self.solver = _Solver()
        self._active_mode = None


def test_replay_restores_last_warm_start_state_before_the_recorded_solve():
    runtime = _Runtime()
    row = {
        "transition_events": [
            {
                "event_type": "path_origin",
                "theta_dot_state": [1.0] * 6,
                "lambda_state": [2.0] * 6,
            },
            {
                "event_type": "force_preempt_direction_retry",
                "theta_dot_state": [3.0] * 6,
                "lambda_state": [4.0] * 6,
            },
        ],
    }
    assert _apply_last_recorded_transition(runtime, row)
    assert runtime.solver.state == {
        "theta_dot_state": [3.0] * 6,
        "lambda_state": [4.0] * 6,
    }
    assert runtime._active_mode == "path"


def test_force_metrics_keep_sample_and_equal_weight_bin_errors_separate():
    samples = []
    for index in range(600):
        time_s = (index + 0.5) * 0.1
        # Every bin receives unequal numbers of samples, so sample and bin
        # weighting are deliberately different.
        repeats = 1 if index % 2 else 3
        force_n = 7.0 if index % 2 else 4.0
        samples.extend((time_s, force_n) for _ in range(repeats))
    result = _force_metrics(samples, source_signal="normal_force_measured_n")
    full = result["full_0_60"]
    assert full["sample_count"] == 1200
    assert full["nonempty_bins"] == 600
    assert full["complete_bin_coverage"] is True
    assert full["sample_mae_n"] == 1.25
    assert full["bin_mean_mae_n"] == 1.5
    formal = result["formal_5_60"]
    assert formal["sample_count"] == 1100
    assert formal["required_bins"] == 550
    assert formal["complete_bin_coverage"] is True
    assert formal["sample_mae_n"] == 1.25
    assert formal["bin_mean_mae_n"] == 1.5


def test_tangent_replacement_keeps_tase_normal_and_posture_but_drops_tase_path_tangent():
    overlay = SimpleNamespace(
        _max_scale_inside_ball=TaseSfcTangentAugmentationRuntime._max_scale_inside_ball,
    )
    diagnostics = {
        "requested_sfc_velocity_base_m_s": [0.006, 0.004, 0.2],
        "sfc_velocity_base_m_s": [0.006, 0.004, 0.0],
        "sfc_velocity_scale_from_existing_caps": 1.0,
    }
    base = np.asarray((0.004, 0.002, -0.001, 0.01, 0.02, 0.03))
    profile = SimpleNamespace(tangential_cap_m_s=0.01, total_linear_cap_m_s=0.01)
    twist, sfc, scale = _compose_replay_twist(
        composition="replace_tase_tangent",
        base_twist=base,
        diagnostics=diagnostics,
        normal_value=(0.0, 0.0, 1.0),
        motion_profile=profile,
        overlay=overlay,
    )
    np.testing.assert_allclose(twist, (0.006, 0.004, -0.001, 0.01, 0.02, 0.03))
    np.testing.assert_allclose(sfc, (0.006, 0.004, 0.0))
    assert scale == 1.0
