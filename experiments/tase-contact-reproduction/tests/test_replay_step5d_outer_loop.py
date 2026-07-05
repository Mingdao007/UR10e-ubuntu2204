import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import replay_step5d_outer_loop as replay_mod


def synthetic_stage25_row(
    *,
    t_s: float,
    normal_tilt_rad: float = 0.0,
    load_n: float = 12.0,
    live_cmd_m_s: float = 0.0001,
) -> dict[str, str]:
    # Tool z axis points down (rotvec [pi, 0, 0] => R = diag(1,-1,-1)). The
    # signed normal load convention is dot(force_base, reaction): a positive
    # load needs base-frame force along the +z reaction, so the TCP-frame
    # force is -load along the tool z axis.
    reference = (math.sin(normal_tilt_rad), 0.0, math.cos(normal_tilt_rad))
    return {
        "t_monotonic_s": f"{t_s:.6f}",
        "ur_output_double_register_35": "25.0",
        "ur_actual_TCP_pose_0": "0.490000",
        "ur_actual_TCP_pose_1": "0.140000",
        "ur_actual_TCP_pose_2": "0.020000",
        "ur_actual_TCP_pose_3": f"{math.pi:.9f}",
        "ur_actual_TCP_pose_4": "0.0",
        "ur_actual_TCP_pose_5": "0.0",
        **{f"ur_actual_TCP_speed_{i}": "0.0" for i in range(6)},
        "_step4e_force_t_x": "0.0",
        "_step4e_force_t_y": "0.0",
        "_step4e_force_t_z": f"{-load_n:.6f}",
        "_step4e_desired_x_m": "0.490000",
        "_step4e_desired_y_m": "0.140000",
        "_step4e_desired_vx_m_s": "0.0",
        "_step4e_desired_vy_m_s": "0.0",
        "_step4e_filtered_normal_b_x": f"{reference[0]:.9f}",
        "_step4e_filtered_normal_b_y": f"{reference[1]:.9f}",
        "_step4e_filtered_normal_b_z": f"{reference[2]:.9f}",
        "step4e_cmd_vx_m_s": f"{live_cmd_m_s:.9f}",
        "step4e_cmd_vy_m_s": "0.0",
        "step4e_cmd_vz_m_s": "0.0",
    }


class ReplayStep5dOuterLoopTest(unittest.TestCase):
    def test_aligned_contact_at_target_produces_near_zero_commands(self) -> None:
        rows = [synthetic_stage25_row(t_s=1.0 + 0.002 * idx) for idx in range(200)]
        result = replay_mod.replay(rows, admittance_scale=20.0, ko=0.5, target_force_n=12.0)
        self.assertEqual(result["replayed_rows"], 200)
        self.assertLess(result["full_angular_cmd_norm_max_rad_s"], 1e-6)
        self.assertLess(result["full_force_channel_cmd_abs_max_m_s"], 1e-4)
        self.assertTrue(result["pass_tail_angular_saturation"])
        self.assertTrue(result["pass_tail_force_channel_cap"])

    def test_small_reference_tilt_stays_inside_angular_box(self) -> None:
        rows = [synthetic_stage25_row(t_s=1.0 + 0.002 * idx, normal_tilt_rad=0.02) for idx in range(200)]
        result = replay_mod.replay(rows, admittance_scale=20.0, ko=0.5, target_force_n=12.0)
        # The paper's quaternion error is half-angle scaled: |xdot_o| = ko * theta / 2.
        self.assertAlmostEqual(result["full_angular_cmd_norm_max_rad_s"], 0.5 * 0.02 / 2.0, delta=0.001)
        self.assertEqual(result["tail_angular_saturation_ratio"], 0.0)

    def test_paper_orientation_gain_saturates_on_stale_latch_error(self) -> None:
        rows = [synthetic_stage25_row(t_s=1.0 + 0.002 * idx, normal_tilt_rad=0.117) for idx in range(200)]
        result = replay_mod.replay(rows, admittance_scale=20.0, ko=5.0, target_force_n=12.0)
        self.assertGreaterEqual(result["tail_angular_saturation_ratio"], 1.0)
        self.assertFalse(result["ok"])

    def test_force_error_command_scale_matches_step5b_evidence_gain(self) -> None:
        # 3 N below target: recalibrated proportional gain is 1/(550*20) m/s
        # per N, so the settled force-channel command is ~0.27 mm/s.
        rows = [synthetic_stage25_row(t_s=1.0 + 0.002 * idx, load_n=9.0) for idx in range(400)]
        result = replay_mod.replay(rows, admittance_scale=20.0, ko=0.5, target_force_n=12.0)
        settled = float(result["tail_force_channel_cmd_abs_max_m_s"])
        self.assertGreater(settled, 0.0001)
        self.assertLess(settled, 0.001)


if __name__ == "__main__":
    unittest.main()
