from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_step5d_autotune_tp as builder  # noqa: E402


class Step5dAutotuneTpBuilderTest(unittest.TestCase):
    def test_frozen_v35_hash_and_continuous_lifecycle(self) -> None:
        base = builder.load_frozen_v35()
        self.assertEqual(
            builder.sha256_bytes(base.encode("utf-8")),
            builder.BASE_SHA256,
        )
        rendered = builder.render_script(base)
        builder.validate_rendered_script(rendered)
        self.assertNotIn("\ncodex_step5d_strict_rnn_ablation_v35()\n", rendered)
        self.assertEqual(
            rendered.count("local campaign_home_pose = get_actual_tcp_pose()"), 1
        )

    def test_handshake_echoes_exact_identity_and_fresh_sequence(self) -> None:
        rendered = builder.render_script()
        for register in range(24, 30):
            self.assertIn(f"read_input_integer_register({register})", rendered)
        for register in range(24, 31):
            self.assertIn(f"write_output_integer_register({register},", rendered)
        self.assertIn("command_seq > last_consumed_command_seq", rendered)
        self.assertIn("ack_sequence > last_consumed_command_seq", rendered)
        self.assertIn("read_input_integer_register(27) == candidate_token", rendered)

    def test_v35_guard_and_qdot_contract_are_preserved_but_profile_accel_is_latched(self) -> None:
        rendered = builder.render_script()
        self.assertIn("codex_abs(normal_force) > 60.0", rendered)
        self.assertIn("force_norm > 100.0", rendered)
        self.assertIn("torque_norm > 3.0", rendered)
        self.assertIn("local qdot_cap_rad_s = 0.500", rendered)
        self.assertIn(
            "local joint_accel_rad_s2 = tp_speedj_accel_rad_s2", rendered
        )
        self.assertIn("return 0.100", rendered)
        self.assertIn("return 0.200", rendered)
        self.assertIn("return 0.500", rendered)

    def test_precontact_entry_and_far_search_are_one_point_five_times_faster(self) -> None:
        rendered = builder.render_script()
        self.assertIn(
            "movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)", rendered
        )
        self.assertIn("40.000, -0.03375, -0.0025)", rendered)
        self.assertNotIn(
            "movel(entry_xy_pose, a=0.090, v=0.060, r=0.0)", rendered
        )
        self.assertNotIn("40.000, -0.0225, -0.0025)", rendered)

    def test_each_continuous_trial_accepts_an_advancing_ready_heartbeat(self) -> None:
        rendered = builder.render_script()
        self.assertIn(
            "if read_input_float_register(27) > 0.5 and current_heartbeat != initial_heartbeat:",
            rendered,
        )
        self.assertNotIn(
            "elif saw_sensor_not_ready and read_input_float_register(27) > 0.5",
            rendered,
        )

    def test_stage23_waits_for_sensor_rearm_after_rezero(self) -> None:
        rendered = builder.render_script()
        stage23 = rendered.split("write_output_float_register(35, 23.0)", 1)[1]
        stage23 = stage23.split("write_output_float_register(35, 24.0)", 1)[0]
        self.assertIn("codex_wait_for_rezero_complete(5.0)", stage23)
        self.assertIn("codex_wait_for_fresh_heartbeat(5.0)", stage23)
        self.assertLess(
            stage23.index("codex_wait_for_fresh_heartbeat(5.0)"),
            stage23.index("codex_step4e_guard_stop_reason()"),
        )

    def test_manual_recovery_reasons_never_auto_return(self) -> None:
        rendered = builder.render_script()
        marker = "if stop_reason == 2 or stop_reason == 3 or stop_reason == 17:"
        self.assertIn(marker, rendered)
        tail = rendered.split(marker, 1)[1].split("elif codex_should_auto_home", 1)[0]
        self.assertIn("codex_autotune_fault_forever", tail)
        self.assertNotIn("movel(", tail)

    def test_home_thresholds_and_ack_barrier_are_explicit(self) -> None:
        rendered = builder.render_script()
        self.assertIn("position_error_m <= 0.003", rendered)
        self.assertIn("orientation_error_rad <= 0.050", rendered)
        self.assertIn("joint_error_rad <= 0.010", rendered)
        self.assertIn("Host may ACK only after immutable bundle", rendered)
        self.assertLess(
            rendered.index("trial_id, 60,"), rendered.index("trial_id, 70,")
        )

    def test_local_triplet_build_requires_no_user_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            stamp = "2026-07-15T0830HKT_STEP5D_STRICT_RNN_AUTOTUNE_V1"
            self.assertEqual(
                builder.main(["--output-dir", str(output), "--stamp", stamp]),
                0,
            )
            script = (output / f"{builder.PROGRAM_NAME}.script").read_text()
            txt = (output / f"{builder.PROGRAM_NAME}.txt").read_text()
            urp = (output / f"{builder.PROGRAM_NAME}.urp").read_bytes()
            builder.validate_triplet(script, txt, urp, stamp)


if __name__ == "__main__":
    unittest.main()
