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
from action_msgs.msg import GoalStatus
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
    solve_tool0_ik,
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
TARGET_ROTVEC_RAD = (-3.044172198, -0.130573165, -0.202188631)
FIRST_CONTACT_Z_M = 0.008044839
FIRST_NEAR_ABOVE_CONTACT_M = 0.020
FIRST_BELOW_CONTACT_MARGIN_M = 0.004
FIRST_SEARCH_FAR_SPEED_M_S = -0.015
FIRST_SEARCH_NEAR_SPEED_M_S = -0.0025
SECOND_SEARCH_FAR_SPEED_M_S = -0.005
SECOND_SEARCH_NEAR_SPEED_M_S = -0.003
DEFAULT_TRACE_FIELDS = [
    "t_rel_s",
    "stage",
    "tcp_x_m",
    "tcp_y_m",
    "tcp_z_m",
    "tcp_rx_rad",
    "tcp_ry_rad",
    "tcp_rz_rad",
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
    "action_terminal_status",
    "action_result_error_code",
    "action_result_error_string",
    "action_result_timeout_s",
    "failure_reason",
]


@dataclass(frozen=True)
class LiveRunnerCommand:
    stage: float
    tcp_pose: tuple[float, float, float, float, float, float]
    result: core.Step5bContactResult
    next_state: core.Step5bContactState
    search_active: bool
    command_twist_base: tuple[float, float, float, float, float, float]


@dataclass(frozen=True)
class ActionGoalOutcome:
    accepted: bool
    terminal_status: int | None = None
    result_error_code: int | None = None
    result_error_string: str | None = None
    result_timeout_s: float | None = None


@dataclass(frozen=True)
class PrepositionPlan:
    stage: float
    target_pose: tuple[float, float, float, float, float, float]
    start_pose: tuple[float, float, float, float, float, float]
    target_q: tuple[float, ...]
    ik_position_error_m: float
    planned_duration_s: float
    steps: int


@dataclass(frozen=True)
class FirstSearchProfile:
    start_z_m: float
    near_start_depth_m: float
    max_down_m: float
    far_speed_m_s: float
    near_speed_m_s: float


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
    auth = run_authorization_status()
    live_runner_accepted = bool(auth.get("open_gates", {}).get("live_runner_auditor_accepted", {}).get("met"))
    return {
        "ok": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "role": "step5b_ros2_remote_contact_live_runner",
        "dry_run": True,
        "execute_live_contact": False,
        "motion_authorized": False,
        "contact_motion_entered": False,
        "sent_goal": False,
        "accepted": live_runner_accepted,
        "live_runner_route": LOCKED_ROUTE,
        "action_name": args.action_name,
        "readiness_summary_path": None if readiness_path is None else str(readiness_path),
        "readiness_pass": readiness_pass,
        "readiness_ok": None if readiness_payload is None else bool(readiness_payload.get("ok")),
        "authorization_status": auth,
        "acceptance_contract": acceptance_contract(),
        "next_gate": auth.get("next_action"),
    }


class Step5bContactLiveRunner(Node):
    def __init__(self, args: argparse.Namespace, model_bundle: CalibratedModel) -> None:
        super().__init__("step5b_contact_live_runner")
        self.args = args
        self.model_bundle = model_bundle
        self.joint_state: JointState | None = None
        self.sent_goal = False
        self.accepted = False
        self.action_terminal_status: int | None = None
        self.action_result_error_code: int | None = None
        self.action_result_error_string: str | None = None
        self.action_result_timeout_s: float | None = None
        self.failure_stage = "not_started"
        self.motion_authorized = False
        self.contact_motion_entered = False
        self.preposition_motion_entered = False
        self.preposition_summary: dict[str, Any] | None = None
        self.trace_rows: list[dict[str, Any]] = []
        self.kunwei_monitor_snapshot: dict[str, Any] | None = None
        self.goal_count = 0
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
        self.trace_rows = []
        start = time.monotonic()
        last_tick = start
        next_progress = start
        first_pose: tuple[float, float, float, float, float, float] | None = None
        monitor_finalized = False
        try:
            self.failure_stage = "kunwei_monitor_start"
            monitor.start()
            if not monitor.wait_ready(self.args.kunwei_ready_timeout_s):
                raise RuntimeError(f"Kunwei monitor did not become ready: {monitor.snapshot()['status']}")

            self.motion_authorized = True
            self.failure_stage = "stage22_preposition_to_step5_start"
            self.preposition_summary = execute_preposition_to_entry(self, monitor, basis)

            self.failure_stage = "stage23_software_baseline_reset"
            time.sleep(self.args.post_preposition_settle_s)
            monitor.reset_baseline_from_recent()
            baseline_snapshot = monitor.snapshot()
            self.preposition_summary["software_baseline_reset"] = {
                "stage": 23.0,
                "ok": True,
                "baseline_si_units": baseline_snapshot.get("baseline_si_units"),
                "ur_zero_ftsensor_called": False,
                "kunwei_hardware_tare_or_config_written": False,
            }

            start = time.monotonic()
            last_tick = start
            next_progress = start
            first_search_profile: FirstSearchProfile | None = None
            latch_start: float | None = None
            self.failure_stage = "live_contact_loop"
            last_command: LiveRunnerCommand | None = None
            while rclpy.ok() and time.monotonic() - start < self.args.max_runtime_s:
                now = time.monotonic()
                dt_s = max(1e-3, min(self.args.command_period_s, now - last_tick))
                last_tick = now
                joint_state = self.wait_for_joint_state()
                positions = _ordered_positions(joint_state)
                placement = fk_tool0_base(self.model_bundle, np.array(positions, dtype=float))
                pose = _pose_from_placement(placement)
                if first_pose is None:
                    first_pose = pose
                snapshot = monitor.snapshot()
                tcp_wrench = zeroed_tcp_wrench(snapshot)
                _hard_guard_wrench(tcp_wrench, self.args.force_norm_hard_stop_n, self.args.torque_norm_hard_stop_nm)
                if not state.normal_acquired:
                    if first_search_profile is None:
                        first_search_profile = first_search_profile_from_pose(pose)
                    stage, search_speed_m_s, exhausted = first_search_stage_and_speed(pose, first_search_profile)
                    if exhausted:
                        self.failure_stage = "contact_search_depth_exhausted"
                        raise RuntimeError(
                            "Step5b first search exhausted retained TP depth before latch: "
                            f"start_z_m={first_search_profile.start_z_m:.6f} "
                            f"current_z_m={pose[2]:.6f} max_down_m={first_search_profile.max_down_m:.6f}"
                        )
                else:
                    if latch_start is None:
                        latch_start = now
                    stage = stage_for_elapsed(now - latch_start, state)
                    search_speed_m_s = self.args.search_speed_m_s
                command = compute_live_command(
                    pose=pose,
                    tcp_wrench=tcp_wrench,
                    sensor_ok=1.0 if snapshot.get("ok") else 0.0,
                    robot_stage=stage,
                    dt_s=dt_s,
                    state=state,
                    params=params,
                    basis=basis,
                    search_speed_m_s=search_speed_m_s,
                )
                last_command = command
                state = command.next_state
                if latch_start is None and state.normal_acquired:
                    latch_start = now
                self.trace_rows.append(_trace_row(now - start, command, sent_goal=False, accepted=False))
                if not any(abs(value) > 1e-12 for value in command.command_twist_base):
                    if now >= next_progress:
                        print_live_progress(
                            t_rel_s=now - start,
                            command=command,
                            first_pose=first_pose,
                            goal_count=self.goal_count,
                            sent=False,
                        )
                        next_progress = now + self.args.progress_period_s
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
                outcome = send_goal(self, positions, q_next, self.args.command_period_s)
                self.goal_count += 1
                self.trace_rows[-1].update(_action_outcome_fields(outcome))
                if now >= next_progress:
                    print_live_progress(
                        t_rel_s=now - start,
                        command=command,
                        first_pose=first_pose,
                        goal_count=self.goal_count,
                        sent=True,
                    )
                    next_progress = now + self.args.progress_period_s
                if state.normal_acquired and command.result.path_time_s >= params.duration_s:
                    break
            incomplete_stage = live_loop_incomplete_stage(
                state=state,
                last_command=last_command,
                params=params,
            )
            if incomplete_stage is not None:
                self.failure_stage = incomplete_stage
                raise RuntimeError(
                    live_loop_incomplete_message(
                        incomplete_stage,
                        last_command=last_command,
                        params=params,
                        max_runtime_s=self.args.max_runtime_s,
                        trace_rows=len(self.trace_rows),
                    )
                )
            monitor.stop()
            monitor_finalized = True
            self.kunwei_monitor_snapshot = monitor.snapshot()
            write_trace(self.args.trace, self.trace_rows)
            return {
                "ok": True,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "role": "step5b_ros2_remote_contact_live_runner",
                "dry_run": False,
                "execute_live_contact": True,
                "motion_authorized": self.motion_authorized,
                "contact_motion_entered": self.contact_motion_entered,
                "preposition_motion_entered": self.preposition_motion_entered,
                "sent_goal": self.sent_goal,
                "accepted": self.accepted,
                "failure_stage": self.failure_stage,
                "live_runner_route": LOCKED_ROUTE,
                "action_name": self.args.action_name,
                "readiness_summary_path": str(readiness_path),
                "readiness_ok": bool(readiness_payload.get("ok")),
                "authorization_status": auth,
                "action_terminal_status": self.action_terminal_status,
                "action_result_error_code": self.action_result_error_code,
                "action_result_error_string": self.action_result_error_string,
                "action_result_timeout_s": self.action_result_timeout_s,
                "goal_count": self.goal_count,
                "preposition": self.preposition_summary,
                "kunwei_monitor": self.kunwei_monitor_snapshot,
                "trace_path": str(self.args.trace),
                "trace_rows": len(self.trace_rows),
                "diagnostic_summary": live_trace_diagnostics(self.trace_rows),
            }
        except Exception as exc:
            self.kunwei_monitor_snapshot = monitor.snapshot()
            persist_partial_failure_trace(self, exc)
            raise
        finally:
            if not monitor_finalized:
                monitor.stop()


def stage_for_elapsed(elapsed_s: float, state: core.Step5bContactState) -> float:
    if not state.normal_acquired:
        return 24.2
    if elapsed_s < 1.0:
        return 25.05
    if elapsed_s < 1.5:
        return 25.1
    if elapsed_s < 2.5:
        return 25.2
    if elapsed_s < 3.5:
        return 25.3
    return 25.0


def entry_target_pose(
    current_pose: tuple[float, float, float, float, float, float],
    basis: core.Step5bPathBasis,
) -> tuple[float, float, float, float, float, float]:
    return (
        basis.origin_xy_m[0],
        basis.origin_xy_m[1],
        current_pose[2],
        TARGET_ROTVEC_RAD[0],
        TARGET_ROTVEC_RAD[1],
        TARGET_ROTVEC_RAD[2],
    )


def pose_to_se3(pose: tuple[float, float, float, float, float, float]) -> pin.SE3:
    return pin.SE3(
        np.array(core.rotvec_to_matrix(pose[3], pose[4], pose[5]), dtype=float),
        np.array(pose[:3], dtype=float),
    )


def plan_preposition_to_entry(
    model_bundle: CalibratedModel,
    start_positions: list[float],
    basis: core.Step5bPathBasis,
    *,
    speed_m_s: float,
    command_period_s: float,
    min_duration_s: float,
    ik_damping: float,
    ik_max_iters: int,
    ik_tolerance_m: float,
) -> PrepositionPlan:
    q_start = np.array(start_positions, dtype=float)
    start_pose = _pose_from_placement(fk_tool0_base(model_bundle, q_start))
    target_pose = entry_target_pose(start_pose, basis)
    q_target, ik_error = solve_tool0_ik(
        model_bundle,
        q_start,
        pose_to_se3(target_pose),
        damping=ik_damping,
        max_iters=ik_max_iters,
        tolerance_m=ik_tolerance_m,
    )
    if ik_error > ik_tolerance_m:
        raise RuntimeError(f"Step5b entry preposition IK error too large: {ik_error:.6f} m > {ik_tolerance_m:.6f} m")
    xy_distance = math.hypot(target_pose[0] - start_pose[0], target_pose[1] - start_pose[1])
    # Quintic smoothstep peaks at 1.875x average speed; size duration so the
    # TCP XY preposition speed cap remains conservative instead of average-only.
    duration_s = max(min_duration_s, 1.875 * xy_distance / max(speed_m_s, 1e-6))
    steps = max(1, int(math.ceil(duration_s / command_period_s)))
    return PrepositionPlan(
        stage=22.0,
        target_pose=target_pose,
        start_pose=start_pose,
        target_q=tuple(float(value) for value in q_target),
        ik_position_error_m=float(ik_error),
        planned_duration_s=float(steps * command_period_s),
        steps=steps,
    )


def _smoothstep5(u: float) -> tuple[float, float]:
    u = max(0.0, min(1.0, u))
    s = 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5
    ds = 30.0 * u**2 - 60.0 * u**3 + 30.0 * u**4
    return s, ds


def build_preposition_trajectory_points(
    start_positions: list[float],
    target_positions: tuple[float, ...],
    plan: PrepositionPlan,
) -> tuple[list[JointTrajectoryPoint], dict[str, Any]]:
    q_start = np.array(start_positions, dtype=float)
    q_target = np.array(target_positions, dtype=float)
    delta = q_target - q_start
    duration_s = max(float(plan.planned_duration_s), 1e-9)
    points: list[JointTrajectoryPoint] = []
    max_joint_velocity = 0.0
    for step_index in range(plan.steps + 1):
        u = step_index / plan.steps
        s, ds_du = _smoothstep5(u)
        t_s = min(step_index * duration_s / plan.steps, duration_s)
        q = q_start + s * delta
        if step_index == 0 or step_index == plan.steps:
            qd = np.zeros_like(delta)
        else:
            qd = delta * ds_du / duration_s
        max_joint_velocity = max(max_joint_velocity, float(np.max(np.abs(qd))) if len(qd) else 0.0)
        points.append(_point([float(value) for value in q], t_s, velocities=[float(value) for value in qd]))
    return points, {
        "strategy": "single_time_parameterized_quintic_joint_trajectory",
        "goal_count": 1,
        "trajectory_point_count": len(points),
        "legacy_repeated_short_goals_rejected": True,
        "max_joint_velocity_command_rad_s": max_joint_velocity,
    }


def execute_preposition_to_entry(
    node: Step5bContactLiveRunner,
    monitor: KunweiPersistentMonitor,
    basis: core.Step5bPathBasis,
) -> dict[str, Any]:
    joint_state = node.wait_for_joint_state()
    start_positions = _ordered_positions(joint_state)
    plan = plan_preposition_to_entry(
        node.model_bundle,
        start_positions,
        basis,
        speed_m_s=node.args.preposition_speed_m_s,
        command_period_s=node.args.preposition_command_period_s,
        min_duration_s=node.args.preposition_min_duration_s,
        ik_damping=node.args.preposition_ik_damping,
        ik_max_iters=node.args.preposition_ik_max_iters,
        ik_tolerance_m=node.args.preposition_ik_tolerance_m,
    )
    monitor.assert_fresh_and_within_force_delta()
    trajectory_points, trajectory_metrics = build_preposition_trajectory_points(start_positions, plan.target_q, plan)
    outcome = send_trajectory_goal(node, trajectory_points, plan.planned_duration_s, monitor=monitor)
    node.goal_count += 1
    node.preposition_motion_entered = True
    current_pose = _pose_from_placement(
        fk_tool0_base(node.model_bundle, np.array(_ordered_positions(node.wait_for_joint_state()), dtype=float))
    )
    print(
        "step5b_live_preposition "
        "stage=22.00 "
        f"points={trajectory_metrics['trajectory_point_count']} "
        f"duration_s={plan.planned_duration_s:.3f} "
        f"tcp=({current_pose[0]:+.4f},{current_pose[1]:+.4f},{current_pose[2]:+.4f})m "
        f"target=({plan.target_pose[0]:+.4f},{plan.target_pose[1]:+.4f},{plan.target_pose[2]:+.4f})m "
        f"goals={node.goal_count}",
        flush=True,
    )
    if not outcome.accepted:
        raise RuntimeError("Step5b entry preposition goal was not accepted")
    final_pose = _pose_from_placement(
        fk_tool0_base(node.model_bundle, np.array(_ordered_positions(node.wait_for_joint_state()), dtype=float))
    )
    target_xyz = plan.target_pose[:3]
    final_xyz = final_pose[:3]
    position_error_m = math.sqrt(sum((final_xyz[index] - target_xyz[index]) ** 2 for index in range(3)))
    if position_error_m > node.args.preposition_position_tolerance_m:
        raise RuntimeError(
            "Step5b entry preposition final error too large: "
            f"{position_error_m:.6f} m > {node.args.preposition_position_tolerance_m:.6f} m"
        )
    return {
        "stage": plan.stage,
        "target_pose": list(plan.target_pose),
        "start_pose": list(plan.start_pose),
        "final_pose": list(final_pose),
        "target_q": list(plan.target_q),
        "ik_position_error_m": plan.ik_position_error_m,
        "final_position_error_m": position_error_m,
        "planned_duration_s": plan.planned_duration_s,
        "steps": plan.steps,
        "command_period_s": node.args.preposition_command_period_s,
        **trajectory_metrics,
        "entry_xy_source": "config/step5_safe_frame.json:basis.origin_xy_m",
        "target_rotvec_source": "programs/step5/step5b_contact_cycloid_baseline_v1.script TARGET_ROTVEC_RAD",
    }


def first_search_profile_from_pose(pose: tuple[float, float, float, float, float, float]) -> FirstSearchProfile:
    near_start_z_m = FIRST_CONTACT_Z_M + FIRST_NEAR_ABOVE_CONTACT_M
    max_end_z_m = FIRST_CONTACT_Z_M - FIRST_BELOW_CONTACT_MARGIN_M
    near_start_depth_m = max(0.0, pose[2] - near_start_z_m)
    max_down_m = pose[2] - max_end_z_m
    if max_down_m < 0.020:
        max_down_m = 0.020
    if max_down_m < near_start_depth_m + 0.010:
        max_down_m = near_start_depth_m + 0.010
    return FirstSearchProfile(
        start_z_m=pose[2],
        near_start_depth_m=near_start_depth_m,
        max_down_m=max_down_m,
        far_speed_m_s=abs(FIRST_SEARCH_FAR_SPEED_M_S),
        near_speed_m_s=abs(FIRST_SEARCH_NEAR_SPEED_M_S),
    )


def first_search_stage_and_speed(
    pose: tuple[float, float, float, float, float, float],
    profile: FirstSearchProfile,
) -> tuple[float, float, bool]:
    search_depth_m = profile.start_z_m - pose[2]
    if search_depth_m >= profile.max_down_m:
        return 24.2, 0.0, True
    if search_depth_m < profile.near_start_depth_m:
        return 24.0, profile.far_speed_m_s, False
    return 24.2, profile.near_speed_m_s, False


def live_loop_incomplete_stage(
    *,
    state: core.Step5bContactState,
    last_command: LiveRunnerCommand | None,
    params: core.Step5bContactParams,
) -> str | None:
    if not state.normal_acquired:
        return "contact_search_timeout"
    if last_command is None or last_command.result.path_time_s < params.duration_s:
        return "contact_path_timeout"
    return None


def live_loop_incomplete_message(
    stage: str,
    *,
    last_command: LiveRunnerCommand | None,
    params: core.Step5bContactParams,
    max_runtime_s: float,
    trace_rows: int,
) -> str:
    if stage == "contact_search_timeout":
        return (
            "Step5b contact search timed out without normal acquisition: "
            f"max_runtime_s={max_runtime_s:.3f} trace_rows={trace_rows}"
        )
    if stage == "contact_path_timeout":
        path_time_s = 0.0 if last_command is None else last_command.result.path_time_s
        return (
            "Step5b contact path timed out before completion: "
            f"path_time_s={path_time_s:.3f} duration_s={params.duration_s:.3f} trace_rows={trace_rows}"
        )
    raise ValueError(f"unknown live loop incomplete stage: {stage}")


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
        tcp_pose=pose,
        result=result,
        next_state=next_state,
        search_active=search_active,
        command_twist_base=twist,
    )


def print_live_progress(
    *,
    t_rel_s: float,
    command: LiveRunnerCommand,
    first_pose: tuple[float, float, float, float, float, float] | None,
    goal_count: int,
    sent: bool,
) -> None:
    pose = command.tcp_pose
    dz = 0.0 if first_pose is None else pose[2] - first_pose[2]
    result = command.result
    twist = command.command_twist_base
    print(
        "step5b_live "
        f"t={t_rel_s:6.2f}s "
        f"stage={command.stage:5.2f} "
        f"tcp=({pose[0]:+.4f},{pose[1]:+.4f},{pose[2]:+.4f})m "
        f"dz={dz:+.4f}m "
        f"cmd=({twist[0]:+.5f},{twist[1]:+.5f},{twist[2]:+.5f})m/s "
        f"force={result.force_norm_n:.3f}N "
        f"load={result.normal_load_n:.3f}N "
        f"valid={result.cmd_valid:.0f} "
        f"latch={str(command.next_state.normal_acquired).lower()} "
        f"goals={goal_count} "
        f"sent={str(sent).lower()} "
        f"hold={result.hold_reason}",
        flush=True,
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


def send_goal(node: Step5bContactLiveRunner, current: list[float], q_next: np.ndarray, duration_s: float) -> ActionGoalOutcome:
    return send_trajectory_goal(
        node,
        [_point(current, 0.0), _point([float(v) for v in q_next], duration_s)],
        duration_s,
    )


def send_trajectory_goal(
    node: Step5bContactLiveRunner,
    points: list[JointTrajectoryPoint],
    duration_s: float,
    *,
    monitor: KunweiPersistentMonitor | None = None,
) -> ActionGoalOutcome:
    if not points:
        raise RuntimeError("FollowJointTrajectory goal has no points")
    goal = FollowJointTrajectory.Goal()
    goal.trajectory.joint_names = JOINT_NAMES
    goal.trajectory.points = points
    future = node.action_client.send_goal_async(goal)
    rclpy.spin_until_future_complete(node, future, timeout_sec=node.args.wait_s)
    goal_handle = future.result()
    if goal_handle is None:
        raise RuntimeError("FollowJointTrajectory goal did not return a handle")
    node.sent_goal = True
    node.accepted = bool(goal_handle.accepted)
    if not goal_handle.accepted:
        raise RuntimeError("FollowJointTrajectory goal was rejected")
    result_future = goal_handle.get_result_async()
    result_timeout_s = float(duration_s) + float(node.args.wait_s)
    if monitor is None:
        rclpy.spin_until_future_complete(node, result_future, timeout_sec=result_timeout_s)
    else:
        deadline = time.monotonic() + result_timeout_s
        while rclpy.ok() and time.monotonic() < deadline and not result_future.done():
            rclpy.spin_once(node, timeout_sec=0.05)
            try:
                monitor.assert_fresh_and_within_force_delta()
            except RuntimeError:
                cancel_goal = getattr(goal_handle, "cancel_goal_async", None)
                if callable(cancel_goal):
                    cancel_future = cancel_goal()
                    rclpy.spin_until_future_complete(node, cancel_future, timeout_sec=1.0)
                raise
    if not result_future.done():
        node.action_result_timeout_s = result_timeout_s
        raise RuntimeError(f"FollowJointTrajectory result timed out after {result_timeout_s:.3f}s")
    wrapped_result = result_future.result()
    if wrapped_result is None:
        raise RuntimeError("FollowJointTrajectory result future returned None")
    node.action_terminal_status = int(wrapped_result.status)
    node.action_result_error_code = int(wrapped_result.result.error_code)
    node.action_result_error_string = str(wrapped_result.result.error_string)
    outcome = ActionGoalOutcome(
        accepted=True,
        terminal_status=node.action_terminal_status,
        result_error_code=node.action_result_error_code,
        result_error_string=node.action_result_error_string,
    )
    if (
        wrapped_result.status != GoalStatus.STATUS_SUCCEEDED
        or wrapped_result.result.error_code != FollowJointTrajectory.Result.SUCCESSFUL
    ):
        raise RuntimeError(
            "FollowJointTrajectory result failed: "
            f"status={node.action_terminal_status} "
            f"error_code={node.action_result_error_code} "
            f"error_string={node.action_result_error_string}"
        )
    return outcome


def _action_outcome_fields(outcome: ActionGoalOutcome) -> dict[str, Any]:
    return {
        "sent_goal": True,
        "accepted": outcome.accepted,
        "action_terminal_status": "" if outcome.terminal_status is None else outcome.terminal_status,
        "action_result_error_code": "" if outcome.result_error_code is None else outcome.result_error_code,
        "action_result_error_string": "" if outcome.result_error_string is None else outcome.result_error_string,
        "action_result_timeout_s": "" if outcome.result_timeout_s is None else outcome.result_timeout_s,
    }


def _node_action_fields(node: Step5bContactLiveRunner) -> dict[str, Any]:
    return {
        "sent_goal": node.sent_goal,
        "accepted": node.accepted,
        "action_terminal_status": "" if node.action_terminal_status is None else node.action_terminal_status,
        "action_result_error_code": "" if node.action_result_error_code is None else node.action_result_error_code,
        "action_result_error_string": "" if node.action_result_error_string is None else node.action_result_error_string,
        "action_result_timeout_s": "" if node.action_result_timeout_s is None else node.action_result_timeout_s,
    }


def persist_partial_failure_trace(node: Step5bContactLiveRunner, exc: Exception) -> None:
    if not node.trace_rows:
        return
    node.trace_rows[-1].update(_node_action_fields(node))
    node.trace_rows[-1]["failure_reason"] = f"{type(exc).__name__}: {exc}"
    write_trace(node.args.trace, node.trace_rows)


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


def _point(
    positions: list[float],
    time_from_start_s: float,
    *,
    velocities: list[float] | None = None,
) -> JointTrajectoryPoint:
    point = JointTrajectoryPoint()
    point.positions = positions
    point.velocities = velocities if velocities is not None else [0.0] * len(positions)
    point.time_from_start.sec = int(time_from_start_s)
    point.time_from_start.nanosec = int(round((time_from_start_s - int(time_from_start_s)) * 1_000_000_000))
    if point.time_from_start.nanosec >= 1_000_000_000:
        point.time_from_start.sec += 1
        point.time_from_start.nanosec -= 1_000_000_000
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


def _row_float(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    if value == "":
        return default
    return float(value)


def _row_bool(row: dict[str, Any], key: str) -> bool:
    value = row.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return bool(value)


def live_trace_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "trace_rows": 0,
            "sent_goal_rows": 0,
            "accepted_goal_rows": 0,
        }
    first = rows[0]
    last = rows[-1]
    first_tcp = [_row_float(first, key) for key in ("tcp_x_m", "tcp_y_m", "tcp_z_m")]
    last_tcp = [_row_float(last, key) for key in ("tcp_x_m", "tcp_y_m", "tcp_z_m")]
    tcp_delta = [last_tcp[index] - first_tcp[index] for index in range(3)]
    stage_counts: dict[str, int] = {}
    for row in rows:
        stage_key = f"{_row_float(row, 'stage'):.2f}"
        stage_counts[stage_key] = stage_counts.get(stage_key, 0) + 1
    return {
        "trace_rows": len(rows),
        "first_t_rel_s": _row_float(first, "t_rel_s"),
        "last_t_rel_s": _row_float(last, "t_rel_s"),
        "stage_counts": stage_counts,
        "last_stage": _row_float(last, "stage"),
        "first_tcp_xyz_m": first_tcp,
        "last_tcp_xyz_m": last_tcp,
        "tcp_delta_xyz_m": tcp_delta,
        "tcp_delta_norm_m": math.sqrt(sum(value * value for value in tcp_delta)),
        "sent_goal_rows": sum(1 for row in rows if _row_bool(row, "sent_goal")),
        "accepted_goal_rows": sum(1 for row in rows if _row_bool(row, "accepted")),
        "max_abs_cmd_linear_m_s": max(
            math.sqrt(
                _row_float(row, "cmd_vx_m_s") ** 2
                + _row_float(row, "cmd_vy_m_s") ** 2
                + _row_float(row, "cmd_vz_m_s") ** 2
            )
            for row in rows
        ),
        "max_abs_cmd_angular_rad_s": max(
            math.sqrt(
                _row_float(row, "cmd_wx_rad_s") ** 2
                + _row_float(row, "cmd_wy_rad_s") ** 2
                + _row_float(row, "cmd_wz_rad_s") ** 2
            )
            for row in rows
        ),
        "max_force_norm_n": max(_row_float(row, "force_norm_n") for row in rows),
        "max_normal_load_n": max(_row_float(row, "normal_load_n") for row in rows),
        "last_cmd_valid": _row_float(last, "cmd_valid"),
        "last_hold_reason": str(last.get("hold_reason", "")),
        "last_failure_reason": str(last.get("failure_reason", "")),
    }


def _trace_row(t_rel_s: float, command: LiveRunnerCommand, *, sent_goal: bool, accepted: bool, failure_reason: str = "") -> dict[str, Any]:
    result = command.result
    pose = command.tcp_pose
    return {
        "t_rel_s": t_rel_s,
        "stage": command.stage,
        "tcp_x_m": pose[0],
        "tcp_y_m": pose[1],
        "tcp_z_m": pose[2],
        "tcp_rx_rad": pose[3],
        "tcp_ry_rad": pose[4],
        "tcp_rz_rad": pose[5],
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
        "action_terminal_status": "",
        "action_result_error_code": "",
        "action_result_error_string": "",
        "action_result_timeout_s": "",
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
    parser.add_argument("--max-runtime-s", type=float, default=120.0)
    parser.add_argument("--command-period-s", type=float, default=0.05)
    parser.add_argument("--progress-period-s", type=float, default=1.0)
    parser.add_argument("--max-joint-step-rad", type=float, default=0.002)
    parser.add_argument("--search-speed-m-s", type=float, default=0.001)
    parser.add_argument("--preposition-speed-m-s", type=float, default=0.020)
    parser.add_argument("--preposition-command-period-s", type=float, default=0.10)
    parser.add_argument("--preposition-min-duration-s", type=float, default=1.0)
    parser.add_argument("--preposition-position-tolerance-m", type=float, default=0.003)
    parser.add_argument("--preposition-ik-damping", type=float, default=1e-4)
    parser.add_argument("--preposition-ik-max-iters", type=int, default=120)
    parser.add_argument("--preposition-ik-tolerance-m", type=float, default=5e-5)
    parser.add_argument("--post-preposition-settle-s", type=float, default=0.20)
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
            "motion_authorized": bool(getattr(node, "motion_authorized", False)),
            "contact_motion_entered": bool(getattr(node, "contact_motion_entered", False)),
            "preposition_motion_entered": bool(getattr(node, "preposition_motion_entered", False)),
            "sent_goal": bool(getattr(node, "sent_goal", False)),
            "accepted": bool(getattr(node, "accepted", False)),
            "action_terminal_status": getattr(node, "action_terminal_status", None),
            "action_result_error_code": getattr(node, "action_result_error_code", None),
            "action_result_error_string": getattr(node, "action_result_error_string", None),
            "action_result_timeout_s": getattr(node, "action_result_timeout_s", None),
            "goal_count": int(getattr(node, "goal_count", 0)),
            "preposition": getattr(node, "preposition_summary", None),
            "failure_stage": getattr(node, "failure_stage", "unknown"),
            "error": f"{type(exc).__name__}: {exc}",
            "live_runner_route": LOCKED_ROUTE,
            "trace_path": str(args.trace),
            "trace_rows": len(getattr(node, "trace_rows", [])),
            "diagnostic_summary": live_trace_diagnostics(getattr(node, "trace_rows", [])),
            "kunwei_monitor": getattr(node, "kunwei_monitor_snapshot", None),
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
