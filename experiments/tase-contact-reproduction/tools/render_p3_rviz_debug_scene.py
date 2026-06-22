#!/usr/bin/env python3
"""Render the P3 RViz debug evidence pack in an isolated display.

Run this under a sourced ROS environment and an X display, typically:

  xvfb-run -a -s '-screen 0 1280x900x24' python3 .../render_p3_rviz_debug_scene.py ...

The publisher child emits only static/offline TF, markers, path, and
robot_description data for RViz. It does not start a bridge, controller, Gazebo,
or any live robot path.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(WORKSPACE.resolve()))
    except ValueError:
        return str(path)


def resolve_manifest_relative(manifest_path: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    candidate = manifest_path.parent / path
    if candidate.exists():
        return candidate
    return WORKSPACE / path


def build_render_report(
    *,
    generated_at: str,
    manifest_path: Path,
    rviz_config_path: Path,
    screenshot_path: Path,
    publisher_summary_path: Path,
    rviz_stdout_path: Path,
    rviz_stderr_path: Path,
    ffmpeg_stderr_path: Path,
    rviz_returncode: int | None,
    publisher_returncode: int | None,
    ffmpeg_returncode: int,
) -> dict[str, Any]:
    manifest = load_json(manifest_path) if manifest_path.is_file() else {}
    screenshot_exists = screenshot_path.is_file() and screenshot_path.stat().st_size > 0
    report = {
        "schema": manifest.get("render_report_schema", "ur10e_p3_rviz_debug_render_report_v1"),
        "generated_at": generated_at,
        "mode": "offline_xvfb_rviz_static_scene_render",
        "claim_tier": manifest.get("claim_tier", "visual_only"),
        "ok": bool(screenshot_exists and ffmpeg_returncode == 0 and publisher_returncode == 0),
        "screenshot_path": rel(screenshot_path),
        "screenshot_sha256": sha256_file(screenshot_path) if screenshot_exists else None,
        "screenshot_bytes": screenshot_path.stat().st_size if screenshot_exists else 0,
        "manifest_path": rel(manifest_path),
        "source_manifest_schema": manifest.get("schema"),
        "rviz_config_path": rel(rviz_config_path),
        "publisher_summary_path": rel(publisher_summary_path),
        "rviz_stdout_path": rel(rviz_stdout_path),
        "rviz_stderr_path": rel(rviz_stderr_path),
        "ffmpeg_stderr_path": rel(ffmpeg_stderr_path),
        "returncodes": {
            "rviz": rviz_returncode,
            "publisher": publisher_returncode,
            "ffmpeg": ffmpeg_returncode,
        },
        "safety_boundary": [
            "offline RViz render only",
            "no live bridge",
            "no controller upload",
            "no TP Play",
            "no URScript",
            "no robot motion",
            "no zero_ftsensor",
            "no payload/TCP/safety/Kunwei writes",
        ],
        "forbidden_claim": "physical Gazebo collision/contact physics; simulated_ft; real bench/live contact",
    }
    if manifest.get("schema") == "ur10e_step5b_rviz_debug_evidence_pack_v1":
        report["current_run_rviz_viewer_candidate_present"] = bool(report["ok"])
        report["formal_step5b_viewer_acceptance_allowed"] = False
        report["formal_viewer_acceptance_blockers"] = [
            "static_rviz_render_not_formal_step5b_same_run",
            "formal_step5b_same_run_not_attempted",
        ]
    return report


def update_manifest_with_render(
    manifest_path: Path,
    *,
    generated_at: str,
    screenshot_path: Path,
    render_report_path: Path,
) -> None:
    payload = load_json(manifest_path)
    screenshot_present = screenshot_path.is_file() and screenshot_path.stat().st_size > 0
    payload["rendered_screenshot_evidence"] = {
        "present": screenshot_present,
        "status": "rendered_screenshot_captured",
        "path": screenshot_path.name,
        "sha256": sha256_file(screenshot_path),
        "bytes": screenshot_path.stat().st_size,
        "generated_at": generated_at,
        "claim_tier": "visual_only",
        "render_report_path": render_report_path.name,
    }
    allow_full_acceptance = bool(payload.get("allow_full_rviz_render_acceptance_from_static_render", True))
    payload["full_rviz_render_acceptance_allowed"] = bool(
        allow_full_acceptance
        and payload.get("all_required_items_evidenced")
        and screenshot_present
    )
    if payload.get("schema") == "ur10e_step5b_rviz_debug_evidence_pack_v1":
        payload["current_run_rviz_viewer_candidate_present"] = bool(
            payload.get("all_required_items_evidenced") and screenshot_present
        )
        payload["formal_step5b_viewer_acceptance_allowed"] = False
        payload["formal_viewer_acceptance_blockers"] = [
            "static_rviz_render_not_formal_step5b_same_run",
            "formal_step5b_same_run_not_attempted",
        ]
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def publisher_child(args: argparse.Namespace) -> int:
    import rclpy
    from geometry_msgs.msg import Point, PoseStamped, TransformStamped
    from nav_msgs.msg import Path as RosPath
    from rclpy.duration import Duration
    from rclpy.qos import DurabilityPolicy, QoSProfile
    from std_msgs.msg import ColorRGBA, String
    from tf2_ros import StaticTransformBroadcaster
    from visualization_msgs.msg import Marker, MarkerArray

    manifest_path = args.manifest
    manifest = load_json(manifest_path)
    marker_topics = manifest["marker_topics"]
    rclpy.init()
    node = rclpy.create_node(manifest.get("publisher_node_name", "ur10e_p3_rviz_static_scene_publisher"))
    transient_qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
    robot_pub = node.create_publisher(String, "/robot_description", transient_qos)
    frame_pub = node.create_publisher(MarkerArray, marker_topics["frame_markers"], transient_qos)
    wrench_pub = node.create_publisher(MarkerArray, marker_topics["wrench_vector"], transient_qos)
    contact_pub = node.create_publisher(MarkerArray, marker_topics["contact_state_markers"], transient_qos)
    label_pub = node.create_publisher(MarkerArray, marker_topics["claim_tier_labels"], transient_qos)
    path_pub = node.create_publisher(RosPath, marker_topics["trajectory_path"], transient_qos)
    static_tf = StaticTransformBroadcaster(node)

    urdf_path = resolve_manifest_relative(manifest_path, manifest.get("source_paths", {}).get("calibrated_urdf"))
    robot_description = String()
    fallback_robot_name = manifest.get("fallback_robot_name", "ur10e_p3_debug")
    robot_description.data = (
        urdf_path.read_text(encoding="utf-8")
        if urdf_path and urdf_path.is_file()
        else f"<robot name='{fallback_robot_name}'/>"
    )
    static_scene = manifest.get("static_scene") if isinstance(manifest.get("static_scene"), dict) else {}
    namespace_prefix = str(manifest.get("marker_namespace_prefix") or "p3")

    def scene_xyz(name: str, default: tuple[float, float, float]) -> tuple[float, float, float]:
        value = static_scene.get(name)
        if not isinstance(value, list) or len(value) != 3:
            return default
        return (float(value[0]), float(value[1]), float(value[2]))

    tool0_xyz = scene_xyz("tool0_xyz", (0.48, 0.18, 0.20))
    flange_xyz = scene_xyz("flange_xyz", (0.48, 0.18, 0.17))
    ft_sensor_xyz = scene_xyz("ft_sensor_xyz", (0.48, 0.18, 0.13))
    tcp_xyz = scene_xyz("tcp_xyz", (0.48, 0.18, 0.06))
    contact_tip_xyz = scene_xyz("contact_tip_xyz", (0.48, 0.18, 0.025))
    contact_surface_xyz = scene_xyz("contact_surface_xyz", (0.48, 0.18, 0.0))
    surface_normal_xyz = scene_xyz("surface_normal_xyz", (0.48, 0.18, 0.05))
    wrench_end_xyz = scene_xyz("wrench_end_xyz", (0.48, 0.18, 0.14))
    label_xyz = scene_xyz("claim_label_xyz", (0.42, 0.08, 0.28))
    path_points = static_scene.get("path_points_xyz")
    if not isinstance(path_points, list):
        path_points = []
    path_points_xyz = [
        (float(row[0]), float(row[1]), float(row[2]))
        for row in path_points
        if isinstance(row, list) and len(row) == 3
    ]
    surface_size = static_scene.get("surface_size_xyz")
    if not isinstance(surface_size, list) or len(surface_size) != 3:
        surface_size = [0.24, 0.16, 0.01]

    def transform(parent: str, child: str, xyz: tuple[float, float, float]) -> TransformStamped:
        msg = TransformStamped()
        msg.header.stamp = node.get_clock().now().to_msg()
        msg.header.frame_id = parent
        msg.child_frame_id = child
        msg.transform.translation.x = xyz[0]
        msg.transform.translation.y = xyz[1]
        msg.transform.translation.z = xyz[2]
        msg.transform.rotation.w = 1.0
        return msg

    transforms = [
        transform("world", "base", (0.0, 0.0, 0.0)),
        transform("base", "base_link", (0.0, 0.0, 0.0)),
        transform("base", "tool0", tool0_xyz),
        transform("base", "flange", flange_xyz),
        transform("base", "ft_sensor", ft_sensor_xyz),
        transform("base", "tcp", tcp_xyz),
        transform("base", "contact_tip", contact_tip_xyz),
        transform("base", "contact_surface", contact_surface_xyz),
        transform("base", "surface_normal", surface_normal_xyz),
    ]

    def color(r: float, g: float, b: float, a: float = 1.0) -> ColorRGBA:
        c = ColorRGBA()
        c.r, c.g, c.b, c.a = r, g, b, a
        return c

    def point(x: float, y: float, z: float) -> Point:
        p = Point()
        p.x, p.y, p.z = x, y, z
        return p

    def marker(ns: str, marker_id: int, marker_type: int, frame_id: str = "base") -> Marker:
        msg = Marker()
        msg.header.frame_id = frame_id
        msg.header.stamp = node.get_clock().now().to_msg()
        msg.ns = ns
        msg.id = marker_id
        msg.type = marker_type
        msg.action = Marker.ADD
        msg.lifetime = Duration(seconds=0).to_msg()
        msg.pose.orientation.w = 1.0
        return msg

    def frame_markers() -> MarkerArray:
        arr = MarkerArray()
        frames = [
            ("tool0", tool0_xyz, color(0.1, 0.4, 1.0)),
            ("flange", flange_xyz, color(0.2, 0.8, 0.9)),
            ("ft_sensor", ft_sensor_xyz, color(0.8, 0.6, 0.1)),
            ("tcp", tcp_xyz, color(0.2, 0.9, 0.2)),
            ("contact_tip", contact_tip_xyz, color(1.0, 0.2, 0.2)),
            ("contact_surface", contact_surface_xyz, color(0.6, 0.6, 0.6)),
        ]
        for idx, (name, xyz, rgba) in enumerate(frames):
            sphere = marker(f"{namespace_prefix}_frames", idx, Marker.SPHERE)
            sphere.pose.position = point(*xyz)
            sphere.scale.x = sphere.scale.y = sphere.scale.z = 0.025
            sphere.color = rgba
            arr.markers.append(sphere)
            label = marker(f"{namespace_prefix}_frame_labels", 100 + idx, Marker.TEXT_VIEW_FACING)
            label.pose.position = point(xyz[0], xyz[1] + 0.035, xyz[2] + 0.02)
            label.scale.z = 0.035
            label.color = color(1.0, 1.0, 1.0)
            label.text = f"{name} | visual_only"
            arr.markers.append(label)
        return arr

    def wrench_markers() -> MarkerArray:
        arr = MarkerArray()
        arrow = marker(f"{namespace_prefix}_wrench", 1, Marker.ARROW)
        arrow.points = [point(*contact_tip_xyz), point(*wrench_end_xyz)]
        arrow.scale.x = 0.012
        arrow.scale.y = 0.024
        arrow.scale.z = 0.024
        arrow.color = color(0.1, 0.8, 1.0)
        arr.markers.append(arrow)
        text = marker(f"{namespace_prefix}_wrench_label", 2, Marker.TEXT_VIEW_FACING)
        text.pose.position = point(wrench_end_xyz[0] + 0.04, wrench_end_xyz[1], wrench_end_xyz[2])
        text.scale.z = 0.035
        text.color = color(0.1, 0.8, 1.0)
        text.text = "canonical wrench markers | visual_only"
        arr.markers.append(text)
        return arr

    def contact_markers() -> MarkerArray:
        arr = MarkerArray()
        surface = marker(f"{namespace_prefix}_contact_surface", 1, Marker.CUBE)
        surface.pose.position = point(*contact_surface_xyz)
        surface.scale.x = float(surface_size[0])
        surface.scale.y = float(surface_size[1])
        surface.scale.z = float(surface_size[2])
        surface.color = color(0.5, 0.5, 0.5, 0.75)
        arr.markers.append(surface)
        return arr

    def label_markers() -> MarkerArray:
        arr = MarkerArray()
        text = marker(f"{namespace_prefix}_claim_boundary", 1, Marker.TEXT_VIEW_FACING)
        text.pose.position = point(*label_xyz)
        text.scale.z = 0.04
        text.color = color(1.0, 1.0, 0.2)
        text.text = str(manifest.get("claim_label_text") or "RViz debug evidence: visual_only | no live robot")
        arr.markers.append(text)
        return arr

    def path_msg() -> RosPath:
        msg = RosPath()
        msg.header.frame_id = "base"
        msg.header.stamp = node.get_clock().now().to_msg()
        points_for_path = path_points_xyz or [
            (0.42 + 0.12 * (idx / 19.0), 0.18 + 0.03 * ((idx / 19.0) - 0.5), 0.055)
            for idx in range(20)
        ]
        for xyz in points_for_path:
            pose = PoseStamped()
            pose.header = msg.header
            pose.pose.position = point(*xyz)
            pose.pose.orientation.w = 1.0
            msg.poses.append(pose)
        return msg

    start = time.monotonic()
    publish_count = 0
    while time.monotonic() - start < args.duration_s:
        stamp = node.get_clock().now().to_msg()
        for tf in transforms:
            tf.header.stamp = stamp
        static_tf.sendTransform(transforms)
        robot_pub.publish(robot_description)
        frame_pub.publish(frame_markers())
        wrench_pub.publish(wrench_markers())
        contact_pub.publish(contact_markers())
        label_pub.publish(label_markers())
        path_pub.publish(path_msg())
        publish_count += 1
        rclpy.spin_once(node, timeout_sec=0.05)
        time.sleep(0.25)

    summary = {
        "schema": manifest.get("publisher_summary_schema", "ur10e_p3_rviz_static_scene_publisher_summary_v1"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "claim_tier": "visual_only",
        "publish_count": publish_count,
        "topics": manifest["topics"],
        "frames": manifest["frames"],
        "safety_boundary": "offline static RViz scene only; no live bridge/controller/robot motion",
    }
    args.publisher_summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    node.destroy_node()
    rclpy.shutdown()
    return 0


def render_main(args: argparse.Namespace) -> int:
    generated_at = args.generated_at or datetime.now().isoformat(timespec="seconds")
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("DISPLAY is not set; run under xvfb-run or another isolated X display")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.manifest.resolve()
    config_path = args.rviz_config.resolve()
    screenshot_path = args.output_dir / "rviz_debug_render.png"
    publisher_summary_path = args.output_dir / "rviz_static_scene_publisher_summary.json"
    render_report_path = args.output_dir / "rviz_render_report.json"
    rviz_stdout_path = args.output_dir / "rviz_stdout.txt"
    rviz_stderr_path = args.output_dir / "rviz_stderr.txt"
    publisher_stdout_path = args.output_dir / "publisher_stdout.txt"
    publisher_stderr_path = args.output_dir / "publisher_stderr.txt"
    ffmpeg_stderr_path = args.output_dir / "ffmpeg_stderr.txt"

    publisher_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--publisher-child",
        "--manifest",
        str(manifest_path),
        "--duration-s",
        str(args.duration_s),
        "--publisher-summary",
        str(publisher_summary_path),
    ]
    rviz_cmd = ["rviz2", "-d", str(config_path)]

    with publisher_stdout_path.open("w", encoding="utf-8") as pub_out, publisher_stderr_path.open("w", encoding="utf-8") as pub_err:
        publisher_proc = subprocess.Popen(publisher_cmd, stdout=pub_out, stderr=pub_err)
    time.sleep(args.publisher_warmup_s)
    with rviz_stdout_path.open("w", encoding="utf-8") as rviz_out, rviz_stderr_path.open("w", encoding="utf-8") as rviz_err:
        rviz_proc = subprocess.Popen(rviz_cmd, stdout=rviz_out, stderr=rviz_err)
    time.sleep(args.rviz_warmup_s)

    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "x11grab",
        "-video_size",
        args.video_size,
        "-i",
        os.environ["DISPLAY"],
        "-frames:v",
        "1",
        str(screenshot_path),
    ]
    with ffmpeg_stderr_path.open("w", encoding="utf-8") as ffmpeg_err:
        ffmpeg_proc = subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=ffmpeg_err, check=False)

    rviz_returncode = rviz_proc.poll()
    if rviz_returncode is None:
        rviz_proc.terminate()
        try:
            rviz_returncode = rviz_proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            rviz_proc.kill()
            rviz_returncode = rviz_proc.wait(timeout=5.0)

    publisher_returncode = publisher_proc.wait(timeout=max(1.0, args.duration_s + 5.0))
    if (
        screenshot_path.is_file()
        and screenshot_path.stat().st_size > 0
        and ffmpeg_proc.returncode == 0
        and publisher_returncode == 0
    ):
        update_manifest_with_render(
            manifest_path,
            generated_at=generated_at,
            screenshot_path=screenshot_path,
            render_report_path=render_report_path,
        )
    report = build_render_report(
        generated_at=generated_at,
        manifest_path=manifest_path,
        rviz_config_path=config_path,
        screenshot_path=screenshot_path,
        publisher_summary_path=publisher_summary_path,
        rviz_stdout_path=rviz_stdout_path,
        rviz_stderr_path=rviz_stderr_path,
        ffmpeg_stderr_path=ffmpeg_stderr_path,
        rviz_returncode=rviz_returncode,
        publisher_returncode=publisher_returncode,
        ffmpeg_returncode=ffmpeg_proc.returncode,
    )
    report["publisher_stdout_path"] = rel(publisher_stdout_path)
    report["publisher_stderr_path"] = rel(publisher_stderr_path)
    render_report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(render_report_path)
    return 0 if report["ok"] else 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--rviz-config", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--video-size", default="1280x900")
    parser.add_argument("--duration-s", type=float, default=12.0)
    parser.add_argument("--publisher-warmup-s", type=float, default=1.5)
    parser.add_argument("--rviz-warmup-s", type=float, default=7.0)
    parser.add_argument("--publisher-child", action="store_true")
    parser.add_argument("--publisher-summary", type=Path, default=Path("rviz_static_scene_publisher_summary.json"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.publisher_child:
        return publisher_child(args)
    if args.rviz_config is None or args.output_dir is None:
        raise SystemExit("--rviz-config and --output-dir are required unless --publisher-child is used")
    return render_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
