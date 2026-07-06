#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_no_contact_p0 as p0  # noqa: E402


P0_FIELDS = [
    "t_monotonic_s",
    "ur_output_double_register_35",
    "_step4e_normal_load_n",
    "force_norm_n",
    "_step5d_stage25_control_mode",
    "_step5d_stage25_echo_consumed",
    "_step5d_intervention_reason",
    "_step5d_outer_xdot_limited_approach_normal_m_s",
    "_step5d_jqdot_raw_approach_normal_m_s",
    "_step5d_jqdot_cmd_approach_normal_m_s",
    "_step5d_constraint_residual_norm",
    "_step5d_lambda_norm",
    "_step5d_active_bounds_count",
]


def write_p0_run(run_dir: Path, rows: list[dict[str, str]]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "bridge_rtde_500hz.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=P0_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def good_rows() -> list[dict[str, str]]:
    rows = []
    for idx in range(4):
        rows.append(
            {
                "t_monotonic_s": f"{1.0 + 0.002 * idx:.6f}",
                "ur_output_double_register_35": "25.0",
                "_step4e_normal_load_n": "0.300000",
                "force_norm_n": "0.800000",
                "_step5d_stage25_control_mode": "speedj_rnn_live",
                "_step5d_stage25_echo_consumed": "1",
                "_step5d_intervention_reason": "solver_warm_start" if idx == 0 else "none",
                "_step5d_outer_xdot_limited_approach_normal_m_s": "0.000100000",
                "_step5d_jqdot_raw_approach_normal_m_s": "0.000095000",
                "_step5d_jqdot_cmd_approach_normal_m_s": "0.000094000",
                "_step5d_constraint_residual_norm": "0.000020000",
                "_step5d_lambda_norm": "0.012000000",
                "_step5d_active_bounds_count": "0",
            }
        )
    return rows


class Step5dNoContactP0Test(unittest.TestCase):
    def test_passes_when_first_speedj_rnn_tick_has_warm_start_and_same_normal_sign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, good_rows())

            result = p0.verify_run_dir(run_dir)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["first_tick"]["intervention_reason"], "solver_warm_start")
        self.assertGreater(result["first_tick"]["jqdot_raw_approach_normal_m_s"], 0.0)
        self.assertEqual(result["blockers"], [])

    def test_fails_without_solver_warm_start_on_first_speedj_rnn_tick(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_intervention_reason"] = "none"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_solver_warm_start", result["blockers"])

    def test_fails_when_raw_jqdot_unloads_while_outer_presses(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_jqdot_raw_approach_normal_m_s"] = "-0.000300000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_press_unload_mismatch", result["blockers"])

    def test_fails_when_outer_command_is_not_pressing(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_outer_xdot_limited_approach_normal_m_s"] = "0.000000000"
        rows[0]["_step5d_jqdot_raw_approach_normal_m_s"] = "0.000000000"
        rows[0]["_step5d_jqdot_cmd_approach_normal_m_s"] = "0.000000000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_outer_not_pressing", result["blockers"])

    def test_fails_when_cmd_jqdot_unloads_while_outer_presses(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_jqdot_cmd_approach_normal_m_s"] = "-0.000300000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_cmd_press_unload_mismatch", result["blockers"])

    def test_fails_when_speedj_rnn_rows_are_not_stage25(self) -> None:
        rows = good_rows()
        for row in rows:
            row["ur_output_double_register_35"] = "24.0"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("no_stage25_speedj_rnn_live_rows", result["blockers"])

    def test_fails_when_any_speedj_rnn_row_is_outside_stage25(self) -> None:
        rows = good_rows()
        rows.append(good_rows()[0] | {"ur_output_double_register_35": "24.0"})
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("non_stage25_speedj_rnn_live_rows_present", result["blockers"])

    def test_fails_when_first_speedj_rnn_tick_was_not_consumed(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_stage25_echo_consumed"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_not_consumed_by_stage25", result["blockers"])

    def test_fails_when_first_speedj_rnn_tick_consumed_evidence_is_fractional(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_stage25_echo_consumed"] = "0.6"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_not_consumed_by_stage25", result["blockers"])

    def test_fails_when_run_is_not_no_contact(self) -> None:
        rows = good_rows()
        rows[0]["_step4e_normal_load_n"] = "5.100000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("normal_load_exceeds_no_contact_limit", result["blockers"])

    def test_fails_when_first_load_or_force_evidence_is_nonfinite(self) -> None:
        rows = good_rows()
        rows[0]["_step4e_normal_load_n"] = "nan"
        rows[0]["force_norm_n"] = "nan"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_normal_load_evidence", result["blockers"])
        self.assertIn("first_speedj_rnn_tick_missing_force_norm_evidence", result["blockers"])

    def test_fails_when_first_lambda_evidence_is_nonfinite(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_lambda_norm"] = "nan"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_lambda_norm", result["blockers"])

    def test_fails_when_active_bounds_evidence_is_fractional(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_active_bounds_count"] = "0.4"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_active_bounds_count", result["blockers"])

    def test_cli_writes_summary_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            summary = Path(tmp) / "p0_summary.json"
            write_p0_run(run_dir, good_rows())

            completed = subprocess.run(
                [
                    "python3",
                    str(ROOT / "tools" / "verify_step5d_no_contact_p0.py"),
                    str(run_dir),
                    "--output",
                    str(summary),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue(summary.exists())
            self.assertTrue(json.loads(summary.read_text(encoding="utf-8"))["ok"])

    def test_cli_returns_24_when_artifact_fails(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_jqdot_cmd_approach_normal_m_s"] = "-0.000300000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            write_p0_run(run_dir, rows)

            completed = subprocess.run(
                [
                    "python3",
                    str(ROOT / "tools" / "verify_step5d_no_contact_p0.py"),
                    str(run_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 24, completed.stdout + completed.stderr)
            self.assertIn("first_speedj_rnn_tick_cmd_press_unload_mismatch", completed.stdout)

    def test_operator_script_does_not_start_contact_bridge(self) -> None:
        script = (ROOT / "scripts" / "step5d-strict-rnn-p0.sh").read_text(encoding="utf-8")

        self.assertIn("validate-run", script)
        self.assertIn("live-ready", script)
        self.assertIn("verify_step5d_no_contact_p0.py", script)
        self.assertNotIn("step5d_no_contact_p0_summary", script)
        self.assertNotIn("contact-bridge", script)


if __name__ == "__main__":
    unittest.main()
