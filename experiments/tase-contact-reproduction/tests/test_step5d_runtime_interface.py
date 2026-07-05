#!/usr/bin/env python3
"""Tests for Step5d live-prep runtime interface defaults."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_runtime_interface as iface  # noqa: E402


class Step5dRuntimeInterfaceTest(unittest.TestCase):
    def test_v24_defaults_are_bridge_tunable(self) -> None:
        runtime = iface.resolve_runtime_interface(program=iface.STEP5D_LIVEPREP_V24_STAGE_ID, root=ROOT, env={})

        self.assertEqual(runtime.program, iface.STEP5D_LIVEPREP_V24_STAGE_ID)
        self.assertEqual(iface.STEP5D_TUNING_BUNDLE, "v24_startup_quarantine_rnn_tracking_guard")
        self.assertEqual(runtime.preload_gate.filtered_min_n, 7.5)
        self.assertEqual(runtime.preload_gate.filtered_max_n, 14.0)
        self.assertEqual(runtime.preload_gate.raw_min_n, 7.0)
        self.assertEqual(runtime.preload_gate.raw_max_n, 15.0)
        self.assertEqual(runtime.preload_gate.hold_s, 0.100)
        self.assertEqual(runtime.preload_gate.recovery_normal_load_max_n, 20.0)
        self.assertEqual(runtime.preload_gate.force_norm_stop_n, 25.0)
        self.assertEqual(runtime.bridge_defaults.max_normal_force_n, 25.0)
        self.assertEqual(runtime.bridge_defaults.max_force_norm_n, 25.0)
        self.assertEqual(runtime.line_entry_param_valid_code, 521.0)
        self.assertEqual(iface.STEP5D_QDOT_CLEAR_STAGE, 25.95)
        self.assertEqual(iface.STEP5D_QDOT_CLEAR_ACK_CYCLES, 3)
        self.assertEqual(iface.STEP5D_QDOT_CLEAR_ZERO_TOL_RAD_S, 0.0005)
        self.assertIn("near-zero", runtime.register_contract["stage25_95"])

    def test_ablation_defaults_split_v25_speedl_and_v26_strict_rnn(self) -> None:
        v25 = iface.resolve_runtime_interface(program=iface.STEP5D_ABLATION_V25_STAGE_ID, root=ROOT, env={})
        v26 = iface.resolve_runtime_interface(program=iface.STEP5D_ABLATION_V26_STAGE_ID, root=ROOT, env={})

        self.assertEqual(v25.stage25_control_mode, "speedl_cartesian_oracle")
        self.assertEqual(v25.bridge_defaults.angular_limit_rad_s, 0.150)
        self.assertEqual(v25.preload_gate.filtered_min_n, 10.5)
        self.assertEqual(v25.preload_gate.raw_max_n, 13.5)
        self.assertEqual(v26.stage25_control_mode, "speedl_cartesian_oracle")
        self.assertEqual(v26.bridge_defaults.angular_limit_rad_s, 0.015)
        self.assertEqual(v26.preload_gate.filtered_min_n, 7.0)
        self.assertEqual(v26.preload_gate.filtered_max_n, 18.0)
        self.assertEqual(v26.preload_gate.raw_min_n, 5.0)
        self.assertEqual(v26.preload_gate.raw_max_n, 20.0)
        self.assertEqual(v26.preload_gate.recovery_normal_load_max_n, 24.0)
        self.assertIn("v25/v26", v26.register_contract["stage25_0"])

    def test_v27_defaults_to_step5b_envelope_with_step5d_shadow(self) -> None:
        runtime = iface.resolve_runtime_interface(program=iface.STEP5D_ABLATION_V27_STAGE_ID, root=ROOT, env={})

        self.assertIn(iface.STEP5D_ABLATION_V27_STAGE_ID, iface.STEP5D_ABLATION_STAGE_IDS)
        self.assertEqual(runtime.stage25_control_mode, "speedl_cartesian_oracle")
        self.assertEqual(runtime.preload_gate.filtered_min_n, 5.0)
        self.assertEqual(runtime.preload_gate.filtered_max_n, 22.0)
        self.assertEqual(runtime.preload_gate.raw_min_n, 3.0)
        self.assertEqual(runtime.preload_gate.raw_max_n, 25.0)
        self.assertEqual(runtime.preload_gate.force_norm_max_n, 35.0)
        self.assertEqual(runtime.preload_gate.recovery_normal_load_max_n, 35.0)
        self.assertEqual(runtime.preload_gate.force_norm_stop_n, 35.0)
        self.assertEqual(runtime.bridge_defaults.max_normal_force_n, 50.0)
        self.assertEqual(runtime.bridge_defaults.max_force_norm_n, 60.0)
        self.assertEqual(runtime.bridge_defaults.max_torque_norm_nm, 3.0)
        self.assertEqual(runtime.bridge_defaults.angular_limit_rad_s, 0.015)
        self.assertEqual(runtime.bridge_defaults.rezero_s, 0.25)
        self.assertIn("v25/v26/v27", runtime.register_contract["stage25_0"])
        self.assertIn("step5b_speedl_live_step5d_shadow", runtime.register_contract["stage25_0"])
        self.assertEqual(runtime.hard_contract["stage25_cadence_max_gap_s"], 0.020)
        self.assertEqual(runtime.hard_contract["stage25_live_control_source"], "step5b_speedl_live_step5d_shadow")

    def test_v28_is_60s_full_run_package_with_v27_live_source_boundary(self) -> None:
        runtime = iface.resolve_runtime_interface(program=iface.STEP5D_ABLATION_V28_STAGE_ID, root=ROOT, env={})

        self.assertIn(iface.STEP5D_ABLATION_V28_STAGE_ID, iface.STEP5D_ABLATION_STAGE_IDS)
        self.assertEqual(runtime.stage25_control_mode, "speedl_cartesian_oracle")
        self.assertEqual(runtime.preload_gate.filtered_min_n, 5.0)
        self.assertEqual(runtime.preload_gate.filtered_max_n, 22.0)
        self.assertEqual(runtime.preload_gate.raw_min_n, 3.0)
        self.assertEqual(runtime.preload_gate.raw_max_n, 25.0)
        self.assertEqual(runtime.preload_gate.force_norm_max_n, 35.0)
        self.assertEqual(runtime.bridge_defaults.max_normal_force_n, 50.0)
        self.assertEqual(runtime.bridge_defaults.max_force_norm_n, 60.0)
        self.assertEqual(runtime.bridge_defaults.max_torque_norm_nm, 3.0)
        self.assertEqual(runtime.bridge_defaults.angular_limit_rad_s, 0.015)
        self.assertEqual(runtime.hard_contract["stage25_live_control_source"], "step5b_speedl_live_step5d_shadow")
        self.assertEqual(runtime.hard_contract["stage25_success_target_s"], 60.0)
        self.assertEqual(runtime.hard_contract["stage25_runtime_limit_s"], 65.0)

    def test_live_ready_reports_actual_baseline_plus_rezero_budget(self) -> None:
        runtime = iface.resolve_runtime_interface(program=iface.STEP5D_ABLATION_V27_STAGE_ID, root=ROOT, env={})

        lines = iface.live_ready_lines(runtime, {"state": "HIT", "age_s": 60.0, "ttl_s": 7200.0, "fingerprint_ok": True})

        self.assertIn("[next] short checks ETA=1-3s, TP Play wait<=20s, baseline+rezero=5.25s", lines)

    def test_step5d_env_overrides_use_step5d_namespace(self) -> None:
        runtime = iface.resolve_runtime_interface(
            program=iface.STEP5D_LIVEPREP_V24_STAGE_ID,
            root=ROOT,
            env={
                "STEP5D_PRELOAD_FILTERED_MIN_N": "6.5",
                "STEP5D_PRELOAD_FILTERED_MAX_N": "13.5",
                "STEP5D_FORCE_DAMPING": "8.5",
                "STEP5D_NORMAL_FILTER_ALPHA": "0.6",
            },
        )

        self.assertEqual(runtime.preload_gate.filtered_min_n, 6.5)
        self.assertEqual(runtime.preload_gate.filtered_max_n, 13.5)
        self.assertEqual(runtime.bridge_defaults.force_damping, 8.5)
        self.assertEqual(runtime.bridge_defaults.normal_filter_alpha, 0.6)


if __name__ == "__main__":
    unittest.main()
