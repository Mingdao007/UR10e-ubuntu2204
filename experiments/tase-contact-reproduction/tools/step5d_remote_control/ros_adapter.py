"""Lazy ROS 2, Dashboard, and Kunwei bindings for Step5d remote control.

Importing this module does not connect to the robot or sensor.  Network and
controller actions only happen after ``live.run_live`` invokes the returned
dependency methods.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import engine, primitives
from .kinematics import CANONICAL_JOINTS, CalibratedKinematics


WATCHDOG_WAITING_ZERO = 0
WATCHDOG_ACTIVE = 1
WATCHDOG_STATUS_STALE_S = 0.5
DASHBOARD_PORT = 29999


class DashboardClient:
    """Small read-only client for the four canonical Dashboard safety queries."""

    def __init__(self, robot_ip: str, *, timeout_s: float = 1.0) -> None:
        self._endpoint = (str(robot_ip), DASHBOARD_PORT)
        self._timeout_s = float(timeout_s)

    def poll(self) -> dict[str, str]:
        responses: dict[str, str] = {}
        with socket.create_connection(self._endpoint, timeout=self._timeout_s) as sock:
            sock.settimeout(self._timeout_s)
            stream = sock.makefile("rwb", buffering=0)
            greeting = stream.readline()
            if not greeting:
                raise RuntimeError("dashboard connection closed before greeting")
            for command in primitives.DASHBOARD_COMMANDS:
                stream.write((command + "\n").encode("ascii"))
                response = stream.readline()
                if not response:
                    raise RuntimeError(f"dashboard connection closed after {command!r}")
                responses[command] = response.decode("utf-8", errors="replace").strip()
        return responses


class DashboardWatcher:
    """Poll Dashboard off the command thread and expose a non-blocking snapshot."""

    def __init__(
        self,
        client: DashboardClient,
        *,
        interval_s: float = 1.0,
        stale_timeout_s: float = 5.0,
    ) -> None:
        self._client = client
        self._interval_s = float(interval_s)
        self._stale_timeout_s = float(stale_timeout_s)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, str] | None = None
        self._updated_at_s: float | None = None
        self._error: str | None = None

    def poll_now(self) -> dict[str, str]:
        snapshot = self._client.poll()
        with self._lock:
            self._snapshot = dict(snapshot)
            self._updated_at_s = time.monotonic()
            self._error = None
        return snapshot

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="step5d-dashboard-safety",
            daemon=True,
        )
        self._thread.start()

    def check(self) -> dict[str, str]:
        now = time.monotonic()
        with self._lock:
            snapshot = None if self._snapshot is None else dict(self._snapshot)
            updated_at_s = self._updated_at_s
            error = self._error
        if error is not None:
            raise RuntimeError(f"dashboard watcher failed: {error}")
        if snapshot is None or updated_at_s is None:
            raise RuntimeError("dashboard watcher has no snapshot")
        if now - updated_at_s > self._stale_timeout_s:
            raise RuntimeError("dashboard watcher snapshot stale")
        return snapshot

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self._stale_timeout_s))
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self.poll_now()
            except Exception as exc:  # pragma: no cover - physical endpoint path
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"
                return


class RosRuntime:
    """Owns ROS topic I/O and a separate node for controller services."""

    def __init__(
        self,
        cfg: Mapping[str, Any],
        *,
        kinematics: CalibratedKinematics,
        monitor: Any,
    ) -> None:
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import (
            DurabilityPolicy,
            HistoryPolicy,
            QoSProfile,
            ReliabilityPolicy,
            qos_profile_sensor_data,
        )
        from sensor_msgs.msg import JointState
        from std_msgs.msg import Float64MultiArray, UInt8

        self._rclpy = rclpy
        self._Float64MultiArray = Float64MultiArray
        self._kinematics = kinematics
        self._monitor = monitor
        self._controller_manager = str(cfg["robot"]["controller_manager"])
        self._lock = threading.Lock()
        self._joint_sample: tuple[tuple[float, ...], tuple[float, ...], float] | None = None
        self._watchdog: tuple[int, float] | None = None
        self._executor_error: str | None = None
        self._owns_context = not rclpy.ok()
        if self._owns_context:
            rclpy.init(args=None)

        suffix = str(int(time.time_ns() % 1_000_000_000))
        self._topic_node = rclpy.create_node(f"step5d_remote_topics_{suffix}")
        self._service_node = rclpy.create_node(f"step5d_remote_services_{suffix}")
        command_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        status_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._publisher = self._topic_node.create_publisher(
            Float64MultiArray,
            str(cfg["controller"]["command_topic"]),
            command_qos,
        )
        self._joint_subscription = self._topic_node.create_subscription(
            JointState,
            str(cfg["controller"]["joint_state_topic"]),
            self._on_joint_state,
            qos_profile_sensor_data,
        )
        self._status_subscription = self._topic_node.create_subscription(
            UInt8,
            str(cfg["controller"]["status_topic"]),
            self._on_watchdog,
            status_qos,
        )
        self._executor = SingleThreadedExecutor()
        self._executor.add_node(self._topic_node)
        self._executor_thread = threading.Thread(
            target=self._spin,
            name="step5d-ros-topic-executor",
            daemon=True,
        )
        self._executor_thread.start()

    def publish(self, qdot: Sequence[float]) -> None:
        values = tuple(float(value) for value in qdot)
        if len(values) != 6:
            raise ValueError("joint velocity command must contain six values")
        message = self._Float64MultiArray()
        message.data = list(values)
        self._publisher.publish(message)

    def sample(self) -> engine.KinematicSample:
        self._raise_executor_error()
        with self._lock:
            joint_sample = self._joint_sample
        if joint_sample is None:
            raise RuntimeError("joint-state sample unavailable")
        q, qd, received_at_s = joint_sample
        kinematic = self._kinematics.evaluate(q, qd, received_at_s)
        wrench = self._monitor.latest_zeroed_wrench_si(now=time.monotonic())
        return engine.KinematicSample(
            q=kinematic.q,
            qd=kinematic.qd,
            tcp_pose=kinematic.tcp_pose,
            tcp_twist=kinematic.tcp_twist,
            wrench_tcp=tuple(float(value) for value in wrench),
            jacobian=kinematic.jacobian,
            q_min=kinematic.q_min,
            q_max=kinematic.q_max,
            timestamp_s=kinematic.timestamp_s,
        )

    def watchdog_status(self) -> int:
        self._raise_executor_error()
        with self._lock:
            watchdog = self._watchdog
        if watchdog is None:
            return WATCHDOG_WAITING_ZERO
        status, received_at_s = watchdog
        if time.monotonic() - received_at_s > WATCHDOG_STATUS_STALE_S:
            raise RuntimeError("watchdog status stale")
        return status

    def spawn_controller(self, command: tuple[str, ...]) -> None:
        subprocess.run(command, check=True, timeout=20.0)

    def activate_watchdog(self) -> None:
        from ur10e_example_controllers.ros2_controller_switching import (
            FORWARD_VELOCITY_CONTROLLER,
            SCALED_TRAJECTORY_CONTROLLER,
            STEP5D_WATCHDOG_CONTROLLER,
            ensure_controller_active,
        )

        result = ensure_controller_active(
            self._service_node,
            controller_manager=self._controller_manager,
            timeout_s=10.0,
            activate_controller=STEP5D_WATCHDOG_CONTROLLER,
            deactivate_active_controllers={
                SCALED_TRAJECTORY_CONTROLLER,
                FORWARD_VELOCITY_CONTROLLER,
            },
        )
        if not result.ok:
            raise RuntimeError(result.reason)

    def deactivate_watchdog(self) -> None:
        from ur10e_example_controllers.ros2_controller_switching import (
            STEP5D_WATCHDOG_CONTROLLER,
            ensure_controller_inactive,
        )

        result = ensure_controller_inactive(
            self._service_node,
            controller_manager=self._controller_manager,
            timeout_s=5.0,
            deactivate_controller=STEP5D_WATCHDOG_CONTROLLER,
        )
        if not result.ok:
            raise RuntimeError(result.reason)

    def unload_watchdog(self) -> None:
        from ur10e_example_controllers.ros2_controller_switching import (
            STEP5D_WATCHDOG_CONTROLLER,
            unload_controller,
        )

        result = unload_controller(
            self._service_node,
            controller_manager=self._controller_manager,
            timeout_s=5.0,
            controller=STEP5D_WATCHDOG_CONTROLLER,
        )
        if not result.ok:
            raise RuntimeError(result.reason)

    def close(self) -> None:
        self._executor.shutdown(timeout_sec=2.0)
        self._executor_thread.join(timeout=2.0)
        self._executor.remove_node(self._topic_node)
        self._topic_node.destroy_node()
        self._service_node.destroy_node()
        if self._owns_context and self._rclpy.ok():
            self._rclpy.shutdown()

    def _on_joint_state(self, message: Any) -> None:
        names = tuple(str(name) for name in message.name)
        if len(names) != len(set(names)):
            return
        positions = tuple(float(value) for value in message.position)
        velocities = tuple(float(value) for value in message.velocity)
        if len(positions) != len(names) or len(velocities) != len(names):
            return
        index = {name: position for position, name in enumerate(names)}
        if any(name not in index for name in CANONICAL_JOINTS):
            return
        q = tuple(positions[index[name]] for name in CANONICAL_JOINTS)
        qd = tuple(velocities[index[name]] for name in CANONICAL_JOINTS)
        with self._lock:
            self._joint_sample = (q, qd, time.monotonic())

    def _on_watchdog(self, message: Any) -> None:
        with self._lock:
            self._watchdog = (int(message.data), time.monotonic())

    def _spin(self) -> None:
        try:
            self._executor.spin()
        except Exception as exc:  # pragma: no cover - ROS executor failure path
            with self._lock:
                self._executor_error = f"{type(exc).__name__}: {exc}"

    def _raise_executor_error(self) -> None:
        with self._lock:
            error = self._executor_error
        if error is not None:
            raise RuntimeError(f"ROS topic executor failed: {error}")


def _build_monitor(cfg: Mapping[str, Any], raw_frames_path: Path) -> Any:
    from ur10e_example_controllers.kunwei_persistent_monitor import (
        KunweiMonitorConfig,
        KunweiPersistentMonitor,
    )

    sensor = cfg["sensor"]
    return KunweiPersistentMonitor(
        KunweiMonitorConfig(
            sensor_ip=str(sensor["ip"]),
            sensor_port=int(sensor["port"]),
            connect_timeout_s=float(sensor["connect_timeout_s"]),
            recv_timeout_s=float(sensor["recv_timeout_s"]),
            window_s=float(sensor["window_s"]),
            latest_max_age_s=float(sensor["latest_max_age_s"]),
            min_recent_samples=int(sensor["min_recent_samples"]),
            max_force_delta_n=float(cfg["force"]["max_force_norm_n"]),
            raw_frames_path=raw_frames_path,
        )
    )


def _launch_driver(command: tuple[str, ...]) -> subprocess.Popen[Any]:
    return subprocess.Popen(command, start_new_session=True)


def _stop_driver(process: subprocess.Popen[Any]) -> None:
    process_group = int(process.pid)

    def group_exists() -> bool:
        process.poll()
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        return True

    def stop_group(sig: signal.Signals, timeout_s: float) -> bool:
        try:
            os.killpg(process_group, sig)
        except ProcessLookupError:
            return True
        deadline_s = time.monotonic() + timeout_s
        while time.monotonic() < deadline_s:
            if not group_exists():
                return True
            time.sleep(0.05)
        return not group_exists()

    if not group_exists():
        process.poll()
        return
    if not stop_group(signal.SIGINT, 5.0):
        if not stop_group(signal.SIGTERM, 2.0):
            if not stop_group(signal.SIGKILL, 2.0):
                raise RuntimeError(f"driver process group {process_group} did not exit")
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


def build_production_dependencies(
    cfg: Mapping[str, Any],
    output_root: Path,
) -> Any:
    """Build, but do not start, the production I/O dependency bundle."""

    from .live import LiveDependencies
    from .runtime import read_boot_id

    monitor = _build_monitor(cfg, output_root / "kunwei_raw_frames.bin")
    kinematics = CalibratedKinematics.from_config(cfg)
    ros = RosRuntime(cfg, kinematics=kinematics, monitor=monitor)
    dashboard = DashboardWatcher(DashboardClient(str(cfg["robot"]["ip"])))

    def preflight() -> None:
        from ament_index_python.packages import get_package_prefix

        get_package_prefix("ur10e_step5d_remote_watchdog")
        kernel = engine.DirectControlKernel.create(cfg)
        kernel.reset()
        solver = kernel.solver
        if not bool(getattr(solver, "cupy_host_staging_pinned", False)):
            raise RuntimeError("CuPy pinned host staging is unavailable")
        if not bool(getattr(solver, "cupy_dedicated_stream", False)):
            raise RuntimeError("CuPy dedicated stream is unavailable")
        equivalence = getattr(solver, "cupy_parallel_equivalence", None)
        if not isinstance(equivalence, Mapping) or equivalence.get("bitwise_equal") is not True:
            raise RuntimeError("CuPy parallel solver equivalence gate failed")

    return LiveDependencies(
        monotonic=time.monotonic,
        epoch_time=time.time,
        sleep=time.sleep,
        dashboard_poll=dashboard.poll_now,
        dashboard_start=dashboard.start,
        dashboard_check=dashboard.check,
        dashboard_stop=dashboard.stop,
        sample_provider=ros.sample,
        publish_cmd=ros.publish,
        watchdog_status=ros.watchdog_status,
        monitor=monitor,
        driver_launch=_launch_driver,
        driver_stop=_stop_driver,
        controller_spawn=ros.spawn_controller,
        controller_activate=ros.activate_watchdog,
        controller_deactivate=ros.deactivate_watchdog,
        controller_unload=ros.unload_watchdog,
        read_boot_id=read_boot_id,
        kernel_factory=engine.DirectControlKernel.create,
        preflight=preflight,
        close=ros.close,
    )
