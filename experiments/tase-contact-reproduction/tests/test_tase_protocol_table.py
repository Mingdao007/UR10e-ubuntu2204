#!/usr/bin/env python3
"""Tests for the paper-level TASE protocol table."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import tase_protocol_table as protocol  # noqa: E402


class TaseProtocolTableTest(unittest.TestCase):
    def test_step5_and_step6_profiles_resolve_shared_parameters(self) -> None:
        step5 = protocol.resolve_experiment_profile("Step5.contact_cycloid", root=ROOT)
        self.assertEqual(step5["step"], "Step5")
        self.assertEqual(step5["task"], "contact_cycloid")
        self.assertEqual(step5["parameters"]["trajectory_duration_s"], 60.0)
        self.assertEqual(step5["parameters"]["diagnostic_window_s"], 10.0)
        self.assertEqual(step5["parameters"]["normal_filter_alpha"], 0.55)
        self.assertEqual(step5["safety_limits"]["total_linear_limit_m_s"], 0.004)
        self.assertEqual(step5["evidence"]["source_profile"], "step5b_v3_success")

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

    def test_step5d_runtime_is_external_instead_of_duplicated_in_table(self) -> None:
        table = protocol.load_protocol_table(ROOT)
        remote = table["external_runtime_profiles"]["Step5d.remote_r012"]
        self.assertEqual(remote["entrypoint"], "step5d_remote_control.sh")
        self.assertEqual(
            remote["parameter_selector"],
            "config/step5d_remote/current.json",
        )
        self.assertFalse(remote["duplicate_inline_parameters"])

    def test_operator_env_cli_exposes_shell_defaults(self) -> None:
        env_text = subprocess.check_output(
            [sys.executable, str(ROOT / "tools" / "tase_protocol_table.py"), "operator-env", "step6b-v2-contact"],
            cwd=ROOT,
            text=True,
        )
        self.assertIn("TASE_STEP6B_TARGET_FORCE_N=5", env_text)
        self.assertIn("TASE_STEP6B_TOTAL_LINEAR_LIMIT_M_S=0.015", env_text)
        self.assertIn("TASE_STEP6B_NORMAL_FILTER_ALPHA=0.35", env_text)

    def test_operator_scripts_seed_defaults_from_protocol_cli(self) -> None:
        for relpath in (
            "scripts/step5b-contact-operator.sh",
            "scripts/step6b-contact-operator.sh",
            "scripts/step6b-v2-contact-operator.sh",
        ):
            text = (ROOT / relpath).read_text(encoding="utf-8")
            self.assertIn("tase_protocol_table.py\" operator-env", text, relpath)


if __name__ == "__main__":
    unittest.main()
