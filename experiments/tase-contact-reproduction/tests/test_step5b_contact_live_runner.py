#!/usr/bin/env python3
from __future__ import annotations

import json
import py_compile
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))
sys.path.insert(0, str(ROOT / "tools"))

from ur10e_example_controllers import step5b_contact_control_core as core  # noqa: E402
from ur10e_example_controllers import step5b_contact_live_runner as runner  # noqa: E402

import step5b_authorization_status as auth_gate  # noqa: E402


class Step5bContactLiveRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._spin_until_future_complete = runner.rclpy.spin_until_future_complete
        runner.rclpy.spin_until_future_complete = lambda *args, **kwargs: None

    def tearDown(self) -> None:
        runner.rclpy.spin_until_future_complete = self._spin_until_future_complete

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

    def test_send_goal_waits_for_succeeded_status_and_success_result(self) -> None:
        node = fake_action_node(
            result_status=runner.GoalStatus.STATUS_SUCCEEDED,
            result_error_code=runner.FollowJointTrajectory.Result.SUCCESSFUL,
        )
        outcome = runner.send_goal(node, [0.0] * 6, fake_q_next(), 0.05)
        self.assertTrue(outcome.accepted)
        self.assertTrue(node.sent_goal)
        self.assertTrue(node.accepted)
        self.assertEqual(node.action_terminal_status, runner.GoalStatus.STATUS_SUCCEEDED)
        self.assertEqual(node.action_result_error_code, runner.FollowJointTrajectory.Result.SUCCESSFUL)
        self.assertEqual(node.action_result_error_string, "ok")

    def test_send_goal_aborted_status_with_success_error_code_fails_closed(self) -> None:
        node = fake_action_node(
            result_status=runner.GoalStatus.STATUS_ABORTED,
            result_error_code=runner.FollowJointTrajectory.Result.SUCCESSFUL,
            error_string="controller reported success after aborted action",
        )
        with self.assertRaisesRegex(RuntimeError, "status=.*error_code=.*error_string="):
            runner.send_goal(node, [0.0] * 6, fake_q_next(), 0.05)
        self.assertTrue(node.sent_goal)
        self.assertTrue(node.accepted)
        self.assertEqual(node.action_terminal_status, runner.GoalStatus.STATUS_ABORTED)
        self.assertEqual(node.action_result_error_code, runner.FollowJointTrajectory.Result.SUCCESSFUL)
        self.assertEqual(node.action_result_error_string, "controller reported success after aborted action")

    def test_send_goal_aborted_result_fails_closed(self) -> None:
        node = fake_action_node(
            result_error_code=-4,
            result_status=runner.GoalStatus.STATUS_ABORTED,
            error_string="aborted",
        )
        with self.assertRaisesRegex(RuntimeError, "result failed"):
            runner.send_goal(node, [0.0] * 6, fake_q_next(), 0.05)
        self.assertTrue(node.sent_goal)
        self.assertTrue(node.accepted)
        self.assertEqual(node.action_terminal_status, runner.GoalStatus.STATUS_ABORTED)
        self.assertEqual(node.action_result_error_code, -4)
        self.assertEqual(node.action_result_error_string, "aborted")

    def test_send_goal_result_timeout_fails_closed(self) -> None:
        node = fake_action_node(result_error_code=runner.FollowJointTrajectory.Result.SUCCESSFUL, result_done=False)
        with self.assertRaisesRegex(RuntimeError, "result timed out"):
            runner.send_goal(node, [0.0] * 6, fake_q_next(), 0.05)
        self.assertTrue(node.sent_goal)
        self.assertTrue(node.accepted)
        self.assertGreater(node.action_result_timeout_s, 0.0)

    def test_send_goal_rejected_fails_before_result(self) -> None:
        node = fake_action_node(accepted=False, result_error_code=runner.FollowJointTrajectory.Result.SUCCESSFUL)
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            runner.send_goal(node, [0.0] * 6, fake_q_next(), 0.05)
        self.assertTrue(node.sent_goal)
        self.assertFalse(node.accepted)
        self.assertIsNone(node.action_terminal_status)

    def test_partial_failure_trace_records_action_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "trace.csv"
            node = SimpleNamespace(
                args=SimpleNamespace(trace=trace),
                sent_goal=True,
                accepted=True,
                action_terminal_status=6,
                action_result_error_code=-4,
                action_result_error_string="aborted",
                action_result_timeout_s=None,
                trace_rows=[
                    {
                        "t_rel_s": 0.1,
                        "stage": 25.0,
                        "cmd_valid": 1.0,
                        "cmd_vx_m_s": 0.0,
                        "cmd_vy_m_s": 0.0,
                        "cmd_vz_m_s": 0.0,
                        "cmd_wx_rad_s": 0.0,
                        "cmd_wy_rad_s": 0.0,
                        "cmd_wz_rad_s": 0.0,
                        "normal_load_n": 0.0,
                        "force_norm_n": 0.0,
                        "force_error_n": 5.0,
                        "orientation_error_rad": 0.0,
                        "hold_reason": "",
                        "normal_filter_source": "locked",
                    }
                ],
            )
            runner.persist_partial_failure_trace(node, RuntimeError("injected"))
            rows = list(csv_dict_rows(trace))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sent_goal"], "True")
        self.assertEqual(rows[0]["accepted"], "True")
        self.assertEqual(rows[0]["action_terminal_status"], "6")
        self.assertEqual(rows[0]["action_result_error_code"], "-4")
        self.assertEqual(rows[0]["action_result_error_string"], "aborted")
        self.assertIn("RuntimeError: injected", rows[0]["failure_reason"])

    def test_live_loop_incomplete_fails_when_search_never_latches(self) -> None:
        stage = runner.live_loop_incomplete_stage(
            state=core.Step5bContactState(normal_acquired=False),
            last_command=SimpleNamespace(result=SimpleNamespace(path_time_s=0.0)),
            params=core.Step5bContactParams(duration_s=12.0),
        )
        self.assertEqual(stage, "contact_search_timeout")
        message = runner.live_loop_incomplete_message(
            stage,
            last_command=SimpleNamespace(result=SimpleNamespace(path_time_s=0.0)),
            params=core.Step5bContactParams(duration_s=12.0),
            max_runtime_s=70.0,
            trace_rows=701,
        )
        self.assertIn("without normal acquisition", message)
        self.assertIn("trace_rows=701", message)

    def test_live_loop_incomplete_fails_when_path_never_completes(self) -> None:
        stage = runner.live_loop_incomplete_stage(
            state=core.Step5bContactState(normal_acquired=True),
            last_command=SimpleNamespace(result=SimpleNamespace(path_time_s=3.0)),
            params=core.Step5bContactParams(duration_s=12.0),
        )
        self.assertEqual(stage, "contact_path_timeout")
        message = runner.live_loop_incomplete_message(
            stage,
            last_command=SimpleNamespace(result=SimpleNamespace(path_time_s=3.0)),
            params=core.Step5bContactParams(duration_s=12.0),
            max_runtime_s=70.0,
            trace_rows=701,
        )
        self.assertIn("path_time_s=3.000", message)
        self.assertIn("duration_s=12.000", message)

    def test_live_loop_complete_returns_no_timeout_stage(self) -> None:
        stage = runner.live_loop_incomplete_stage(
            state=core.Step5bContactState(normal_acquired=True),
            last_command=SimpleNamespace(result=SimpleNamespace(path_time_s=12.0)),
            params=core.Step5bContactParams(duration_s=12.0),
        )
        self.assertIsNone(stage)


class FakeFuture:
    def __init__(self, value: object, *, done: bool = True) -> None:
        self._value = value
        self._done = done

    def result(self) -> object:
        return self._value

    def done(self) -> bool:
        return self._done


class FakeGoalHandle:
    def __init__(self, *, accepted: bool, result_future: FakeFuture) -> None:
        self.accepted = accepted
        self._result_future = result_future

    def get_result_async(self) -> FakeFuture:
        return self._result_future


class FakeActionClient:
    def __init__(self, handle: FakeGoalHandle) -> None:
        self.handle = handle
        self.sent_goals = []

    def send_goal_async(self, goal: object) -> FakeFuture:
        self.sent_goals.append(goal)
        return FakeFuture(self.handle)


def fake_action_node(
    *,
    accepted: bool = True,
    result_error_code: int,
    result_status: int = runner.GoalStatus.STATUS_SUCCEEDED,
    error_string: str = "ok",
    result_done: bool = True,
) -> SimpleNamespace:
    result_payload = SimpleNamespace(
        status=result_status,
        result=SimpleNamespace(error_code=result_error_code, error_string=error_string),
    )
    result_future = FakeFuture(result_payload, done=result_done)
    handle = FakeGoalHandle(accepted=accepted, result_future=result_future)
    return SimpleNamespace(
        args=SimpleNamespace(wait_s=0.1),
        action_client=FakeActionClient(handle),
        sent_goal=False,
        accepted=False,
        action_terminal_status=None,
        action_result_error_code=None,
        action_result_error_string=None,
        action_result_timeout_s=None,
    )


def fake_q_next() -> object:
    return [0.01] * 6


def csv_dict_rows(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


if __name__ == "__main__":
    unittest.main()
