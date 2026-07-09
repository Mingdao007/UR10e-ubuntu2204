#!/usr/bin/env python3
"""Tests for Step5d live-prep runtime interface defaults."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
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

    def test_v29_is_contact_strict_rnn_live_candidate_with_v28_contact_envelope(self) -> None:
        runtime = iface.resolve_runtime_interface(program=iface.STEP5D_ABLATION_V29_STAGE_ID, root=ROOT, env={})

        self.assertEqual(iface.STEP5D_ABLATION_V29_STAGE_ID, "step5d_strict_rnn_ablation_v29")
        self.assertIn(iface.STEP5D_ABLATION_V29_STAGE_ID, iface.STEP5D_ABLATION_STAGE_IDS)
        self.assertEqual(runtime.stage25_control_mode, "speedj_rnn_live")
        self.assertEqual(runtime.preload_gate.filtered_min_n, 5.0)
        self.assertEqual(runtime.preload_gate.filtered_max_n, 22.0)
        self.assertEqual(runtime.preload_gate.raw_min_n, 3.0)
        self.assertEqual(runtime.preload_gate.raw_max_n, 25.0)
        self.assertEqual(runtime.preload_gate.force_norm_max_n, 35.0)
        self.assertEqual(runtime.bridge_defaults.target_force_n, 12.0)
        self.assertEqual(runtime.bridge_defaults.max_normal_force_n, 50.0)
        self.assertEqual(runtime.bridge_defaults.max_force_norm_n, 60.0)
        self.assertEqual(runtime.bridge_defaults.max_torque_norm_nm, 3.0)
        self.assertEqual(runtime.bridge_defaults.angular_limit_rad_s, 0.015)
        self.assertEqual(runtime.hard_contract["stage25_live_control_source"], "strict_rnn_live_speedj")
        self.assertEqual(runtime.hard_contract["stage25_success_target_s"], 60.0)
        self.assertEqual(runtime.hard_contract["stage25_runtime_limit_s"], 65.0)
        self.assertFalse(runtime.hard_contract["no_contact_p0_capture"])
        self.assertIn("v29", runtime.register_contract["stage25_0"])
        self.assertIn("strict RNN live", runtime.register_contract["stage25_0"])
        self.assertIn("layout 524", runtime.register_contract["stage25_0"])

    def test_live_ready_without_authorization_never_claims_live_bridge(self) -> None:
        runtime = iface.resolve_runtime_interface(program=iface.STEP5D_ABLATION_V29_STAGE_ID, root=ROOT, env={})

        lines = iface.live_ready_lines(runtime, {"state": "HIT", "age_s": 60.0, "ttl_s": 7200.0, "fingerprint_ok": True})

        joined = "\n".join(lines)
        self.assertIn("[step5d][phase=liveprep-blocked][rebuild=no][upload=no]", lines)
        self.assertNotIn("phase=live-bridge", joined)
        self.assertNotIn("TP Play wait", joined)

    def test_live_ready_reports_awaiting_authorization_only_after_offline_readiness(self) -> None:
        runtime = iface.resolve_runtime_interface(program=iface.STEP5D_ABLATION_V29_STAGE_ID, root=ROOT, env={})
        readiness = {
            "schema_version": "step5d_liveprep_readiness_v1",
            "program": iface.STEP5D_ABLATION_V29_STAGE_ID,
            "workflow_state": "awaiting_live_authorization",
            "ready_for_explicit_live_authorization": True,
            "blockers": [],
        }

        lines = iface.live_ready_lines(
            runtime,
            {"state": "HIT", "age_s": 60.0, "ttl_s": 7200.0, "fingerprint_ok": True},
            readiness=readiness,
        )

        joined = "\n".join(lines)
        self.assertIn("[step5d][phase=awaiting-live-authorization][rebuild=no][upload=no]", lines)
        self.assertNotIn("phase=live-bridge", joined)
        self.assertNotIn("TP Play wait", joined)

    def test_v29_live_ready_reports_readback_blocked_until_controller_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = json.loads(table_path.read_text(encoding="utf-8"))
            row = next(row for row in table["stages"] if row.get("id") == iface.STEP5D_ABLATION_V29_STAGE_ID)
            row["acceptance"]["controller_readback_verified"] = False
            row["local_delivery_evidence"]["controller_readback_verified"] = False
            row["package_delivery"]["controller_readback_verified"] = False
            table_path.write_text(json.dumps(table), encoding="utf-8")
            runtime = iface.resolve_runtime_interface(program=iface.STEP5D_ABLATION_V29_STAGE_ID, root=tmp_root, env={})

        lines = iface.live_ready_lines(runtime, {"state": "HIT", "age_s": 60.0, "ttl_s": 7200.0, "fingerprint_ok": True})

        joined = "\n".join(lines)
        self.assertIn("[step5d][phase=readback-blocked][rebuild=no][upload=required]", lines)
        self.assertIn("controller read-back required before TP handoff", joined)
        self.assertNotIn("TP Play wait", joined)
        self.assertNotIn("phase=live-bridge", joined)

    def test_v29_runtime_profile_is_pinned_in_interface_evidence(self) -> None:
        runtime = iface.resolve_runtime_interface(program=iface.STEP5D_ABLATION_V29_STAGE_ID, root=ROOT, env={})

        self.assertEqual(
            runtime.hard_contract.get("runtime_profile"),
            {
                "backend": "cupy",
                "inner_iterations": 1024,
                "epsilon": 0.010,
                "sigr_exponent_r": 0.8,
                "qdot_cap_rad_s": 0.05,
                "control_mode": "speedj_rnn_live",
                "joint_layout_code": 524.0,
            },
        )

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
