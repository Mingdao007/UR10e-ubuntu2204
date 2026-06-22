from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import rclpy
from controller_manager_msgs.srv import ListControllers, SwitchController


SCALED_TRAJECTORY_CONTROLLER = "scaled_joint_trajectory_controller"
FORWARD_VELOCITY_CONTROLLER = "forward_velocity_controller"


@dataclass(frozen=True)
class ControllerSwitchResult:
    ok: bool
    reason: str
    switched: bool
    controllers: dict[str, str]


def ensure_forward_velocity_controller(
    node: Any,
    *,
    controller_manager: str = "/controller_manager",
    timeout_s: float = 5.0,
    velocity_controller: str = FORWARD_VELOCITY_CONTROLLER,
    trajectory_controller: str = SCALED_TRAJECTORY_CONTROLLER,
) -> ControllerSwitchResult:
    controllers = list_controller_states(node, controller_manager=controller_manager, timeout_s=timeout_s)
    if controllers.get(velocity_controller) == "active":
        return ControllerSwitchResult(True, "forward_velocity_controller_already_active", False, controllers)
    if velocity_controller not in controllers:
        return ControllerSwitchResult(False, f"missing_controller:{velocity_controller}", False, controllers)

    switch_client = node.create_client(SwitchController, _service_name(controller_manager, "switch_controller"))
    if not switch_client.wait_for_service(timeout_sec=timeout_s):
        return ControllerSwitchResult(False, "switch_controller_service_unavailable", False, controllers)

    request = SwitchController.Request()
    request.activate_controllers = [velocity_controller]
    request.deactivate_controllers = [trajectory_controller] if controllers.get(trajectory_controller) == "active" else []
    request.strictness = getattr(SwitchController.Request, "STRICT", 2)
    request.start_asap = True
    future = switch_client.call_async(request)
    _spin_until_done(node, future, timeout_s)
    if not future.done() or future.result() is None:
        return ControllerSwitchResult(False, "switch_controller_no_result", False, controllers)
    if not bool(getattr(future.result(), "ok", False)):
        return ControllerSwitchResult(False, "switch_controller_failed", False, controllers)

    after = list_controller_states(node, controller_manager=controller_manager, timeout_s=timeout_s)
    if after.get(velocity_controller) != "active":
        return ControllerSwitchResult(False, f"forward_velocity_controller_not_active:{after.get(velocity_controller)}", True, after)
    return ControllerSwitchResult(True, "forward_velocity_controller_active", True, after)


def list_controller_states(
    node: Any,
    *,
    controller_manager: str = "/controller_manager",
    timeout_s: float = 5.0,
) -> dict[str, str]:
    list_client = node.create_client(ListControllers, _service_name(controller_manager, "list_controllers"))
    if not list_client.wait_for_service(timeout_sec=timeout_s):
        raise RuntimeError("list_controllers_service_unavailable")
    future = list_client.call_async(ListControllers.Request())
    _spin_until_done(node, future, timeout_s)
    if not future.done() or future.result() is None:
        raise RuntimeError("list_controllers_no_result")
    return {controller.name: controller.state for controller in future.result().controller}


def _service_name(controller_manager: str, service: str) -> str:
    return controller_manager.rstrip("/") + "/" + service


def _spin_until_done(node: Any, future: Any, timeout_s: float) -> None:
    if future.done():
        return
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout_s)
