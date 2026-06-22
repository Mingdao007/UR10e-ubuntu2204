#!/usr/bin/env python3
"""Build current-run Step5b probe visual observer artifacts.

This is an offline visual candidate builder. It reads the current non-formal
Step5b probe world and M3 wrench trace, then renders viewer-level diagnostic
images without starting a Gazebo action runner or spending a formal Step5b
attempt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


STAGE_ID = "step5b"
TOPIC = "/ur10e/contact/gazebo/step5b/contacts"
OBSERVATION_SCOPE = "non_formal_step5b_transport_probe"
VISUAL_SCHEMA = "ur10e_step5b_visual_observer_artifacts_v1"
PROBE_MODEL_NAME = "step5b_nonformal_forced_eoat_probe"
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_workspace_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return WORKSPACE / path


def _run_id(run_dir: Path) -> str:
    return run_dir.name


def _manifest_path(run_dir: Path) -> Path:
    candidates = sorted((run_dir / "probe_worlds").glob("step5b_forced_contact_probe_world.manifest.json"))
    if not candidates:
        raise FileNotFoundError("step5b_forced_contact_probe_world.manifest.json not found")
    return candidates[-1]


def _world_path(manifest: dict[str, Any]) -> Path:
    value = manifest.get("probe_world") or manifest.get("output_world")
    if not value:
        raise ValueError("probe world path missing from manifest")
    return Path(str(value))


def _vec3_from_pose(text: str | None) -> tuple[float, float, float]:
    if not text:
        return (0.0, 0.0, 0.0)
    parts = [float(part) for part in text.split()]
    if len(parts) < 3:
        return (0.0, 0.0, 0.0)
    return (parts[0], parts[1], parts[2])


def _extract_reference_path(world_path: Path) -> list[tuple[float, float, float]]:
    root = ET.parse(world_path).getroot()
    model = root.find(".//model[@name='step5b_reference_path_visual']")
    if model is None:
        return []
    model_xyz = _vec3_from_pose(model.findtext("./pose"))
    points: list[tuple[float, float, float]] = []
    for link in model.findall("./link"):
        xyz = _vec3_from_pose(link.findtext("./pose"))
        points.append((model_xyz[0] + xyz[0], model_xyz[1] + xyz[1], model_xyz[2] + xyz[2]))
    return points


def _extract_probe_pose(world_path: Path) -> tuple[float, float, float]:
    root = ET.parse(world_path).getroot()
    model = root.find(f".//model[@name='{PROBE_MODEL_NAME}']")
    if model is None:
        return (0.0, 0.0, 0.0)
    return _vec3_from_pose(model.findtext("./pose"))


def _surface(manifest: dict[str, Any]) -> dict[str, float]:
    surface = manifest.get("surface") if isinstance(manifest.get("surface"), dict) else {}
    return {
        "min_x_m": float(surface.get("min_x_m") or -0.55),
        "max_x_m": float(surface.get("max_x_m") or -0.43),
        "min_y_m": float(surface.get("min_y_m") or -0.25),
        "max_y_m": float(surface.get("max_y_m") or -0.06),
        "top_z_m": float(surface.get("top_z_m") or 0.008),
    }


def _contact_target(manifest: dict[str, Any]) -> tuple[float, float, float]:
    target = manifest.get("contact_target_pose_world") if isinstance(manifest.get("contact_target_pose_world"), dict) else {}
    return (
        float(target.get("x_m") or 0.0),
        float(target.get("y_m") or 0.0),
        float(target.get("z_m") or target.get("surface_top_z_m") or 0.0),
    )


def _trace_rows(run_dir: Path) -> list[dict[str, Any]]:
    trace = run_dir / "step5b_total_wrench" / "step5b_total_contact_wrench_trace.json"
    if not trace.is_file():
        return []
    payload = _load_json(trace)
    return [row for row in payload.get("rows") or [] if isinstance(row, dict)]


def _setup_ax(ax: Any, title: str) -> None:
    ax.set_title(title, fontsize=13)
    ax.grid(True, alpha=0.18)
    ax.set_aspect("equal", adjustable="box")


def _draw_top(ax: Any, surface: dict[str, float], path: list[tuple[float, float, float]], target: tuple[float, float, float], probe: tuple[float, float, float]) -> None:
    rect = Rectangle(
        (surface["min_x_m"], surface["min_y_m"]),
        surface["max_x_m"] - surface["min_x_m"],
        surface["max_y_m"] - surface["min_y_m"],
        facecolor="#c8c2b7",
        edgecolor="#333333",
        linewidth=1.5,
        alpha=0.85,
    )
    ax.add_patch(rect)
    if path:
        ax.plot([p[0] for p in path], [p[1] for p in path], color="#1f77b4", linewidth=2.2, label="reference path")
    ax.scatter(
        [target[0]],
        [target[1]],
        s=180,
        facecolors="none",
        edgecolors="#d62728",
        linewidths=2.2,
        label="contact target",
        zorder=5,
    )
    ax.scatter([probe[0]], [probe[1]], s=90, c="#2ca02c", marker="s", label="probe/TCP pad", zorder=4)
    ax.set_xlabel("world x (m)")
    ax.set_ylabel("world y (m)")
    ax.legend(loc="best", fontsize=8)


def _draw_side(ax: Any, surface: dict[str, float], path: list[tuple[float, float, float]], target: tuple[float, float, float], probe: tuple[float, float, float], trace_rows: list[dict[str, Any]]) -> None:
    y_values = [p[1] for p in path] or [surface["min_y_m"], surface["max_y_m"]]
    ax.add_patch(
        Rectangle(
            (surface["min_y_m"], surface["top_z_m"] - 0.008),
            surface["max_y_m"] - surface["min_y_m"],
            0.008,
            facecolor="#c8c2b7",
            edgecolor="#333333",
            alpha=0.85,
        )
    )
    if path:
        ax.plot([p[1] for p in path], [p[2] for p in path], color="#1f77b4", linewidth=2.0)
    ax.scatter([target[1]], [target[2]], s=80, c="#d62728")
    ax.scatter([probe[1]], [probe[2]], s=90, c="#2ca02c", marker="s")
    load = max((float(row.get("normal_load_n") or 0.0) for row in trace_rows), default=0.0)
    arrow_height = min(max(load / 300.0, 0.02), 0.12)
    ax.arrow(target[1], target[2], 0.0, arrow_height, width=0.001, color="#17becf", length_includes_head=True)
    ax.set_xlim(min(y_values) - 0.04, max(y_values) + 0.04)
    ax.set_ylim(surface["top_z_m"] - 0.02, max(probe[2] + 0.05, surface["top_z_m"] + 0.24))
    ax.set_xlabel("world y (m)")
    ax.set_ylabel("world z (m)")


def _draw_close(ax: Any, surface: dict[str, float], target: tuple[float, float, float], probe: tuple[float, float, float]) -> None:
    span = 0.08
    ax.add_patch(
        Rectangle(
            (target[0] - span / 2, target[1] - span / 2),
            span,
            span,
            facecolor="#e2ded7",
            edgecolor="#333333",
            alpha=0.9,
        )
    )
    ax.scatter(
        [target[0]],
        [target[1]],
        s=260,
        facecolors="none",
        edgecolors="#d62728",
        linewidths=2.4,
        label="contact region",
        zorder=5,
    )
    ax.scatter([probe[0]], [probe[1]], s=180, c="#2ca02c", marker="s", label="probe/TCP pad")
    ax.set_xlim(target[0] - span, target[0] + span)
    ax.set_ylim(target[1] - span, target[1] + span)
    ax.set_xlabel("world x (m)")
    ax.set_ylabel("world y (m)")
    ax.legend(loc="best", fontsize=8)


def _draw_oblique(ax: Any, surface: dict[str, float], path: list[tuple[float, float, float]], target: tuple[float, float, float], probe: tuple[float, float, float], trace_rows: list[dict[str, Any]]) -> None:
    xs = [surface["min_x_m"], surface["max_x_m"]]
    ys = [surface["min_y_m"], surface["max_y_m"]]
    xx = np.array([[xs[0], xs[1]], [xs[0], xs[1]]])
    yy = np.array([[ys[0], ys[0]], [ys[1], ys[1]]])
    zz = np.array([[surface["top_z_m"], surface["top_z_m"]], [surface["top_z_m"], surface["top_z_m"]]])
    ax.plot_surface(xx, yy, zz, color="#c8c2b7", alpha=0.75, edgecolor="#444444")
    if path:
        ax.plot([p[0] for p in path], [p[1] for p in path], [p[2] for p in path], color="#1f77b4", linewidth=2.0)
    ax.scatter([target[0]], [target[1]], [target[2]], s=65, c="#d62728")
    ax.scatter([probe[0]], [probe[1]], [probe[2]], s=85, c="#2ca02c", marker="s")
    load = max((float(row.get("normal_load_n") or 0.0) for row in trace_rows), default=0.0)
    arrow_height = min(max(load / 300.0, 0.02), 0.12)
    ax.quiver(target[0], target[1], target[2], 0, 0, arrow_height, color="#17becf", linewidth=2.0)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.set_zlabel("z")
    ax.view_init(elev=28, azim=-48)


def _save_fig(fig: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def render_views(output_dir: Path, manifest: dict[str, Any], world_path: Path, trace_rows: list[dict[str, Any]]) -> dict[str, Path]:
    surface = _surface(manifest)
    target = _contact_target(manifest)
    probe = _extract_probe_pose(world_path)
    path_points = _extract_reference_path(world_path)
    outputs = {
        "close_detail": output_dir / "gazebo_close_detail.png",
        "side_view": output_dir / "gazebo_side_view.png",
        "oblique_view": output_dir / "gazebo_oblique_view.png",
        "top_or_path_view": output_dir / "gazebo_top_or_path_view.png",
    }

    fig, ax = plt.subplots(figsize=(8.0, 6.0))
    _setup_ax(ax, "Step5b Probe Close Detail")
    _draw_close(ax, surface, target, probe)
    _save_fig(fig, outputs["close_detail"])

    fig, ax = plt.subplots(figsize=(8.0, 6.0))
    _setup_ax(ax, "Step5b Probe Side View")
    _draw_side(ax, surface, path_points, target, probe, trace_rows)
    _save_fig(fig, outputs["side_view"])

    fig = plt.figure(figsize=(8.0, 6.0))
    ax = fig.add_subplot(111, projection="3d")
    ax.set_title("Step5b Probe Oblique View", fontsize=13)
    _draw_oblique(ax, surface, path_points, target, probe, trace_rows)
    _save_fig(fig, outputs["oblique_view"])

    fig, ax = plt.subplots(figsize=(8.0, 6.0))
    _setup_ax(ax, "Step5b Probe Top/Path View")
    _draw_top(ax, surface, path_points, target, probe)
    _save_fig(fig, outputs["top_or_path_view"])
    return outputs


def image_metrics(paths: dict[str, Path]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for key, path in paths.items():
        with Image.open(path).convert("RGB") as image:
            width, height = image.size
            pixels = list(image.getdata())
            total = len(pixels)
            nonwhite = sum(1 for pixel in pixels if any(channel < 245 for channel in pixel))
            edge_pixels = []
            edge_pixels.extend(image.crop((0, 0, width, 8)).getdata())
            edge_pixels.extend(image.crop((0, height - 8, width, height)).getdata())
            edge_pixels.extend(image.crop((0, 0, 8, height)).getdata())
            edge_pixels.extend(image.crop((width - 8, 0, width, height)).getdata())
            dark_edge = sum(1 for pixel in edge_pixels if all(channel < 30 for channel in pixel))
            rows[key] = {
                "path": str(path),
                "sha256": _sha256(path),
                "width_px": width,
                "height_px": height,
                "nonwhite_pixel_fraction": nonwhite / total if total else 0.0,
                "dark_edge_pixel_fraction": dark_edge / len(edge_pixels) if edge_pixels else 0.0,
                "likely_blank": (nonwhite / total if total else 0.0) < 0.01,
                "black_border_or_empty_grid_failure": (dark_edge / len(edge_pixels) if edge_pixels else 0.0) > 0.25,
            }
    return {
        "schema": "ur10e_step5b_visual_image_metrics_v1",
        "generated_at": _now(),
        "stage_id": STAGE_ID,
        "images": rows,
        "all_nonblank": all(not row["likely_blank"] for row in rows.values()),
        "black_border_or_empty_grid_failure": any(row["black_border_or_empty_grid_failure"] for row in rows.values()),
    }


def build_contact_sheet(paths: dict[str, Path], output: Path) -> Path:
    images = [(key, Image.open(path).convert("RGB")) for key, path in paths.items()]
    thumb_w, thumb_h = 720, 540
    sheet = Image.new("RGB", (thumb_w * 2, thumb_h * 2), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (key, image) in enumerate(images):
        image.thumbnail((thumb_w - 20, thumb_h - 50))
        x = (index % 2) * thumb_w + 10
        y = (index // 2) * thumb_h + 35
        draw.text((x, y - 25), key, fill=(20, 20, 20))
        sheet.paste(image, (x, y))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)
    for _key, image in images:
        image.close()
    return output


def _rviz_candidate_status(run_dir: Path, output_dir: Path) -> tuple[dict[str, Any], Path]:
    report_path = run_dir / "step5b_rviz_debug" / "rviz_render_report.json"
    manifest_path = run_dir / "step5b_rviz_debug" / "step5b_rviz_debug_manifest.json"
    report = _load_json(report_path) if report_path.is_file() else {}
    manifest = _load_json(manifest_path) if manifest_path.is_file() else {}
    screenshot_path = _resolve_workspace_path(str(report.get("screenshot_path"))) if report.get("screenshot_path") else None
    present = bool(
        report.get("ok") is True
        and report.get("current_run_rviz_viewer_candidate_present") is True
        and screenshot_path is not None
        and screenshot_path.is_file()
    )
    payload = {
        "schema": "ur10e_step5b_current_run_rviz_candidate_status_v1",
        "generated_at": _now(),
        "run_id": _run_id(run_dir),
        "stage_id": STAGE_ID,
        "claim_tier": "visual_only",
        "current_run_rviz_viewer_candidate_present": present,
        "formal_step5b_viewer_acceptance_allowed": False,
        "formal_viewer_evidence_present": False,
        "render_report_path": str(report_path) if report_path.is_file() else None,
        "render_report_sha256": _sha256(report_path),
        "debug_manifest_path": str(manifest_path) if manifest_path.is_file() else None,
        "debug_manifest_sha256": _sha256(manifest_path),
        "screenshot_path": str(screenshot_path) if screenshot_path and screenshot_path.is_file() else None,
        "screenshot_sha256": report.get("screenshot_sha256") if present else None,
        "render_ok": report.get("ok") is True,
        "manifest_all_required_items_evidenced": manifest.get("all_required_items_evidenced") is True,
        "status": (
            "current_run_static_rviz_candidate_present"
            if present
            else "blocked_current_run_rviz_not_rendered"
        ),
        "required_for_final_acceptance": True,
        "blockers": (
            [
                "static_rviz_render_not_formal_step5b_same_run",
                "formal_step5b_same_run_not_attempted",
            ]
            if present
            else ["current_run_rviz_viewer_level_candidate_missing"]
        ),
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
    }
    path = _write_json(output_dir / "rviz_candidate_status.json", payload)
    return payload, path


def build_visual_observer(run_dir: Path) -> Path:
    manifest_path = _manifest_path(run_dir)
    manifest = _load_json(manifest_path)
    world_path = _world_path(manifest)
    output_dir = run_dir / "step5b_visual_observer"
    trace_rows = _trace_rows(run_dir)
    image_paths = render_views(output_dir, manifest, world_path, trace_rows)
    sheet_path = build_contact_sheet(image_paths, output_dir / "viewer_contact_sheet.png")
    metrics = image_metrics(image_paths)
    metrics_path = _write_json(output_dir / "image_metrics.json", metrics)
    rviz_status, rviz_status_path = _rviz_candidate_status(run_dir, output_dir)
    rviz_candidate_present = bool(rviz_status["current_run_rviz_viewer_candidate_present"])
    rviz_unavailable_path = _write_json(
        output_dir / "rviz_unavailable.json",
        {
            "schema": "ur10e_step5b_current_run_rviz_unavailable_v1",
            "generated_at": _now(),
            "run_id": _run_id(run_dir),
            "stage_id": STAGE_ID,
            "claim_tier": "visual_only",
            "status": rviz_status["status"],
            "reason": (
                "Current-run static RViz debug candidate is present, but it is not formal Step5b viewer acceptance"
                if rviz_candidate_present
                else "M4 non-formal probe visual candidate only; no formal Step5b RViz render has been run"
            ),
            "required_for_final_acceptance": True,
            "blockers": rviz_status["blockers"],
            "rviz_candidate_status_path": str(rviz_status_path),
            "step5b_attempt_spent": False,
            "formal_step5b_attempt": False,
        },
    )
    core_visuals_ok = bool(metrics["all_nonblank"] and not metrics["black_border_or_empty_grid_failure"])
    review = {
        "schema": VISUAL_SCHEMA,
        "generated_at": _now(),
        "run_id": _run_id(run_dir),
        "stage_id": STAGE_ID,
        "source_topic": TOPIC,
        "observation_scope": OBSERVATION_SCOPE,
        "claim_tier": "visual_only",
        "visibility_assessment_scope": "schematic_nonformal_probe_overlay_only",
        "visibility_flag_source": "matplotlib_overlay_nonblank_and_black_border_metrics",
        "formal_viewer_evidence_present": False,
        "formal_required_items": {
            "ur10e_arm_visible": False,
            "eoat_chain_attached_to_arm_visible": False,
            "rviz_viewer_candidate_present": rviz_candidate_present,
            "robot_to_tool_to_surface_relationship_visible": False,
            "formal_same_run_step5b_artifact": False,
        },
        "viewer_level_pass": False,
        "tcp_visible": core_visuals_ok,
        "eoat_visible": core_visuals_ok,
        "contact_surface_visible": core_visuals_ok,
        "path_visible": core_visuals_ok,
        "contact_region_understandable": core_visuals_ok,
        "camera_distance_ok": core_visuals_ok,
        "black_border_or_empty_grid_failure": bool(metrics["black_border_or_empty_grid_failure"]),
        "visual_contact_appearance_pass": core_visuals_ok,
        "physical_contact_evidence_pass": False,
        "failure_reasons": [
            "non_formal_step5b_probe_visual_only",
            "schematic_overlay_not_formal_gazebo_rviz_viewer_evidence",
            "ur10e_arm_and_attached_eoat_chain_not_rendered",
            *(
                ["static_rviz_candidate_not_formal_observer_acceptance"]
                if rviz_candidate_present
                else ["current_run_rviz_viewer_level_candidate_missing"]
            ),
            "formal_step5b_same_run_not_attempted",
        ],
        "artifacts": {
            **{key: {"path": str(path), "sha256": _sha256(path)} for key, path in image_paths.items()},
            "viewer_contact_sheet": {"path": str(sheet_path), "sha256": _sha256(sheet_path)},
            "rviz_candidate_status": {"path": str(rviz_status_path), "sha256": _sha256(rviz_status_path)},
            "rviz_unavailable": {"path": str(rviz_unavailable_path), "sha256": _sha256(rviz_unavailable_path)},
            "image_metrics": {"path": str(metrics_path), "sha256": _sha256(metrics_path)},
        },
        "image_metrics_path": str(metrics_path),
        "viewer_contact_sheet_path": str(sheet_path),
        "rviz_unavailable_path": str(rviz_unavailable_path),
        "m4_visual_path_debuggable": core_visuals_ok,
        "m5_go_allowed": False,
        "m5_go_blockers": [
            "formal_step5b_same_run_not_attempted",
            *(
                ["static_rviz_candidate_not_formal_observer_acceptance"]
                if rviz_candidate_present
                else ["current_run_rviz_viewer_level_candidate_missing"]
            ),
        ],
        "allowed_claim": "current-run non-formal Step5b probe visual candidate only",
        "forbidden_claim": "formal Step5b observer acceptance; real bench/live contact; physical acceptance from visuals alone",
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
    }
    review_path = _write_json(output_dir / "observer_review.json", review)
    return review_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(build_visual_observer(args.run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
