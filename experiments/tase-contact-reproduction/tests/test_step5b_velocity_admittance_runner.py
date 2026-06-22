#!/usr/bin/env python3
from __future__ import annotations

import py_compile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers import ros2_controller_switching as switching  # noqa: E402
from ur10e_example_controllers import step5b_velocity_admittance_runner as velocity  # noqa: E402


class Step5bVelocityAdmittanceRunnerTest(unittest.TestCase):
    def test_entrypoint_compiles_and_declares_forward_velocity_contract(self) -> None:
        entrypoint = Path(velocity.__file__).resolve()
        py_compile.compile(str(entrypoint), doraise=True)
        contract = velocity.acceptance_contract()
        self.assertEqual(contract["contact_control_strategy"], "forward_velocity_admittance")
        self.assertEqual(contract["velocity_command_topic"], "/forward_velocity_controller/commands")
        self.assertEqual(contract["velocity_command_type"], "std_msgs/msg/Float64MultiArray")
        self.assertEqual(contract["live_runner_route"], "ros2_remote_control_headless")
        self.assertEqual(contract["joint_order"], velocity.JOINT_NAMES)

    def test_source_uses_no_follow_joint_trajectory_action_client(self) -> None:
        text = Path(velocity.__file__).read_text(encoding="utf-8")
        self.assertNotIn("FollowJointTrajectory", text)
        self.assertNotIn("ActionClient", text)
        self.assertNotIn("scaled_joint_trajectory_controller/follow_joint_trajectory", text)

    def test_damped_least_squares_qdot_respects_joint_velocity_cap(self) -> None:
        jacobian = np.eye(6)
        twist = (0.2, -0.1, 0.08, 0.0, 0.03, -0.04)
        qdot = velocity.damped_least_squares_qdot(
            jacobian,
            twist,
            damping=0.0,
            max_joint_velocity_rad_s=0.05,
        )
        self.assertEqual(qdot.shape, (6,))
        self.assertLessEqual(float(np.max(np.abs(qdot))), 0.05 + 1e-12)

    def test_publish_velocity_command_sends_six_element_float64_multi_array(self) -> None:
        publisher = FakePublisher()
        velocity.publish_velocity_command(publisher, [0.01, 0.02, 0.03, 0.04, 0.05, 0.06])
        self.assertEqual(len(publisher.messages), 1)
        self.assertEqual(list(publisher.messages[0].data), [0.01, 0.02, 0.03, 0.04, 0.05, 0.06])

    def test_stale_kunwei_zeroes_then_returns_nonzero_failure(self) -> None:
        publisher = FakePublisher()
        monitor = FakeMonitor(RuntimeError("Kunwei monitor stale or not ready: waiting_for_recent_window"))
        result = velocity.assert_monitor_ready_or_stop(
            monitor,
            publisher,
            reason_prefix="pre_publish_guard",
        )
        self.assertFalse(result.ok)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("pre_publish_guard", result.failure_reason)
        self.assertEqual(list(publisher.messages[-1].data), [0.0] * 6)

    def test_velocity_trace_summary_records_forward_velocity_strategy(self) -> None:
        summary = velocity.velocity_trace_summary(
            rows=[
                {"loop_dt_s": 0.0020, "published": True},
                {"loop_dt_s": 0.0024, "published": True},
                {"loop_dt_s": 0.0018, "published": False},
            ],
            command_period_s=0.002,
        )
        self.assertEqual(summary["contact_control_strategy"], "forward_velocity_admittance")
        self.assertEqual(summary["intended_command_period_s"], 0.002)
        self.assertEqual(summary["published_velocity_command_count"], 2)
        self.assertGreater(summary["actual_loop_rate_hz"], 0.0)
        self.assertGreaterEqual(summary["loop_jitter_p95_s"], 0.0)


class ControllerSwitchingTest(unittest.TestCase):
    def test_scaled_active_velocity_inactive_switches_to_forward_velocity(self) -> None:
        node = FakeNode(
            list_responses=[
                [
                    controller("scaled_joint_trajectory_controller", "active"),
                    controller("forward_velocity_controller", "inactive"),
                ],
                [
                    controller("scaled_joint_trajectory_controller", "inactive"),
                    controller("forward_velocity_controller", "active"),
                ],
            ],
            switch_ok=True,
        )
        result = switching.ensure_forward_velocity_controller(node, controller_manager="/controller_manager", timeout_s=0.1)
        self.assertTrue(result.ok)
        self.assertTrue(result.switched)
        self.assertEqual(node.switch_requests[0].activate_controllers, ["forward_velocity_controller"])
        self.assertEqual(node.switch_requests[0].deactivate_controllers, ["scaled_joint_trajectory_controller"])

    def test_switch_failure_blocks_velocity_command_publication(self) -> None:
        node = FakeNode(
            list_responses=[
                [
                    controller("scaled_joint_trajectory_controller", "active"),
                    controller("forward_velocity_controller", "inactive"),
                ]
            ],
            switch_ok=False,
        )
        result = switching.ensure_forward_velocity_controller(node, controller_manager="/controller_manager", timeout_s=0.1)
        self.assertFalse(result.ok)
        self.assertIn("switch_controller_failed", result.reason)

    def test_velocity_already_active_does_not_switch(self) -> None:
        node = FakeNode(
            list_responses=[
                [
                    controller("scaled_joint_trajectory_controller", "inactive"),
                    controller("forward_velocity_controller", "active"),
                ]
            ],
            switch_ok=True,
        )
        result = switching.ensure_forward_velocity_controller(node, controller_manager="/controller_manager", timeout_s=0.1)
        self.assertTrue(result.ok)
        self.assertFalse(result.switched)
        self.assertEqual(node.switch_requests, [])


class FakePublisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, msg: object) -> None:
        self.messages.append(msg)


class FakeMonitor:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls = 0

    def assert_fresh_and_within_force_delta(self) -> None:
        self.calls += 1
        if self.exc is not None:
            raise self.exc


class FakeFuture:
    def __init__(self, value: object) -> None:
        self._value = value

    def done(self) -> bool:
        return True

    def result(self) -> object:
        return self._value


class FakeClient:
    def __init__(self, response_factory) -> None:
        self._response_factory = response_factory

    def wait_for_service(self, timeout_sec: float) -> bool:
        return True

    def call_async(self, request: object) -> FakeFuture:
        return FakeFuture(self._response_factory(request))


class FakeNode:
    def __init__(self, *, list_responses: list[list[object]], switch_ok: bool) -> None:
        self._list_responses = list(list_responses)
        self._switch_ok = switch_ok
        self.switch_requests = []

    def create_client(self, srv_type: object, service_name: str) -> FakeClient:
        if service_name.endswith("/list_controllers"):
            return FakeClient(lambda request: SimpleNamespace(controller=self._list_responses.pop(0)))
        if service_name.endswith("/switch_controller"):
            def respond(request: object) -> object:
                self.switch_requests.append(request)
                return SimpleNamespace(ok=self._switch_ok)

            return FakeClient(respond)
        raise AssertionError(service_name)


def controller(name: str, state: str) -> object:
    return SimpleNamespace(name=name, state=state)


if __name__ == "__main__":
    unittest.main()
