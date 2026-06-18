from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pinocchio as pin
import rclpy
from control_msgs.action import FollowJointTrajectory
from rclpy.action import ActionClient
from rclpy.node import Node
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectoryPoint

from . import step5b_contact_control_core as core
from .kunwei_persistent_monitor import KunweiMonitorConfig, KunweiPersistentMonitor
from .no_contact_motion_probe import dashboard_exchange
from .step5a_cartesian_cycloid_motion import (
    DEFAULT_CALIBRATION_YAML,
    DEFAULT_XACRO_PATH,
    EXPECTED_CALIBRATION_HASH,
    JOINT_NAMES,
    CalibratedModel,
    build_calibrated_model,
    fk_tool0_base,
)


WORKSPACE = Path(__file__).resolve().parents[3]
EXPERIMENT = WORKSPACE / "experiments" / "tase-contact-reproduction"
CONFIG = EXPERIMENT / "config"
RUN_ROOT = EXPERIMENT / "runs"
STAGE_TABLE = CONFIG / "step5_stage_table.json"
SAFE_FRAME = CONFIG / "step5_safe_frame.json"
AUTHORIZATION_STATUS = EXPERIMENT / "tools" / "step5b_authorization_status.py"
TEXTBOOK_SPEC = CONFIG / "local_control_textbook_spec.json"
LOCKED_ROUTE = "ros2_remote_control_headless"
ENTRYPOINT_NAME = "step5b_contact_live_runner"
DEFAULT_ACTION_NAME = "/scaled_joint_trajectory_controller/follow_joint_trajectory"
DEFAULT_TRACE_FIELDS = [
    "t_rel_s",
    "stage",
    "cmd_valid",
    "cmd_vx_m_s",
    "cmd_vy_m_s",
    "cmd_vz_m_s",
    "cmd_wx_rad_s",
    "cmd_wy_rad_s",
    "cmd_wz_rad_s",
    "normal_load_n",
    "force_norm_n",
    "force_error_n",
    "orientation_error_rad",
    "hold_reason",
    "normal_filter_source",
    "sent_goal",
    "accepted",
    "failure_reason",
]


@dataclass(frozen=True)
class LiveRunnerCommand:
    stage: float
    result: core.Step5bContactResult
    next_state: core.Step5bContactState
    search_active: bool
    command_twist_base: tuple[float, float, float, float, float, float]


def acceptance_contract(entrypoint: Path | None = None) -> dict[str, Any]:
    entrypoint_path = entrypoint or Path(__file__).resolve()
    return {
        "version": 1,
        "role": "step5b_live_runner_acceptance_candidate",
        "live_runner_entrypoint": str(entrypoint_path),
        "live_runner_console_script": ENTRYPOINT_NAME,
        "live_runner_route": LOCKED_ROUTE,
        "ros2_interface": {
            "action": "control_msgs/action/FollowJointTrajectory",
            "default_action_name": DEFAULT_ACTION_NAME,
            "controller": "scaled_joint_trajectory_controller",
        },
        "contact_core": "ur10e_example_controllers.step5b_contact_control_core.compute_step5b_contact_sample",
        "force_source": "kunwei_software_baselined_stream",
        "zero_policy": {
            "ur_zero_ftsensor_called": False,
            "kunwei_hardware_tare_or_config_written": False,
            "software_baseline_subtraction": True,
        },
        "locked_params": [
            str(STAGE_TABLE) + "#step5_contact_cycloid_baseline_v1",
            str(TEXTBOOK_SPEC),
        ],
        "safety_boundary": [
            "default mode is dry-run contract only",
            "no TP Play/upload",
            "no bridge start",
            "no URScript send",
            "no RTDE input writes",
            "no zero_ftsensor",
            "no Kunwei tare/config/write",
            "live command path requires step5b_authorization_status.py authorized",
            "live command path requires explicit --execute-live-contact",
        ],
    }


def latest_readiness_summary(runs_dir: Path = RUN_ROOT) -> Path | None:
    if not runs_dir.is_dir():
        return None
    candidates = sorted(
        (path for path in runs_dir.glob("step5b_zero_policy_readiness_*/summary.json") if path.is_file()),
        key=lambda path: path.parent.name,
    )
    return candidates[-1] if candidates else None


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_stage_table(path: Path = STAGE_TABLE) -> dict[str, Any]:
    payload = load_json(path)
    stages = payload.get("stages", [])
    for stage in stages:
        if stage.get("id") == "step5_contact_cycloid_baseline_v1":
            return stage
    raise RuntimeError(f"stage table missing step5_contact_cycloid_baseline_v1: {path}")


def load_params_and_basis(stage_table: Path = STAGE_TABLE, safe_frame: Path = SAFE_FRAME) -> tuple[core.Step5bContactParams, core.Step5bPathBasis]:
    stage = load_stage_table(stage_table)
    params = core.Step5bContactParams.from_metadata_args({}, stage=stage)
    basis = core.Step5bPathBasis.from_safe_frame(load_json(safe_frame))
    return params, basis


def readiness_ok(summary_path: Path) -> tuple[bool, dict[str, Any]]:
    summary = load_json(summary_path)
    zero_policy = summary.get("zero_policy", {})
    ok = (
        bool(summary.get("ok"))
        and summary.get("force_source") == "kunwei_software_baselined_stream"
        and zero_policy.get("ur_zero_ftsensor_called") is False
        and zero_policy.get("kunwei_hardware_tare_or_config_written") is False
        and zero_policy.get("software_baseline_subtraction") is True
    )
    return ok, summary


def run_authorization_status() -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(AUTHORIZATION_STATUS), "--json"],
        cwd=str(EXPERIMENT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"authorization status did not emit JSON: {result.stdout!r} {result.stderr!r}") from exc
    payload["exit_code"] = result.returncode
    return payload


def default_run_dir() -> Path:
    return RUN_ROOT / f"step5b_contact_live_runner_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def dry_run_summary(args: argparse.Namespace) -> dict[str, Any]:
    readiness_path = args.readiness_summary or latest_readiness_summary(args.runs_dir)
    readiness_pass = False
    readiness_payload: dict[str, Any] | None = None
    if readiness_path is not None and readiness_path.is_file():
        readiness_pass, readiness_payload = readiness_ok(readiness_path)
    return {
        "ok": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "role": "step5b_ros2_remote_contact_live_runner",
        "dry_run": True,
        "execute_live_contact": False,
        "motion_authorized": False,
        "contact_motion_entered": False,
        "sent_goal": False,
        "accepted": False,
        "live_runner_route": LOCKED_ROUTE,
        "action_name": args.action_name,
        "readiness_summary_path": None if readiness_path is None else str(readiness_path),
        "readiness_pass": readiness_pass,
        "readiness_ok": None if readiness_payload is None else bool(readiness_payload.get("ok")),
        "acceptance_contract": acceptance_contract(),
        "next_gate": "auditor_acceptance_required_before_operator_final_trigger",
    }


class Step5bContactLiveRunner(Node):
    def __init__(self, args: argparse.Namespace, model_bundle: CalibratedModel) -> None:
        super().__init__("step5b_contact_live_runner")
        self.args = args
        self.model_bundle = model_bundle
        self.joint_state: JointState | None = None
        self.sent_goal = False
        self.accepted = False
        self.failure_stage = "not_started"
        self.contact_motion_entered = False
        self.create_subscription(JointState, args.joint_state_topic, self._on_joint_state, 50)
        self.action_client = ActionClient(self, FollowJointTrajectory, args.action_name)

    def _on_joint_state(self, msg: JointState) -> None:
        self.joint_state = msg

    def wait_for_joint_state(self) -> JointState:
        deadline = time.monotonic() + self.args.wait_s
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            if self.joint_state is not None and all(name in self.joint_state.name for name in JOINT_NAMES):
                return self.joint_state
        raise RuntimeError(f"Timed out waiting for joint state on {self.args.joint_state_topic}")

    def run(self) -> dict[str, Any]:
        self.failure_stage = "authorization_gate"
        auth = run_authorization_status()
        if not bool(auth.get("authorized")):
            raise RuntimeError(f"Step5b authorization gate is not green: {auth.get('blocking_reasons')}")

        self.failure_stage = "readiness_gate"
        readiness_path = self.args.readiness_summary or latest_readiness_summary(self.args.runs_dir)
        if readiness_path is None:
            raise RuntimeError("No Step5b readiness summary found")
        readiness_pass, readiness_payload = readiness_ok(readiness_path)
        if not readiness_pass:
            raise RuntimeError(f"Step5b readiness summary is not acceptable: {readiness_path}")

        self.failure_stage = "dashboard_gate"
        dashboard = dashboard_exchange(self.args.robot_ip, ["is in remote control", "safetymode", "robotmode", "running"])
        _validate_dashboard(dashboard)

        self.failure_stage = "action_server_gate"
        if not self.action_client.wait_for_server(timeout_sec=self.args.wait_s):
            raise RuntimeError(f"Action server unavailable: {self.args.action_name}")

        monitor = KunweiPersistentMonitor(
            KunweiMonitorConfig(
                sensor_ip=self.args.kunwei_sensor_ip,
                sensor_port=self.args.kunwei_sensor_port,
                window_s=self.args.kunwei_window_s,
                latest_max_age_s=self.args.kunwei_latest_max_age_s,
                min_recent_samples=self.args.kunwei_min_recent_samples,
                max_force_delta_n=self.args.force_norm_hard_stop_n,
                raw_frames_path=self.args.kunwei_raw_frames,
            )
        )
        params, basis = load_params_and_basis(self.args.stage_table, self.args.safe_frame)
        state = core.Step5bContactState()
        trace_rows: list[dict[str, Any]] = []
        start = time.monotonic()
        last_tick = start
        monitor_finalized = False
        try:
            self.failure_stage = "kunwei_monitor_start"
            monitor.start()
            if not monitor.wait_ready(self.args.kunwei_ready_timeout_s):
                raise RuntimeError(f"Kunwei monitor did not become ready: {monitor.snapshot()['status']}")

            self.failure_stage = "live_contact_loop"
            while rclpy.ok() and time.monotonic() - start < self.args.max_runtime_s:
                now = time.monotonic()
                dt_s = max(1e-3, min(self.args.command_period_s, now - last_tick))
                last_tick = now
                joint_state = self.wait_for_joint_state()
                positions = _ordered_positions(joint_state)
                placement = fk_tool0_base(self.model_bundle, np.array(positions, dtype=float))
                pose = _pose_from_placement(placement)
                snapshot = monitor.snapshot()
                tcp_wrench = zeroed_tcp_wrench(snapshot)
                _hard_guard_wrench(tcp_wrench, self.args.force_norm_hard_stop_n, self.args.torque_norm_hard_stop_nm)
                stage = stage_for_elapsed(now - start, state)
                command = compute_live_command(
                    pose=pose,
                    tcp_wrench=tcp_wrench,
                    sensor_ok=1.0 if snapshot.get("ok") else 0.0,
                    robot_stage=stage,
                    dt_s=dt_s,
                    state=state,
                    params=params,
                    basis=basis,
                    search_speed_m_s=self.args.search_speed_m_s,
                )
                state = command.next_state
                trace_rows.append(_trace_row(now - start, command, sent_goal=False, accepted=False))
                if not any(abs(value) > 1e-12 for value in command.command_twist_base):
                    time.sleep(self.args.command_period_s)
                    continue
                q_next = integrate_twist_to_joint_position(
                    self.model_bundle,
                    np.array(positions, dtype=float),
                    command.command_twist_base,
                    self.args.command_period_s,
                    max_joint_step_rad=self.args.max_joint_step_rad,
                )
                self.contact_motion_entered = True
                send_goal(self, positions, q_next, self.args.command_period_s)
                trace_rows[-1]["sent_goal"] = True
                trace_rows[-1]["accepted"] = self.accepted
                if state.normal_acquired and command.result.path_time_s >= params.duration_s:
                    break
            monitor.stop()
            monitor_finalized = True
            write_trace(self.args.trace, trace_rows)
            return {
                "ok": True,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "role": "step5b_ros2_remote_contact_live_runner",
                "dry_run": False,
                "execute_live_contact": True,
                "motion_authorized": True,
                "contact_motion_entered": self.contact_motion_entered,
                "sent_goal": self.sent_goal,
                "accepted": self.accepted,
                "failure_stage": self.failure_stage,
                "live_runner_route": LOCKED_ROUTE,
                "action_name": self.args.action_name,
                "readiness_summary_path": str(readiness_path),
                "readiness_ok": bool(readiness_payload.get("ok")),
                "authorization_status": auth,
                "kunwei_monitor": monitor.snapshot(),
                "trace_path": str(self.args.trace),
                "trace_rows": len(trace_rows),
            }
        finally:
            if not monitor_finalized:
                monitor.stop()


def stage_for_elapsed(elapsed_s: float, state: core.Step5bContactState) -> float:
    if not state.normal_acquired:
        return 24.2
    if elapsed_s < 1.0:
        return 25.05
    if elapsed_s < 2.0:
        return 25.2
    if elapsed_s < 2.5:
        return 25.3
    return 25.0


def compute_live_command(
    *,
    pose: tuple[float, float, float, float, float, float],
    tcp_wrench: tuple[float, float, float, float, float, float],
    sensor_ok: float,
    robot_stage: float,
    dt_s: float,
    state: core.Step5bContactState,
    params: core.Step5bContactParams,
    basis: core.Step5bPathBasis,
    search_speed_m_s: float,
) -> LiveRunnerCommand:
    sample = core.Step5bSample(
        tcp_pose=pose,
        tcp_wrench=tcp_wrench,
        sensor_ok=sensor_ok,
        robot_stage=robot_stage,
        dt_s=dt_s,
    )
    result, next_state = core.compute_step5b_contact_sample(sample, state, params, basis)
    if robot_stage < 25.0 and next_state.normal_acquired:
        _assert_search_and_contact_posture_agree(pose, result)
    search_active = robot_stage < 25.0 and not next_state.normal_acquired
    if search_active:
        rotation = core.rotvec_to_matrix(pose[3], pose[4], pose[5])
        tcp_z_axis_b = (rotation[0][2], rotation[1][2], rotation[2][2])
        twist = (
            tcp_z_axis_b[0] * search_speed_m_s,
            tcp_z_axis_b[1] * search_speed_m_s,
            tcp_z_axis_b[2] * search_speed_m_s,
            0.0,
            0.0,
            0.0,
        )
    elif result.cmd_valid > 0.5:
        _assert_contact_semantics(result)
        twist = result.twist
    else:
        twist = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return LiveRunnerCommand(
        stage=robot_stage,
        result=result,
        next_state=next_state,
        search_active=search_active,
        command_twist_base=twist,
    )


def integrate_twist_to_joint_position(
    model_bundle: CalibratedModel,
    q: np.ndarray,
    twist_base: tuple[float, float, float, float, float, float],
    dt_s: float,
    *,
    max_joint_step_rad: float,
) -> np.ndarray:
    pin.computeJointJacobians(model_bundle.model, model_bundle.data, q)
    pin.updateFramePlacements(model_bundle.model, model_bundle.data)
    jacobian = pin.getFrameJacobian(
        model_bundle.model,
        model_bundle.data,
        model_bundle.tool0_frame_id,
        pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
    )
    target_twist = np.array(twist_base, dtype=float)
    dq = np.linalg.pinv(jacobian, rcond=1e-4) @ target_twist
    step = dq * dt_s
    step_norm = float(np.linalg.norm(step))
    if step_norm > max_joint_step_rad:
        step *= max_joint_step_rad / step_norm
    return pin.integrate(model_bundle.model, q, step)


def send_goal(node: Step5bContactLiveRunner, current: list[float], q_next: np.ndarray, duration_s: float) -> None:
    goal = FollowJointTrajectory.Goal()
    goal.trajectory.joint_names = JOINT_NAMES
    goal.trajectory.points = [_point(current, 0.0), _point([float(v) for v in q_next], duration_s)]
    future = node.action_client.send_goal_async(goal)
    rclpy.spin_until_future_complete(node, future, timeout_sec=node.args.wait_s)
    goal_handle = future.result()
    if goal_handle is None:
        raise RuntimeError("FollowJointTrajectory goal did not return a handle")
    node.sent_goal = True
    node.accepted = bool(goal_handle.accepted)
    if not goal_handle.accepted:
        raise RuntimeError("FollowJointTrajectory goal was rejected")


def zeroed_tcp_wrench(snapshot: dict[str, Any]) -> tuple[float, float, float, float, float, float]:
    latest = snapshot.get("latest_si_units")
    baseline = snapshot.get("baseline_si_units")
    if latest is None or baseline is None:
        raise RuntimeError("Kunwei monitor has no latest/baseline SI units")
    keys = ("Fx_N", "Fy_N", "Fz_N", "Mx_Nm", "My_Nm", "Mz_Nm")
    return tuple(float(latest[key]) - float(baseline[key]) for key in keys)  # type: ignore[return-value]


def _hard_guard_wrench(wrench: tuple[float, float, float, float, float, float], force_limit_n: float, torque_limit_nm: float) -> None:
    force_norm = math.sqrt(sum(value * value for value in wrench[:3]))
    torque_norm = math.sqrt(sum(value * value for value in wrench[3:]))
    if force_norm >= force_limit_n:
        raise RuntimeError(f"force_norm_hard_stop: {force_norm:.3f} N >= {force_limit_n:.3f} N")
    if torque_norm >= torque_limit_nm:
        raise RuntimeError(f"torque_norm_hard_stop: {torque_norm:.3f} Nm >= {torque_limit_nm:.3f} Nm")


def _assert_contact_semantics(result: core.Step5bContactResult) -> None:
    approach = result.approach_normal_b
    control = result.control_normal_b
    if core.dot3(approach, control) > -0.999:
        raise RuntimeError("contact semantic mismatch: approach_normal is not opposite control reaction normal")


def _assert_search_and_contact_posture_agree(
    pose: tuple[float, float, float, float, float, float],
    result: core.Step5bContactResult,
) -> None:
    rotation = core.rotvec_to_matrix(pose[3], pose[4], pose[5])
    tcp_z_axis_b = (rotation[0][2], rotation[1][2], rotation[2][2])
    if core.dot3(tcp_z_axis_b, result.approach_normal_b) <= 0.0:
        raise RuntimeError("contact semantic mismatch: search TCP +Z is opposite the latched approach normal")


def _pose_from_placement(placement: pin.SE3) -> tuple[float, float, float, float, float, float]:
    rotvec = pin.log3(placement.rotation)
    return (
        float(placement.translation[0]),
        float(placement.translation[1]),
        float(placement.translation[2]),
        float(rotvec[0]),
        float(rotvec[1]),
        float(rotvec[2]),
    )


def _ordered_positions(joint_state: JointState) -> list[float]:
    by_name = {name: joint_state.position[index] for index, name in enumerate(joint_state.name)}
    return [float(by_name[name]) for name in JOINT_NAMES]


def _point(positions: list[float], time_from_start_s: float) -> JointTrajectoryPoint:
    point = JointTrajectoryPoint()
    point.positions = positions
    point.velocities = [0.0] * len(positions)
    point.time_from_start.sec = int(time_from_start_s)
    point.time_from_start.nanosec = int((time_from_start_s - int(time_from_start_s)) * 1_000_000_000)
    return point


def _validate_dashboard(responses: dict[str, str]) -> None:
    if responses.get("is in remote control") != "true":
        raise RuntimeError(f"Remote Control is not true: {responses.get('is in remote control')}")
    if "NORMAL" not in responses.get("safetymode", ""):
        raise RuntimeError(f"Safety mode is not NORMAL: {responses.get('safetymode')}")
    if "RUNNING" not in responses.get("robotmode", ""):
        raise RuntimeError(f"Robot mode is not RUNNING: {responses.get('robotmode')}")


def write_trace(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DEFAULT_TRACE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in DEFAULT_TRACE_FIELDS})


def _trace_row(t_rel_s: float, command: LiveRunnerCommand, *, sent_goal: bool, accepted: bool, failure_reason: str = "") -> dict[str, Any]:
    result = command.result
    return {
        "t_rel_s": t_rel_s,
        "stage": command.stage,
        "cmd_valid": result.cmd_valid,
        "cmd_vx_m_s": command.command_twist_base[0],
        "cmd_vy_m_s": command.command_twist_base[1],
        "cmd_vz_m_s": command.command_twist_base[2],
        "cmd_wx_rad_s": command.command_twist_base[3],
        "cmd_wy_rad_s": command.command_twist_base[4],
        "cmd_wz_rad_s": command.command_twist_base[5],
        "normal_load_n": result.normal_load_n,
        "force_norm_n": result.force_norm_n,
        "force_error_n": result.force_error_n,
        "orientation_error_rad": result.orientation_error_rad,
        "hold_reason": result.hold_reason,
        "normal_filter_source": result.normal_filter_source,
        "sent_goal": sent_goal,
        "accepted": accepted,
        "failure_reason": failure_reason,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Step5b ROS2 Remote Control/headless live contact runner.")
    parser.add_argument("--describe-acceptance", action="store_true")
    parser.add_argument("--execute-live-contact", action="store_true", help="Enter live command path after all gates pass.")
    parser.add_argument("--robot-ip", default="192.168.1.18")
    parser.add_argument("--action-name", default=DEFAULT_ACTION_NAME)
    parser.add_argument("--joint-state-topic", default="/joint_states")
    parser.add_argument("--wait-s", type=float, default=15.0)
    parser.add_argument("--max-runtime-s", type=float, default=70.0)
    parser.add_argument("--command-period-s", type=float, default=0.05)
    parser.add_argument("--max-joint-step-rad", type=float, default=0.002)
    parser.add_argument("--search-speed-m-s", type=float, default=0.001)
    parser.add_argument("--force-norm-hard-stop-n", type=float, default=60.0)
    parser.add_argument("--torque-norm-hard-stop-nm", type=float, default=3.0)
    parser.add_argument("--kunwei-sensor-ip", default="192.168.50.25")
    parser.add_argument("--kunwei-sensor-port", type=int, default=5152)
    parser.add_argument("--kunwei-ready-timeout-s", type=float, default=3.0)
    parser.add_argument("--kunwei-window-s", type=float, default=0.5)
    parser.add_argument("--kunwei-latest-max-age-s", type=float, default=0.25)
    parser.add_argument("--kunwei-min-recent-samples", type=int, default=20)
    parser.add_argument("--calibration-yaml", type=Path, default=DEFAULT_CALIBRATION_YAML)
    parser.add_argument("--xacro-path", type=Path, default=DEFAULT_XACRO_PATH)
    parser.add_argument("--stage-table", type=Path, default=STAGE_TABLE)
    parser.add_argument("--safe-frame", type=Path, default=SAFE_FRAME)
    parser.add_argument("--runs-dir", type=Path, default=RUN_ROOT)
    parser.add_argument("--readiness-summary", type=Path, default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--trace", type=Path, default=None)
    parser.add_argument("--kunwei-raw-frames", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.describe_acceptance:
        print(json.dumps(acceptance_contract(), indent=2, sort_keys=True))
        return 0

    run_dir = args.run_dir or default_run_dir()
    args.summary = args.summary or run_dir / "summary.json"
    args.trace = args.trace or run_dir / "step5b_contact_live_runner_trace.csv"
    args.kunwei_raw_frames = args.kunwei_raw_frames or run_dir / "kunwei_raw_frames.bin"

    if not args.execute_live_contact:
        payload = dry_run_summary(args)
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    model_bundle = build_calibrated_model(args.calibration_yaml, args.xacro_path)
    if model_bundle.calibration_hash != EXPECTED_CALIBRATION_HASH:
        raise SystemExit(f"unexpected calibration hash: {model_bundle.calibration_hash}")
    rclpy.init(args=None)
    node = Step5bContactLiveRunner(args, model_bundle)
    try:
        payload = node.run()
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        payload = {
            "ok": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "role": "step5b_ros2_remote_contact_live_runner",
            "dry_run": False,
            "execute_live_contact": True,
            "motion_authorized": False,
            "contact_motion_entered": bool(getattr(node, "contact_motion_entered", False)),
            "sent_goal": bool(getattr(node, "sent_goal", False)),
            "accepted": bool(getattr(node, "accepted", False)),
            "failure_stage": getattr(node, "failure_stage", "unknown"),
            "error": f"{type(exc).__name__}: {exc}",
            "live_runner_route": LOCKED_ROUTE,
        }
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
