from __future__ import annotations

import argparse
import csv
import json
import math
import socket
import sys
import tempfile
import time
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pinocchio as pin
import rclpy
import xacro
import yaml
from control_msgs.action import FollowJointTrajectory
from rclpy.action import ActionClient
from rclpy.node import Node
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectoryPoint

from .guarded_contact_recovery_shadow import WORKSPACE_ROOT
from .kunwei_persistent_monitor import KunweiMonitorConfig, KunweiPersistentMonitor
from .no_contact_cycloid_shadow import DEFAULT_CONFIG, TRACE_FIELDS, _iter_reference_rows, load_no_contact_config


JOINT_NAMES = [
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
]
EXPECTED_CALIBRATION_HASH = "calib_7367377276742883610"
DEFAULT_CALIBRATION_YAML = WORKSPACE_ROOT / "src" / "ur10e_bringup" / "config" / "ur10e_calibration.yaml"
DEFAULT_XACRO_PATH = Path("/opt/ros/humble/share/ur_description/urdf/ur.urdf.xacro")
DEFAULT_GATE_A_POSITION_ERROR_LIMIT_M = 0.005
DEFAULT_TRACE_MAX_SAMPLE_GAP_S = 0.08
DEFAULT_JOINT_HISTORY_MAX_SAMPLES = 50000

COMMAND_FIELDS = [f"command_{name}_rad" for name in JOINT_NAMES]
COMMAND_FK_FIELDS = ["commanded_fk_x_m", "commanded_fk_y_m", "commanded_fk_z_m"]
OBSERVED_FK_FIELDS = ["observed_fk_x_m", "observed_fk_y_m", "observed_fk_z_m"]
OBSERVED_JOINT_FIELDS = [f"observed_{name}_rad" for name in JOINT_NAMES]
TRACE_ALIGNMENT_FIELDS = [
    "observed_sample_t_rel_s",
    "observed_sample_dt_to_row_s",
    "observed_sample_gap_s",
    "alignment_status",
]
LIVE_TRACE_FIELDS = (
    TRACE_FIELDS
    + COMMAND_FIELDS
    + COMMAND_FK_FIELDS
    + OBSERVED_FK_FIELDS
    + OBSERVED_JOINT_FIELDS
    + TRACE_ALIGNMENT_FIELDS
    + [
        "cartesian_error_m",
        "achieved_speed_m_s",
        "kunwei_force_delta_n",
    ]
)


@dataclass(frozen=True)
class CalibratedModel:
    model: pin.Model
    data: pin.Data
    urdf_text: str
    calibration_hash: str
    base_frame_id: int
    tool0_frame_id: int


@dataclass(frozen=True)
class ObservedJointSample:
    receive_monotonic_s: float
    header_stamp_s: float | None
    positions: list[float]


def load_calibration_hash(path: Path) -> str:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return str(payload["kinematics"]["hash"])
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"calibration YAML has no kinematics.hash: {path}") from exc


def generate_calibrated_urdf(
    calibration_yaml: Path = DEFAULT_CALIBRATION_YAML,
    xacro_path: Path = DEFAULT_XACRO_PATH,
) -> tuple[str, str]:
    if not calibration_yaml.exists():
        raise FileNotFoundError(f"calibration YAML not found: {calibration_yaml}")
    if not xacro_path.exists():
        raise FileNotFoundError(f"UR xacro not found: {xacro_path}")
    calibration_hash = load_calibration_hash(calibration_yaml)
    doc = xacro.process_file(
        str(xacro_path),
        mappings={
            "name": "ur",
            "ur_type": "ur10e",
            "robot_ip": "192.168.1.18",
            "kinematics_params": str(calibration_yaml),
        },
    )
    return doc.toxml(), calibration_hash


def build_calibrated_model(
    calibration_yaml: Path = DEFAULT_CALIBRATION_YAML,
    xacro_path: Path = DEFAULT_XACRO_PATH,
) -> CalibratedModel:
    urdf_text, calibration_hash = generate_calibrated_urdf(calibration_yaml, xacro_path)
    with tempfile.NamedTemporaryFile("w", suffix=".urdf", encoding="utf-8", delete=False) as handle:
        handle.write(urdf_text)
        temp_urdf = Path(handle.name)
    try:
        model = pin.buildModelFromUrdf(str(temp_urdf))
    finally:
        temp_urdf.unlink(missing_ok=True)
    for frame in ("base", "tool0"):
        if not model.existFrame(frame):
            raise RuntimeError(f"calibrated URDF missing required frame: {frame}")
    if model.nq != 6 or model.nv != 6:
        raise RuntimeError(f"expected nq=6,nv=6, got nq={model.nq},nv={model.nv}")
    return CalibratedModel(
        model=model,
        data=model.createData(),
        urdf_text=urdf_text,
        calibration_hash=calibration_hash,
        base_frame_id=model.getFrameId("base"),
        tool0_frame_id=model.getFrameId("tool0"),
    )


class Step5aCartesianCycloidMotion(Node):
    def __init__(self, args: argparse.Namespace, config: dict[str, Any], model_bundle: CalibratedModel) -> None:
        super().__init__("step5a_cartesian_cycloid_motion")
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
                    positions=_ordered_positions(msg),
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
        monitor_ready_ok = False
        monitor_finalized = False
        trace_rows: list[dict[str, Any]] = []
        start_positions: list[float] = []
        dashboard: dict[str, str] = {}
        try:
            self.failure_stage = "kunwei_monitor_start"
            monitor.start()
            monitor_ready_ok = monitor.wait_ready(self.args.kunwei_ready_timeout_s)
            if not monitor_ready_ok:
                raise RuntimeError(f"Kunwei persistent monitor did not become ready: {monitor.snapshot()['status']}")

            self.failure_stage = "dashboard_gate"
            dashboard = dashboard_exchange(self.args.robot_ip, ["is in remote control", "safetymode", "robotmode", "running"])
            _validate_dashboard(dashboard)

            self.failure_stage = "joint_state_gate"
            joint_state = self.wait_for_joint_state()
            start_positions = _ordered_positions(joint_state)

            self.failure_stage = "ik_generation"
            goal = FollowJointTrajectory.Goal()
            goal.trajectory.joint_names = JOINT_NAMES
            trajectory_points, trace_rows, metrics = build_cartesian_cycloid_trajectory(
                self.config,
                self.model_bundle,
                start_positions,
                ik_damping=self.args.ik_damping,
                ik_max_iters=self.args.ik_max_iters,
                ik_tolerance_m=self.args.ik_tolerance_m,
            )
            if metrics["max_commanded_fk_speed_m_s"] > float(self.config["velocity_cap_m_s"]) + self.args.speed_cap_tolerance_m_s:
                raise RuntimeError(
                    "Commanded FK speed exceeds Step5a cap: "
                    f"{metrics['max_commanded_fk_speed_m_s']:.6f} > {float(self.config['velocity_cap_m_s']):.6f}"
                )
            goal.trajectory.points = trajectory_points

            self.failure_stage = "action_server_gate"
            if not self.action_client.wait_for_server(timeout_sec=self.args.wait_s):
                raise RuntimeError(f"Action server unavailable: {self.args.action_name}")

            if not self.args.execute:
                _write_trace(self.args.trace, trace_rows)
                monitor.stop()
                monitor_finalized = True
                monitor.write_summary(self.args.kunwei_summary, _kunwei_extra(False, self))
                return self._summary(
                    dashboard,
                    _load_json_or_snapshot(self.args.kunwei_summary, monitor),
                    start_positions,
                    trace_rows,
                    metrics,
                    _empty_trace_alignment("not_executed"),
                )

            self.failure_stage = "pre_send_kunwei_gate"
            monitor.assert_fresh_and_within_force_delta()
            send_start = time.monotonic()
            self.failure_stage = "send_goal"
            send_future = self.action_client.send_goal_async(goal)
            rclpy.spin_until_future_complete(self, send_future, timeout_sec=self.args.wait_s)
            goal_handle = send_future.result()
            if goal_handle is None:
                raise RuntimeError("FollowJointTrajectory goal did not return a handle")
            self.sent_goal = True
            self.accepted = bool(goal_handle.accepted)
            self.trajectory_authority_entered = self.accepted
            if not goal_handle.accepted:
                raise RuntimeError("FollowJointTrajectory goal was rejected")

            self.failure_stage = "trajectory_execution"
            result_future = goal_handle.get_result_async()
            deadline = time.monotonic() + float(self.config["duration_s"]) + self.args.wait_s
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
            for _ in range(20):
                rclpy.spin_once(self, timeout_sec=0.01)
            trace_alignment = augment_trace_with_observed_fk(
                trace_rows,
                self.model_bundle,
                self.joint_history,
                send_start,
                start_positions,
                max_sample_gap_s=self.args.trace_max_sample_gap_s,
            )
            _write_trace(self.args.trace, trace_rows)
            monitor.stop()
            monitor_finalized = True
            monitor.write_summary(self.args.kunwei_summary, _kunwei_extra(True, self))
            if result.result.error_code != FollowJointTrajectory.Result.SUCCESSFUL:
                raise RuntimeError(f"Trajectory failed: {result.result.error_code} {result.result.error_string}")
            return self._summary(
                dashboard,
                _load_json_or_snapshot(self.args.kunwei_summary, monitor),
                start_positions,
                trace_rows,
                metrics,
                trace_alignment,
            )
        finally:
            if not monitor_finalized:
                monitor.stop()
                monitor.write_summary(self.args.kunwei_summary, _kunwei_extra(bool(self.args.execute), self))

    def _summary(
        self,
        dashboard: dict[str, str],
        kunwei_snapshot: dict[str, Any],
        start_positions: list[float],
        trace_rows: list[dict[str, Any]],
        metrics: dict[str, Any],
        trace_alignment: dict[str, Any],
    ) -> dict[str, Any]:
        observed_errors = [float(row["cartesian_error_m"]) for row in trace_rows if row.get("cartesian_error_m") not in {"", None}]
        achieved_speeds = [float(row["achieved_speed_m_s"]) for row in trace_rows if row.get("achieved_speed_m_s") not in {"", None}]
        rows = len(trace_rows)
        phase_final = float(trace_rows[-1]["phase_rad"]) if trace_rows else math.nan
        max_reference_speed = max(float(row["reference_speed_m_s"]) for row in trace_rows) if trace_rows else math.nan
        summary = {
            "ok": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "stage_id": self.config.get("stage_id", "step5a_ros2_remote_no_contact_v1"),
            "role": "step5a_live_no_contact_cartesian_cycloid",
            "motion_kind": "cartesian_cycloid",
            "execute": bool(self.args.execute),
            "live_air_motion_authorized": bool(self.args.execute),
            "contact_policy": dict(self.config.get("contact_policy", {})),
            "force_source": "kunwei_force_monitor",
            "kunwei_monitor": kunwei_snapshot,
            "kunwei_artifact_ok": _kunwei_artifact_ok(kunwei_snapshot),
            "ur_internal_force_delta_advisory": {"enabled": False, "status": "not_required_for_step5a_force_source"},
            "robot_ip": self.args.robot_ip,
            "action_name": self.args.action_name,
            "dashboard": dashboard,
            "rows": rows,
            "phase_final": phase_final,
            "velocity_cap_m_s": float(self.config["velocity_cap_m_s"]),
            "max_reference_speed_m_s": max_reference_speed,
            "max_commanded_fk_speed_m_s": metrics["max_commanded_fk_speed_m_s"],
            "max_achieved_speed_m_s": max(achieved_speeds) if achieved_speeds else math.nan,
            "max_cartesian_position_error_m": max(observed_errors) if observed_errors else metrics["max_commanded_position_error_m"],
            "reference_final_offset_xyz_m": metrics["reference_final_offset_xyz_m"],
            "reference_returns_to_anchor": metrics["reference_returns_to_anchor"],
            "commanded_net_displacement_xyz_m": metrics["commanded_net_displacement_xyz_m"],
            "commanded_net_displacement_norm_m": metrics["commanded_net_displacement_norm_m"],
            "anchor_pose_base": metrics["anchor_pose_base"],
            "ik_source": {
                "solver": "pinocchio_calibrated_tool0_warm_start_deterministic_dls",
                "calibration_yaml": str(self.args.calibration_yaml),
                "xacro_path": str(self.args.xacro_path),
                "calibration_hash": self.model_bundle.calibration_hash,
                "expected_calibration_hash": EXPECTED_CALIBRATION_HASH,
                "base_frame": "base",
                "tool_frame": "tool0",
                "orientation": "fixed_from_first_joint_state_fk",
                "delta_z_m": 0.0,
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
            "trace_rows": rows,
            "trace_fields": LIVE_TRACE_FIELDS,
            "trace_alignment": trace_alignment,
            "joint_state_history": {
                "retained_samples": len(self.joint_history),
                "max_samples": int(getattr(self.args, "joint_history_max_samples", DEFAULT_JOINT_HISTORY_MAX_SAMPLES)),
            },
            "gate_a_thresholds": {
                "velocity_cap_m_s": float(self.config["velocity_cap_m_s"]),
                "cartesian_position_error_limit_m": self.args.gate_a_position_error_limit_m,
                "trace_max_sample_gap_s": self.args.trace_max_sample_gap_s,
            },
        }
        summary["acceptance"] = step5a_acceptance(summary)
        summary["gate_a_pass"] = bool(summary["acceptance"]["gate_a_pass"])
        summary["ok"] = bool(summary["gate_a_pass"])
        validate_cartesian_acceptance_summary(summary)
        return summary


def build_cartesian_cycloid_trajectory(
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
    q = np.array(start_positions, dtype=float)
    anchor = fk_tool0_base(model_bundle, q)
    points: list[JointTrajectoryPoint] = []
    trace_rows: list[dict[str, Any]] = []
    commanded_positions: list[np.ndarray] = []
    commanded_xyz: list[np.ndarray] = []
    max_commanded_error = 0.0
    for row in rows:
        target = pin.SE3(anchor.rotation, anchor.translation + np.array([float(row["desired_x_m"]), float(row["desired_y_m"]), 0.0]))
        q, error_norm = solve_tool0_ik(
            model_bundle,
            q,
            target,
            damping=ik_damping,
            max_iters=ik_max_iters,
            tolerance_m=ik_tolerance_m,
        )
        max_commanded_error = max(max_commanded_error, error_norm)
        commanded_positions.append(q.copy())
        placement = fk_tool0_base(model_bundle, q)
        commanded_xyz.append(placement.translation.copy())

        point = JointTrajectoryPoint()
        point.positions = [float(value) for value in q]
        t_rel_s = int(row["row_index"]) * dt
        point.time_from_start.sec = int(t_rel_s)
        point.time_from_start.nanosec = int(round((t_rel_s - int(t_rel_s)) * 1_000_000_000))
        if point.time_from_start.nanosec >= 1_000_000_000:
            point.time_from_start.sec += 1
            point.time_from_start.nanosec -= 1_000_000_000
        points.append(point)

        trace_row = dict(row)
        trace_row["cmd_enabled"] = True
        for name, value in zip(COMMAND_FIELDS, q):
            trace_row[name] = float(value)
        trace_row["commanded_fk_x_m"] = float(placement.translation[0])
        trace_row["commanded_fk_y_m"] = float(placement.translation[1])
        trace_row["commanded_fk_z_m"] = float(placement.translation[2])
        trace_row["observed_fk_x_m"] = ""
        trace_row["observed_fk_y_m"] = ""
        trace_row["observed_fk_z_m"] = ""
        trace_row["cartesian_error_m"] = ""
        trace_row["achieved_speed_m_s"] = ""
        trace_row["kunwei_force_delta_n"] = ""
        trace_rows.append(trace_row)

    for index, point in enumerate(points):
        if index == 0:
            point.velocities = [0.0] * len(JOINT_NAMES)
        else:
            point.velocities = [float(value) for value in (commanded_positions[index] - commanded_positions[index - 1]) / dt]

    commanded_speeds = [
        float(np.linalg.norm(commanded_xyz[index] - commanded_xyz[index - 1]) / dt) for index in range(1, len(commanded_xyz))
    ]
    final_reference_offset = np.array(
        [
            float(rows[-1]["desired_x_m"]) if rows else 0.0,
            float(rows[-1]["desired_y_m"]) if rows else 0.0,
            0.0,
        ],
        dtype=float,
    )
    commanded_net_displacement = commanded_xyz[-1] - commanded_xyz[0] if len(commanded_xyz) >= 2 else np.zeros(3)
    metrics = {
        "max_commanded_fk_speed_m_s": max(commanded_speeds) if commanded_speeds else 0.0,
        "max_commanded_position_error_m": max_commanded_error,
        "reference_final_offset_xyz_m": [float(value) for value in final_reference_offset],
        "reference_returns_to_anchor": bool(np.linalg.norm(final_reference_offset) <= 1e-6),
        "commanded_net_displacement_xyz_m": [float(value) for value in commanded_net_displacement],
        "commanded_net_displacement_norm_m": float(np.linalg.norm(commanded_net_displacement)),
        "anchor_pose_base": {
            "frame": "base_to_tool0",
            "position_xyz_m": [float(value) for value in anchor.translation],
            "rotation_matrix_row_major": [float(value) for value in anchor.rotation.reshape(-1)],
        },
    }
    return points, trace_rows, metrics


def solve_tool0_ik(
    model_bundle: CalibratedModel,
    q_seed: np.ndarray,
    target: pin.SE3,
    *,
    damping: float,
    max_iters: int,
    tolerance_m: float,
) -> tuple[np.ndarray, float]:
    q = q_seed.copy()
    last_error_norm = math.inf
    for _ in range(max_iters):
        placement = fk_tool0_base(model_bundle, q)
        error = pin.log(placement.inverse() * target).vector
        last_error_norm = float(np.linalg.norm(error[:3]))
        if last_error_norm <= tolerance_m and float(np.linalg.norm(error[3:])) <= 1e-4:
            break
        pin.computeJointJacobians(model_bundle.model, model_bundle.data, q)
        jacobian = pin.getFrameJacobian(
            model_bundle.model,
            model_bundle.data,
            model_bundle.tool0_frame_id,
            pin.ReferenceFrame.LOCAL,
        )
        lhs = jacobian @ jacobian.T + (damping * damping) * np.eye(6)
        delta = jacobian.T @ np.linalg.solve(lhs, error)
        step_norm = float(np.linalg.norm(delta))
        if step_norm > 0.05:
            delta *= 0.05 / step_norm
        q = pin.integrate(model_bundle.model, q, delta)
    return q, last_error_norm


def fk_tool0_base(model_bundle: CalibratedModel, q: np.ndarray) -> pin.SE3:
    pin.forwardKinematics(model_bundle.model, model_bundle.data, q)
    pin.updateFramePlacements(model_bundle.model, model_bundle.data)
    base = model_bundle.data.oMf[model_bundle.base_frame_id]
    tool0 = model_bundle.data.oMf[model_bundle.tool0_frame_id]
    return base.inverse() * tool0


def augment_trace_with_observed_fk(
    trace_rows: list[dict[str, Any]],
    model_bundle: CalibratedModel,
    joint_history: list[ObservedJointSample],
    send_start: float,
    anchor_positions: list[float],
    *,
    max_sample_gap_s: float = DEFAULT_TRACE_MAX_SAMPLE_GAP_S,
) -> dict[str, Any]:
    observed = [
        {
            "t_rel_s": 0.0,
            "header_stamp_s": None,
            "positions": np.array(anchor_positions, dtype=float),
            "source": "anchor",
        }
    ]
    for sample in joint_history:
        if sample.receive_monotonic_s < send_start:
            continue
        observed.append(
            {
                "t_rel_s": sample.receive_monotonic_s - send_start,
                "header_stamp_s": sample.header_stamp_s,
                "positions": np.array(sample.positions, dtype=float),
                "source": "joint_state",
            }
        )
    observed = _dedupe_observed_samples(sorted(observed, key=lambda sample: float(sample["t_rel_s"])))
    if not observed:
        return _empty_trace_alignment("no_observed_samples")

    previous_xyz: np.ndarray | None = None
    previous_t: float | None = None
    max_gap_s = 0.0
    max_dt_to_row_s = 0.0
    extrapolated_rows = 0
    interpolated_rows = 0
    exact_rows = 0
    row0_anchor_error = math.nan
    for row in trace_rows:
        target_t = float(row["t_rel_s"])
        sample = _interpolate_observed_sample(observed, target_t)
        positions = sample["positions"]
        placement = fk_tool0_base(model_bundle, positions)
        xyz = placement.translation.copy()
        max_gap_s = max(max_gap_s, float(sample["gap_s"]))
        max_dt_to_row_s = max(max_dt_to_row_s, float(sample["dt_to_row_s"]))
        if str(sample["alignment_status"]).startswith("extrapolated"):
            extrapolated_rows += 1
        elif sample["alignment_status"] == "interpolated":
            interpolated_rows += 1
        elif sample["alignment_status"] in {"exact", "anchor"}:
            exact_rows += 1
        row["observed_fk_x_m"] = float(xyz[0])
        row["observed_fk_y_m"] = float(xyz[1])
        row["observed_fk_z_m"] = float(xyz[2])
        for field, value in zip(OBSERVED_JOINT_FIELDS, positions):
            row[field] = float(value)
        row["observed_sample_t_rel_s"] = float(sample["t_rel_s"])
        row["observed_sample_dt_to_row_s"] = float(sample["dt_to_row_s"])
        row["observed_sample_gap_s"] = float(sample["gap_s"])
        row["alignment_status"] = str(sample["alignment_status"])
        commanded = np.array([float(row["commanded_fk_x_m"]), float(row["commanded_fk_y_m"]), float(row["commanded_fk_z_m"])])
        row["cartesian_error_m"] = float(np.linalg.norm(xyz - commanded))
        if int(row["row_index"]) == 0:
            row0_anchor_error = float(row["cartesian_error_m"])
        if previous_xyz is None or previous_t is None or target_t <= previous_t:
            row["achieved_speed_m_s"] = 0.0
        else:
            row["achieved_speed_m_s"] = float(np.linalg.norm(xyz - previous_xyz) / (target_t - previous_t))
        previous_xyz = xyz
        previous_t = target_t
    raw_after_send = max(0, len(observed) - 1)
    trace_alignment_ok = (
        raw_after_send >= 2
        and extrapolated_rows == 0
        and max_gap_s <= max_sample_gap_s
        and math.isfinite(row0_anchor_error)
        and row0_anchor_error <= 1e-9
    )
    return {
        "trace_alignment_ok": trace_alignment_ok,
        "observed_sample_count_raw": raw_after_send,
        "observed_sample_count_with_anchor": len(observed),
        "resampled_rows": len(trace_rows),
        "exact_rows": exact_rows,
        "interpolated_rows": interpolated_rows,
        "extrapolated_rows": extrapolated_rows,
        "max_interpolation_gap_s": max_gap_s,
        "max_observed_dt_to_row_s": max_dt_to_row_s,
        "row0_anchor_error_m": row0_anchor_error,
        "trace_max_sample_gap_s": max_sample_gap_s,
    }


def _dedupe_observed_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sample in samples:
        if out and math.isclose(float(sample["t_rel_s"]), float(out[-1]["t_rel_s"]), rel_tol=0.0, abs_tol=1e-9):
            out[-1] = sample
        else:
            out.append(sample)
    return out


def _interpolate_observed_sample(samples: list[dict[str, Any]], target_t: float) -> dict[str, Any]:
    times = [float(sample["t_rel_s"]) for sample in samples]
    index = bisect_left(times, target_t)
    if index < len(samples) and math.isclose(times[index], target_t, rel_tol=0.0, abs_tol=1e-9):
        status = "anchor" if target_t == 0.0 else "exact"
        return {
            "t_rel_s": times[index],
            "positions": samples[index]["positions"],
            "gap_s": 0.0,
            "dt_to_row_s": 0.0,
            "alignment_status": status,
        }
    if index == 0:
        return {
            "t_rel_s": times[0],
            "positions": samples[0]["positions"],
            "gap_s": math.inf,
            "dt_to_row_s": abs(times[0] - target_t),
            "alignment_status": "extrapolated_before_first",
        }
    if index >= len(samples):
        return {
            "t_rel_s": times[-1],
            "positions": samples[-1]["positions"],
            "gap_s": math.inf,
            "dt_to_row_s": abs(times[-1] - target_t),
            "alignment_status": "extrapolated_after_last",
        }
    before = samples[index - 1]
    after = samples[index]
    t0 = times[index - 1]
    t1 = times[index]
    gap = t1 - t0
    if gap <= 0.0:
        weight = 0.0
    else:
        weight = (target_t - t0) / gap
    positions = before["positions"] + weight * (after["positions"] - before["positions"])
    return {
        "t_rel_s": target_t,
        "positions": positions,
        "gap_s": gap,
        "dt_to_row_s": min(abs(target_t - t0), abs(t1 - target_t)),
        "alignment_status": "interpolated",
    }


def _empty_trace_alignment(status: str) -> dict[str, Any]:
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


def step5a_acceptance(payload: dict[str, Any]) -> dict[str, Any]:
    thresholds = payload.get("gate_a_thresholds", {})
    velocity_cap = float(payload.get("velocity_cap_m_s", thresholds.get("velocity_cap_m_s", 0.009)))
    error_limit = float(thresholds.get("cartesian_position_error_limit_m", DEFAULT_GATE_A_POSITION_ERROR_LIMIT_M))
    trace_alignment = payload.get("trace_alignment", {})
    checks = {
        "cartesian_shape": (
            payload.get("role") == "step5a_live_no_contact_cartesian_cycloid"
            and payload.get("motion_kind") == "cartesian_cycloid"
            and _int_eq(payload.get("rows"), 1101)
            and _float_close(payload.get("phase_final"), 6.0, abs_tol=1e-9)
        ),
        "trajectory_result": (
            payload.get("sent_goal") is True
            and payload.get("accepted") is True
            and _int_eq(payload.get("result_error_code"), 0)
        ),
        "kunwei_evidence": _kunwei_artifact_ok(payload.get("kunwei_monitor", {})),
        "speed_cap": (
            _finite_leq(payload.get("max_reference_speed_m_s"), velocity_cap)
            and _finite_leq(payload.get("max_commanded_fk_speed_m_s"), velocity_cap)
            and _finite_leq(payload.get("max_achieved_speed_m_s"), velocity_cap)
        ),
        "cartesian_equivalence": (
            _finite_leq(payload.get("max_cartesian_position_error_m"), error_limit)
            and trace_alignment.get("trace_alignment_ok") is True
        ),
    }
    failed = [name for name, ok in checks.items() if not ok]
    return {
        **checks,
        "gate_a_pass": not failed,
        "failed_conditions": failed,
        "thresholds": {
            "velocity_cap_m_s": velocity_cap,
            "cartesian_position_error_limit_m": error_limit,
            "trace_max_sample_gap_s": thresholds.get("trace_max_sample_gap_s", DEFAULT_TRACE_MAX_SAMPLE_GAP_S),
        },
    }


def _finite_leq(value: Any, limit: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number <= limit + 1e-12


def _int_eq(value: Any, expected: int) -> bool:
    try:
        return int(value) == expected
    except (TypeError, ValueError):
        return False


def _float_close(value: Any, expected: float, *, abs_tol: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and math.isclose(number, expected, rel_tol=0.0, abs_tol=abs_tol)


def _kunwei_artifact_ok(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("ok") is True
        and payload.get("stream_start_command_sent") is True
        and payload.get("stream_stop_command_sent") is True
        and payload.get("failure_reason") is None
    )


def _kunwei_extra(live_motion: bool, node: Step5aCartesianCycloidMotion) -> dict[str, Any]:
    return {
        "live_motion": live_motion,
        "failure_stage": node.failure_stage,
        "sent_goal": node.sent_goal,
        "accepted": node.accepted,
        "result_status": node.result_status,
        "result_error_code": node.result_error_code,
        "result_error_string": node.result_error_string,
        "trajectory_authority_entered": node.trajectory_authority_entered,
    }


def _load_json_or_snapshot(path: Path, monitor: KunweiPersistentMonitor) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return monitor.snapshot()


def validate_cartesian_acceptance_summary(payload: dict[str, Any]) -> bool:
    required = [
        "role",
        "motion_kind",
        "rows",
        "phase_final",
        "sent_goal",
        "accepted",
        "result_error_code",
        "kunwei_artifact_ok",
        "velocity_cap_m_s",
        "max_reference_speed_m_s",
        "max_commanded_fk_speed_m_s",
        "max_achieved_speed_m_s",
        "max_cartesian_position_error_m",
        "anchor_pose_base",
        "ik_source",
        "contact_policy",
        "trace_alignment",
    ]
    missing = [field for field in required if field not in payload]
    if missing:
        raise RuntimeError(f"Step5a Cartesian summary missing fields: {missing}")
    if "acceptance" not in payload:
        payload["acceptance"] = step5a_acceptance(payload)
    if "gate_a_pass" not in payload:
        payload["gate_a_pass"] = bool(payload["acceptance"]["gate_a_pass"])
    return bool(payload["gate_a_pass"])


def dashboard_exchange(host: str, commands: list[str], *, port: int = 29999, timeout_s: float = 2.0) -> dict[str, str]:
    responses: dict[str, str] = {}
    with socket.create_connection((host, port), timeout=timeout_s) as sock:
        sock.settimeout(timeout_s)
        responses["banner"] = sock.recv(4096).decode("utf-8", errors="replace").strip()
        for command in commands:
            sock.sendall((command + "\n").encode("utf-8"))
            responses[command] = sock.recv(4096).decode("utf-8", errors="replace").strip()
    return responses


def _ordered_positions(joint_state: JointState) -> list[float]:
    by_name = {name: joint_state.position[index] for index, name in enumerate(joint_state.name)}
    return [float(by_name[name]) for name in JOINT_NAMES]


def _joint_state_header_stamp_s(joint_state: JointState) -> float | None:
    stamp = joint_state.header.stamp
    if stamp.sec == 0 and stamp.nanosec == 0:
        return None
    return float(stamp.sec) + float(stamp.nanosec) * 1e-9


def _validate_dashboard(responses: dict[str, str]) -> None:
    if responses.get("is in remote control") != "true":
        raise RuntimeError(f"Remote Control is not true: {responses.get('is in remote control')}")
    if "NORMAL" not in responses.get("safetymode", ""):
        raise RuntimeError(f"Safety mode is not NORMAL: {responses.get('safetymode')}")
    if "RUNNING" not in responses.get("robotmode", ""):
        raise RuntimeError(f"Robot mode is not RUNNING: {responses.get('robotmode')}")
    running = responses.get("running")
    if running not in {"Program running: false", "Program running: true"}:
        raise RuntimeError(f"Unexpected Dashboard running state: {running}")


def _write_trace(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LIVE_TRACE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in LIVE_TRACE_FIELDS})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run explicit Step5a live no-contact Cartesian cycloid motion.")
    parser.add_argument("--execute", action="store_true", help="Actually send the trajectory goal.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--robot-ip", default="192.168.1.18")
    parser.add_argument("--action-name", default="/scaled_joint_trajectory_controller/follow_joint_trajectory")
    parser.add_argument("--joint-state-topic", default="/joint_states")
    parser.add_argument("--wait-s", type=float, default=15.0)
    parser.add_argument("--calibration-yaml", type=Path, default=DEFAULT_CALIBRATION_YAML)
    parser.add_argument("--xacro-path", type=Path, default=DEFAULT_XACRO_PATH)
    parser.add_argument("--ik-damping", type=float, default=1e-4)
    parser.add_argument("--ik-max-iters", type=int, default=80)
    parser.add_argument("--ik-tolerance-m", type=float, default=5e-5)
    parser.add_argument("--speed-cap-tolerance-m-s", type=float, default=0.0005)
    parser.add_argument("--gate-a-position-error-limit-m", type=float, default=DEFAULT_GATE_A_POSITION_ERROR_LIMIT_M)
    parser.add_argument("--trace-max-sample-gap-s", type=float, default=DEFAULT_TRACE_MAX_SAMPLE_GAP_S)
    parser.add_argument("--joint-history-max-samples", type=int, default=DEFAULT_JOINT_HISTORY_MAX_SAMPLES)
    parser.add_argument("--kunwei-sensor-ip", default="192.168.50.25")
    parser.add_argument("--kunwei-sensor-port", type=int, default=5152)
    parser.add_argument("--kunwei-ready-timeout-s", type=float, default=3.0)
    parser.add_argument("--kunwei-window-s", type=float, default=0.5)
    parser.add_argument("--kunwei-latest-max-age-s", type=float, default=0.25)
    parser.add_argument("--kunwei-min-recent-samples", type=int, default=20)
    parser.add_argument("--kunwei-max-force-delta-n", type=float, default=8.0)
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
    node = Step5aCartesianCycloidMotion(args, config, model_bundle)
    try:
        summary = node.run()
        text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(text, encoding="utf-8")
        print(text, end="")
        return 0 if summary["ok"] else 2
    except Exception as exc:
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "stage_id": config.get("stage_id", "step5a_ros2_remote_no_contact_v1"),
            "role": "step5a_live_no_contact_cartesian_cycloid",
            "motion_kind": "cartesian_cycloid",
            "sent_goal": bool(getattr(node, "sent_goal", False)),
            "accepted": bool(getattr(node, "accepted", False)),
            "result_status": getattr(node, "result_status", None),
            "result_error_code": getattr(node, "result_error_code", None),
            "result_error_string": getattr(node, "result_error_string", None),
            "failure_stage": getattr(node, "failure_stage", "unknown"),
            "trajectory_authority_entered": bool(getattr(node, "trajectory_authority_entered", False)),
            "force_source": "kunwei_force_monitor",
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(text, encoding="utf-8")
        print(text, end="", file=sys.stderr)
        return 2
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
