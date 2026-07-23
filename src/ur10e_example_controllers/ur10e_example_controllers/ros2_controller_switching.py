from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import rclpy
from controller_manager_msgs.srv import ListControllers, SwitchController, UnloadController


SCALED_TRAJECTORY_CONTROLLER = "scaled_joint_trajectory_controller"
FORWARD_VELOCITY_CONTROLLER = "forward_velocity_controller"
STEP5D_WATCHDOG_CONTROLLER = "step5d_watchdog_controller"


@dataclass(frozen=True)
class ControllerSwitchResult:
    ok: bool
    reason: str
    switched: bool
    controllers: dict[str, str]


@dataclass(frozen=True)
class ControllerUnloadResult:
    ok: bool
    reason: str
    unloaded: bool
    controllers: dict[str, str]


def ensure_forward_velocity_controller(
    node: Any,
    *,
    controller_manager: str = "/controller_manager",
    timeout_s: float = 5.0,
    velocity_controller: str = FORWARD_VELOCITY_CONTROLLER,
    trajectory_controller: str = SCALED_TRAJECTORY_CONTROLLER,
) -> ControllerSwitchResult:
    return ensure_controller_active(
        node,
        controller_manager=controller_manager,
        timeout_s=timeout_s,
        activate_controller=velocity_controller,
        deactivate_active_controllers={trajectory_controller},
        already_active_reason="forward_velocity_controller_already_active",
        missing_reason_prefix="missing_controller",
        activate_failed_reason="switch_controller_failed",
        verify_inactive_reason_prefix="forward_velocity_controller_not_active",
        success_reason="forward_velocity_controller_active",
    )


def ensure_controller_active(
    node: Any,
    *,
    controller_manager: str = "/controller_manager",
    timeout_s: float = 5.0,
    activate_controller: str,
    deactivate_active_controllers: set[str] | tuple[str, ...] | list[str] | None = None,
    already_active_reason: str = "controller_already_active",
    missing_reason_prefix: str = "missing_controller",
    activate_failed_reason: str = "switch_controller_failed",
    verify_inactive_reason_prefix: str = "controller_not_active",
    success_reason: str = "controller_active",
) -> ControllerSwitchResult:
    controllers = list_controller_states(node, controller_manager=controller_manager, timeout_s=timeout_s)
    if controllers.get(activate_controller) == "active":
        return ControllerSwitchResult(True, already_active_reason, False, controllers)
    if activate_controller not in controllers:
        return ControllerSwitchResult(False, f"{missing_reason_prefix}:{activate_controller}", False, controllers)

    switch_client = node.create_client(SwitchController, _service_name(controller_manager, "switch_controller"))
    if not switch_client.wait_for_service(timeout_sec=timeout_s):
        return ControllerSwitchResult(False, "switch_controller_service_unavailable", False, controllers)

    request = SwitchController.Request()
    request.activate_controllers = [activate_controller]
    request.deactivate_controllers = []
    if deactivate_active_controllers:
        request.deactivate_controllers = [
            str(controller_name)
            for controller_name in deactivate_active_controllers
            if controllers.get(controller_name) == "active"
        ]
    request.strictness = getattr(SwitchController.Request, "STRICT", 2)
    request.start_asap = True
    future = switch_client.call_async(request)
    _spin_until_done(node, future, timeout_s)
    if not future.done() or future.result() is None:
        return ControllerSwitchResult(False, "switch_controller_no_result", False, controllers)
    if not bool(getattr(future.result(), "ok", False)):
        return ControllerSwitchResult(False, activate_failed_reason, False, controllers)

    after = list_controller_states(node, controller_manager=controller_manager, timeout_s=timeout_s)
    if after.get(activate_controller) != "active":
        return ControllerSwitchResult(False, f"{verify_inactive_reason_prefix}:{after.get(activate_controller)}", True, after)
    return ControllerSwitchResult(True, success_reason, True, after)


def ensure_controller_inactive(
    node: Any,
    *,
    controller_manager: str = "/controller_manager",
    timeout_s: float = 5.0,
    deactivate_controller: str,
    already_inactive_reason: str = "controller_already_inactive",
    missing_reason_prefix: str = "missing_controller",
    deactivate_failed_reason: str = "deactivate_controller_failed",
    verify_active_reason_prefix: str = "controller_not_inactive",
    success_reason: str = "controller_inactive",
) -> ControllerSwitchResult:
    controllers = list_controller_states(node, controller_manager=controller_manager, timeout_s=timeout_s)
    if controllers.get(deactivate_controller) == "inactive":
        return ControllerSwitchResult(True, already_inactive_reason, False, controllers)
    if deactivate_controller not in controllers:
        return ControllerSwitchResult(False, f"{missing_reason_prefix}:{deactivate_controller}", False, controllers)

    switch_client = node.create_client(SwitchController, _service_name(controller_manager, "switch_controller"))
    if not switch_client.wait_for_service(timeout_sec=timeout_s):
        return ControllerSwitchResult(False, "switch_controller_service_unavailable", False, controllers)

    request = SwitchController.Request()
    request.deactivate_controllers = [deactivate_controller]
    request.activate_controllers = []
    request.strictness = getattr(SwitchController.Request, "STRICT", 2)
    request.start_asap = True
    future = switch_client.call_async(request)
    _spin_until_done(node, future, timeout_s)
    if not future.done() or future.result() is None:
        return ControllerSwitchResult(False, "switch_controller_no_result", False, controllers)
    if not bool(getattr(future.result(), "ok", False)):
        return ControllerSwitchResult(False, deactivate_failed_reason, False, controllers)

    after = list_controller_states(node, controller_manager=controller_manager, timeout_s=timeout_s)
    if after.get(deactivate_controller) != "inactive":
        return ControllerSwitchResult(
            False,
            f"{verify_active_reason_prefix}:{after.get(deactivate_controller)}",
            True,
            after,
        )
    return ControllerSwitchResult(True, success_reason, True, after)


def unload_controller(
    node: Any,
    *,
    controller_manager: str = "/controller_manager",
    timeout_s: float = 5.0,
    controller: str,
) -> ControllerUnloadResult:
    controllers = list_controller_states(node, controller_manager=controller_manager, timeout_s=timeout_s)
    if controller not in controllers:
        return ControllerUnloadResult(True, "controller_not_loaded", False, controllers)
    if controllers[controller] == "active":
        return ControllerUnloadResult(False, "controller_still_active", False, controllers)

    client = node.create_client(UnloadController, _service_name(controller_manager, "unload_controller"))
    if not client.wait_for_service(timeout_sec=timeout_s):
        return ControllerUnloadResult(False, "unload_controller_service_unavailable", False, controllers)
    request = UnloadController.Request()
    request.name = controller
    future = client.call_async(request)
    _spin_until_done(node, future, timeout_s)
    if not future.done() or future.result() is None:
        return ControllerUnloadResult(False, "unload_controller_no_result", False, controllers)
    if not bool(getattr(future.result(), "ok", False)):
        return ControllerUnloadResult(False, "unload_controller_failed", False, controllers)

    after = list_controller_states(node, controller_manager=controller_manager, timeout_s=timeout_s)
    if controller in after:
        return ControllerUnloadResult(False, "controller_still_loaded", True, after)
    return ControllerUnloadResult(True, "controller_unloaded", True, after)


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
