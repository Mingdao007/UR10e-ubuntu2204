#!/usr/bin/env python3
from __future__ import annotations

import unittest
from types import SimpleNamespace
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ur10e_example_controllers import ros2_controller_switching as switching


class ControllerSwitchingTest(unittest.TestCase):
    def test_watchdog_activate_switches_active_watchdog_and_deactivates_forward_and_trajectory(self) -> None:
        node = FakeNode(
            list_responses=[
                [
                    _controller("scaled_joint_trajectory_controller", "active"),
                    _controller("forward_velocity_controller", "active"),
                    _controller("step5d_watchdog_controller", "inactive"),
                ],
                [
                    _controller("scaled_joint_trajectory_controller", "inactive"),
                    _controller("forward_velocity_controller", "inactive"),
                    _controller("step5d_watchdog_controller", "active"),
                ],
            ],
            switch_ok=True,
        )
        result = switching.ensure_controller_active(
            node,
            controller_manager="/controller_manager",
            timeout_s=0.1,
            activate_controller="step5d_watchdog_controller",
            deactivate_active_controllers=[
                switching.SCALED_TRAJECTORY_CONTROLLER,
                switching.FORWARD_VELOCITY_CONTROLLER,
            ],
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.switched)
        self.assertEqual(result.reason, "controller_active")
        self.assertEqual(node.switch_requests[0].activate_controllers, ["step5d_watchdog_controller"])
        self.assertEqual(
            sorted(node.switch_requests[0].deactivate_controllers),
            sorted(["scaled_joint_trajectory_controller", "forward_velocity_controller"]),
        )

    def test_watchdog_activate_no_switch_when_already_active(self) -> None:
        node = FakeNode(
            list_responses=[
                [
                    _controller("step5d_watchdog_controller", "active"),
                ],
            ],
            switch_ok=True,
        )
        result = switching.ensure_controller_active(
            node,
            activate_controller="step5d_watchdog_controller",
            timeout_s=0.1,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.switched)
        self.assertEqual(result.reason, "controller_already_active")
        self.assertEqual(node.switch_requests, [])

    def test_watchdog_activate_missing_controller(self) -> None:
        node = FakeNode(
            list_responses=[
                [
                    _controller("forward_velocity_controller", "inactive"),
                ],
            ],
            switch_ok=True,
        )
        result = switching.ensure_controller_active(
            node,
            activate_controller="step5d_watchdog_controller",
            timeout_s=0.1,
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.switched)
        self.assertIn("missing_controller:step5d_watchdog_controller", result.reason)

    def test_watchdog_deactivate_only_target_controller(self) -> None:
        node = FakeNode(
            list_responses=[
                [
                    _controller("step5d_watchdog_controller", "active"),
                    _controller("scaled_joint_trajectory_controller", "active"),
                    _controller("forward_velocity_controller", "inactive"),
                ],
                [
                    _controller("step5d_watchdog_controller", "inactive"),
                    _controller("scaled_joint_trajectory_controller", "active"),
                    _controller("forward_velocity_controller", "inactive"),
                ],
            ],
            switch_ok=True,
        )
        result = switching.ensure_controller_inactive(
            node,
            timeout_s=0.1,
            deactivate_controller="step5d_watchdog_controller",
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.switched)
        self.assertEqual(result.reason, "controller_inactive")
        self.assertEqual(node.switch_requests[0].activate_controllers, [])
        self.assertEqual(node.switch_requests[0].deactivate_controllers, ["step5d_watchdog_controller"])

    def test_watchdog_switch_failure_returns_failure_reason(self) -> None:
        node = FakeNode(
            list_responses=[
                [
                    _controller("step5d_watchdog_controller", "inactive"),
                ],
            ],
            switch_ok=False,
        )
        result = switching.ensure_controller_active(
            node,
            activate_controller="step5d_watchdog_controller",
            timeout_s=0.1,
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.switched)
        self.assertEqual(result.reason, "switch_controller_failed")

    def test_watchdog_unload_verifies_controller_disappears(self) -> None:
        node = FakeNode(
            list_responses=[
                [_controller("step5d_watchdog_controller", "inactive")],
                [],
            ],
            switch_ok=True,
        )
        result = switching.unload_controller(
            node,
            timeout_s=0.1,
            controller="step5d_watchdog_controller",
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.unloaded)
        self.assertEqual(result.reason, "controller_unloaded")
        self.assertEqual(node.unload_requests[0].name, "step5d_watchdog_controller")

    def test_watchdog_unload_refuses_active_controller(self) -> None:
        node = FakeNode(
            list_responses=[[_controller("step5d_watchdog_controller", "active")]],
            switch_ok=True,
        )
        result = switching.unload_controller(
            node,
            timeout_s=0.1,
            controller="step5d_watchdog_controller",
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.unloaded)
        self.assertEqual(result.reason, "controller_still_active")
        self.assertEqual(node.unload_requests, [])


class FakeFuture:
    def __init__(self, response: object) -> None:
        self._response = response

    def done(self) -> bool:
        return True

    def result(self) -> object:
        return self._response


class FakeClient:
    def __init__(self, callback) -> None:
        self._callback = callback

    def wait_for_service(self, timeout_sec: float) -> bool:
        return True

    def call_async(self, request: object) -> FakeFuture:
        return FakeFuture(self._callback(request))


class FakeNode:
    def __init__(
        self,
        *,
        list_responses: list[list[object]],
        switch_ok: bool,
        unload_ok: bool = True,
    ) -> None:
        self._list_responses = list(list_responses)
        self._switch_ok = switch_ok
        self._unload_ok = unload_ok
        self.switch_requests: list[object] = []
        self.unload_requests: list[object] = []

    def create_client(self, srv_type: object, service_name: str):
        if service_name.endswith("/list_controllers"):
            return FakeClient(lambda request: SimpleNamespace(controller=self._list_responses.pop(0)))
        if service_name.endswith("/switch_controller"):
            return FakeClient(self._switch_response)
        if service_name.endswith("/unload_controller"):
            return FakeClient(self._unload_response)
        raise AssertionError(f"unexpected service: {service_name}")

    def _switch_response(self, request: object) -> object:
        self.switch_requests.append(request)
        return SimpleNamespace(ok=self._switch_ok)

    def _unload_response(self, request: object) -> object:
        self.unload_requests.append(request)
        return SimpleNamespace(ok=self._unload_ok)


def _controller(name: str, state: str) -> object:
    return SimpleNamespace(name=name, state=state)


if __name__ == "__main__":
    unittest.main()
