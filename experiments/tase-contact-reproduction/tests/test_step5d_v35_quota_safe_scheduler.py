#!/usr/bin/env python3
"""Regression tests for v35 quota-safe scheduler-only ablation."""

from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_liveprep as builder  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
from step5d_runtime_interface import (  # noqa: E402
    STEP5D_ABLATION_V35_STAGE_ID,
    build_stage_env,
)


class Step5dV35QuotaSafeSchedulerTest(unittest.TestCase):
    def test_v35_keeps_v34_control_parameters(self) -> None:
        spec = builder.ABLATION_SPECS[STEP5D_ABLATION_V35_STAGE_ID]
        self.assertEqual(builder.joint_accel_rad_s2(spec), 0.1)
        self.assertEqual(builder.host_qdot_slew_rad_s2(spec), 0.1)
        self.assertEqual(spec.qdot_cap_rad_s, 0.5)
        self.assertEqual(spec.stage25_success_target_s, 60.0)
        self.assertEqual(spec.stage25_runtime_limit_s, 75.0)

    def test_v35_stage_env_preserves_permissive_guard_profile(self) -> None:
        env = build_stage_env(STEP5D_ABLATION_V35_STAGE_ID)
        self.assertEqual(env["BRIDGE_PROFILE"], STEP5D_ABLATION_V35_STAGE_ID)
        self.assertEqual(env["STEP5D_RNN_BACKEND"], "cupy")
        self.assertEqual(env["STEP5D_RNN_INNER_ITERATIONS"], "512")
        self.assertEqual(env["STEP5D_QDOT_LIMIT_RAD_S"], "0.500")
        self.assertEqual(env["STEP5D_QDOT_SLEW_RAD_S2"], "0.100")
        self.assertEqual(env["BRIDGE_SENSOR_STALE_S"], "2.00")

    def test_v35_never_promotes_any_thread_to_realtime(self) -> None:
        other = {"policy": "SCHED_OTHER", "policy_value": os.SCHED_OTHER, "priority": 0}
        threads = {
            "current_tid": 10,
            "thread_count": 3,
            "policy_counts": {"SCHED_OTHER/0": 3},
            "threads": [
                {"tid": tid, "is_control_thread": tid == 10, **other}
                for tid in (10, 11, 12)
            ],
        }
        quota = {"sched_rt_period_us": 1_000_000, "sched_rt_runtime_us": 950_000}
        with (
            patch.object(bridge, "runtime_scheduler_metadata", return_value=other),
            patch.object(bridge, "runtime_thread_scheduler_snapshot", return_value=threads),
            patch.object(bridge, "linux_rt_bandwidth_metadata", side_effect=[quota, quota]),
            patch.object(bridge.os, "sched_setscheduler") as set_scheduler,
        ):
            lifecycle = bridge.configure_v35_quota_safe_scheduler(
                STEP5D_ABLATION_V35_STAGE_ID
            )
        set_scheduler.assert_not_called()
        self.assertTrue(lifecycle["quota_safe_verified"])
        self.assertFalse(lifecycle["promotion_verified"])
        self.assertEqual(lifecycle["control_thread_scheduler"], other)
        self.assertEqual(lifecycle["helper_non_other_thread_count"], 0)
        self.assertEqual(lifecycle["rt_runtime_consumption_policy"], "no_sched_fifo_threads")

    def test_v35_rejects_rt_quota_change_between_scheduler_snapshots(self) -> None:
        other = {"policy": "SCHED_OTHER", "policy_value": os.SCHED_OTHER, "priority": 0}
        threads = {
            "current_tid": 10,
            "thread_count": 1,
            "policy_counts": {"SCHED_OTHER/0": 1},
            "threads": [{"tid": 10, "is_control_thread": True, **other}],
        }
        before = {"sched_rt_period_us": 1_000_000, "sched_rt_runtime_us": 950_000}
        after = {"sched_rt_period_us": 1_000_000, "sched_rt_runtime_us": 900_000}
        with (
            patch.object(bridge, "runtime_scheduler_metadata", return_value=other),
            patch.object(bridge, "runtime_thread_scheduler_snapshot", return_value=threads),
            patch.object(bridge, "linux_rt_bandwidth_metadata", side_effect=[before, after]),
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot prove kernel RT bandwidth remained unchanged"):
                bridge.configure_v35_quota_safe_scheduler(STEP5D_ABLATION_V35_STAGE_ID)

    def test_v35_rejects_any_inherited_fifo_helper(self) -> None:
        other = {"policy": "SCHED_OTHER", "policy_value": os.SCHED_OTHER, "priority": 0}
        threads = {
            "current_tid": 10,
            "thread_count": 2,
            "policy_counts": {"SCHED_OTHER/0": 1, "SCHED_FIFO/20": 1},
            "threads": [
                {"tid": 10, "is_control_thread": True, **other},
                {"tid": 11, "is_control_thread": False, "policy": "SCHED_FIFO", "policy_value": os.SCHED_FIFO, "priority": 20},
            ],
        }
        with (
            patch.object(bridge, "runtime_scheduler_metadata", return_value=other),
            patch.object(bridge, "runtime_thread_scheduler_snapshot", return_value=threads),
            patch.object(bridge, "linux_rt_bandwidth_metadata", return_value={}),
        ):
            with self.assertRaisesRegex(RuntimeError, "every process thread"):
                bridge.configure_v35_quota_safe_scheduler(STEP5D_ABLATION_V35_STAGE_ID)

    def test_v35_raw_bridge_requires_explicit_live_authorization(self) -> None:
        current = {
            "program": STEP5D_ABLATION_V35_STAGE_ID,
            "v35_candidate": {
                "current": True,
                "package": {"controller_readback_verified": True},
                "review_v3": {
                    "status": "accepted_degraded_1+0_with_deterministic_closure"
                },
                "live_authorized": False,
            },
        }
        args = SimpleNamespace(
            bridge_profile=STEP5D_ABLATION_V35_STAGE_ID,
            step5d_stage25_control_mode="speedj_rnn_live",
            step5d_rnn_backend="cupy",
            step5d_rnn_inner_iterations=512,
            step5d_epsilon=0.01,
            step5d_sigr_exponent_r=0.8,
            step5d_qdot_limit_rad_s=0.5,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "current_stage.json").write_text(
                json.dumps(current), encoding="utf-8"
            )
            with self.assertRaisesRegex(SystemExit, "explicit live/contact authorization is missing"):
                bridge.require_v29_live_bridge_authorization(args, root=root)

    def test_v35_raw_bridge_recomputes_frozen_evidence_after_authorization(self) -> None:
        current = {
            "program": STEP5D_ABLATION_V35_STAGE_ID,
            "v35_candidate": {
                "current": True,
                "package": {"controller_readback_verified": True},
                "review_v3": {
                    "status": "accepted_degraded_1+0_with_deterministic_closure"
                },
                "live_authorized": True,
            },
        }
        args = SimpleNamespace(
            bridge_profile=STEP5D_ABLATION_V35_STAGE_ID,
            step5d_stage25_control_mode="speedj_rnn_live",
            step5d_rnn_backend="cupy",
            step5d_rnn_inner_iterations=512,
            step5d_epsilon=0.01,
            step5d_sigr_exponent_r=0.8,
            step5d_qdot_limit_rad_s=0.5,
        )
        expected = {"ok": True, "program": STEP5D_ABLATION_V35_STAGE_ID}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "current_stage.json").write_text(
                json.dumps(current), encoding="utf-8"
            )
            with patch.object(bridge, "verify_v35_evidence_freeze", return_value=expected) as verify:
                result = bridge.require_v29_live_bridge_authorization(args, root=root)
        self.assertEqual(result, expected)
        verify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
