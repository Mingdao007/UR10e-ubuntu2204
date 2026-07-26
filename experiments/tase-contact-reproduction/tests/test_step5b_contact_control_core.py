#!/usr/bin/env python3
"""Offline tests for the Step5b contact-control core (increment 1a).

Validates the faithfully-ported pure control math against the bridge's exact
formulas and behaviors. No ROS2, no robot.
"""

from __future__ import annotations

import math
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers import step5b_contact_control_core as core  # noqa: E402


class Step5bContactControlCoreTest(unittest.TestCase):
    def basis(self) -> core.Step5bPathBasis:
        frame = json.loads((ROOT / "config" / "step5_safe_frame.json").read_text(encoding="utf-8"))
        return core.Step5bPathBasis.from_safe_frame(frame)

    def sample(
        self,
        *,
        stage: float = 25.0,
        force: core.Vec3 = (0.0, 0.0, 5.0),
        pose: tuple[float, float, float, float, float, float] | None = None,
        dt_s: float = 0.002,
        sensor_ok: float = 1.0,
    ) -> core.Step5bSample:
        if pose is None:
            pose = (0.487795411149049, 0.12932679270060748, 0.02, 0.0, 0.0, 0.0)
        return core.Step5bSample(
            tcp_pose=pose,
            tcp_wrench=(force[0], force[1], force[2], 0.0, 0.0, 0.0),
            sensor_ok=sensor_ok,
            robot_stage=stage,
            dt_s=dt_s,
        )

    def test_cycloid_reference_at_zero_is_zero(self) -> None:
        ref = core.cycloid_reference(0.0)
        self.assertAlmostEqual(ref["along_m"], 0.0)
        self.assertAlmostEqual(ref["lateral_m"], 0.0)
        self.assertAlmostEqual(ref["along_v_m_s"], 0.0)
        self.assertAlmostEqual(ref["lateral_v_m_s"], 0.0)

    def test_cycloid_reference_matches_bridge_formula(self) -> None:
        t = 7.3
        phase = core.CYCLOID_OMEGA_RAD_S * t
        ref = core.cycloid_reference(t)
        self.assertAlmostEqual(ref["along_m"], 0.015 * (phase - math.sin(phase)))
        self.assertAlmostEqual(ref["lateral_m"], 0.015 * (1.0 - math.cos(phase)))
        self.assertAlmostEqual(ref["along_v_m_s"], 0.0015 * (1.0 - math.cos(phase)))
        self.assertAlmostEqual(ref["lateral_v_m_s"], 0.0015 * math.sin(phase))

    def test_normalize_fallback_on_zero(self) -> None:
        self.assertEqual(core.normalize3((0.0, 0.0, 0.0)), (0.0, 0.0, 1.0))
        self.assertEqual(core.normalize3((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)), (1.0, 0.0, 0.0))

    def test_frame_contract_approach_is_negative_reaction(self) -> None:
        # The Step5d v4 semantic bug site: approach_normal MUST be -reaction_normal.
        filt = (0.0, 0.0, 5.0)
        r = core.reaction_normal(filt)
        a = core.approach_normal(filt)
        self.assertAlmostEqual(core.norm3(r), 1.0)
        self.assertEqual(a, (-r[0], -r[1], -r[2]))
        self.assertAlmostEqual(a[2], -1.0)

    def test_normal_load_is_projection_on_reaction_normal(self) -> None:
        force_base = (0.0, 0.0, 8.0)
        filt = (0.0, 0.0, 1.0)
        self.assertAlmostEqual(core.normal_load_n(force_base, filt), 8.0)
        # Force opposing the reaction normal gives negative load.
        self.assertAlmostEqual(core.normal_load_n((0.0, 0.0, -3.0), filt), -3.0)

    def test_v31_filter_hold_conditions(self) -> None:
        current = (0.0, 0.0, 1.0)
        cand = (0.0, 1.0, 0.0)
        # stale sensor -> hold
        out, reason = core.v31_filtered_live_normal(current, cand, 10.0, sensor_ok=0.0)
        self.assertEqual(out, current)
        self.assertEqual(reason, "hold_stale")
        # low force -> hold
        out, reason = core.v31_filtered_live_normal(current, cand, 1.0, sensor_ok=1.0)
        self.assertEqual(reason, "hold_low_force")
        # reverse (dot < 0) -> hold
        out, reason = core.v31_filtered_live_normal(current, (0.0, 0.0, -1.0), 10.0, sensor_ok=1.0)
        self.assertEqual(reason, "hold_reverse")

    def test_v31_filter_blends_and_stays_unit(self) -> None:
        current = (0.0, 0.0, 1.0)
        cand = (1.0, 0.0, 0.0)
        out, reason = core.v31_filtered_live_normal(current, cand, 10.0, sensor_ok=1.0, alpha=0.35)
        self.assertEqual(reason, "filtered_live_alpha")
        self.assertAlmostEqual(core.norm3(out), 1.0)
        # blended direction tilts from +z toward +x but not past it
        self.assertGreater(out[0], 0.0)
        self.assertGreater(out[2], 0.0)

    def test_live_normal_candidate_unit_and_friction_projection(self) -> None:
        raw, cand, force_n = core.live_normal_candidate((0.0, 0.0, 6.0), friction_projection=False)
        self.assertAlmostEqual(core.norm3(cand), 1.0)
        self.assertAlmostEqual(force_n, 6.0)
        # friction projection removes the bridge's real Step4e line tangent.
        _, cand_fp, _ = core.live_normal_candidate((4.0, 0.0, 3.0), friction_projection=True)
        tangent_b = (core.STEP4E_LINE_UNIT_XY[0], core.STEP4E_LINE_UNIT_XY[1], 0.0)
        self.assertAlmostEqual(core.dot3(cand_fp, tangent_b), 0.0)

    def test_latch_acquisition_and_reset_across_stage_transition(self) -> None:
        params = core.Step5bContactParams(bridge_path_p_gain=0.0, bridge_normal_friction_projection="off")
        state = core.Step5bContactState()
        _result, state = core.compute_step5b_contact_sample(
            self.sample(stage=24.0, force=(0.0, 0.0, 3.0)),
            state,
            params,
            self.basis(),
        )
        self.assertTrue(state.normal_acquired)
        self.assertEqual(state.latched_normal_b, (0.0, 0.0, 1.0))

        _result, state = core.compute_step5b_contact_sample(
            self.sample(stage=26.0, force=(0.0, 0.0, 3.0)),
            state,
            params,
            self.basis(),
        )
        self.assertFalse(state.normal_acquired)
        self.assertIsNone(state.latched_normal_b)

    def test_filtered_live_hold_reasons_in_sample_result(self) -> None:
        params = core.Step5bContactParams(bridge_path_p_gain=0.0, bridge_normal_friction_projection="off")
        state = core.Step5bContactState(
            latched_normal_b=(0.0, 0.0, 1.0),
            filtered_normal_b=(0.0, 0.0, 1.0),
            latched_normal_locked=True,
            normal_acquired=True,
        )
        result, _state = core.compute_step5b_contact_sample(
            self.sample(stage=25.0, force=(1.0, 0.0, 0.0), sensor_ok=0.0),
            state,
            params,
            self.basis(),
        )
        self.assertEqual(result.normal_filter_source, "hold_stale")
        self.assertEqual(result.hold_reason, "hold_stale")

        result, _state = core.compute_step5b_contact_sample(
            self.sample(stage=25.0, force=(0.1, 0.0, 0.0), sensor_ok=1.0),
            state,
            params,
            self.basis(),
        )
        self.assertEqual(result.normal_filter_source, "hold_low_force")

    def test_force_tracking_sign_low_load_presses_high_load_unloads(self) -> None:
        params = core.Step5bContactParams(bridge_path_p_gain=0.0)
        state = core.Step5bContactState(
            latched_normal_b=(0.0, 0.0, 1.0),
            filtered_normal_b=(0.0, 0.0, 1.0),
            latched_normal_locked=True,
            normal_acquired=True,
        )
        low, _ = core.compute_step5b_contact_sample(
            self.sample(stage=25.0, force=(0.0, 0.0, 1.0)),
            state,
            params,
            self.basis(),
        )
        self.assertLess(low.cmd_vz_m_s, 0.0)
        self.assertGreater(core.dot3(low.twist[:3], low.approach_normal_b), 0.0)

        high, _ = core.compute_step5b_contact_sample(
            self.sample(stage=25.0, force=(0.0, 0.0, 10.0)),
            state,
            params,
            self.basis(),
        )
        self.assertGreater(high.cmd_vz_m_s, 0.0)
        self.assertGreater(core.dot3(high.twist[:3], high.reaction_normal_b), 0.0)

    def test_orientation_target_uses_approach_normal(self) -> None:
        params = core.Step5bContactParams(bridge_path_p_gain=0.0, bridge_normal_friction_projection="off")
        state = core.Step5bContactState(
            latched_normal_b=(1.0, 0.0, 0.0),
            filtered_normal_b=(1.0, 0.0, 0.0),
            latched_normal_locked=True,
            normal_acquired=True,
        )
        result, _ = core.compute_step5b_contact_sample(
            self.sample(stage=25.0, force=(4.0, 0.0, 0.0)),
            state,
            params,
            self.basis(),
        )
        self.assertEqual(result.approach_normal_b, (-1.0, -0.0, -0.0))
        self.assertLess(result.cmd_wy_rad_s, 0.0)

    def test_step5b_path_reference_matches_safe_frame_stage_table_values(self) -> None:
        frame = json.loads((ROOT / "config" / "step5_safe_frame.json").read_text(encoding="utf-8"))
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        stage = next(item for item in table["stages"] if item["id"] == "step5_contact_cycloid_baseline_v1")
        params = core.Step5bContactParams.from_metadata_args({}, stage=stage)
        basis = core.Step5bPathBasis.from_safe_frame(frame)
        ref = core.step5b_path_reference((0.0, 0.0), 30.0, params, basis)
        phase = stage["phase_law"]["omega_rad_s"] * 30.0
        along = stage["amplitude_m"] * (phase - math.sin(phase))
        lateral = stage["amplitude_m"] * (1.0 - math.cos(phase))
        expected_x = basis.origin_xy_m[0] + along * basis.u_along_xy[0] + lateral * basis.p_lateral_xy[0]
        expected_y = basis.origin_xy_m[1] + along * basis.u_along_xy[1] + lateral * basis.p_lateral_xy[1]
        self.assertAlmostEqual(ref["desired_xy"][0], expected_x)
        self.assertAlmostEqual(ref["desired_xy"][1], expected_y)

    def test_rotvec_identity_and_orthonormal(self) -> None:
        ident = core.rotvec_to_matrix(0.0, 0.0, 0.0)
        self.assertEqual(ident, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        m = core.rotvec_to_matrix(0.3, -0.2, 0.5)
        # columns orthonormal
        for i in range(3):
            col = (m[0][i], m[1][i], m[2][i])
            self.assertAlmostEqual(core.norm3(col), 1.0)

    def test_core_has_no_io_imports(self) -> None:
        src = (WORKSPACE / "src" / "ur10e_example_controllers" / "ur10e_example_controllers" / "step5b_contact_control_core.py").read_text(encoding="utf-8")
        for forbidden in (
            "import rclpy",
            "import socket",
            "speedl(",
            "force_mode(",
            "Dashboard",
            "import rtde",
            "zero_ftsensor",
        ):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main()
