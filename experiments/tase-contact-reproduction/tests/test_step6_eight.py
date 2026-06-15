#!/usr/bin/env python3
"""Offline checks for Step6 8-shaped waypoint calibration and package generation."""

from __future__ import annotations

import gzip
import math
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step6_eight_safe_frame as safe_frame  # noqa: E402
import build_step6a_eight_no_contact as step6a  # noqa: E402
import build_step6b_contact as step6b  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
import step6_eight  # noqa: E402
import upload_ur_tp_package  # noqa: E402


class Step6EightTest(unittest.TestCase):
    def test_step6_table_contains_step6a_and_active_step6b_contact(self) -> None:
        table = step6_eight.load_step6_table()
        stages = {stage["id"]: stage for stage in table["stages"]}
        self.assertIn("step6a_eight_no_contact_v1", stages)
        self.assertIn("step6_contact_eight_baseline_v1", stages)
        self.assertIn("step6_contact_eight_baseline_v2", stages)
        self.assertFalse(stages["step6a_eight_no_contact_v1"]["active"])
        self.assertFalse(stages["step6_contact_eight_baseline_v1"]["active"])
        active_contact = [
            stage
            for stage in table["stages"]
            if stage.get("active") and stage.get("contact") is True and stage.get("bridge") is True
        ]
        self.assertEqual([stage["id"] for stage in active_contact], ["step6_contact_eight_baseline_v2"])
        self.assertEqual(active_contact[0]["program"], step6b.PROGRAM_NAME)
        self.assertEqual(active_contact[0]["bridge_profile"]["step4e_version"], "step6b_v2")
        self.assertEqual(active_contact[0]["bridge_limits"]["motion_limit_m_s"], 0.015)
        self.assertEqual(active_contact[0]["bridge_limits"]["total_linear_limit_m_s"], 0.015)
        self.assertEqual(active_contact[0]["bridge_limits"]["angular_limit_rad_s"], 0.06)

    def test_waypoint_guide_uses_five_named_points(self) -> None:
        rows = step6_eight.waypoint_rows()
        self.assertEqual(
            [row["label"] for row in rows],
            ["center_start", "right_upper", "right_lower", "left_upper", "left_lower"],
        )
        self.assertAlmostEqual(rows[1]["t_s"], (math.pi / 4.0) / 0.2, places=12)
        self.assertAlmostEqual(rows[1]["local_x_m"], 0.04 / math.sqrt(2.0), places=12)
        self.assertAlmostEqual(rows[1]["local_y_m"], 0.01, places=12)

        html = step6_eight.build_waypoint_guide_html({"program": "test", "formula": "formula", "waypoints": rows, "draft_points": [], "note": "note"})
        self.assertIn("Step6 8-Shaped Waypoint Guide", html)
        self.assertIn("right_upper", html)
        self.assertIn("left_lower", html)

    def test_safe_frame_from_synthetic_five_points_passes_guard(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step6_eight_test_") as tmp:
            points_path = safe_frame.synthetic_session(Path(tmp))
            payload = safe_frame.make_payload(
                points_path,
                guard_line_x_m=0.4888784335298146,
                residual_threshold_mm=1.0,
            )
        self.assertEqual(payload["status"], "ok")
        self.assertLess(payload["max_residual_mm"], 1e-9)
        self.assertTrue(payload["guard"]["passed"])
        self.assertTrue(payload["policy"]["no_scale"])
        self.assertAlmostEqual(payload["fixed_z"]["fixed_base_z_m"], 0.034, places=12)
        self.assertEqual(payload["policy"]["xyz_policy"], "TCP x/y/z are geometry inputs; force and rx/ry/rz are logged only and ignored by the fit")

    def test_step6a_package_has_no_contact_policy_and_fresh_cached_contents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step6_eight_test_") as tmp:
            points_path = safe_frame.synthetic_session(Path(tmp))
            frame_payload = safe_frame.make_payload(
                points_path,
                guard_line_x_m=0.4888784335298146,
                residual_threshold_mm=1.0,
            )
        metrics = step6a.build_metrics(frame_payload)
        geom = step6a.line_cfg(step6a.load_json(step6a.CONFIG_PATH))
        stamp = "2026-06-12T2130HKT_STEP6A_EIGHT_NO_CONTACT_V1"
        script = step6a.build_script(stamp, "2026-06-12T21:30:00+08:00", geom, metrics)
        txt = step6a.build_txt(stamp, metrics)
        urp = step6a.build_urp(script, step6a.PROGRAM_NAME, step6a.CONTROLLER_DIR)
        step6a.validate_package(script, txt, urp, stamp, metrics)
        xml = gzip.decompress(urp).decode("utf-8")

        self.assertIn("STEP6_FLOW.md", script)
        self.assertIn("STEP6_WAYPOINTS", script)
        self.assertIn("local fixed_base_z_m = 0.034000000", script)
        self.assertIn("local path_duration_s = 30.000", script)
        self.assertIn("STEP6_TIMING", script)
        self.assertIn("local fast_hold_s = 0.001", script)
        self.assertIn("t = t + get_steptime()", script)
        self.assertNotIn("t = t + hold_s", script)
        self.assertIn("along=0.04*sin(0.2t)", script)
        self.assertIn("lateral=0.01*sin(0.4t)", script)
        self.assertNotIn("read_input_float_register", script)
        self.assertNotIn("force_mode", script)
        self.assertNotIn("zero_ftsensor()", script.replace("no zero_ftsensor()", ""))
        self.assertNotIn("step4g_eight_seed_normal_v1", script)
        self.assertIn(f'URProgram name="{step6a.PROGRAM_NAME}"', xml)
        self.assertIn(f'directory="{step6a.CONTROLLER_DIR}"', xml)
        self.assertIn(f"{step6a.CONTROLLER_DIR}/{step6a.PROGRAM_NAME}.script", xml)
        self.assertIn(stamp, xml)

    def test_upload_validator_accepts_step6a_triplet(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step6_eight_test_") as tmp:
            tmp_path = Path(tmp)
            points_path = safe_frame.synthetic_session(tmp_path / "points")
            frame_payload = safe_frame.make_payload(
                points_path,
                guard_line_x_m=0.4888784335298146,
                residual_threshold_mm=1.0,
            )
            metrics = step6a.build_metrics(frame_payload)
            geom = step6a.line_cfg(step6a.load_json(step6a.CONFIG_PATH))
            stamp = "2026-06-12T2131HKT_STEP6A_EIGHT_NO_CONTACT_V1"
            script = step6a.build_script(stamp, "2026-06-12T21:31:00+08:00", geom, metrics)
            txt = step6a.build_txt(stamp, metrics)
            urp = step6a.build_urp(script, step6a.PROGRAM_NAME, step6a.CONTROLLER_DIR)
            files = {
                ".script": tmp_path / f"{step6a.PROGRAM_NAME}.script",
                ".txt": tmp_path / f"{step6a.PROGRAM_NAME}.txt",
                ".urp": tmp_path / f"{step6a.PROGRAM_NAME}.urp",
            }
            files[".script"].write_text(script, encoding="utf-8")
            files[".txt"].write_text(txt, encoding="utf-8")
            files[".urp"].write_bytes(urp)

            result = upload_ur_tp_package.validate_package(
                files,
                step6a.PROGRAM_NAME,
                step6a.CONTROLLER_DIR,
                require_exact_cached_script=True,
            )
        self.assertEqual(result["program"], step6a.PROGRAM_NAME)
        self.assertEqual(result["target_dir"], step6a.CONTROLLER_DIR)

    def test_step6b_v2_feasibility_gate_passes_and_legacy_limits_fail(self) -> None:
        frame = step6_eight.load_safe_frame()
        v2_metrics = step6b.assert_feasible(frame, step6b.variant_config("v2"))
        self.assertAlmostEqual(v2_metrics["reference_speed_max_m_s"], 0.00894427190999916, places=12)
        self.assertAlmostEqual(v2_metrics["required_total_linear_m_s"], math.hypot(0.00894427190999916, 0.003), places=12)
        self.assertLess(v2_metrics["required_total_linear_m_s"], 0.015)
        self.assertGreaterEqual(v2_metrics["orientation_capacity_rad"], v2_metrics["orientation_required_rad"])
        with self.assertRaisesRegex(RuntimeError, "Step6b profile infeasible"):
            step6b.assert_feasible(frame, step6b.variant_config("v1"))

    def test_step6b_package_has_contact_bridge_contract_and_fresh_cached_contents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step6_eight_test_") as tmp:
            points_path = safe_frame.synthetic_session(Path(tmp))
            frame_payload = safe_frame.make_payload(
                points_path,
                guard_line_x_m=0.4888784335298146,
                residual_threshold_mm=1.0,
            )
        geom = step6b.line_cfg(step6b.load_json(step6b.CONFIG_PATH))
        variant = step6b.variant_config("v2")
        metrics = step6b.assert_feasible(frame_payload, variant)
        stamp = "2026-06-12T2230HKT_STEP6B_CONTACT_EIGHT_BASELINE_V2"
        script = step6b.build_script(stamp, "2026-06-12T22:30:00+08:00", geom, frame_payload, variant, metrics)
        txt = step6b.build_txt(stamp, variant, metrics)
        urp = step6b.build_urp(script, step6b.PROGRAM_NAME, step6b.CONTROLLER_DIR)
        step6b.validate_package(script, txt, urp, stamp, variant)
        xml = gzip.decompress(urp).decode("utf-8")

        self.assertIn("STEP6_FLOW.md", script)
        self.assertIn("STEP6_TABLE_SOURCE: config/step6_stage_table.json", script)
        self.assertIn("STEP6_SAFE_FRAME_SOURCE: config/step6_eight_safe_frame.json", script)
        self.assertIn("step4e-version=step6b_v2", script)
        self.assertIn("--step4e-version step6b_v2", txt)
        self.assertIn("--step4e-path-shape eight", txt)
        self.assertIn("--target-force-n 5.0", script)
        self.assertIn("step4e-motion-limit-m-s=0.015", script)
        self.assertIn("step4e-total-linear-limit-m-s=0.015", script)
        self.assertIn("step4e-angular-limit-rad-s=0.060", script)
        self.assertIn("Offline feasibility:", txt)
        self.assertIn("local line_runtime_limit_s = 35.000", script)
        self.assertIn("local line_success_progress_m = 30.000000000", script)
        self.assertIn("first-contact normal latch", script)
        self.assertIn("25.2 attitude correction", script)
        self.assertIn("25.3 line-entry gate", script)
        self.assertNotIn("step4g_eight_seed_normal_v1", script)
        self.assertNotIn("step5b_contact_cycloid_baseline_v1", script)
        self.assertIn(f'URProgram name="{step6b.PROGRAM_NAME}"', xml)
        self.assertIn(f'directory="{step6b.CONTROLLER_DIR}"', xml)
        self.assertIn(f"{step6b.CONTROLLER_DIR}/{step6b.PROGRAM_NAME}.script", xml)
        self.assertIn(stamp, xml)

    def test_bridge_accepts_step6b_profile_name(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step6b_v2",
                "--step4e-path-shape",
                "eight",
                "--step4e-motion-limit-m-s",
                "0.015",
                "--step4e-total-linear-limit-m-s",
                "0.015",
                "--step4e-angular-limit-rad-s",
                "0.060",
            ]
        )
        self.assertEqual(args.step4e_version, "step6b_v2")
        self.assertEqual(args.step4e_path_shape, "eight")
        self.assertEqual(args.step4e_motion_limit_m_s, 0.015)
        self.assertEqual(args.step4e_total_linear_limit_m_s, 0.015)
        self.assertEqual(args.step4e_angular_limit_rad_s, 0.060)

    def test_step6b_bridge_path_reference_uses_step6_safe_frame(self) -> None:
        frame = step6_eight.load_safe_frame()
        origin = tuple(float(v) for v in frame["basis"]["origin_xy_m"])
        u_along = tuple(float(v) for v in frame["basis"]["u_along_xy"])
        p_lateral = tuple(float(v) for v in frame["basis"]["p_lateral_xy"])
        end_ref = bridge.step6_contact_path_reference(origin, 30.0, stage_id="step6_contact_eight_baseline_v2")
        local_end = end_ref["local"]
        self.assertEqual(end_ref["stage_id"], "step6_contact_eight_baseline_v2")
        self.assertAlmostEqual(end_ref["progress"], 30.0, places=12)
        self.assertAlmostEqual(local_end["local_x_m"], 0.04 * math.sin(6.0), places=12)
        self.assertAlmostEqual(local_end["local_y_m"], 0.01 * math.sin(12.0), places=12)

        samples = [
            bridge.step6_contact_path_reference(origin, idx * 0.1, stage_id="step6_contact_eight_baseline_v2")
            for idx in range(301)
        ]
        max_speed = max(
            math.hypot(sample["desired_velocity_xy"][0], sample["desired_velocity_xy"][1])
            for sample in samples
        )
        self.assertAlmostEqual(max_speed, 0.00894427190999916, places=12)
        self.assertLessEqual(
            max(sample["desired_xy"][0] for sample in samples),
            float(frame["guard"]["guard_line_x_m"]) + 1e-12,
        )
        start_ref = samples[0]
        self.assertAlmostEqual(start_ref["desired_xy"][0], origin[0], places=12)
        self.assertAlmostEqual(start_ref["desired_xy"][1], origin[1], places=12)
        self.assertAlmostEqual(
            start_ref["desired_velocity_xy"][0],
            0.008 * u_along[0] + 0.004 * p_lateral[0],
            places=12,
        )
        self.assertAlmostEqual(
            start_ref["desired_velocity_xy"][1],
            0.008 * u_along[1] + 0.004 * p_lateral[1],
            places=12,
        )

    def test_upload_validator_accepts_step6b_triplet(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step6_eight_test_") as tmp:
            tmp_path = Path(tmp)
            points_path = safe_frame.synthetic_session(tmp_path / "points")
            frame_payload = safe_frame.make_payload(
                points_path,
                guard_line_x_m=0.4888784335298146,
                residual_threshold_mm=1.0,
            )
            geom = step6b.line_cfg(step6b.load_json(step6b.CONFIG_PATH))
            variant = step6b.variant_config("v2")
            metrics = step6b.assert_feasible(frame_payload, variant)
            stamp = "2026-06-12T2231HKT_STEP6B_CONTACT_EIGHT_BASELINE_V2"
            script = step6b.build_script(stamp, "2026-06-12T22:31:00+08:00", geom, frame_payload, variant, metrics)
            txt = step6b.build_txt(stamp, variant, metrics)
            urp = step6b.build_urp(script, step6b.PROGRAM_NAME, step6b.CONTROLLER_DIR)
            files = {
                ".script": tmp_path / f"{step6b.PROGRAM_NAME}.script",
                ".txt": tmp_path / f"{step6b.PROGRAM_NAME}.txt",
                ".urp": tmp_path / f"{step6b.PROGRAM_NAME}.urp",
            }
            files[".script"].write_text(script, encoding="utf-8")
            files[".txt"].write_text(txt, encoding="utf-8")
            files[".urp"].write_bytes(urp)

            result = upload_ur_tp_package.validate_package(
                files,
                step6b.PROGRAM_NAME,
                step6b.CONTROLLER_DIR,
                require_exact_cached_script=True,
            )
        self.assertEqual(result["program"], step6b.PROGRAM_NAME)
        self.assertEqual(result["target_dir"], step6b.CONTROLLER_DIR)


if __name__ == "__main__":
    unittest.main()
