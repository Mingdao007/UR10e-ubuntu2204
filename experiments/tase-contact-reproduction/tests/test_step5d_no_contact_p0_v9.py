#!/usr/bin/env python3
"""Offline contract and package tests for P0 v9."""

from __future__ import annotations

import csv
import json
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
    build_p0_v9_target,
    canonical_cycloid_lift_reference,
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
        omega_minus=(-0.5,) * 6,
        omega_plus=(0.5,) * 6,
        normal_motion_policy="diagnostic_only",
    )


class Step5dNoContactP0V9Test(unittest.TestCase):
    def test_diagnostic_normal_policy_keeps_tp_matched_qdot_slew(self) -> None:
        obs = observation((0.1, 0.0, 0.0, 0.0, 0.0, 0.0))
        raw = ControlCandidate(
            qdot=(0.1, 0.0, 0.0, 0.0, 0.0, 0.0),
            predicted_twist=(0.1, 0.0, 0.0, 0.0, 0.0, 0.0),
            residual_norm=0.0,
            active_bounds_count=0,
            frame_id="base",
            solver_status="40",
            diagnostics={"active_bounds_mask": (False,) * 6},
        )
        deferred = bridge.base.DeferredV30Diagnostics(capacity=1)

        candidate, _dls, decision, _command = (
            bridge.base.step5d_v30_contract_pipeline(
                obs,
                raw,
                previous_qdot=ZERO6,
                safety_envelope=SafetyEnvelope(),
                deferred_diagnostics=deferred,
            )
        )

        self.assertTrue(decision.accepted)
        self.assertTrue(candidate.diagnostics["slew_active"])
        self.assertAlmostEqual(candidate.qdot[0], 0.0001)

    def test_v9_bypasses_legacy_press_only_p0_gate_and_register_path(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )

        self.assertGreaterEqual(
            source.count("and not step5d_no_contact_p0_v9_profile"),
            2,
        )

    def test_cli_target_force_validator_keeps_force_controller_disabled(self) -> None:
        bridge.base.validate_common_target_force(
            SimpleNamespace(bridge_profile=PROFILE, target_force_n=0.0)
        )
        with self.assertRaisesRegex(SystemExit, "requires --target-force-n 0.0"):
            bridge.base.validate_common_target_force(
                SimpleNamespace(bridge_profile=PROFILE, target_force_n=1.0)
            )

    def test_generation_entrypoint_always_deploys_and_fresh_reads_back(self) -> None:
        script = (ROOT / "scripts" / "step5d-strict-rnn-p0-v9.sh").read_text(encoding="utf-8")
        self.assertIn("generate-deliver|generate-local)", script)
        self.assertIn('upload_ur_tp_package.py" "${PROFILE}"', script)
        self.assertIn("--force-upload-readback", script)
        self.assertNotIn("generate-local never uploads", script)

    def test_reference_is_full_canonical_cycloid_with_twenty_mm_z_lift(self) -> None:
        stage = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        p0_v9 = next(item for item in stage["stages"] if item["id"] == PROFILE)
        self.assertEqual(p0_v9["phase_law"]["omega_rad_s"], 0.1)
        self.assertEqual(p0_v9["phase_law"]["theta_rate_rad_s"], 0.1)
        self.assertEqual(canonical_cycloid_lift_reference(0.0)[0], (0.0, 0.0, 0.0))
        midpoint, midpoint_velocity = canonical_cycloid_lift_reference(30.0)
        self.assertAlmostEqual(midpoint[0], 0.04288319987910199)
        self.assertAlmostEqual(midpoint[1], 0.02984988744900668)
        self.assertAlmostEqual(midpoint[2], 0.010)
        endpoint, endpoint_velocity = canonical_cycloid_lift_reference(60.0)
        self.assertAlmostEqual(endpoint[0], 0.09419123247298389)
        self.assertAlmostEqual(endpoint[1], 0.0005974457002445104)
        self.assertAlmostEqual(endpoint[2], 0.020)
        self.assertGreater(midpoint_velocity[2], 0.0)
        self.assertAlmostEqual(endpoint_velocity[2], 0.0)

    def test_target_uses_safe_xy_basis_and_relative_base_z_without_speed_cap(self) -> None:
        target = build_p0_v9_target(
            tcp_pose_base=(0.4, 0.1, 0.2, 0.0, 0.0, 0.0),
            anchor_tcp_pose_base=(0.4, 0.1, 0.2, 0.0, 0.0, 0.0),
            safe_u_along_xy=(-0.01, 0.99995),
            safe_p_lateral_xy=(-0.99995, -0.01),
            approach_normal_base=(0.1, 0.0, -0.994987437),
            path_time_s=30.0,
            normal_load_n=1.9,
            jacobian=np.eye(6),
        )
        tangent = np.asarray(target.tangent_base)
        lateral = np.asarray(target.lateral_base)
        self.assertAlmostEqual(float(np.dot(tangent, lateral)), 0.0, places=12)
        self.assertGreater(abs(target.path_diagnostics["commanded_tangent_velocity_m_s"]), 0.0005)
        self.assertAlmostEqual(target.path_diagnostics["target_along_displacement_m"], 0.04288319987910199)
        self.assertAlmostEqual(target.path_diagnostics["target_lateral_displacement_m"], 0.02984988744900668)
        self.assertAlmostEqual(target.path_diagnostics["target_z_displacement_m"], 0.010)
        self.assertGreater(target.desired_twist[2], 0.0)
        self.assertEqual(
            target.path_diagnostics["force_sign_convention"],
            "step5_step6_positive_normal_load",
        )
        self.assertAlmostEqual(target.posture_policy["effective_ko"], 0.01)
        self.assertEqual(target.posture_policy["load_schedule"], "disabled_constant_weak_hold")

    def test_guard_v2_accepts_normal_residual_and_active_bound_diagnostics(self) -> None:
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
        self.assertTrue(SafetyEnvelope(qdot_cap_rad_s=0.5).evaluate(obs, candidate).accepted)
        diagnostic_desired = (0.2, 0.0, 0.2, 0.0, 0.0, 0.0)
        diagnostic_obs = observation(diagnostic_desired)
        diagnostic = ControlCandidate(
            qdot=(0.2, 0.0, 0.1, 0.0, 0.0, 0.0),
            predicted_twist=(0.2, 0.0, 0.1, 0.0, 0.0, 0.0),
            residual_norm=0.1,
            active_bounds_count=1,
            frame_id="base",
            solver_status="40",
        )
        decision = SafetyEnvelope(qdot_cap_rad_s=0.5).evaluate(diagnostic_obs, diagnostic)
        self.assertTrue(decision.accepted, decision.reason)

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
            "_step5d_p0_safe_hold_active": 1.0,
            "_step5d_dls_shadow_normal_direction_class_difference": 1.0,
        }
        output = {"output_double_register_35": 25.0}
        self.assertEqual(bridge._qualification(values, output), (True, "qualified"))
        values["_step5d_stage25_echo_layout_tag"] = 0.0
        self.assertEqual(bridge._qualification(values, output), (False, "echo_layout_not_524"))

    def test_runtime_has_no_normal_speed_or_displacement_guard(self) -> None:
        self.assertFalse(hasattr(bridge, "_apply_no_contact_runtime_guards"))

    def test_guard_v2_ignores_force_and_allows_unbaselined_stream_ready(self) -> None:
        args = SimpleNamespace(
            bridge_profile=PROFILE,
            max_normal_force_n=2.0,
            max_force_norm_n=5.0,
            max_torque_norm_nm=3.0,
            sensor_stale_s=2.0,
            rtde_hz=500.0,
            output_dir=Path("/tmp/p0-v9-guard-v2-test"),
        )
        self.assertIsNone(
            bridge.base.guard_stop_reason(
                args,
                {
                    "normal_force_n": 1000.0,
                    "force_norm_n": 1000.0,
                    "torque_norm_nm": 1000.0,
                },
            )
        )
        state = bridge.base.BridgeState()
        state.step5d_model_bundle = object()
        state.step5d_tcp_offset_tool0 = np.zeros(3)
        state.step5d_solver = object()
        state.step5d_v30_deferred_diagnostics = object()
        state.step5d_v30_policy = object()
        runtime = {"status": "ok"}
        payload = bridge.base.step5d_bridge_ready_payload(
            args,
            {"runtime_scheduler": {}},
            state,
            runtime,
            rtde_connected=True,
            rtde_send_succeeded=True,
            samples=1,
            baseline_ready=False,
            sensor_age_s=1.9,
            parse_errors=7,
        )
        self.assertIsNotNone(payload)
        self.assertFalse(payload["baseline_ready"])
        self.assertEqual(payload["parse_errors"], 7)

    def test_generated_package_has_full_echo_and_no_speedl_path(self) -> None:
        spec = liveprep.spec_for(PROFILE)
        script = liveprep.build_no_contact_p0_script("2026-07-14T1700HKT_TEST", "2026-07-14 17:00 HKT", spec)
        self.assertIn("P0V9_FULL_STAGE25_ECHO", script)
        self.assertIn("write_output_float_register(46, stage25_layout_tag)", script)
        self.assertIn("write_output_float_register(42, cmd_valid)", script)
        self.assertNotIn("speedl([cmd_vx", script)
        self.assertIn("local qdot_cap_rad_s = 0.500", script)
        self.assertIn("if stale_s_clear > 1.000", script)
        self.assertIn("if stale_s2 > 1.000", script)
        self.assertIn("local stage25_runtime_limit_s = 75.000", script)
        self.assertNotIn("codex_abs(normal_force) >", script)
        self.assertNotIn("force_norm >", script)
        self.assertNotIn("torque_norm >", script)
        self.assertNotIn("cartesian_linear_cap_m_s", script)
        self.assertNotIn("cartesian_angular_cap_rad_s", script)
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
        samples = (
            (0.0, 0.0, 0.0, False),
            (0.043, 0.030, 0.010, False),
            (0.0942, 0.0006, 0.020, True),
        )
        for along_m, lateral_m, z_m, terminal in samples:
            rows.append(
                {
                    "_step5d_p0_v9_qualified": 1.0,
                    "_step5d_p0_v9_qualified_s": 60.0 if terminal else 30.0,
                    "_step5d_p0_v9_canary_stop_active": 1.0 if terminal else 0.0,
                    "_step5d_p0_rnn_accepted": 1.0,
                    "_step5d_p0_safe_hold_active": 1.0,
                    "_step5d_dls_shadow_normal_direction_class_difference": 1.0,
                    "_step5d_stage25_echo_layout_tag": 524.0,
                    "_step5d_stage25_echo_cmd_valid": 1.0,
                    "_step5d_stage25_echo_consumed": 1.0,
                    "_step5d_rnn_inner_iterations": 512.0,
                    "_step5d_p0_v9_actual_tangent_displacement_m": along_m,
                    "_step5d_p0_v9_target_tangent_displacement_m": along_m,
                    "_step5d_p0_v9_actual_along_displacement_m": along_m,
                    "_step5d_p0_v9_target_along_displacement_m": along_m,
                    "_step5d_p0_v9_actual_lateral_displacement_m": lateral_m,
                    "_step5d_p0_v9_target_lateral_displacement_m": lateral_m,
                    "_step5d_p0_v9_actual_z_displacement_m": z_m,
                    "_step5d_p0_v9_target_z_displacement_m": z_m,
                    "_step5d_p0_v9_xyz_tracking_error_norm_m": 0.0,
                    "_step5d_p0_v9_anchor_normal_displacement_m": 1.0,
                    "normal_force_n": 1000.0,
                    "force_norm_n": 1000.0,
                    "torque_norm_nm": 1000.0,
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
