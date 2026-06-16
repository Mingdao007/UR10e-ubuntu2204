#!/usr/bin/env python3
"""Focused tests for the Step5d ROS2 remote-control shadow route."""

from __future__ import annotations

import csv
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_step5d_remote"))

from ur10e_step5d_remote import state_machine  # noqa: E402
from ur10e_step5d_remote.replay_shadow import (  # noqa: E402
    DEFAULT_CONFIG,
    TRACE_FIELDS,
    build_cage,
    load_config,
    run_shadow_replay,
)
from ur10e_step5d_remote.state_machine import (  # noqa: E402
    CageBounds,
    ShadowSample,
    Step5dRemoteConfig,
    Step5dRemoteMachine,
    Step5dRemoteState,
    approach_normal_from_reaction,
    signed_normal_load_n,
)


RUNS = ROOT / "runs"
V15A = RUNS / "bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v15a_20260616_160355" / "bridge_rtde_500hz.csv"
V14 = RUNS / "bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v14_20260615_231805" / "bridge_rtde_500hz.csv"
V11 = RUNS / "bridge_step5d_strict_rnn_liveprep_v11_20260615_204601" / "bridge_rtde_500hz.csv"
STEP5B = RUNS / "bridge_step5b_contact_cycloid_baseline_v1_20260612_082352" / "bridge_rtde_500hz.csv"
STEP6B = RUNS / "bridge_step6b_contact_eight_baseline_v1_20260612_223047" / "bridge_rtde_500hz.csv"


class Step5dRos2RemoteShadowTest(unittest.TestCase):
    def test_default_config_and_launch_are_no_motion(self) -> None:
        config, raw = load_config(DEFAULT_CONFIG)
        self.assertFalse(config.enable_motion)
        self.assertFalse(raw["enable_motion"])
        launch = (WORKSPACE / "src" / "ur10e_step5d_remote" / "launch" / "step5d_remote_shadow.launch.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('DeclareLaunchArgument("enable_motion", default_value="false")', launch)

    def test_v15a_low_load_does_not_become_long_zero_qdot_hold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_shadow_replay(
                config_path=DEFAULT_CONFIG,
                output_dir=Path(tmp),
                replay_csvs=[V15A],
            )
        self.assertTrue(summary["acceptance"]["long_hold_removed"])
        self.assertLess(summary["max_hold_duty"], 0.25)
        self.assertGreater(summary["state_counts"].get("ACTIVE_REACQUIRE", 0), 0)
        self.assertGreater(summary["state_counts"].get("FAIL_FAST_STOP_REQUEST", 0), 0)
        self.assertFalse(summary["cmd_enabled_any"])

    def test_v14_predicted_speed_is_classified_by_cage_braking_margin(self) -> None:
        config = Step5dRemoteConfig()
        cage = CageBounds(min_xyz=(-0.1, -0.1, -0.1), max_xyz=(0.1, 0.1, 0.1))
        inside = cage.evaluate(
            (0.0, 0.0, 0.0),
            actual_tcp_speed_m_s=0.004,
            predicted_tcp_speed_m_s=0.051,
            config=config,
        )
        exhausted = cage.evaluate(
            (0.095, 0.0, 0.0),
            actual_tcp_speed_m_s=0.004,
            predicted_tcp_speed_m_s=0.051,
            config=config,
        )
        self.assertEqual(inside["reason"], "inside_broad_tcp_cage")
        self.assertGreater(inside["braking_margin_m"], 0.0)
        self.assertEqual(exhausted["reason"], "tcp_cage_braking_margin_exhausted")
        self.assertLessEqual(exhausted["braking_margin_m"], 0.0)
        self.assertTrue(V14.exists())

    def test_v11_early_escape_is_intercepted_before_qdot_rail(self) -> None:
        config, raw = load_config(DEFAULT_CONFIG)
        cage = build_cage([WORKSPACE / value for value in raw["baseline_cage_csvs"]], config)
        machine = Step5dRemoteMachine(config=config, cage=cage)
        with V11.open(newline="", encoding="utf-8") as handle:
            for row_index, row in enumerate(csv.DictReader(handle)):
                stage = float(row.get("ur_output_double_register_35") or "nan")
                if abs(stage - 25.0) > 0.05:
                    continue
                sample = ShadowSample(
                    source_csv=str(V11),
                    row_index=row_index,
                    t_rel_s=float(row["t_monotonic_s"]),
                    normal_load_n=float(row.get("_step4e_normal_load_n") or 0.0),
                    force_norm_n=float(row.get("force_norm_n") or 0.0),
                    torque_norm_nm=float(row.get("torque_norm_nm") or 0.0),
                    tcp_xyz=(
                        float(row["ur_actual_TCP_pose_0"]),
                        float(row["ur_actual_TCP_pose_1"]),
                        float(row["ur_actual_TCP_pose_2"]),
                    ),
                    actual_tcp_speed_m_s=float(row.get("_step4e_actual_speed_norm_m_s") or 0.0),
                    predicted_tcp_speed_m_s=None,
                    reaction_normal=(
                        float(row.get("_step4e_control_normal_b_x") or 0.0),
                        float(row.get("_step4e_control_normal_b_y") or 0.0),
                        float(row.get("_step4e_control_normal_b_z") or -1.0),
                    ),
                )
                result = machine.step(sample)
                if result["would_state"] in {
                    Step5dRemoteState.SHORT_DWELL_HOLD.value,
                    Step5dRemoteState.ACTIVE_REACQUIRE.value,
                    Step5dRemoteState.FAIL_FAST_STOP_REQUEST.value,
                }:
                    self.assertLess(row_index, 22000)
                    qdot = max(abs(float(row.get(f"_step5c_cmd_qd{idx}_rad_s") or 0.0)) for idx in range(6))
                    self.assertLess(qdot, 0.30)
                    return
        self.fail("v11 Stage25 did not trigger shadow intervention")

    def test_step5b_step6b_baselines_do_not_immediately_hard_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_shadow_replay(
                config_path=DEFAULT_CONFIG,
                output_dir=Path(tmp),
                replay_csvs=[STEP5B, STEP6B],
                max_rows_per_csv=100,
            )
        for source in summary["source_summaries"]:
            self.assertIsNone(source["first_fail_fast"])
            self.assertGreater(source["rows_replayed"], 0)

    def test_force_frame_static_scan_rejects_raw_force_as_press_direction(self) -> None:
        source = inspect.getsource(state_machine)
        forbidden = [
            "approach = sample.force",
            "approach_normal = force_base",
            "reacquire_direction = reaction_normal",
            "raw_force_as_press",
        ]
        for token in forbidden:
            self.assertNotIn(token, source)
        self.assertEqual(approach_normal_from_reaction((0.0, 0.0, -1.0)), (0.0, 0.0, 1.0))
        self.assertAlmostEqual(signed_normal_load_n((0.0, 0.0, -2.0), (0.0, 0.0, -1.0)), 2.0)

    def test_artifact_schema_contains_required_summary_and_trace_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            summary = run_shadow_replay(
                config_path=DEFAULT_CONFIG,
                output_dir=out,
                replay_csvs=[V15A],
                max_rows_per_csv=5,
            )
            summary_path = out / "summary.json"
            trace_path = out / "shadow_trace.csv"
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
            with trace_path.open(newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle))
        self.assertEqual(payload["artifact_dir"], summary["artifact_dir"])
        self.assertIn("policy", payload)
        for field in TRACE_FIELDS:
            self.assertIn(field, header)
        self.assertFalse(payload["cmd_enabled_any"])


if __name__ == "__main__":
    unittest.main()
