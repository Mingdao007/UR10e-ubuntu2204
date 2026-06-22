#!/usr/bin/env python3
"""Build a current-run Step5b RViz debug evidence pack.

This pack is offline-only. It writes an RViz config and manifest for a static
current-run visual candidate, but it does not start Gazebo, a bridge,
controllers, TP programs, URScript, or any live robot path.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
PACKAGE_ROOT = WORKSPACE / "src" / "ur10e_example_controllers"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from ur10e_example_controllers import step5b_simulation_mvp as step5b_mvp  # noqa: E402

SCHEMA = "ur10e_step5b_rviz_debug_evidence_pack_v1"
RVIZ_CONFIG_NAME = "ur10e_step5b_debug.rviz"
MANIFEST_NAME = "step5b_rviz_debug_manifest.json"

REQUIRED_ITEMS = [
    "TF tree",
    "robot model",
    "EOAT/tool frames",
    "TCP/contact_tip/contact_surface frames",
    "contact target and surface markers",
    "reaction and approach vector markers",
    "Step5b probe path markers",
    "frame and claim-tier labels",
]

RVIZ_MARKER_TOPICS = {
    "frame_markers": "/ur10e/step5b/rviz/contact_frame_markers",
    "wrench_vector": "/ur10e/step5b/rviz/wrench_vector",
    "contact_state_markers": "/ur10e/step5b/rviz/contact_state_markers",
    "trajectory_path": "/ur10e/step5b/rviz/trajectory_path",
    "claim_tier_labels": "/ur10e/step5b/rviz/claim_tier_labels",
}


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(WORKSPACE.resolve()))
    except ValueError:
        return str(path)


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _vec3(value: Any, default: tuple[float, float, float]) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        return default
    try:
        return (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError):
        return default


def _pose_vec3(text: str | None) -> tuple[float, float, float]:
    if not text:
        return (0.0, 0.0, 0.0)
    try:
        parts = [float(part) for part in text.split()]
    except ValueError:
        return (0.0, 0.0, 0.0)
    if len(parts) < 3:
        return (0.0, 0.0, 0.0)
    return (parts[0], parts[1], parts[2])


def _probe_manifest_path(run_dir: Path) -> Path:
    candidates = sorted((run_dir / "probe_worlds").glob("step5b_forced_contact_probe_world.manifest.json"))
    if not candidates:
        fallback = run_dir / "step5b_gz_transport_probe_manifest.json"
        if fallback.is_file():
            return fallback
        raise FileNotFoundError(f"missing Step5b probe manifest under {run_dir}")
    return candidates[-1]


def _path_from_payload(payload: dict[str, Any], key: str, *, base: Path) -> Path | None:
    value = payload.get(key)
    if not value:
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    candidate = base / path
    if candidate.exists():
        return candidate
    return WORKSPACE / path


def _extract_probe_pose(world_path: Path | None) -> tuple[float, float, float] | None:
    if world_path is None or not world_path.is_file():
        return None
    root = ET.parse(world_path).getroot()
    model = root.find(".//model[@name='step5b_nonformal_forced_eoat_probe']")
    if model is None:
        return None
    return _pose_vec3(model.findtext("./pose"))


def _extract_reference_path(world_path: Path | None) -> list[list[float]]:
    if world_path is None or not world_path.is_file():
        return []
    root = ET.parse(world_path).getroot()
    model = root.find(".//model[@name='step5b_reference_path_visual']")
    if model is None:
        return []
    model_xyz = _pose_vec3(model.findtext("./pose"))
    points: list[list[float]] = []
    for link in model.findall("./link"):
        xyz = _pose_vec3(link.findtext("./pose"))
        points.append([model_xyz[0] + xyz[0], model_xyz[1] + xyz[1], model_xyz[2] + xyz[2]])
    return points


def _contact_target(payload: dict[str, Any]) -> tuple[float, float, float]:
    target = payload.get("contact_target_pose_world") if isinstance(payload.get("contact_target_pose_world"), dict) else {}
    return (
        float(target.get("x_m") or -0.486),
        float(target.get("y_m") or -0.224),
        float(target.get("z_m") or target.get("surface_top_z_m") or 0.008),
    )


def _surface(payload: dict[str, Any]) -> dict[str, float]:
    surface = payload.get("surface") if isinstance(payload.get("surface"), dict) else {}
    min_x = float(surface.get("min_x_m") or -0.523)
    max_x = float(surface.get("max_x_m") or -0.437)
    min_y = float(surface.get("min_y_m") or -0.244)
    max_y = float(surface.get("max_y_m") or -0.069)
    top_z = float(surface.get("top_z_m") or 0.008)
    return {
        "center_x_m": (min_x + max_x) / 2.0,
        "center_y_m": (min_y + max_y) / 2.0,
        "size_x_m": max_x - min_x,
        "size_y_m": max_y - min_y,
        "size_z_m": float(surface.get("size_z_m") or 0.008),
        "top_z_m": top_z,
    }


def _trace_summary(run_dir: Path) -> tuple[Path | None, dict[str, Any]]:
    trace_path = run_dir / "step5b_total_wrench" / "step5b_total_contact_wrench_trace.json"
    if not trace_path.is_file():
        return None, {}
    payload = load_json(trace_path)
    rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
    first = rows[0] if rows else {}
    return trace_path, {
        "row_count": len(rows),
        "max_normal_load_n": max((float(row.get("normal_load_n") or 0.0) for row in rows), default=0.0),
        "reaction_normal": _vec3(first.get("reaction_normal"), (0.0, 0.0, 1.0)),
        "approach_normal": _vec3(first.get("approach_normal"), (0.0, 0.0, -1.0)),
    }


def _static_scene(run_dir: Path, probe_payload: dict[str, Any], probe_manifest_path: Path) -> dict[str, Any]:
    world_path = _path_from_payload(probe_payload, "probe_world", base=probe_manifest_path.parent)
    target = _contact_target(probe_payload)
    surface = _surface(probe_payload)
    probe_pose = _extract_probe_pose(world_path) or (target[0], target[1], target[2] + 0.08)
    trace_path, trace = _trace_summary(run_dir)
    reaction = trace.get("reaction_normal", (0.0, 0.0, 1.0))
    approach = trace.get("approach_normal", (0.0, 0.0, -1.0))
    force_scale = min(max(float(trace.get("max_normal_load_n") or 0.0) / 2500.0, 0.04), 0.14)
    path_points = _extract_reference_path(world_path)
    if not path_points:
        path_points = [
            [target[0] - 0.05, target[1] - 0.02, target[2] + 0.055],
            [target[0], target[1], target[2] + 0.04],
            [target[0] + 0.05, target[1] + 0.02, target[2] + 0.055],
        ]
    contact_tip = [target[0], target[1], max(target[2] + 0.012, probe_pose[2] - 0.055)]
    tcp = [target[0], target[1], contact_tip[2] + 0.035]
    return {
        "source_trace_path": rel(trace_path) if trace_path else None,
        "tool0_xyz": [target[0], target[1], tcp[2] + 0.14],
        "flange_xyz": [target[0], target[1], tcp[2] + 0.10],
        "ft_sensor_xyz": [target[0], target[1], tcp[2] + 0.065],
        "tcp_xyz": tcp,
        "contact_tip_xyz": contact_tip,
        "contact_surface_xyz": [surface["center_x_m"], surface["center_y_m"], target[2] - surface["size_z_m"] / 2.0],
        "surface_normal_xyz": [target[0], target[1], target[2] + 0.08],
        "surface_size_xyz": [surface["size_x_m"], surface["size_y_m"], surface["size_z_m"]],
        "wrench_end_xyz": [
            contact_tip[0] + reaction[0] * force_scale,
            contact_tip[1] + reaction[1] * force_scale,
            contact_tip[2] + reaction[2] * force_scale,
        ],
        "approach_end_xyz": [
            contact_tip[0] + approach[0] * 0.06,
            contact_tip[1] + approach[1] * 0.06,
            contact_tip[2] + approach[2] * 0.06,
        ],
        "claim_label_xyz": [target[0] + 0.05, target[1] + 0.08, tcp[2] + 0.16],
        "path_points_xyz": path_points,
        "trace_summary": trace,
    }


def rviz_config_text(static_scene: dict[str, Any] | None = None) -> str:
    marker_array_class = "rviz_default_plugins/MarkerArray"
    scene = static_scene or {}
    focal = _vec3(scene.get("contact_tip_xyz"), (-0.486, -0.224, 0.033))
    return f"""# Step5b current-run RViz debug evidence
# Evidence topic: /tf
# Evidence topic: /tf_static
# Evidence topic: /robot_description
# Evidence topic: {RVIZ_MARKER_TOPICS["frame_markers"]}
# Evidence topic: {RVIZ_MARKER_TOPICS["wrench_vector"]}
# Evidence topic: {RVIZ_MARKER_TOPICS["contact_state_markers"]}
# Evidence topic: {RVIZ_MARKER_TOPICS["trajectory_path"]}
# Evidence topic: {RVIZ_MARKER_TOPICS["claim_tier_labels"]}
Panels:
  - Class: rviz_common/Displays
    Name: Displays
  - Class: rviz_common/Selection
    Name: Selection
Visualization Manager:
  Class: ""
  Displays:
    - Class: rviz_default_plugins/TF
      Enabled: true
      Name: TF tree
      Frames:
        world:
          Value: true
        base:
          Value: true
        base_link:
          Value: true
        tool0:
          Value: true
        flange:
          Value: true
        ft_sensor:
          Value: true
        tcp:
          Value: true
        contact_tip:
          Value: true
        contact_surface:
          Value: true
        surface_normal:
          Value: true
    - Class: rviz_default_plugins/RobotModel
      Description Topic:
        Value: /robot_description
      Enabled: true
      Name: Robot model
      TF Prefix: ""
    - Class: {marker_array_class}
      Enabled: true
      Name: Step5b EOAT/tool frame markers
      Topic:
        Value: {RVIZ_MARKER_TOPICS["frame_markers"]}
    - Class: {marker_array_class}
      Enabled: true
      Name: Step5b reaction and contact vector markers
      Topic:
        Value: {RVIZ_MARKER_TOPICS["wrench_vector"]}
    - Class: {marker_array_class}
      Enabled: true
      Name: Step5b contact target and surface markers
      Topic:
        Value: {RVIZ_MARKER_TOPICS["contact_state_markers"]}
    - Class: rviz_default_plugins/Path
      Enabled: true
      Name: Step5b probe path
      Topic:
        Value: {RVIZ_MARKER_TOPICS["trajectory_path"]}
    - Class: {marker_array_class}
      Enabled: true
      Name: Claim-tier and frame labels
      Topic:
        Value: {RVIZ_MARKER_TOPICS["claim_tier_labels"]}
  Enabled: true
  Global Options:
    Fixed Frame: base
  Name: root
  Tools:
    - Class: rviz_default_plugins/Interact
    - Class: rviz_default_plugins/MoveCamera
  Value: true
  Views:
    Current:
      Class: rviz_default_plugins/Orbit
      Distance: 0.42
      Enable Stereo Rendering:
        Stereo Eye Separation: 0.06
        Stereo Focal Distance: 1
        Swap Stereo Eyes: false
        Value: false
      Focal Point:
        X: {focal[0]:.9f}
        Y: {focal[1]:.9f}
        Z: {focal[2]:.9f}
      Focal Shape Fixed Size: true
      Focal Shape Size: 0.05
      Invert Z Axis: false
      Name: Current View
      Near Clip Distance: 0.01
      Pitch: 0.55
      Target Frame: base
      Yaw: 5.55
    Saved: ~
"""


def missing_required_items(payload: dict[str, Any]) -> list[str]:
    evidenced = payload.get("evidenced_items", {})
    missing: list[str] = []
    for item in REQUIRED_ITEMS:
        row = evidenced.get(item, {})
        if not isinstance(row, dict) or not bool(row.get("evidenced")):
            missing.append(item)
    return missing


def build_manifest(
    run_dir: Path,
    *,
    generated_at: str | None = None,
    rviz_config_sha256: str | None = None,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    probe_manifest_path = _probe_manifest_path(run_dir)
    probe_payload = load_json(probe_manifest_path)
    trace_path, trace = _trace_summary(run_dir)
    visual_review_path = run_dir / "step5b_visual_observer" / "observer_review.json"
    static_scene = _static_scene(run_dir, probe_payload, probe_manifest_path)
    source_paths = {
        "run_dir": str(run_dir),
        "probe_manifest": str(probe_manifest_path),
        "probe_world": str(_path_from_payload(probe_payload, "probe_world", base=probe_manifest_path.parent)),
        "total_wrench_trace": str(trace_path) if trace_path else None,
        "visual_observer_review": str(visual_review_path) if visual_review_path.is_file() else None,
        "calibrated_urdf": str(step5b_mvp.CALIBRATED_URDF) if step5b_mvp.CALIBRATED_URDF.is_file() else None,
    }
    source_hashes = {
        "probe_manifest_sha256": sha256_file(probe_manifest_path),
        "total_wrench_trace_sha256": sha256_file(trace_path),
        "visual_observer_review_sha256": sha256_file(visual_review_path),
        "calibrated_urdf_sha256": sha256_file(step5b_mvp.CALIBRATED_URDF),
    }
    evidenced_items = {
        "TF tree": {
            "evidenced": True,
            "evidence_type": "rviz_config_tf_display_plus_static_publisher",
            "topics": ["/tf", "/tf_static"],
            "frames": ["world", "base", "base_link", "tool0", "flange", "ft_sensor", "tcp", "contact_tip", "contact_surface", "surface_normal"],
        },
        "robot model": {
            "evidenced": True,
            "evidence_type": "rviz_robot_model_display_plus_offline_robot_description",
            "topics": ["/robot_description"],
            "source_paths": [source_paths["calibrated_urdf"]],
        },
        "EOAT/tool frames": {
            "evidenced": True,
            "evidence_type": "static_tf_plus_marker_config",
            "topics": [RVIZ_MARKER_TOPICS["frame_markers"]],
            "frames": ["tool0", "flange", "ft_sensor"],
        },
        "TCP/contact_tip/contact_surface frames": {
            "evidenced": True,
            "evidence_type": "static_tf_plus_marker_config",
            "topics": [RVIZ_MARKER_TOPICS["frame_markers"]],
            "frames": ["tcp", "contact_tip", "contact_surface", "surface_normal"],
        },
        "contact target and surface markers": {
            "evidenced": True,
            "evidence_type": "marker_config_from_current_run_probe_manifest",
            "topics": [RVIZ_MARKER_TOPICS["contact_state_markers"]],
            "source_paths": [source_paths["probe_manifest"]],
        },
        "reaction and approach vector markers": {
            "evidenced": bool(trace),
            "evidence_type": "marker_config_from_current_run_total_wrench_trace",
            "topics": [RVIZ_MARKER_TOPICS["wrench_vector"]],
            "source_paths": [source_paths["total_wrench_trace"]],
            "trace_summary": trace,
        },
        "Step5b probe path markers": {
            "evidenced": True,
            "evidence_type": "rviz_path_display_plus_probe_world_reference_path",
            "topics": [RVIZ_MARKER_TOPICS["trajectory_path"]],
            "source_paths": [source_paths["probe_world"]],
        },
        "frame and claim-tier labels": {
            "evidenced": True,
            "evidence_type": "rviz_text_marker_config_plus_manifest_claim_boundary",
            "topics": [RVIZ_MARKER_TOPICS["claim_tier_labels"]],
            "claim_tier": "visual_only",
        },
    }
    return {
        "schema": SCHEMA,
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "offline_no_live_step5b_rviz_debug_evidence_pack",
        "claim_tier": "visual_only",
        "evidence_mode": "rviz_config_manifest_only_not_rendered",
        "run_dir": str(run_dir),
        "rviz_config_path": RVIZ_CONFIG_NAME,
        "rviz_config_sha256": rviz_config_sha256,
        "required_items": REQUIRED_ITEMS,
        "evidenced_items": evidenced_items,
        "all_required_items_evidenced": not missing_required_items({"evidenced_items": evidenced_items}),
        "rendered_screenshot_evidence": {
            "present": False,
            "status": "not_rendered_no_screenshot",
            "claim_tier": "visual_only",
        },
        "current_run_rviz_viewer_candidate_present": False,
        "formal_step5b_viewer_acceptance_allowed": False,
        "full_rviz_render_acceptance_allowed": False,
        "allow_full_rviz_render_acceptance_from_static_render": False,
        "formal_viewer_acceptance_blockers": [
            "static_rviz_render_not_formal_step5b_same_run",
            "formal_step5b_same_run_not_attempted",
        ],
        "render_report_schema": "ur10e_step5b_rviz_debug_render_report_v1",
        "publisher_summary_schema": "ur10e_step5b_rviz_static_scene_publisher_summary_v1",
        "publisher_node_name": "ur10e_step5b_rviz_static_scene_publisher",
        "fallback_robot_name": "ur10e_step5b_debug",
        "marker_namespace_prefix": "step5b",
        "claim_label_text": "Step5b RViz debug evidence: visual_only | no live robot",
        "marker_topics": RVIZ_MARKER_TOPICS,
        "topics": ["/tf", "/tf_static", "/robot_description", *RVIZ_MARKER_TOPICS.values()],
        "frames": ["world", "base", "base_link", "tool0", "flange", "ft_sensor", "tcp", "contact_tip", "contact_surface", "surface_normal"],
        "source_paths": source_paths,
        "source_hashes": source_hashes,
        "static_scene": static_scene,
        "trace_summary": trace,
        "safety_boundary": [
            "offline RViz static scene only",
            "no Gazebo launch",
            "no bridge start",
            "no controller upload",
            "no TP Play",
            "no URScript",
            "no robot motion",
            "no zero_ftsensor",
            "no payload/TCP/safety writes",
        ],
        "forbidden_claim": "formal Step5b observer acceptance; physical Gazebo collision/contact physics; simulated_ft; real bench/live contact",
    }


def write_pack(output_dir: Path, run_dir: Path, *, generated_at: str | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    rviz_path = output_dir / RVIZ_CONFIG_NAME
    manifest = build_manifest(run_dir, generated_at=generated_at, rviz_config_sha256=None)
    config = rviz_config_text(manifest.get("static_scene") if isinstance(manifest.get("static_scene"), dict) else None)
    rviz_path.write_text(config, encoding="utf-8")
    manifest["rviz_config_sha256"] = hashlib.sha256(config.encode("utf-8")).hexdigest()
    manifest["artifact_path"] = str(output_dir / MANIFEST_NAME)
    manifest_path = output_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_pack(args.output_dir, args.run_dir, generated_at=args.generated_at)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
