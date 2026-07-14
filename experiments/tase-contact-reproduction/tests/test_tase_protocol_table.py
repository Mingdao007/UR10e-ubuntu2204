#!/usr/bin/env python3
"""Tests for the paper-level TASE protocol table."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_runtime_interface as step5d_iface  # noqa: E402
import tase_protocol_table as protocol  # noqa: E402


class TaseProtocolTableTest(unittest.TestCase):
    def test_step5_and_step6_profiles_resolve_shared_parameters(self) -> None:
        step5 = protocol.resolve_experiment_profile("Step5.step5d_rnn", root=ROOT)
        self.assertEqual(step5["step"], "Step5")
        self.assertEqual(step5["task"], "contact_cycloid")
        self.assertEqual(step5["flow"]["zero_timing"], "after_far_search_before_near_search")
        self.assertEqual(step5["parameters"]["trajectory_duration_s"], 60.0)
        self.assertEqual(step5["parameters"]["diagnostic_window_s"], 10.0)
        self.assertEqual(step5["parameters"]["zero_hold_s"], 1.0)
        self.assertEqual(step5["parameters"]["normal_filter_alpha"], 0.55)
        self.assertEqual(step5["safety_limits"]["total_linear_limit_m_s"], 1.0)
        self.assertEqual(step5["evidence"]["source_profile"], "step5b_v3_success")

        legacy = protocol.resolve_experiment_profile("Step5.step5d_rnn_legacy_v27", root=ROOT)
        self.assertEqual(legacy["parameters"]["zero_hold_s"], 0.25)
        self.assertEqual(legacy["safety_limits"]["total_linear_limit_m_s"], 0.004)

        step6 = protocol.resolve_experiment_profile("Step6.contact_eight", root=ROOT)
        self.assertEqual(step6["step"], "Step6")
        self.assertEqual(step6["task"], "contact_eight")
        self.assertEqual(step6["parameters"]["trajectory_duration_s"], 30.0)
        self.assertEqual(step6["parameters"]["target_force_n"], 5.0)
        self.assertEqual(step6["parameters"]["normal_filter_alpha"], 0.35)
        self.assertEqual(step6["safety_limits"]["total_linear_limit_m_s"], 0.015)
        self.assertEqual(step6["safety_limits"]["angular_limit_rad_s"], 0.060)

    def test_validator_accepts_current_protocol_table(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "validate_tase_protocol_table.py"), "--json"],
            cwd=ROOT,
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"ok": true', result.stdout)

    def test_step5d_runtime_defaults_come_from_protocol_table(self) -> None:
        profile = protocol.resolve_experiment_profile("Step5.step5d_rnn", root=ROOT)
        runtime = step5d_iface.resolve_runtime_interface(
            program=step5d_iface.STEP5D_ABLATION_V32_STAGE_ID,
            root=ROOT,
            env={},
        )

        self.assertEqual(runtime.bridge_defaults.target_force_n, profile["parameters"]["target_force_n"])
        self.assertEqual(runtime.bridge_defaults.normal_filter_alpha, profile["parameters"]["normal_filter_alpha"])
        self.assertEqual(runtime.bridge_defaults.rezero_s, profile["parameters"]["zero_hold_s"])
        self.assertEqual(runtime.bridge_defaults.total_linear_limit_m_s, profile["safety_limits"]["total_linear_limit_m_s"])
        self.assertEqual(runtime.bridge_defaults.angular_limit_rad_s, profile["safety_limits"]["angular_limit_rad_s"])
        self.assertEqual(runtime.bridge_defaults.max_force_norm_n, profile["safety_limits"]["force_norm_guard_n"])

    def test_operator_env_cli_exposes_shell_defaults(self) -> None:
        env_text = subprocess.check_output(
            [sys.executable, str(ROOT / "tools" / "tase_protocol_table.py"), "operator-env", "step6b-v2-contact"],
            cwd=ROOT,
            text=True,
        )
        self.assertIn("TASE_STEP6B_TARGET_FORCE_N=5.0", env_text)
        self.assertIn("TASE_STEP6B_TOTAL_LINEAR_LIMIT_M_S=0.015", env_text)
        self.assertIn("TASE_STEP6B_NORMAL_FILTER_ALPHA=0.35", env_text)

    def test_operator_scripts_seed_defaults_from_protocol_cli(self) -> None:
        for relpath in (
            "scripts/step5d-liveprep-operator.sh",
            "scripts/step5b-contact-operator.sh",
            "scripts/step6b-contact-operator.sh",
            "scripts/step6b-v2-contact-operator.sh",
        ):
            text = (ROOT / relpath).read_text(encoding="utf-8")
            self.assertIn("tase_protocol_table.py\" operator-env", text, relpath)


if __name__ == "__main__":
    unittest.main()
