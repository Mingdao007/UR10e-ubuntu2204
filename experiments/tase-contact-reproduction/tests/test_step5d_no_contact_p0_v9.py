#!/usr/bin/env python3
"""Offline contract and package tests for P0 v9."""

from __future__ import annotations

import csv
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_liveprep as liveprep  # noqa: E402
import step5d_p0_v9_bridge as bridge  # noqa: E402
import verify_step5d_no_contact_p0_v9 as verifier  # noqa: E402
from step5d_control_contract import (  # noqa: E402
    ControlCandidate,
    SafetyEnvelope,
    Step5dObservation,
    classify_normal_motion,
    compute_dls_shadow,
)
from step5d_p0_v9_control_core import (  # noqa: E402
    P0_V9_TANGENTIAL_SPEED_CAP_M_S,
    build_p0_v9_target,
    one_sided_smooth_reference,
)
from step5d_runtime_interface import STEP5D_NO_CONTACT_P0_V9_STAGE_ID  # noqa: E402


PROFILE = STEP5D_NO_CONTACT_P0_V9_STAGE_ID
IDENTITY6 = tuple(tuple(1.0 if i == j else 0.0 for j in range(6)) for i in range(6))
ZERO6 = (0.0,) * 6


def observation(desired: tuple[float, ...]) -> Step5dObservation:
    return Step5dObservation(
        sequence=1,
        timestamp_s=1.0,
        q=ZERO6,
        qd=ZERO6,
        tcp_pose=ZERO6,
        tcp_twist=ZERO6,
        wrench=ZERO6,
        jacobian=IDENTITY6,
        desired_twist=desired,  # type: ignore[arg-type]
        reaction_normal=(0.0, 0.0, 1.0),
        approach_normal=(0.0, 0.0, -1.0),
        command_frame="base",
        normal_frame="base",
        omega_minus=(-0.05,) * 6,
        omega_plus=(0.05,) * 6,
        normal_motion_policy="normal_zero",
    )


class Step5dNoContactP0V9Test(unittest.TestCase):
    def test_reference_is_two_mm_one_sided_three_cycle(self) -> None:
        self.assertAlmostEqual(one_sided_smooth_reference(0.0)[0], 0.0)
        self.assertAlmostEqual(one_sided_smooth_reference(10.0)[0], 0.002)
        self.assertAlmostEqual(one_sided_smooth_reference(20.0)[0], 0.0)
        self.assertAlmostEqual(one_sided_smooth_reference(60.0)[0], 0.0)
        peak_velocity = one_sided_smooth_reference(5.0)[1]
        self.assertAlmostEqual(peak_velocity, math.pi * 1e-4)

    def test_target_projects_safe_tangent_and_commands_zero_normal(self) -> None:
        target = build_p0_v9_target(
            tcp_pose_base=(0.4, 0.1, 0.2, 0.0, 0.0, 0.0),
            anchor_tcp_pose_base=(0.4, 0.1, 0.2, 0.0, 0.0, 0.0),
            safe_u_along_xy=(-0.01, 0.99995),
            approach_normal_base=(0.1, 0.0, -0.994987437),
            path_time_s=5.0,
            normal_load_n=1.9,
            jacobian=np.eye(6),
        )
        approach = np.asarray(target.approach_normal_base)
        tangent = np.asarray(target.tangent_base)
        self.assertAlmostEqual(float(np.dot(tangent, approach)), 0.0, places=12)
        self.assertAlmostEqual(float(np.dot(target.desired_twist[:3], approach)), 0.0, places=12)
        self.assertLessEqual(abs(target.path_diagnostics["commanded_tangent_velocity_m_s"]), P0_V9_TANGENTIAL_SPEED_CAP_M_S)
        self.assertAlmostEqual(target.posture_policy["effective_ko"], 0.01)
        self.assertEqual(target.posture_policy["load_schedule"], "disabled_constant_weak_hold")

    def test_normal_zero_policy_accepts_neutral_and_rejects_nonzero(self) -> None:
        desired = (0.0002, 0.0, 0.0, 0.0, 0.0, 0.0)
        obs = observation(desired)
        candidate = ControlCandidate(
            qdot=desired,
            predicted_twist=desired,
            residual_norm=0.0,
            active_bounds_count=0,
            frame_id="base",
            solver_status="40",
        )
        self.assertTrue(SafetyEnvelope().evaluate(obs, candidate).accepted)
        bad_desired = (0.0002, 0.0, 2e-8, 0.0, 0.0, 0.0)
        bad_obs = observation(bad_desired)
        bad = ControlCandidate(
            qdot=bad_desired,
            predicted_twist=bad_desired,
            residual_norm=0.0,
            active_bounds_count=0,
            frame_id="base",
            solver_status="40",
        )
        self.assertEqual(SafetyEnvelope().evaluate(bad_obs, bad).reason, "no_contact_target_normal_nonzero")

    def test_dls_neutral_classification_does_not_report_mismatch(self) -> None:
        self.assertEqual(classify_normal_motion(9e-6), "neutral")
        desired = (0.0002, 0.0, 0.0, 0.0, 0.0, 0.0)
        obs = observation(desired)
        strict = ControlCandidate(desired, desired, 0.0, 0, "base", "40")
        shadow = compute_dls_shadow(obs, strict)
        self.assertEqual(shadow.strict_normal_class, "neutral")
        self.assertEqual(shadow.dls_normal_class, "neutral")
        self.assertFalse(shadow.normal_direction_class_difference)

    def test_qualification_resets_on_any_failed_echo_or_acceptance(self) -> None:
        values = {
            "_step5d_stage25_echo_layout_tag": 524.0,
            "_step5d_stage25_echo_cmd_valid": 1.0,
            "_step5d_stage25_echo_consumed": 1.0,
            "_step5d_p0_rnn_accepted": 1.0,
            "_step5d_p0_safe_hold_active": 0.0,
            "_step5d_dls_shadow_normal_direction_class_difference": 0.0,
        }
        output = {"output_double_register_35": 25.0}
        self.assertEqual(bridge._qualification(values, output), (True, "qualified"))
        values["_step5d_stage25_echo_layout_tag"] = 0.0
        self.assertEqual(bridge._qualification(values, output), (False, "echo_layout_not_524"))

    def test_runtime_guards_stop_normal_speed_dwell_and_displacement(self) -> None:
        state = bridge.base.BridgeState()
        state.step5d_p0_v9_anchor_tcp_pose = (0.4, 0.1, 0.2, 0.0, 0.0, 0.0)
        state.step5d_p0_v9_approach_normal = (0.0, 0.0, 1.0)
        output = {
            "actual_TCP_pose": (0.4, 0.1, 0.2, 0.0, 0.0, 0.0),
            "actual_TCP_speed": (0.0, 0.0, 0.00021, 0.0, 0.0, 0.0),
        }
        values: dict[str, float] = {}
        self.assertIsNone(bridge._apply_no_contact_runtime_guards(state, output, values, 0.002))
        self.assertEqual(
            bridge._apply_no_contact_runtime_guards(state, output, values, 0.002),
            "no_contact_actual_normal_speed_dwell_exceeded",
        )
        state.p0_v9_normal_speed_violation_s = 0.0
        output["actual_TCP_speed"] = ZERO6
        output["actual_TCP_pose"] = (0.4, 0.1, 0.20051, 0.0, 0.0, 0.0)
        self.assertEqual(
            bridge._apply_no_contact_runtime_guards(state, output, values, 0.002),
            "no_contact_anchor_normal_displacement_exceeded",
        )

    def test_generated_package_has_full_echo_and_no_speedl_path(self) -> None:
        spec = liveprep.spec_for(PROFILE)
        script = liveprep.build_no_contact_p0_script("2026-07-14T1700HKT_TEST", "2026-07-14 17:00 HKT", spec)
        self.assertIn("P0V9_FULL_STAGE25_ECHO", script)
        self.assertIn("write_output_float_register(46, stage25_layout_tag)", script)
        self.assertIn("write_output_float_register(42, cmd_valid)", script)
        self.assertNotIn("speedl([cmd_vx", script)
        self.assertIn("write_output_float_register(28, stop_reason)", script)
        self.assertIn("write_output_float_register(35, 26.0)", script)

    def test_run_manifest_uses_v9_evidence_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            manifest = bridge.base.write_bridge_run_manifest(
                SimpleNamespace(
                    output_dir=run_dir,
                    bridge_profile=PROFILE,
                    step5d_stop_register_canary_s=60.0,
                    rtde_hz=500.0,
                    bridge_mode="line",
                ),
                argv=[],
                pointer_path=run_dir / "latest.json",
            )
        self.assertIn("p0_v9_canary", manifest)
        self.assertNotIn("p0_v8_canary", manifest)
        self.assertEqual(manifest["p0_v9_canary"]["phase_s"], 60.0)

    def test_verifier_accepts_complete_synthetic_contract(self) -> None:
        rows = []
        for tangent_m, terminal in ((0.0, False), (0.002, False), (0.0, True)):
            rows.append(
                {
                    "_step5d_p0_v9_qualified": 1.0,
                    "_step5d_p0_v9_qualified_s": 60.0 if terminal else 30.0,
                    "_step5d_p0_v9_canary_stop_active": 1.0 if terminal else 0.0,
                    "_step5d_p0_rnn_accepted": 1.0,
                    "_step5d_p0_safe_hold_active": 0.0,
                    "_step5d_dls_shadow_normal_direction_class_difference": 0.0,
                    "_step5d_stage25_echo_layout_tag": 524.0,
                    "_step5d_stage25_echo_cmd_valid": 1.0,
                    "_step5d_stage25_echo_consumed": 1.0,
                    "_step5d_rnn_inner_iterations": 512.0,
                    "_step5d_p0_v9_actual_tangent_displacement_m": tangent_m,
                    "_step5d_p0_v9_target_tangent_displacement_m": tangent_m,
                    "_step5d_p0_v9_anchor_normal_displacement_m": 0.0,
                    "normal_force_n": 0.0,
                    "force_norm_n": 0.0,
                    "torque_norm_nm": 0.0,
                    **{f"step4e_cmd_v{axis}_m_s": 0.0 for axis in ("x", "y", "z")},
                    **{f"step4e_cmd_w{axis}_rad_s": 0.0 for axis in ("x", "y", "z")},
                    "step4e_controller_state": 524.0,
                    "step4e_cmd_valid": 1.0,
                    "stop_request": 1.0 if terminal else 0.0,
                    "ur_output_double_register_35": 26.0 if terminal else 25.0,
                    "ur_output_double_register_28": 1.0 if terminal else 0.0,
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "bridge_rtde_500hz.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            result = verifier.verify(csv_path)
        self.assertTrue(result["ok"], result["blockers"])


if __name__ == "__main__":
    unittest.main()
