#!/usr/bin/env python3
from __future__ import annotations

import json
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))
sys.path.insert(0, str(ROOT / "tools"))

from ur10e_example_controllers import step5b_contact_control_core as core  # noqa: E402
from ur10e_example_controllers import step5b_contact_live_runner as runner  # noqa: E402

import step5b_authorization_status as auth_gate  # noqa: E402


class Step5bContactLiveRunnerTest(unittest.TestCase):
    def test_acceptance_contract_uses_locked_ros2_headless_route(self) -> None:
        contract = runner.acceptance_contract()
        self.assertEqual(contract["live_runner_route"], "ros2_remote_control_headless")
        self.assertEqual(contract["ros2_interface"]["action"], "control_msgs/action/FollowJointTrajectory")
        self.assertFalse(contract["zero_policy"]["ur_zero_ftsensor_called"])
        self.assertFalse(contract["zero_policy"]["kunwei_hardware_tare_or_config_written"])
        self.assertTrue(contract["zero_policy"]["software_baseline_subtraction"])
        self.assertIn("compute_step5b_contact_sample", contract["contact_core"])

    def test_entrypoint_compiles_for_authorization_gate(self) -> None:
        entrypoint = Path(runner.__file__).resolve()
        py_compile.compile(str(entrypoint), doraise=True)
        with tempfile.TemporaryDirectory() as tmp:
            acceptance = Path(tmp) / "acceptance.json"
            acceptance.write_text(
                json.dumps(
                    {
                        "accepted": True,
                        "live_runner_entrypoint": str(entrypoint),
                        "live_runner_route": "ros2_remote_control_headless",
                    }
                ),
                encoding="utf-8",
            )
            result = auth_gate.evaluate_acceptance(acceptance, locked_route="ros2_remote_control_headless")
        self.assertTrue(result["met"], result)
        self.assertEqual(result["route"], "ros2_remote_control_headless")

    def test_default_main_is_dry_run_and_writes_summary_without_motion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.json"
            rc = runner.main(["--summary", str(summary), "--runs-dir", str(ROOT / "runs")])
            self.assertEqual(rc, 0)
            payload = json.loads(summary.read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["execute_live_contact"])
        self.assertFalse(payload["motion_authorized"])
        self.assertFalse(payload["contact_motion_entered"])
        self.assertFalse(payload["sent_goal"])
        self.assertEqual(payload["live_runner_route"], "ros2_remote_control_headless")

    def test_compute_live_command_preserves_approach_reaction_contract(self) -> None:
        params = core.Step5bContactParams()
        basis = core.Step5bPathBasis(origin_xy_m=(0.0, 0.0), u_along_xy=(1.0, 0.0), p_lateral_xy=(0.0, 1.0))
        state = core.Step5bContactState(
            latched_normal_b=(0.0, 0.0, 1.0),
            filtered_normal_b=(0.0, 0.0, 1.0),
            latched_normal_locked=True,
            normal_acquired=True,
        )
        command = runner.compute_live_command(
            pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            tcp_wrench=(0.0, 0.0, 5.0, 0.0, 0.0, 0.0),
            sensor_ok=1.0,
            robot_stage=25.0,
            dt_s=0.002,
            state=state,
            params=params,
            basis=basis,
            search_speed_m_s=0.001,
        )
        self.assertFalse(command.search_active)
        self.assertLess(core.dot3(command.result.approach_normal_b, command.result.control_normal_b), -0.999)

    def test_search_latch_fails_if_approach_opposes_tcp_search_axis(self) -> None:
        params = core.Step5bContactParams(bridge_min_force_for_control_n=1.0)
        basis = core.Step5bPathBasis(origin_xy_m=(0.0, 0.0), u_along_xy=(1.0, 0.0), p_lateral_xy=(0.0, 1.0))
        with self.assertRaisesRegex(RuntimeError, "search TCP \\+Z"):
            runner.compute_live_command(
                pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                tcp_wrench=(0.0, 0.0, 2.0, 0.0, 0.0, 0.0),
                sensor_ok=1.0,
                robot_stage=24.2,
                dt_s=0.002,
                state=core.Step5bContactState(),
                params=params,
                basis=basis,
                search_speed_m_s=0.001,
            )

    def test_source_does_not_use_legacy_live_surfaces(self) -> None:
        text = Path(runner.__file__).read_text(encoding="utf-8")
        forbidden = [
            "speedl(",
            "servoj(",
            "zero_ftsensor(",
            '"play"',
            '"load"',
            "rtde_control",
            "kunwei_tare",
        ]
        for token in forbidden:
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
