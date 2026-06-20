#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from textwrap import dedent
from typing import Sequence


os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

WORKSPACE = Path(__file__).resolve().parents[3]
SRC_PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(SRC_PACKAGE) not in sys.path:
    sys.path.insert(0, str(SRC_PACKAGE))

from ur10e_example_controllers import ur10e_gazebo_matrix_runner as runner  # noqa: E402
from ur10e_example_controllers.step5a_cartesian_cycloid_motion import JOINT_NAMES  # noqa: E402


DEFAULT_WORLD_NAME = "ur10e_step5_table_world"
DEFAULT_MODEL_NAME = "active_tcp_marker"
DEFAULT_UPDATE_PERIOD_S = 0.10
DEFAULT_SERVICE_TIMEOUT_MS = 1000
POSE_SOURCE_ACTIVE_TCP = "joint_states_to_runner_fk_active_tcp_base"


@dataclass(frozen=True)
class MarkerPose:
    t_s: float
    x_m: float
    y_m: float
    z_m: float


@dataclass(frozen=True)
class MarkerArtifacts:
    manifest_path: Path
    trace_path: Path


@lru_cache(maxsize=1)
def _model_bundle():
    from ur10e_example_controllers.step5a_cartesian_cycloid_motion import build_calibrated_model

    return build_calibrated_model()


def build_marker_model_sdf(model_name: str = DEFAULT_MODEL_NAME) -> str:
    return dedent(
        f"""\
        <sdf version="1.7">
          <model name="{model_name}">
            <static>true</static>
            <link name="tcp_marker_link">
              <visual name="tcp_magenta_sphere">
                <pose>0 0 0.022 0 0 0</pose>
                <geometry>
                  <sphere>
                    <radius>0.026</radius>
                  </sphere>
                </geometry>
                <material>
                  <ambient>1 0 1 1</ambient>
                  <diffuse>1 0 1 1</diffuse>
                  <emissive>0.7 0 0.7 1</emissive>
                </material>
              </visual>
              <visual name="tcp_white_mast">
                <pose>0 0 0.085 0 0 0</pose>
                <geometry>
                  <cylinder>
                    <radius>0.006</radius>
                    <length>0.170</length>
                  </cylinder>
                </geometry>
                <material>
                  <ambient>1 1 1 1</ambient>
                  <diffuse>1 1 1 1</diffuse>
                  <emissive>0.8 0.8 0.8 1</emissive>
                </material>
              </visual>
              <visual name="tcp_cyan_crossbar_x">
                <pose>0 0 0.060 0 1.57079632679 0</pose>
                <geometry>
                  <cylinder>
                    <radius>0.005</radius>
                    <length>0.100</length>
                  </cylinder>
                </geometry>
                <material>
                  <ambient>0 1 1 1</ambient>
                  <diffuse>0 1 1 1</diffuse>
                  <emissive>0 0.7 0.7 1</emissive>
                </material>
              </visual>
              <visual name="tcp_magenta_crossbar_y">
                <pose>0 0 0.060 1.57079632679 0 0</pose>
                <geometry>
                  <cylinder>
                    <radius>0.005</radius>
                    <length>0.100</length>
                  </cylinder>
                </geometry>
                <material>
                  <ambient>1 0 1 1</ambient>
                  <diffuse>1 0 1 1</diffuse>
                  <emissive>0.7 0 0.7 1</emissive>
                </material>
              </visual>
            </link>
          </model>
        </sdf>
        """
    )


def write_marker_model_sdf(output_dir: Path, model_name: str = DEFAULT_MODEL_NAME) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = (output_dir / f"{model_name}.sdf").resolve()
    path.write_text(build_marker_model_sdf(model_name), encoding="utf-8")
    return path


def marker_pose_from_joint_positions(
    joint_positions: Sequence[float],
    *,
    t_s: float = 0.0,
    model_bundle=None,
) -> MarkerPose:
    import numpy as np

    from ur10e_example_controllers.step5a_cartesian_cycloid_motion import fk_tool0_base

    if len(joint_positions) != len(JOINT_NAMES):
        raise ValueError(f"expected {len(JOINT_NAMES)} joint positions, got {len(joint_positions)}")
    bundle = model_bundle if model_bundle is not None else _model_bundle()
    fk = fk_tool0_base(bundle, np.array([float(value) for value in joint_positions], dtype=float))
    active_tcp_xyz = runner.active_tcp_xyz_from_tool0_pose(fk)
    return MarkerPose(
        t_s=float(t_s),
        x_m=float(active_tcp_xyz[0]),
        y_m=float(active_tcp_xyz[1]),
        z_m=float(active_tcp_xyz[2]),
    )


def write_marker_artifacts(
    output_dir: Path,
    *,
    stage_id: str,
    world_name: str,
    model_name: str,
    pose_records: Sequence[MarkerPose],
    pose_source: str,
    spawned: bool | None = None,
    create_service: str | None = None,
    set_pose_service: str | None = None,
    marker_sdf_path: Path | None = None,
) -> MarkerArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_path = output_dir / "tcp_marker_pose_trace.csv"
    with trace_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["t_s", "x_m", "y_m", "z_m"])
        writer.writeheader()
        for pose in pose_records:
            writer.writerow(
                {
                    "t_s": f"{pose.t_s:.9f}",
                    "x_m": f"{pose.x_m:.9f}",
                    "y_m": f"{pose.y_m:.9f}",
                    "z_m": f"{pose.z_m:.9f}",
                }
            )

    manifest = {
        "schema": "ur10e_gazebo_tcp_marker_manifest_v2",
        "stage_id": stage_id,
        "world_name": world_name,
        "model_name": model_name,
        "pose_source": pose_source,
        "pose_frame": runner.ACTIVE_TCP_FRAME,
        "tool_frame": runner.TOOL0_FRAME,
        "active_tcp_offset_tool0_m": list(runner.ACTIVE_TCP_OFFSET_TOOL0_M),
        "pose_count": len(pose_records),
        "trace_path": str(trace_path),
        "marker_sdf_path": str(marker_sdf_path) if marker_sdf_path is not None else None,
        "spawned": spawned,
        "create_service": create_service,
        "set_pose_service": set_pose_service,
        "first_pose": _pose_payload(pose_records[0]) if pose_records else None,
        "final_pose": _pose_payload(pose_records[-1]) if pose_records else None,
    }
    manifest_path = output_dir / "tcp_marker_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return MarkerArtifacts(manifest_path=manifest_path, trace_path=trace_path)


def build_create_request(
    *,
    model_name: str,
    pose: MarkerPose,
    marker_sdf_path: Path,
) -> str:
    return (
        f"sdf_filename: {_quoted(marker_sdf_path)} "
        f"name: {_quoted(model_name)} "
        "allow_renaming: false "
        f"pose {{ {_pose_text(pose)} }}"
    )


def build_set_pose_request(*, model_name: str, pose: MarkerPose) -> str:
    return f"name: {_quoted(model_name)} {_pose_text(pose)}"


def build_remove_request(*, model_name: str) -> str:
    return f"name: {_quoted(model_name)} type: MODEL"


def spawn_marker_model(
    *,
    world_name: str,
    model_name: str,
    pose: MarkerPose,
    marker_sdf_path: Path,
    timeout_ms: int = DEFAULT_SERVICE_TIMEOUT_MS,
) -> bool:
    request = build_create_request(model_name=model_name, pose=pose, marker_sdf_path=marker_sdf_path)
    return _call_ign_service(
        _service(world_name, "create"),
        "ignition.msgs.EntityFactory",
        request,
        timeout_ms=timeout_ms,
    )


def set_marker_pose(
    *,
    world_name: str,
    model_name: str,
    pose: MarkerPose,
    timeout_ms: int = DEFAULT_SERVICE_TIMEOUT_MS,
) -> bool:
    return _call_ign_service(
        _service(world_name, "set_pose"),
        "ignition.msgs.Pose",
        build_set_pose_request(model_name=model_name, pose=pose),
        timeout_ms=timeout_ms,
    )


def remove_marker_model(
    *,
    world_name: str,
    model_name: str,
    timeout_ms: int = DEFAULT_SERVICE_TIMEOUT_MS,
) -> bool:
    return _call_ign_service(
        _service(world_name, "remove"),
        "ignition.msgs.Entity",
        build_remove_request(model_name=model_name),
        timeout_ms=timeout_ms,
    )


def _call_ign_service(service: str, reqtype: str, request: str, *, timeout_ms: int) -> bool:
    completed = subprocess.run(
        [
            "ign",
            "service",
            "-s",
            service,
            "--reqtype",
            reqtype,
            "--reptype",
            "ignition.msgs.Boolean",
            "--timeout",
            str(timeout_ms),
            "--req",
            request,
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=max(timeout_ms / 1000.0 + 1.0, 2.0),
    )
    return completed.returncode == 0 and "data: true" in completed.stdout


def _quoted(value: object) -> str:
    return json.dumps(str(value))


def _pose_text(pose: MarkerPose) -> str:
    return (
        f"position {{ x: {pose.x_m:.9f} y: {pose.y_m:.9f} z: {pose.z_m:.9f} }} "
        "orientation { w: 1 }"
    )


def _legacy_spawn_marker_model(
    gz_node,
    *,
    world_name: str,
    model_name: str,
    pose: MarkerPose,
    marker_sdf_path: Path | None = None,
    timeout_ms: int = DEFAULT_SERVICE_TIMEOUT_MS,
) -> bool:
    from gz.msgs10.boolean_pb2 import Boolean
    from gz.msgs10.entity_factory_pb2 import EntityFactory

    request = EntityFactory()
    if marker_sdf_path is None:
        request.sdf = build_marker_model_sdf(model_name)
    else:
        request.sdf_filename = str(marker_sdf_path)
    request.name = model_name
    request.allow_renaming = False
    request.relative_to = "world"
    _fill_pose(request.pose, model_name, pose)
    ok, response = gz_node.request(
        _service(world_name, "create"),
        request,
        EntityFactory,
        Boolean,
        timeout_ms,
    )
    return bool(ok and response.data)


def _legacy_set_marker_pose(
    gz_node,
    *,
    world_name: str,
    model_name: str,
    pose: MarkerPose,
    timeout_ms: int = DEFAULT_SERVICE_TIMEOUT_MS,
) -> bool:
    from gz.msgs10.boolean_pb2 import Boolean
    from gz.msgs10.pose_pb2 import Pose

    request = Pose()
    _fill_pose(request, model_name, pose)
    ok, response = gz_node.request(
        _service(world_name, "set_pose"),
        request,
        Pose,
        Boolean,
        timeout_ms,
    )
    return bool(ok and response.data)


def _legacy_remove_marker_model(
    gz_node,
    *,
    world_name: str,
    model_name: str,
    timeout_ms: int = DEFAULT_SERVICE_TIMEOUT_MS,
) -> bool:
    from gz.msgs10.boolean_pb2 import Boolean
    from gz.msgs10.entity_pb2 import Entity

    request = Entity()
    request.name = model_name
    request.type = Entity.MODEL
    ok, response = gz_node.request(
        _service(world_name, "remove"),
        request,
        Entity,
        Boolean,
        timeout_ms,
    )
    return bool(ok and response.data)


def _pose_payload(pose: MarkerPose) -> dict[str, float]:
    return {"t_s": pose.t_s, "x_m": pose.x_m, "y_m": pose.y_m, "z_m": pose.z_m}


def _service(world_name: str, action: str) -> str:
    return f"/world/{world_name}/{action}"


def _fill_pose(message, model_name: str, pose: MarkerPose) -> None:
    message.name = model_name
    message.position.x = pose.x_m
    message.position.y = pose.y_m
    message.position.z = pose.z_m
    message.orientation.w = 1.0
    message.orientation.x = 0.0
    message.orientation.y = 0.0
    message.orientation.z = 0.0


def run_follower(args: argparse.Namespace) -> MarkerArtifacts:
    import rclpy
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node as RosNode
    from sensor_msgs.msg import JointState

    marker_sdf_path = write_marker_model_sdf(args.output_dir, args.model_name)
    stop_requested = {"value": False}

    def _request_stop(_signum, _frame) -> None:
        stop_requested["value"] = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    class MarkerFollowerNode(RosNode):
        def __init__(self) -> None:
            super().__init__("ur10e_gazebo_tcp_marker_follower")
            self.latest_positions: list[float] | None = None
            self.pose_records: list[MarkerPose] = []
            self.spawned = False
            self.start_time = time.monotonic()
            self.last_update_time = 0.0
            self.create_subscription(JointState, args.joint_state_topic, self._on_joint_state, 10)
            self.create_timer(max(args.update_period_s, 0.02), self._update_marker)

        def _on_joint_state(self, msg: JointState) -> None:
            by_name = dict(zip(msg.name, msg.position))
            if all(name in by_name for name in JOINT_NAMES):
                self.latest_positions = [float(by_name[name]) for name in JOINT_NAMES]

        def _update_marker(self) -> None:
            if self.latest_positions is None:
                return
            now = time.monotonic()
            if now - self.last_update_time < args.update_period_s * 0.5:
                return
            self.last_update_time = now
            pose = marker_pose_from_joint_positions(
                self.latest_positions,
                t_s=now - self.start_time,
            )
            if not self.spawned:
                remove_marker_model(
                    world_name=args.world_name,
                    model_name=args.model_name,
                    timeout_ms=args.service_timeout_ms,
                )
                self.spawned = spawn_marker_model(
                    world_name=args.world_name,
                    model_name=args.model_name,
                    pose=pose,
                    marker_sdf_path=marker_sdf_path,
                    timeout_ms=args.service_timeout_ms,
                )
            if self.spawned:
                set_marker_pose(
                    world_name=args.world_name,
                    model_name=args.model_name,
                    pose=pose,
                    timeout_ms=args.service_timeout_ms,
                )
            self.pose_records.append(pose)

    rclpy.init()
    node = MarkerFollowerNode()
    try:
        deadline = time.monotonic() + args.duration_s if args.duration_s > 0 else None
        while rclpy.ok() and not stop_requested["value"] and (deadline is None or time.monotonic() < deadline):
            try:
                rclpy.spin_once(node, timeout_sec=0.1)
            except (ExternalShutdownException, KeyboardInterrupt):
                stop_requested["value"] = True
    finally:
        artifacts = write_marker_artifacts(
            args.output_dir,
            stage_id=args.stage,
            world_name=args.world_name,
            model_name=args.model_name,
            pose_records=node.pose_records,
            pose_source=POSE_SOURCE_ACTIVE_TCP,
            spawned=node.spawned,
            create_service=_service(args.world_name, "create"),
            set_pose_service=_service(args.world_name, "set_pose"),
            marker_sdf_path=marker_sdf_path,
        )
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return artifacts


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Follow the UR10e active TCP in Gazebo with a visible marker model.")
    parser.add_argument("--stage", required=True, choices=sorted(runner.offline.STAGE_REGISTRY))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--world-name", default=DEFAULT_WORLD_NAME)
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--joint-state-topic", default="/joint_states")
    parser.add_argument("--update-period-s", type=float, default=DEFAULT_UPDATE_PERIOD_S)
    parser.add_argument("--duration-s", type=float, default=0.0, help="Run duration; <=0 means until interrupted.")
    parser.add_argument("--service-timeout-ms", type=int, default=DEFAULT_SERVICE_TIMEOUT_MS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    artifacts = run_follower(args)
    print(artifacts.manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
