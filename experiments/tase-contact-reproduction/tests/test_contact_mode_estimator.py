#!/usr/bin/env python3

from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from contact_mode_estimator import (  # noqa: E402
    ContactModeObserver,
    EstimationMode,
    ModeConfig,
    ModeObservation,
    Step5dV3Adapter,
)


def observation(**overrides: object) -> ModeObservation:
    base = ModeObservation(
        t_monotonic_s=1.0,
        dt_s=0.01,
        stage=0.0,
        bridge_profile="step5d_strict_rnn_no_contact_p0_v7",
        baseline_ready=True,
        sensor_fresh=True,
        sample_finite=True,
        source_valid=True,
        normal_load_n=0.1,
        force_norm_n=0.2,
        source_contact_mask=False,
        control_contact_window=False,
        normal_acquired=False,
        force_settle_ready=False,
        contact_safety_state=0.0,
        cmd_valid=False,
        linear_speed_m_s=0.0,
        angular_speed_rad_s=0.0,
        phase_s=0.0,
    )
    return replace(base, **overrides)


class ContactModeObserverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ModeConfig(
            static_dwell_s=0.03,
            impact_dwell_s=0.03,
            release_dwell_s=0.03,
        )

    def test_static_no_contact_requires_dwell_before_updates(self) -> None:
        observer = ContactModeObserver(self.config)
        first = observer.decide(observation())
        second = observer.decide(observation(t_monotonic_s=1.01))
        third = observer.decide(observation(t_monotonic_s=1.02))
        self.assertEqual(first.mode, EstimationMode.FREE_REFERENCE)
        self.assertEqual(second.update_allowed, 0)
        self.assertEqual(third.mode, EstimationMode.FREE_STATIC)
        self.assertEqual(third.update_allowed, 1)
        self.assertEqual(third.contact_mask, 0)

    def test_controller_search_stage_always_freezes(self) -> None:
        decision = ContactModeObserver(self.config).decide(observation(stage=24.2))
        self.assertEqual(decision.mode, EstimationMode.SEARCH)
        self.assertEqual(decision.contact_mask, 1)
        self.assertEqual(decision.update_allowed, 0)

    def test_contact_onset_impact_then_tracking(self) -> None:
        observer = ContactModeObserver(self.config)
        decisions = [
            observer.decide(
                observation(
                    t_monotonic_s=1.0 + idx * 0.01,
                    stage=25.0,
                    source_contact_mask=True,
                    normal_acquired=True,
                    normal_load_n=10.0,
                    force_norm_n=10.0,
                )
            )
            for idx in range(4)
        ]
        self.assertEqual(decisions[0].mode, EstimationMode.IMPACT)
        self.assertEqual(decisions[-1].mode, EstimationMode.CONTACT_TRACK)
        self.assertTrue(all(decision.update_allowed == 0 for decision in decisions))

    def test_release_requires_dwell_and_one_rebaseline_state(self) -> None:
        observer = ContactModeObserver(self.config)
        observer.decide(
            observation(
                stage=25.0,
                source_contact_mask=True,
                normal_acquired=True,
                normal_load_n=8.0,
                force_norm_n=8.0,
            )
        )
        decisions = [
            observer.decide(
                observation(
                    t_monotonic_s=1.01 + idx * 0.01,
                    stage=25.1,
                    normal_load_n=0.0,
                    force_norm_n=0.0,
                )
            )
            for idx in range(3)
        ]
        self.assertEqual(decisions[0].mode, EstimationMode.LIFT_OFF)
        self.assertEqual(decisions[-1].mode, EstimationMode.REBASELINE)
        self.assertTrue(all(decision.update_allowed == 0 for decision in decisions))

    def test_invalid_sample_is_fail_closed(self) -> None:
        decision = ContactModeObserver(self.config).decide(
            observation(sensor_fresh=False)
        )
        self.assertEqual(decision.mode, EstimationMode.INVALID)
        self.assertEqual(decision.contact_mask, 1)
        self.assertEqual(decision.update_allowed, 0)


class Step5dV3AdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        metadata = {
            "args": {
                "bridge_profile": "step5d_strict_rnn_no_contact_p0_v7",
                "bias_contact_normal_threshold_n": 0.8,
                "bias_contact_force_norm_threshold_n": 2.2,
            }
        }
        self.adapter = Step5dV3Adapter.from_metadata(
            metadata,
            base_config=ModeConfig(),
            max_sensor_age_s=0.1,
        )

    def bridge_row(self) -> dict[str, object]:
        return {
            "t_monotonic_s": "10.002",
            "sensor_age_s": "0.002",
            "normal_force_n": "0.1",
            "force_norm_n": "0.2",
            "baseline_ready": "1",
            "zero_event_id": "3",
            "bias_estimation_contact_mask": "0",
            "control_contact_window": "0",
            "_step4e_normal_acquired": "0",
            "_step5d_force_settle_ready": "0",
            "_step5d_contact_safety_state": "0",
            "step4e_cmd_valid": "0",
            "ur_output_double_register_35": "24.2",
            "_step4e_path_time_s": "0.5",
            **{f"ur_actual_TCP_speed_{idx}": "0" for idx in range(6)},
        }

    def sensor_row(self) -> dict[str, object]:
        return {
            "t_monotonic_s": "10.001",
            "zero_event_id": "3",
            "bias_estimation_contact_mask": "0",
            "normal_force_n": "0.1",
            "force_norm_n": "0.2",
            "fx_n_zeroed": "0.1",
            "fy_n_zeroed": "0.0",
            "fz_n_zeroed": "-0.1",
            "mx_nm_zeroed": "0.0",
            "my_nm_zeroed": "0.0",
            "mz_nm_zeroed": "0.0",
        }

    def test_adapter_preserves_causal_timestamp_and_run_thresholds(self) -> None:
        obs = self.adapter.observation(
            self.bridge_row(), self.sensor_row(), previous_bridge_t_s=10.0
        )
        self.assertTrue(obs.sensor_fresh)
        self.assertTrue(obs.source_valid)
        self.assertAlmostEqual(obs.dt_s, 0.002)
        self.assertEqual(obs.stage, 24.2)
        self.assertEqual(self.adapter.mode_config.contact_normal_enter_n, 0.8)

    def test_future_sensor_sample_invalidates_observation(self) -> None:
        sensor = self.sensor_row()
        sensor["t_monotonic_s"] = "10.003"
        obs = self.adapter.observation(
            self.bridge_row(), sensor, previous_bridge_t_s=10.0
        )
        self.assertFalse(obs.sensor_fresh)

    def test_zero_epoch_mismatch_invalidates_source(self) -> None:
        sensor = self.sensor_row()
        sensor["zero_event_id"] = "4"
        obs = self.adapter.observation(
            self.bridge_row(), sensor, previous_bridge_t_s=10.0
        )
        self.assertFalse(obs.source_valid)


if __name__ == "__main__":
    unittest.main()
