from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
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

from .guarded_contact_recovery_shadow import WORKSPACE_ROOT, _json_safe
from .kunwei_persistent_monitor import KunweiMonitorConfig, KunweiPersistentMonitor
from .no_contact_cycloid_shadow import TRACE_FIELDS, _iter_reference_rows, load_no_contact_config
from .step5a_cartesian_cycloid_motion import (
    BASE_OFFSET_FIELDS,
    COMMAND_FIELDS,
    COMMAND_FK_FIELDS,
    DEFAULT_CALIBRATION_YAML,
    DEFAULT_JOINT_HISTORY_MAX_SAMPLES,
    DEFAULT_STEP5_SAFE_FRAME,
    DEFAULT_TRACE_MAX_SAMPLE_GAP_S,
    DEFAULT_XACRO_PATH,
    EXPECTED_CALIBRATION_HASH,
    JOINT_NAMES,
    OBSERVED_FK_FIELDS,
    OBSERVED_JOINT_FIELDS,
    TRACE_ALIGNMENT_FIELDS,
    CalibratedModel,
    ObservedJointSample,
    _joint_state_header_stamp_s,
    _kunwei_artifact_ok,
    _load_json_or_snapshot,
    _validate_dashboard,
    augment_trace_with_observed_fk,
    build_calibrated_model,
    dashboard_exchange,
    fk_tool0_base,
    load_step5_safe_frame_reference,
    solve_tool0_ik,
    step5_local_offset_to_base,
)


DEFAULT_CONFIG = WORKSPACE_ROOT / "src" / "ur10e_example_controllers" / "config" / "historical_5a_fixed_z.yaml"
DEFAULT_POSITION_SPEED_M_S = 0.004
DEFAULT_POSITION_TOLERANCE_M = 0.003

REFERENCE_BASE_FIELDS = ["reference_base_x_m", "reference_base_y_m", "reference_base_z_m"]
HISTORICAL_TRACE_FIELDS = (
    TRACE_FIELDS
    + BASE_OFFSET_FIELDS
    + REFERENCE_BASE_FIELDS
    + COMMAND_FIELDS
    + COMMAND_FK_FIELDS
    + OBSERVED_FK_FIELDS
    + OBSERVED_JOINT_FIELDS
    + TRACE_ALIGNMENT_FIELDS
    + ["cartesian_error_m", "achieved_speed_m_s", "kunwei_force_delta_n"]
)
POSITION_TRACE_FIELDS = [
    "row_index",
    "t_rel_s",
    "segment",
    *REFERENCE_BASE_FIELDS,
    *COMMAND_FIELDS,
    *COMMAND_FK_FIELDS,
]


class Step5aHistoricalFixedZMotion(Node):
    def __init__(self, args: argparse.Namespace, config: dict[str, Any], model_bundle: CalibratedModel) -> None:
        super().__init__("step5a_historical_fixed_z_motion")
        self.args = args
        self.config = config
        self.model_bundle = model_bundle
        self.joint_state: JointState | None = None
        self.joint_history: list[ObservedJointSample] = []
        self.sent_goal = False
        self.accepted = False
        self.result_status: int | None = None
        self.result_error_code: int | None = None
        self.result_error_string: str | None = None
        self.failure_stage = "not_started"
        self.trajectory_authority_entered = False
        self.create_subscription(JointState, args.joint_state_topic, self._on_joint_state, 50)
        self.action_client = ActionClient(self, FollowJointTrajectory, args.action_name)

    def _on_joint_state(self, msg: JointState) -> None:
        self.joint_state = msg
        if all(name in msg.name for name in JOINT_NAMES):
            self.joint_history.append(
                ObservedJointSample(
                    receive_monotonic_s=time.monotonic(),
                    header_stamp_s=_joint_state_header_stamp_s(msg),
                    positions=ordered_positions(msg),
                )
            )
            history_limit = int(getattr(self.args, "joint_history_max_samples", DEFAULT_JOINT_HISTORY_MAX_SAMPLES))
            if history_limit > 0 and len(self.joint_history) > history_limit:
                self.joint_history = self.joint_history[-history_limit:]

    def wait_for_joint_state(self) -> JointState:
        deadline = time.monotonic() + self.args.wait_s
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            if self.joint_state is not None and all(name in self.joint_state.name for name in JOINT_NAMES):
                return self.joint_state
        raise RuntimeError(f"Timed out waiting for joint state on {self.args.joint_state_topic}")

    def run(self) -> dict[str, Any]:
        monitor = KunweiPersistentMonitor(
            KunweiMonitorConfig(
                sensor_ip=self.args.kunwei_sensor_ip,
                sensor_port=self.args.kunwei_sensor_port,
                window_s=self.args.kunwei_window_s,
                latest_max_age_s=self.args.kunwei_latest_max_age_s,
                min_recent_samples=self.args.kunwei_min_recent_samples,
                max_force_delta_n=self.args.kunwei_max_force_delta_n,
                raw_frames_path=self.args.kunwei_raw_frames,
            )
        )
        monitor_finalized = False
        trace_rows: list[dict[str, Any]] = []
        trace_alignment: dict[str, Any] = empty_trace_alignment("not_executed")
        start_positions: list[float] = []
        dashboard: dict[str, str] = {}
        metrics: dict[str, Any] = {}
        try:
            self.failure_stage = "kunwei_monitor_start"
            monitor.start()
            if not monitor.wait_ready(self.args.kunwei_ready_timeout_s):
                raise RuntimeError(f"Kunwei persistent monitor did not become ready: {monitor.snapshot()['status']}")

            self.failure_stage = "dashboard_gate"
            dashboard = dashboard_exchange(self.args.robot_ip, ["is in remote control", "safetymode", "robotmode", "running"])
            _validate_dashboard(dashboard)

            self.failure_stage = "joint_state_gate"
            joint_state = self.wait_for_joint_state()
            start_positions = ordered_positions(joint_state)
            start_pose = fk_tool0_base(self.model_bundle, np.array(start_positions, dtype=float))

            self.failure_stage = "trajectory_generation"
            if self.args.mode == "position":
                trajectory_points, trace_rows, metrics = build_positioning_trajectory(
                    self.config,
                    self.model_bundle,
                    start_positions,
                    max_cartesian_speed_m_s=self.args.position_speed_m_s,
                    sample_period_s=self.args.sample_period_s,
                    min_segment_duration_s=self.args.min_segment_duration_s,
                    ik_damping=self.args.ik_damping,
                    ik_max_iters=self.args.ik_max_iters,
                    ik_tolerance_m=self.args.ik_tolerance_m,
                )
                trace_fields = POSITION_TRACE_FIELDS
            else:
                precheck = fixed_z_start_precheck(self.config, start_pose, self.args.position_tolerance_m)
                if not precheck["ok"]:
                    raise RuntimeError(
                        "Current TCP is not at fixed-Z Step5a start; run step5a_live_historical_5a_position.sh first. "
                        f"position_error_m={precheck['position_error_m']:.6f}"
                    )
                trajectory_points, trace_rows, metrics = build_historical_fixed_z_trajectory(
                    self.config,
                    self.model_bundle,
                    start_positions,
                    ik_damping=self.args.ik_damping,
                    ik_max_iters=self.args.ik_max_iters,
                    ik_tolerance_m=self.args.ik_tolerance_m,
                )
                trace_fields = HISTORICAL_TRACE_FIELDS

            if metrics["max_commanded_fk_speed_m_s"] > float(self.config["velocity_cap_m_s"]) + self.args.speed_cap_tolerance_m_s:
                raise RuntimeError(
                    "Commanded FK speed exceeds fixed-Z Step5a cap: "
                    f"{metrics['max_commanded_fk_speed_m_s']:.6f} > {float(self.config['velocity_cap_m_s']):.6f}"
                )

            self.failure_stage = "action_server_gate"
            if not self.action_client.wait_for_server(timeout_sec=self.args.wait_s):
                raise RuntimeError(f"Action server unavailable: {self.args.action_name}")

            if not self.args.execute:
                write_trace(self.args.trace, trace_rows, trace_fields)
                monitor.stop()
                monitor_finalized = True
                monitor.write_summary(self.args.kunwei_summary, self._kunwei_extra(False))
                return self._summary(
                    dashboard,
                    _load_json_or_snapshot(self.args.kunwei_summary, monitor),
                    start_positions,
                    trace_rows,
                    metrics,
                    trace_alignment,
                    trace_fields,
                )

            self.failure_stage = "pre_send_kunwei_gate"
            monitor.assert_fresh_and_within_force_delta()
            goal = FollowJointTrajectory.Goal()
            goal.trajectory.joint_names = list(JOINT_NAMES)
            goal.trajectory.points = trajectory_points
            send_start = time.monotonic()
            self.failure_stage = "send_goal"
            self.sent_goal = True
            send_future = self.action_client.send_goal_async(goal)
            rclpy.spin_until_future_complete(self, send_future, timeout_sec=self.args.wait_s)
            if not send_future.done() or send_future.result() is None:
                raise RuntimeError("FollowJointTrajectory goal did not return a handle")
            goal_handle = send_future.result()
            self.accepted = bool(goal_handle.accepted)
            self.trajectory_authority_entered = self.accepted
            if not goal_handle.accepted:
                raise RuntimeError("FollowJointTrajectory goal was rejected")

            self.failure_stage = "trajectory_execution"
            result_future = goal_handle.get_result_async()
            deadline = time.monotonic() + float(metrics["duration_s"]) + self.args.wait_s
            while rclpy.ok() and time.monotonic() < deadline and not result_future.done():
                rclpy.spin_once(self, timeout_sec=0.05)
                try:
                    monitor.assert_fresh_and_within_force_delta()
                except RuntimeError:
                    cancel_future = goal_handle.cancel_goal_async()
                    rclpy.spin_until_future_complete(self, cancel_future, timeout_sec=1.0)
                    raise

            result = result_future.result()
            if result is None:
                raise RuntimeError("Timed out waiting for FollowJointTrajectory result")
            self.result_status = int(result.status)
            self.result_error_code = int(result.result.error_code)
            self.result_error_string = str(result.result.error_string)
            for _ in range(50):
                rclpy.spin_once(self, timeout_sec=0.02)
            if self.args.mode == "path":
                trace_alignment = augment_trace_with_observed_fk(
                    trace_rows,
                    self.model_bundle,
                    self.joint_history,
                    send_start,
                    start_positions,
                    max_sample_gap_s=self.args.trace_max_sample_gap_s,
                )
            else:
                metrics["final_position_error_m"] = final_position_error(
                    self.model_bundle,
                    self.joint_state,
                    np.array(metrics["target_pose_base"]["position_xyz_m"], dtype=float),
                )
            write_trace(self.args.trace, trace_rows, trace_fields)
            monitor.stop()
            monitor_finalized = True
            monitor.write_summary(self.args.kunwei_summary, self._kunwei_extra(True))
            if result.result.error_code != FollowJointTrajectory.Result.SUCCESSFUL:
                raise RuntimeError(f"Trajectory failed: {result.result.error_code} {result.result.error_string}")
            return self._summary(
                dashboard,
                _load_json_or_snapshot(self.args.kunwei_summary, monitor),
                start_positions,
                trace_rows,
                metrics,
                trace_alignment,
                trace_fields,
            )
        finally:
            if not monitor_finalized:
                monitor.stop()
                monitor.write_summary(self.args.kunwei_summary, self._kunwei_extra(bool(self.args.execute)))

    def _kunwei_extra(self, live_motion: bool) -> dict[str, Any]:
        return {
            "live_motion": live_motion,
            "failure_stage": self.failure_stage,
            "sent_goal": self.sent_goal,
            "accepted": self.accepted,
            "result_status": self.result_status,
            "result_error_code": self.result_error_code,
            "result_error_string": self.result_error_string,
            "trajectory_authority_entered": self.trajectory_authority_entered,
        }

    def _summary(
        self,
        dashboard: dict[str, str],
        kunwei_snapshot: dict[str, Any],
        start_positions: list[float],
        trace_rows: list[dict[str, Any]],
        metrics: dict[str, Any],
        trace_alignment: dict[str, Any],
        trace_fields: list[str],
    ) -> dict[str, Any]:
        achieved_speeds = [float(row["achieved_speed_m_s"]) for row in trace_rows if row.get("achieved_speed_m_s") not in {"", None}]
        cartesian_errors = [float(row["cartesian_error_m"]) for row in trace_rows if row.get("cartesian_error_m") not in {"", None}]
        result_ok = (
            (not self.args.execute)
            or (
                self.sent_goal
                and self.accepted
                and self.result_error_code == FollowJointTrajectory.Result.SUCCESSFUL
            )
        )
        if self.args.mode == "position":
            role = "step5a_live_fixed_z_historical_positioning_not_contact"
            motion_kind = "fixed_z_start_positioning"
            max_achieved_speed = None
            position_ok = metrics.get("final_position_error_m") is None or float(metrics["final_position_error_m"]) <= self.args.position_tolerance_m
            ok = result_ok and bool(position_ok) and _kunwei_artifact_ok(kunwei_snapshot)
        else:
            role = "step5a_live_fixed_z_historical_no_contact_visual_gate"
            motion_kind = "historical_fixed_z_cartesian_cycloid"
            max_achieved_speed = max(achieved_speeds) if achieved_speeds else math.nan
            ok = (
                result_ok
                and _kunwei_artifact_ok(kunwei_snapshot)
                and len(trace_rows) == 1101
                and metrics["max_reference_speed_m_s"] <= float(self.config["velocity_cap_m_s"]) + 1e-12
                and metrics["max_commanded_fk_speed_m_s"] <= float(self.config["velocity_cap_m_s"]) + self.args.speed_cap_tolerance_m_s
                and (not achieved_speeds or max_achieved_speed <= float(self.config["velocity_cap_m_s"]) + self.args.speed_cap_tolerance_m_s)
                and (not cartesian_errors or max(cartesian_errors) <= self.args.cartesian_position_error_limit_m)
                and trace_alignment.get("trace_alignment_ok") is True
            )
        return {
            "ok": bool(ok),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "stage_id": self.config.get("stage_id", "step5a_historical_fixed_z_no_contact_v1"),
            "role": role,
            "motion_kind": motion_kind,
            "execute": bool(self.args.execute),
            "live_air_motion_authorized": bool(self.args.execute),
            "step5a_gate_a_acceptance": "not_applicable_historical_fixed_z_visual_gate",
            "step5b_contact_authorized": False,
            "visual_inspection_gate": "operator_review_required_before_step5b_contact",
            "contact_policy": dict(self.config.get("contact_policy", {})),
            "force_source": "kunwei_force_monitor",
            "kunwei_monitor": kunwei_snapshot,
            "kunwei_artifact_ok": _kunwei_artifact_ok(kunwei_snapshot),
            "robot_ip": self.args.robot_ip,
            "action_name": self.args.action_name,
            "dashboard": dashboard,
            "rows": len(trace_rows),
            "duration_s": metrics["duration_s"],
            "sample_period_s": self.args.sample_period_s,
            "fixed_base_z_m": float(self.config["fixed_base_z_m"]),
            "velocity_cap_m_s": float(self.config["velocity_cap_m_s"]),
            "acceleration_bound_m_s2": float(self.config.get("acceleration_bound_m_s2", 0.300)),
            "cartesian_task_space_spec": cartesian_task_space_spec(self.config, metrics),
            "max_reference_speed_m_s": metrics.get("max_reference_speed_m_s"),
            "max_commanded_fk_speed_m_s": metrics["max_commanded_fk_speed_m_s"],
            "max_achieved_speed_m_s": max_achieved_speed,
            "max_cartesian_position_error_m": max(cartesian_errors) if cartesian_errors else metrics.get("max_commanded_position_error_m"),
            "cartesian_reference_frame": metrics["cartesian_reference_frame"],
            "target_pose_base": metrics.get("target_pose_base"),
            "start_pose_base": metrics.get("start_pose_base"),
            "reference_local_final_offset_xy_m": metrics.get("reference_local_final_offset_xy_m"),
            "reference_final_base_xyz_m": metrics.get("reference_final_base_xyz_m"),
            "commanded_net_displacement_xyz_m": metrics.get("commanded_net_displacement_xyz_m"),
            "commanded_net_displacement_norm_m": metrics.get("commanded_net_displacement_norm_m"),
            "clearance_estimate": fixed_z_clearance_estimate(float(self.config["fixed_base_z_m"])),
            "ik_source": {
                "solver": "pinocchio_calibrated_tool0_warm_start_deterministic_dls",
                "calibration_yaml": str(self.args.calibration_yaml),
                "xacro_path": str(self.args.xacro_path),
                "calibration_hash": self.model_bundle.calibration_hash,
                "expected_calibration_hash": EXPECTED_CALIBRATION_HASH,
                "base_frame": "base",
                "tool_frame": "tool0",
                "orientation": "fixed_from_first_joint_state_fk",
                "z_policy": "absolute_fixed_base_z_m",
            },
            "start_positions": dict(zip(JOINT_NAMES, start_positions)),
            "sent_goal": self.sent_goal,
            "accepted": self.accepted,
            "result_status": self.result_status,
            "result_error_code": self.result_error_code,
            "result_error_string": self.result_error_string,
            "failure_stage": self.failure_stage,
            "trajectory_authority_entered": self.trajectory_authority_entered,
            "trace_path": str(self.args.trace),
            "trace_rows": len(trace_rows),
            "trace_fields": trace_fields,
            "trace_alignment": trace_alignment,
            "joint_state_history": {
                "retained_samples": len(self.joint_history),
                "max_samples": int(getattr(self.args, "joint_history_max_samples", DEFAULT_JOINT_HISTORY_MAX_SAMPLES)),
            },
            "thresholds": {
                "position_tolerance_m": self.args.position_tolerance_m,
                "cartesian_position_error_limit_m": self.args.cartesian_position_error_limit_m,
                "trace_max_sample_gap_s": self.args.trace_max_sample_gap_s,
                "kunwei_max_force_delta_n": self.args.kunwei_max_force_delta_n,
            },
        }


def build_positioning_trajectory(
    config: dict[str, Any],
    model_bundle: CalibratedModel,
    start_positions: list[float],
    *,
    max_cartesian_speed_m_s: float,
    sample_period_s: float,
    min_segment_duration_s: float,
    ik_damping: float = 1e-4,
    ik_max_iters: int = 80,
    ik_tolerance_m: float = 5e-5,
) -> tuple[list[JointTrajectoryPoint], list[dict[str, Any]], dict[str, Any]]:
    q = np.array(start_positions, dtype=float)
    start_pose = fk_tool0_base(model_bundle, q)
    reference_frame = load_historical_reference_frame(config)
    target_xyz = fixed_z_start_xyz(reference_frame, float(config["fixed_base_z_m"]))
    via_xyz = np.array([target_xyz[0], target_xyz[1], float(start_pose.translation[2])], dtype=float)
    targets = [
        ("xy_at_current_z", via_xyz),
        ("fixed_z_descent", target_xyz),
    ]
    points: list[JointTrajectoryPoint] = []
    trace_rows: list[dict[str, Any]] = []
    commanded_xyz = [start_pose.translation.copy()]
    elapsed = 0.0
    row_index = 0
    previous_q = q.copy()
    for segment, xyz in targets:
        start_xyz = commanded_xyz[-1]
        distance = float(np.linalg.norm(xyz - start_xyz))
        duration = max(min_segment_duration_s, distance * 1.02 / max_cartesian_speed_m_s if max_cartesian_speed_m_s > 0 else min_segment_duration_s)
        steps = max(1, int(math.ceil(duration / sample_period_s)))
        segment_start_q = previous_q.copy()
        for step in range(1, steps + 1):
            blend = step / steps
            target_translation = start_xyz + blend * (xyz - start_xyz)
            target_pose = pin.SE3(start_pose.rotation, target_translation)
            previous_q, _ = solve_tool0_ik(
                model_bundle,
                previous_q,
                target_pose,
                damping=ik_damping,
                max_iters=ik_max_iters,
                tolerance_m=ik_tolerance_m,
            )
            t_rel_s = elapsed + duration * blend
            placement = fk_tool0_base(model_bundle, previous_q)
            point = make_point(previous_q, t_rel_s)
            points.append(point)
            row = {
                "row_index": row_index,
                "t_rel_s": t_rel_s,
                "segment": segment,
                "reference_base_x_m": float(target_translation[0]),
                "reference_base_y_m": float(target_translation[1]),
                "reference_base_z_m": float(target_translation[2]),
            }
            for name, value in zip(COMMAND_FIELDS, previous_q):
                row[name] = float(value)
            row["commanded_fk_x_m"] = float(placement.translation[0])
            row["commanded_fk_y_m"] = float(placement.translation[1])
            row["commanded_fk_z_m"] = float(placement.translation[2])
            trace_rows.append(row)
            row_index += 1
            commanded_xyz.append(placement.translation.copy())
        elapsed += duration
        previous_q = segment_start_q if False else previous_q
    set_point_velocities(points, sample_period_s)
    speeds = commanded_speeds(commanded_xyz, [0.0] + [float(row["t_rel_s"]) for row in trace_rows])
    return points, trace_rows, {
        "duration_s": elapsed,
        "max_commanded_fk_speed_m_s": max(speeds) if speeds else 0.0,
        "max_reference_speed_m_s": max_cartesian_speed_m_s,
        "max_commanded_position_error_m": max(
            float(np.linalg.norm(np.array([row["reference_base_x_m"], row["reference_base_y_m"], row["reference_base_z_m"]])
                                 - np.array([row["commanded_fk_x_m"], row["commanded_fk_y_m"], row["commanded_fk_z_m"]])))
            for row in trace_rows
        )
        if trace_rows
        else 0.0,
        "cartesian_reference_frame": reference_frame,
        "start_pose_base": pose_payload(start_pose),
        "target_pose_base": {"frame": "base_to_tool0", "position_xyz_m": [float(value) for value in target_xyz]},
        "reference_final_base_xyz_m": [float(value) for value in target_xyz],
        "commanded_net_displacement_xyz_m": [float(value) for value in (commanded_xyz[-1] - commanded_xyz[0])],
        "commanded_net_displacement_norm_m": float(np.linalg.norm(commanded_xyz[-1] - commanded_xyz[0])),
    }


def build_historical_fixed_z_trajectory(
    config: dict[str, Any],
    model_bundle: CalibratedModel,
    start_positions: list[float],
    *,
    ik_damping: float = 1e-4,
    ik_max_iters: int = 80,
    ik_tolerance_m: float = 5e-5,
) -> tuple[list[JointTrajectoryPoint], list[dict[str, Any]], dict[str, Any]]:
    rows = _iter_reference_rows(config)
    dt = float(config["sample_period_s"])
    reference_frame = load_historical_reference_frame(config)
    origin_xy = np.array(reference_frame["origin_xy_m"], dtype=float)
    q = np.array(start_positions, dtype=float)
    start_pose = fk_tool0_base(model_bundle, q)
    fixed_z = float(config["fixed_base_z_m"])
    points: list[JointTrajectoryPoint] = []
    trace_rows: list[dict[str, Any]] = []
    commanded_xyz: list[np.ndarray] = []
    commanded_positions: list[np.ndarray] = []
    reference_xyz_values: list[np.ndarray] = []
    max_commanded_error = 0.0
    for row in rows:
        base_offset = step5_local_offset_to_base(reference_frame, float(row["desired_x_m"]), float(row["desired_y_m"]))
        reference_xyz = np.array([origin_xy[0] + base_offset[0], origin_xy[1] + base_offset[1], fixed_z], dtype=float)
        target = pin.SE3(start_pose.rotation, reference_xyz)
        q, error_norm = solve_tool0_ik(
            model_bundle,
            q,
            target,
            damping=ik_damping,
            max_iters=ik_max_iters,
            tolerance_m=ik_tolerance_m,
        )
        max_commanded_error = max(max_commanded_error, error_norm)
        placement = fk_tool0_base(model_bundle, q)
        commanded_positions.append(q.copy())
        commanded_xyz.append(placement.translation.copy())
        reference_xyz_values.append(reference_xyz.copy())
        t_rel_s = int(row["row_index"]) * dt
        points.append(make_point(q, t_rel_s))
        trace_row = dict(row)
        trace_row["cmd_enabled"] = True
        trace_row["desired_z_m"] = fixed_z
        trace_row["reference_base_offset_x_m"] = float(base_offset[0])
        trace_row["reference_base_offset_y_m"] = float(base_offset[1])
        trace_row["reference_base_offset_z_m"] = 0.0
        trace_row["reference_base_x_m"] = float(reference_xyz[0])
        trace_row["reference_base_y_m"] = float(reference_xyz[1])
        trace_row["reference_base_z_m"] = float(reference_xyz[2])
        for name, value in zip(COMMAND_FIELDS, q):
            trace_row[name] = float(value)
        trace_row["commanded_fk_x_m"] = float(placement.translation[0])
        trace_row["commanded_fk_y_m"] = float(placement.translation[1])
        trace_row["commanded_fk_z_m"] = float(placement.translation[2])
        for field in OBSERVED_FK_FIELDS + OBSERVED_JOINT_FIELDS + TRACE_ALIGNMENT_FIELDS:
            trace_row[field] = ""
        trace_row["cartesian_error_m"] = ""
        trace_row["achieved_speed_m_s"] = ""
        trace_row["kunwei_force_delta_n"] = ""
        trace_rows.append(trace_row)
    for index, point in enumerate(points):
        if index == 0:
            point.velocities = [0.0] * len(JOINT_NAMES)
        else:
            point.velocities = [float(value) for value in (commanded_positions[index] - commanded_positions[index - 1]) / dt]
    speeds = [
        float(np.linalg.norm(commanded_xyz[index] - commanded_xyz[index - 1]) / dt)
        for index in range(1, len(commanded_xyz))
    ]
    final_local_offset = [
        float(rows[-1]["desired_x_m"]) if rows else 0.0,
        float(rows[-1]["desired_y_m"]) if rows else 0.0,
    ]
    net = commanded_xyz[-1] - commanded_xyz[0] if len(commanded_xyz) >= 2 else np.zeros(3)
    return points, trace_rows, {
        "duration_s": float(config["duration_s"]),
        "max_commanded_fk_speed_m_s": max(speeds) if speeds else 0.0,
        "max_reference_speed_m_s": max(float(row["reference_speed_m_s"]) for row in rows) if rows else 0.0,
        "max_commanded_position_error_m": max_commanded_error,
        "cartesian_reference_frame": reference_frame,
        "start_pose_base": pose_payload(start_pose),
        "target_pose_base": {"frame": "base_to_tool0", "position_xyz_m": [float(value) for value in reference_xyz_values[0]]},
        "reference_local_final_offset_xy_m": final_local_offset,
        "reference_final_base_xyz_m": [float(value) for value in reference_xyz_values[-1]],
        "commanded_net_displacement_xyz_m": [float(value) for value in net],
        "commanded_net_displacement_norm_m": float(np.linalg.norm(net)),
    }


def load_historical_reference_frame(config: dict[str, Any]) -> dict[str, Any]:
    path = Path(config.get("safe_frame_path", DEFAULT_STEP5_SAFE_FRAME))
    if not path.is_absolute():
        path = WORKSPACE_ROOT / path
    return load_step5_safe_frame_reference(path)


def fixed_z_start_xyz(reference_frame: dict[str, Any], fixed_z: float) -> np.ndarray:
    origin = reference_frame["origin_xy_m"]
    return np.array([float(origin[0]), float(origin[1]), fixed_z], dtype=float)


def fixed_z_start_precheck(config: dict[str, Any], current_pose: pin.SE3, tolerance_m: float) -> dict[str, Any]:
    reference_frame = load_historical_reference_frame(config)
    target_xyz = fixed_z_start_xyz(reference_frame, float(config["fixed_base_z_m"]))
    error = float(np.linalg.norm(current_pose.translation - target_xyz))
    return {
        "ok": error <= tolerance_m,
        "position_error_m": error,
        "tolerance_m": tolerance_m,
        "current_xyz_m": [float(value) for value in current_pose.translation],
        "target_xyz_m": [float(value) for value in target_xyz],
    }


def final_position_error(model_bundle: CalibratedModel, joint_state: JointState | None, target_xyz: np.ndarray) -> float | None:
    if joint_state is None or not all(name in joint_state.name for name in JOINT_NAMES):
        return None
    placement = fk_tool0_base(model_bundle, np.array(ordered_positions(joint_state), dtype=float))
    return float(np.linalg.norm(placement.translation - target_xyz))


def fixed_z_clearance_estimate(fixed_z: float) -> dict[str, Any]:
    taught_z = {
        "start": 0.01864373664583356,
        "mid": 0.018024039341072384,
        "end": 0.019423891372941468,
    }
    return {
        name: {
            "taught_z_m": z,
            "fixed_z_m": fixed_z,
            "clearance_m": fixed_z - z,
            "clearance_mm": (fixed_z - z) * 1000.0,
        }
        for name, z in taught_z.items()
    }


def cartesian_task_space_spec(config: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    frame = metrics["cartesian_reference_frame"]
    return {
        "reference_frame": {
            "mode": frame["mode"],
            "safe_frame_path": frame["safe_frame_path"],
            "local_x_axis": frame["local_x_axis"],
            "local_y_axis": frame["local_y_axis"],
            "u_along_xy": frame["u_along_xy"],
            "p_lateral_xy": frame["p_lateral_xy"],
            "origin_xy_m": frame["origin_xy_m"],
        },
        "position_trajectory": "historical_step5a_cycloid_local_xy_mapped_to_base_xy_with_absolute_fixed_base_z",
        "velocity_profile": "cycloid_analytic_vxy_sampled_at_fixed_sample_period",
        "acceleration_profile_or_bound": {
            "mode": "bounded_by_legacy_trajectory_action_and_historical_tp_acceleration_limit",
            "acceleration_bound_m_s2": float(config.get("acceleration_bound_m_s2", 0.300)),
        },
        "timing_law": config.get("timing_law", "linear_time_phase"),
        "phase_parameters": {
            "omega_rad_s": float(config["omega_rad_s"]),
            "final_phase_rad": float(config["final_phase_rad"]),
            "duration_s": float(config["duration_s"]),
        },
        "sample_period_s": float(config["sample_period_s"]),
        "caps": {
            "velocity_cap_m_s": float(config["velocity_cap_m_s"]),
            "kunwei_max_force_delta_n_default": 2.0,
        },
        "endpoint_semantics": config.get("endpoint_semantics", "non_returning_historical_step5a_endpoint"),
        "bench_proven_corrections": {
            "safe_frame_xy_remapping": True,
            "calibration_hash": EXPECTED_CALIBRATION_HASH,
            "fixed_base_z_m": float(config["fixed_base_z_m"]),
            "ik_warm_start": "previous_row_solution",
            "driver_lifecycle_workaround": "single_sustained_launch_with_activate_joint_controller_true_and_retry",
        },
    }


def commanded_speeds(xyz_values: list[np.ndarray], times: list[float]) -> list[float]:
    return [
        float(np.linalg.norm(xyz_values[index] - xyz_values[index - 1]) / (times[index] - times[index - 1]))
        for index in range(1, len(xyz_values))
        if times[index] > times[index - 1]
    ]


def set_point_velocities(points: list[JointTrajectoryPoint], fallback_dt: float) -> None:
    for index, point in enumerate(points):
        if index == 0 or index == len(points) - 1:
            point.velocities = [0.0] * len(JOINT_NAMES)
            continue
        prev = points[index - 1]
        next_point = points[index + 1]
        dt = duration_to_s(next_point.time_from_start) - duration_to_s(prev.time_from_start)
        if dt <= 0.0:
            dt = fallback_dt
        point.velocities = [float((a - b) / dt) for a, b in zip(next_point.positions, prev.positions)]


def duration_to_s(point_time: Any) -> float:
    return float(point_time.sec) + float(point_time.nanosec) * 1e-9


def make_point(q: np.ndarray, t_rel_s: float) -> JointTrajectoryPoint:
    point = JointTrajectoryPoint()
    point.positions = [float(value) for value in q]
    point.time_from_start.sec = int(t_rel_s)
    point.time_from_start.nanosec = int(round((t_rel_s - int(t_rel_s)) * 1_000_000_000))
    if point.time_from_start.nanosec >= 1_000_000_000:
        point.time_from_start.sec += 1
        point.time_from_start.nanosec -= 1_000_000_000
    return point


def pose_payload(pose: pin.SE3) -> dict[str, Any]:
    return {
        "frame": "base_to_tool0",
        "position_xyz_m": [float(value) for value in pose.translation],
        "rotation_matrix_row_major": [float(value) for value in pose.rotation.reshape(-1)],
    }


def ordered_positions(joint_state: JointState) -> list[float]:
    by_name = dict(zip(joint_state.name, joint_state.position))
    return [float(by_name[name]) for name in JOINT_NAMES]


def write_trace(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _json_safe(row.get(field, "")) for field in fields})


def empty_trace_alignment(status: str) -> dict[str, Any]:
    return {
        "trace_alignment_ok": False,
        "status": status,
        "observed_sample_count_raw": 0,
        "observed_sample_count_with_anchor": 0,
        "resampled_rows": 0,
        "exact_rows": 0,
        "interpolated_rows": 0,
        "extrapolated_rows": 0,
        "max_interpolation_gap_s": None,
        "max_observed_dt_to_row_s": None,
        "row0_anchor_error_m": None,
        "trace_max_sample_gap_s": DEFAULT_TRACE_MAX_SAMPLE_GAP_S,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Step5a historical fixed-Z no-contact positioning or path motion.")
    parser.add_argument("--mode", choices=["position", "path"], required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--robot-ip", default="192.168.1.18")
    parser.add_argument("--action-name", default="/scaled_joint_trajectory_controller/follow_joint_trajectory")
    parser.add_argument("--joint-state-topic", default="/joint_states")
    parser.add_argument("--wait-s", type=float, default=30.0)
    parser.add_argument("--sample-period-s", type=float, default=0.02)
    parser.add_argument("--position-speed-m-s", type=float, default=DEFAULT_POSITION_SPEED_M_S)
    parser.add_argument("--min-segment-duration-s", type=float, default=2.0)
    parser.add_argument("--position-tolerance-m", type=float, default=DEFAULT_POSITION_TOLERANCE_M)
    parser.add_argument("--cartesian-position-error-limit-m", type=float, default=0.005)
    parser.add_argument("--trace-max-sample-gap-s", type=float, default=DEFAULT_TRACE_MAX_SAMPLE_GAP_S)
    parser.add_argument("--joint-history-max-samples", type=int, default=DEFAULT_JOINT_HISTORY_MAX_SAMPLES)
    parser.add_argument("--calibration-yaml", type=Path, default=DEFAULT_CALIBRATION_YAML)
    parser.add_argument("--xacro-path", type=Path, default=DEFAULT_XACRO_PATH)
    parser.add_argument("--ik-damping", type=float, default=1e-4)
    parser.add_argument("--ik-max-iters", type=int, default=80)
    parser.add_argument("--ik-tolerance-m", type=float, default=5e-5)
    parser.add_argument("--speed-cap-tolerance-m-s", type=float, default=0.0005)
    parser.add_argument("--kunwei-sensor-ip", default="192.168.50.25")
    parser.add_argument("--kunwei-sensor-port", type=int, default=5152)
    parser.add_argument("--kunwei-ready-timeout-s", type=float, default=3.0)
    parser.add_argument("--kunwei-window-s", type=float, default=0.5)
    parser.add_argument("--kunwei-latest-max-age-s", type=float, default=0.25)
    parser.add_argument("--kunwei-min-recent-samples", type=int, default=20)
    parser.add_argument("--kunwei-max-force-delta-n", type=float, default=2.0)
    parser.add_argument("--kunwei-summary", type=Path, required=True)
    parser.add_argument("--kunwei-raw-frames", type=Path, default=None)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args(argv)

    config = load_no_contact_config(args.config)
    model_bundle = build_calibrated_model(args.calibration_yaml, args.xacro_path)
    if model_bundle.calibration_hash != EXPECTED_CALIBRATION_HASH:
        raise SystemExit(f"unexpected calibration hash: {model_bundle.calibration_hash}")

    rclpy.init(args=None)
    node = Step5aHistoricalFixedZMotion(args, config, model_bundle)
    try:
        summary = node.run()
        text = json.dumps(_json_safe(summary), indent=2, sort_keys=True) + "\n"
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(text, encoding="utf-8")
        print(text, end="")
        return 0 if summary["ok"] else 2
    except Exception as exc:
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "stage_id": config.get("stage_id", "step5a_historical_fixed_z_no_contact_v1"),
            "role": (
                "step5a_live_fixed_z_historical_positioning_not_contact"
                if args.mode == "position"
                else "step5a_live_fixed_z_historical_no_contact_visual_gate"
            ),
            "motion_kind": "fixed_z_start_positioning" if args.mode == "position" else "historical_fixed_z_cartesian_cycloid",
            "sent_goal": bool(getattr(node, "sent_goal", False)),
            "accepted": bool(getattr(node, "accepted", False)),
            "result_status": getattr(node, "result_status", None),
            "result_error_code": getattr(node, "result_error_code", None),
            "result_error_string": getattr(node, "result_error_string", None),
            "failure_stage": getattr(node, "failure_stage", "unknown"),
            "trajectory_authority_entered": bool(getattr(node, "trajectory_authority_entered", False)),
            "force_source": "kunwei_force_monitor",
        }
        text = json.dumps(_json_safe(payload), indent=2, sort_keys=True) + "\n"
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(text, encoding="utf-8")
        print(text, end="", file=sys.stderr)
        return 2
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
