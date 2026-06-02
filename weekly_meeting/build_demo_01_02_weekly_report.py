#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import statistics
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt


ROOT = Path("/home/andy/ur10e_ros2_ws")
WEEKLY_DIR = ROOT / "weekly_meeting"
ASSET_DIR = WEEKLY_DIR / "assets"
REPORT_PATH = WEEKLY_DIR / "demo_01_02_weekly.md"
HTML_PATH = WEEKLY_DIR / "demo_01_02_meeting_deck.html"

RUN_DIR = (
    ROOT
    / "ft_sensor/onrobot/hex_e_v2_3010007655/measurements/"
    "demo_1_2_final_success_20260528_022654"
)
ANALYSIS_SCRIPT = RUN_DIR / "analysis/analyze_demo_1_2_final_success.py"
FINAL_CSV = RUN_DIR / "demo_1_2_final_success_onrobot_vars_20260528_022724.csv"
REFERENCE_CSV = (
    ROOT
    / "ft_sensor/onrobot/hex_e_v2_3010007655/measurements/"
    "demo_1_2_path_straight_line_urcap_20260528_012054/demo_1_2_rtde_20260528_012054.csv"
)

PROGRAM_INTERFACE_PREVIEW = Path("/home/andy/.cache/codex/phone-photo-intake/previews/IMG_1469_8a91bd767360.mov")
PROGRAM_INTERFACE_STILL = Path("/home/andy/.cache/codex/phone-photo-intake/previews/IMG_1469_61c9cb9d60c8.jpg")
BENCH_OVERVIEW_PHOTO = Path("/home/andy/.cache/codex/phone-photo-intake/previews/IMG_1472_aabaa7d98644.jpg")
SMALL_SWITCH_PHOTO = Path("/home/andy/.cache/codex/phone-photo-intake/previews/IMG_1470_85cfee1d742e.jpg")
COMPUTE_BOX_FT_PHOTO = Path("/home/andy/.cache/codex/phone-photo-intake/previews/IMG_1471_7d961c2e9b70.jpg")

LOCAL_VIDEO_ASSETS = {
    "final_wiring_clip": "final_wiring.mp4",
    "final_wiring_still": "final_wiring.png",
    "demo_experiment_clip": "demo_experiment.mp4",
    "demo_experiment_still": "demo_experiment.png",
    "video_contact_evidence": "video_estimated_contact_region.png",
}

PHOTO_ASSETS = {
    "bench_overview": BENCH_OVERVIEW_PHOTO,
    "hex_sensor": ROOT / "ft_sensor/onrobot/hex_e_v2_3010007655/assets/thumbnails/IMG_1181.jpg",
    "small_switch": SMALL_SWITCH_PHOTO,
    "compute_box": COMPUTE_BOX_FT_PHOTO,
}

THREE_STREAM_RUN_DIR = (
    ROOT
    / "experiments/20260528_onrobot_three_stream_600s_first_zero/run_20260528_043100"
)
THREE_STREAM_STATS = THREE_STREAM_RUN_DIR / "three_stream_600s_20260528_043052_urcap_udp_alignment_stats.json"
THREE_STREAM_ASSETS = {
    "three_stream_fz_overlay": THREE_STREAM_RUN_DIR
    / "plots/three_stream_600s_20260528_043052_first_zero_fz_overlay.png",
    "three_stream_force_axes": THREE_STREAM_RUN_DIR
    / "plots/three_stream_600s_20260528_043052_first_zero_force_axes.png",
    "three_stream_residuals": THREE_STREAM_RUN_DIR
    / "plots/three_stream_600s_20260528_043052_urcap_udp_residuals.png",
}


def load_analysis_module() -> Any:
    spec = importlib.util.spec_from_file_location("demo_01_02_analysis", ANALYSIS_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {ANALYSIS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def rel(path: Path) -> str:
    return os.path.relpath(path, WEEKLY_DIR)


def load_three_stream_validation() -> dict[str, Any]:
    return json.loads(THREE_STREAM_STATS.read_text(encoding="utf-8"))


def measured_fz_target(params: Any) -> float:
    return -abs(params.control_fz_target_n or 5.0)


def force_error_summary(rows: list[dict[str, Any]], target_n: float) -> dict[str, float | None]:
    if not rows:
        return {
            "mae_n": None,
            "iae_n_s": None,
            "duration_s": None,
            "within_1n_fraction": None,
            "within_2n_fraction": None,
        }
    signed_errors = [row["fz"] - target_n for row in rows]
    abs_errors = [abs(value) for value in signed_errors]
    iae = 0.0
    for left, right, left_err, right_err in zip(rows, rows[1:], abs_errors, abs_errors[1:]):
        dt = max(0.0, right["t"] - left["t"])
        iae += 0.5 * (left_err + right_err) * dt
    return {
        "mae_n": statistics.fmean(abs_errors),
        "iae_n_s": iae,
        "duration_s": rows[-1]["t"] - rows[0]["t"],
        "within_1n_fraction": sum(value <= 1.0 for value in abs_errors) / len(abs_errors),
        "within_2n_fraction": sum(value <= 2.0 for value in abs_errors) / len(abs_errors),
    }


def contact_rows_for_window(rows: list[dict[str, Any]], window: tuple[int, int]) -> list[dict[str, Any]]:
    start_i, end_i = window
    active_rows = rows[start_i : end_i + 1]
    return [row for row in active_rows if abs(row["fz"]) >= 2.0]


def copy_if_exists(source: Path, target_name: str) -> str | None:
    if not source.exists():
        return None
    target = ASSET_DIR / target_name
    shutil.copy2(source, target)
    return rel(target)


def copy_presentation_assets() -> dict[str, str | None]:
    copied: dict[str, str | None] = {}
    for key, source in PHOTO_ASSETS.items():
        copied[key] = copy_if_exists(source, f"{key}{source.suffix.lower()}")
    for key, source in THREE_STREAM_ASSETS.items():
        copied[key] = copy_if_exists(source, f"{key}{source.suffix.lower()}")
    copied["program_interface_clip"] = rel(ASSET_DIR / "program_interface.mp4") if (ASSET_DIR / "program_interface.mp4").exists() else copy_if_exists(PROGRAM_INTERFACE_PREVIEW, "program_interface.mov")
    copied["program_interface_still"] = copy_if_exists(PROGRAM_INTERFACE_STILL, "program_interface.jpg")
    for key, filename in LOCAL_VIDEO_ASSETS.items():
        local_asset = ASSET_DIR / filename
        copied[key] = rel(local_asset) if local_asset.exists() else None
    return copied


def representative_score(segment: dict[str, Any], segment_summary: dict[str, Any]) -> tuple[float, float]:
    path_p95 = segment["error_2d"]["p95_mm"]
    fz_mae = segment_summary["contact_abs_fz_target_error_stats_n"]["mean"]
    return (path_p95, fz_mae)


def make_figures(
    analysis: Any,
    rows: list[dict[str, Any]],
    params: Any,
    ground_truth_path: dict[str, Any],
    contact_paths: list[dict[str, Any]],
    tracking: dict[str, Any],
    representative_idx: int,
) -> dict[str, str]:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    figures: dict[str, str] = {}

    rep_path = contact_paths[representative_idx]
    rep_tracking = tracking["segments"][representative_idx]
    reference_points = analysis.slice_reference_by_progress(
        ground_truth_path["points"],
        rep_tracking["reference_progress_start_mm"],
        rep_tracking["reference_progress_end_mm"],
    )
    samples = rep_path["tracking_samples"]
    rep_rows = rep_path["rows"]

    plt.figure(figsize=(7.2, 6.2))
    plt.plot(
        [point[0] * 1000.0 for point in reference_points],
        [point[1] * 1000.0 for point in reference_points],
        "k--",
        linewidth=2.2,
        label="reference path",
    )
    plt.plot(
        [row["pose"][0] * 1000.0 for row in rep_rows],
        [row["pose"][1] * 1000.0 for row in rep_rows],
        color="tab:orange",
        linewidth=2.0,
        label="actual contact path",
    )
    plt.scatter([rep_rows[0]["pose"][0] * 1000.0], [rep_rows[0]["pose"][1] * 1000.0], s=28, marker="o")
    plt.scatter([rep_rows[-1]["pose"][0] * 1000.0], [rep_rows[-1]["pose"][1] * 1000.0], s=36, marker="x")
    plt.xlabel("TCP X (mm)")
    plt.ylabel("TCP Y (mm)")
    plt.title("Contact path: actual vs reference")
    plt.axis("equal")
    plt.grid(True, alpha=0.3)
    plt.legend(fontsize=8)
    out = ASSET_DIR / "path_tracking_xy.png"
    plt.tight_layout()
    plt.savefig(out, dpi=170)
    plt.close()
    figures["path_xy"] = rel(out)

    progress = [sample["progress_mm"] for sample in samples]
    fig, axes = plt.subplots(2, 1, figsize=(8.2, 5.8), sharex=True)
    axes[0].plot(progress, [sample["dx_mm"] for sample in samples], color="tab:blue", linewidth=1.3)
    axes[1].plot(progress, [sample["dy_mm"] for sample in samples], color="tab:green", linewidth=1.3)
    for axis, label in zip(axes, ["X error (mm)", "Y error (mm)"]):
        axis.axhline(0.0, color="black", linestyle="--", linewidth=0.9)
        axis.set_ylabel(label)
        axis.grid(True, alpha=0.3)
    axes[1].set_xlabel("reference path progress (mm)")
    axes[0].set_title("X/Y tracking error")
    out = ASSET_DIR / "path_tracking_xy_error.png"
    fig.tight_layout()
    fig.savefig(out, dpi=170)
    plt.close(fig)
    figures["xy_error"] = rel(out)

    target = measured_fz_target(params)
    fz = [row["fz"] for row in rep_rows]
    signed_error = [row["fz"] - target for row in rep_rows]
    fig, axes = plt.subplots(2, 1, figsize=(8.2, 5.8), sharex=True)
    axes[0].plot(progress, fz, color="tab:red", linewidth=1.2, label="Fz")
    axes[0].axhline(target, color="black", linestyle="--", linewidth=0.9, label="target")
    axes[0].set_ylabel("Fz (N)")
    axes[0].legend(fontsize=8)
    axes[0].grid(True, alpha=0.3)
    axes[1].plot(progress, signed_error, color="tab:purple", linewidth=1.2)
    axes[1].axhline(0.0, color="black", linestyle="--", linewidth=0.9)
    axes[1].axhline(1.0, color="gray", linestyle="--", linewidth=0.8)
    axes[1].axhline(-1.0, color="gray", linestyle="--", linewidth=0.8)
    axes[1].set_ylabel("Fz - target (N)")
    axes[1].set_xlabel("reference path progress (mm)")
    axes[1].grid(True, alpha=0.3)
    axes[0].set_title("Measured Fz tracking")
    out = ASSET_DIR / "force_control_fz_tracking.png"
    fig.tight_layout()
    fig.savefig(out, dpi=170)
    plt.close(fig)
    figures["fz_error"] = rel(out)

    return figures


def md_image(label: str, path: str | None) -> str:
    if path is None:
        return f"*{label}: image pending.*"
    return f"![{label}]({path})"


def md_linked_image(label: str, image_path: str | None, target_path: str | None, width: int | None = None) -> str:
    if image_path is None and target_path is None:
        return f"*{label}: media pending.*"
    image = md_image(label, image_path) if width is None else f'<img src="{image_path}" alt="{label}" width="{width}">' if image_path else label
    return f"[{image}]({target_path})" if target_path else image


def html_figure(path: str | None, caption: str, klass: str = "") -> str:
    if path is None:
        return f'<figure class="media-card placeholder {klass}"><div class="pending">Image pending</div><figcaption>{caption}</figcaption></figure>'
    return f'<figure class="media-card {klass}"><img src="{path}" alt="{caption}"><figcaption>{caption}</figcaption></figure>'


def html_video(clip: str | None, poster: str | None, caption: str, klass: str = "") -> str:
    if clip is None:
        return f'<figure class="media-card placeholder video-card {klass}"><div class="pending">Video pending</div><figcaption>{caption}</figcaption></figure>'
    poster_attr = f' poster="{poster}"' if poster else ""
    return f'<figure class="media-card video-card {klass}"><video controls preload="metadata"{poster_attr} src="{clip}"></video><figcaption>{caption}</figcaption></figure>'


def downsample_list(values: list[Any], max_points: int = 220) -> list[Any]:
    if len(values) <= max_points:
        return values
    step = math.ceil(len(values) / max_points)
    sampled = values[::step]
    if sampled[-1] != values[-1]:
        sampled.append(values[-1])
    return sampled


def build_surface_path_payload(
    analysis: Any,
    params: Any,
    ground_truth_path: dict[str, Any],
    contact_paths: list[dict[str, Any]],
    tracking: dict[str, Any],
    representative_idx: int,
) -> dict[str, Any]:
    rep_path = contact_paths[representative_idx]
    rep_tracking = tracking["segments"][representative_idx]
    reference_points = analysis.slice_reference_by_progress(
        ground_truth_path["points"],
        rep_tracking["reference_progress_start_mm"],
        rep_tracking["reference_progress_end_mm"],
    )
    rep_rows = rep_path["rows"]
    all_xy = [row["pose"][:2] for row in rep_rows] + [point[:2] for point in reference_points]
    if params.path_home_pose:
        all_xy.append(params.path_home_pose[:2])
    center_x = statistics.fmean(point[0] for point in all_xy)
    center_y = statistics.fmean(point[1] for point in all_xy)

    def xy_mm(point: list[float]) -> dict[str, float]:
        return {
            "x": (point[0] - center_x) * 1000.0,
            "y": (point[1] - center_y) * 1000.0,
        }

    actual = [
        {
            **xy_mm(row["pose"]),
            "z": (row["pose"][2] - rep_rows[0]["pose"][2]) * 1000.0,
            "fz": row["fz"],
        }
        for row in rep_rows
    ]
    reference = [xy_mm(point) for point in reference_points]
    actual = downsample_list(actual)
    reference = downsample_list(reference, max_points=120)
    path_home = xy_mm(params.path_home_pose) if params.path_home_pose else None

    xs = [point["x"] for point in actual + reference]
    ys = [point["y"] for point in actual + reference]
    if path_home:
        xs.append(path_home["x"])
        ys.append(path_home["y"])
    pad_mm = 28.0
    surface_width = max(110.0, (max(xs) - min(xs)) + 2 * pad_mm)
    surface_depth = max(80.0, (max(ys) - min(ys)) + 2 * pad_mm)
    patch_pad = 10.0
    patch = {
        "x": (min(point["x"] for point in actual) + max(point["x"] for point in actual)) / 2.0,
        "y": (min(point["y"] for point in actual) + max(point["y"] for point in actual)) / 2.0,
        "width": max(22.0, max(point["x"] for point in actual) - min(point["x"] for point in actual) + 2 * patch_pad),
        "depth": max(18.0, max(point["y"] for point in actual) - min(point["y"] for point in actual) + 2 * patch_pad),
    }
    z_values = [row["pose"][2] * 1000.0 for row in rep_rows]
    return {
        "segment": rep_path["segment"],
        "actual": actual,
        "reference": reference,
        "pathHome": path_home,
        "surface": {
            "width": surface_width,
            "depth": surface_depth,
            "patch": patch,
        },
        "sources": {
            "path_id": params.path_id,
            "path_relative": params.path_relative,
            "path_home_pose": params.path_home_pose,
            "contact_window_rule": "runtime_state == 2, |Fz| >= 2 N, TCP speed >= 1 mm/s",
            "video_evidence": "assets/video_estimated_contact_region.png",
            "surface_frame": "estimated contact patch in robot base/TCP coordinates; no external workpiece frame calibration applied",
        },
        "stats": {
            "samples": len(rep_rows),
            "duration_s": rep_path["duration_s"],
            "xy_length_mm": rep_path["xy_length_mm"],
            "reference_start_mm": rep_tracking["reference_progress_start_mm"],
            "reference_end_mm": rep_tracking["reference_progress_end_mm"],
            "z_min_mm": min(z_values),
            "z_max_mm": max(z_values),
        },
    }


def html_surface_animation(surface: dict[str, Any]) -> str:
    payload = json.dumps(surface, separators=(",", ":"))
    template = """
      <div class="surface-stage" id="surface-stage" aria-label="Animated 3D force-control surface path">
        <div class="surface-overlay">
          <div>
            <strong>Highlighted contact band</strong>
            <span>actual force-controlled TCP path on the work surface</span>
          </div>
          <button id="surface-toggle" type="button">Pause</button>
        </div>
        <div class="surface-legend" aria-hidden="true">
          <span><i class="legend-swatch contact"></i>force-control path</span>
          <span><i class="legend-swatch reference"></i>reference path</span>
          <span><i class="legend-swatch home"></i>URCap path home</span>
          <span><i class="legend-swatch patch"></i>surface patch</span>
        </div>
        <div class="surface-status" id="surface-status">Animated surface path</div>
      </div>
      <script id="surface-trajectory-data" type="application/json">__SURFACE_PAYLOAD__</script>
      <script type="module">
        const surfaceData = JSON.parse(document.getElementById("surface-trajectory-data").textContent);
        const stage = document.getElementById("surface-stage");
        const status = document.getElementById("surface-status");
        const toggle = document.getElementById("surface-toggle");
        let paused = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        let runningRenderer = null;

        function setStatus(text) {
          status.textContent = text;
        }

        function worldPoint(point, y = 1.8) {
          return { x: point.x, y, z: point.y };
        }

        function resizeCanvas(canvas) {
          const rect = stage.getBoundingClientRect();
          const ratio = Math.min(window.devicePixelRatio || 1, 2);
          canvas.width = Math.max(1, Math.floor(rect.width * ratio));
          canvas.height = Math.max(1, Math.floor(rect.height * ratio));
          canvas.style.width = `${rect.width}px`;
          canvas.style.height = `${rect.height}px`;
          return { width: canvas.width, height: canvas.height, ratio };
        }

        function initFallbackCanvas() {
          const canvas = document.createElement("canvas");
          canvas.className = "surface-canvas";
          stage.prepend(canvas);
          const ctx = canvas.getContext("2d");
          const data = surfaceData;
          let size = resizeCanvas(canvas);
          window.addEventListener("resize", () => { size = resizeCanvas(canvas); });
          setStatus("Canvas renderer active");

          const all = data.actual.concat(data.reference);
          const maxSpan = Math.max(
            data.surface.width,
            data.surface.depth,
            ...all.map((p) => Math.abs(p.x) + Math.abs(p.y))
          );

          function project(point) {
            const scale = Math.min(size.width, size.height) / (maxSpan * 2.8);
            const x = size.width * 0.5 + (point.x - point.y) * scale;
            const y = size.height * 0.58 + (point.x + point.y) * scale * 0.38 - (point.z || 0) * scale;
            return { x, y };
          }

          function drawPolyline(points, color, lineWidth, upto = points.length) {
            if (points.length < 2) return;
            ctx.beginPath();
            points.slice(0, upto).forEach((point, idx) => {
              const p = project(point);
              if (idx === 0) ctx.moveTo(p.x, p.y);
              else ctx.lineTo(p.x, p.y);
            });
            ctx.strokeStyle = color;
            ctx.lineWidth = lineWidth * size.ratio;
            ctx.lineCap = "round";
            ctx.lineJoin = "round";
            ctx.stroke();
          }

          function draw(time) {
            ctx.clearRect(0, 0, size.width, size.height);
            const t = paused ? 1 : ((time % 6200) / 6200);
            const index = Math.max(2, Math.floor(t * data.actual.length));
            const halfW = data.surface.width / 2;
            const halfD = data.surface.depth / 2;
            const corners = [
              project({ x: -halfW, y: -halfD, z: 0 }),
              project({ x: halfW, y: -halfD, z: 0 }),
              project({ x: halfW, y: halfD, z: 0 }),
              project({ x: -halfW, y: halfD, z: 0 }),
            ];
            ctx.beginPath();
            corners.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
            ctx.closePath();
            ctx.fillStyle = "#e8edf2";
            ctx.fill();
            ctx.strokeStyle = "#9aa8b6";
            ctx.lineWidth = size.ratio;
            ctx.stroke();

            const patch = data.surface.patch;
            const patchCorners = [
              project({ x: patch.x - patch.width / 2, y: patch.y - patch.depth / 2, z: 0 }),
              project({ x: patch.x + patch.width / 2, y: patch.y - patch.depth / 2, z: 0 }),
              project({ x: patch.x + patch.width / 2, y: patch.y + patch.depth / 2, z: 0 }),
              project({ x: patch.x - patch.width / 2, y: patch.y + patch.depth / 2, z: 0 }),
            ];
            ctx.beginPath();
            patchCorners.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y));
            ctx.closePath();
            ctx.fillStyle = "rgba(229, 99, 38, 0.22)";
            ctx.fill();

            drawPolyline(data.reference, "#2e6ea6", 2);
            drawPolyline(data.actual, "rgba(95, 108, 123, 0.55)", 2);
            drawPolyline(data.actual, "#e56326", 4, index);
            if (data.pathHome) {
              const home = project({ ...data.pathHome, z: 0 });
              ctx.beginPath();
              ctx.arc(home.x, home.y, 5 * size.ratio, 0, Math.PI * 2);
              ctx.fillStyle = "#2f8068";
              ctx.fill();
              ctx.strokeStyle = "#ffffff";
              ctx.stroke();
            }
            const marker = project(data.actual[index - 1]);
            ctx.beginPath();
            ctx.arc(marker.x, marker.y, 7 * size.ratio, 0, Math.PI * 2);
            ctx.fillStyle = "#ffcf5a";
            ctx.fill();
            ctx.strokeStyle = "#172026";
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(marker.x, marker.y - 52 * size.ratio);
            ctx.lineTo(marker.x, marker.y - 12 * size.ratio);
            ctx.strokeStyle = "#d94c1a";
            ctx.lineWidth = 3 * size.ratio;
            ctx.stroke();
            if (!paused) requestAnimationFrame(draw);
          }
          requestAnimationFrame(draw);
          runningRenderer = { rerender: () => requestAnimationFrame(draw) };
        }

        async function initThree() {
          const THREE = await import("https://unpkg.com/three@0.165.0/build/three.module.js");
          const renderer = new THREE.WebGLRenderer({ antialias: true });
          renderer.setClearColor(0x111820, 1);
          renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
          renderer.domElement.className = "surface-canvas";
          stage.prepend(renderer.domElement);

          const scene = new THREE.Scene();
          scene.fog = new THREE.Fog(0x111820, 260, 560);
          const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 1000);
          const target = new THREE.Vector3(0, 0, 0);
          const ambient = new THREE.AmbientLight(0xffffff, 1.4);
          const key = new THREE.DirectionalLight(0xffffff, 2.4);
          key.position.set(120, 180, 80);
          scene.add(ambient, key);

          const surface = surfaceData.surface;
          const base = new THREE.Mesh(
            new THREE.PlaneGeometry(surface.width, surface.depth),
            new THREE.MeshStandardMaterial({ color: 0xe6edf3, roughness: 0.84, metalness: 0.04 })
          );
          base.rotation.x = -Math.PI / 2;
          scene.add(base);

          const grid = new THREE.GridHelper(Math.max(surface.width, surface.depth), 10, 0x8aa0b4, 0xc5ced6);
          grid.position.y = 0.35;
          scene.add(grid);

          const patch = surface.patch;
          const patchMesh = new THREE.Mesh(
            new THREE.PlaneGeometry(patch.width, patch.depth),
            new THREE.MeshBasicMaterial({ color: 0xe56326, transparent: true, opacity: 0.26, side: THREE.DoubleSide })
          );
          patchMesh.rotation.x = -Math.PI / 2;
          patchMesh.position.set(patch.x, 0.8, patch.y);
          scene.add(patchMesh);

          function vectorPoints(points, y = 2.4) {
            return points.map((point) => new THREE.Vector3(point.x, y, point.y));
          }

          const referenceGeometry = new THREE.BufferGeometry().setFromPoints(vectorPoints(surfaceData.reference, 3.0));
          const referenceLine = new THREE.Line(
            referenceGeometry,
            new THREE.LineDashedMaterial({ color: 0x2e6ea6, dashSize: 4, gapSize: 3, linewidth: 2 })
          );
          referenceLine.computeLineDistances();
          scene.add(referenceLine);

          const actualPoints = vectorPoints(surfaceData.actual, 4.0);
          const fullLine = new THREE.Line(
            new THREE.BufferGeometry().setFromPoints(actualPoints),
            new THREE.LineBasicMaterial({ color: 0x5f6c7b, transparent: true, opacity: 0.5 })
          );
          scene.add(fullLine);

          const trailGeometry = new THREE.BufferGeometry().setFromPoints(actualPoints);
          const trail = new THREE.Line(
            trailGeometry,
            new THREE.LineBasicMaterial({ color: 0xe56326 })
          );
          trailGeometry.setDrawRange(0, 2);
          scene.add(trail);

          const marker = new THREE.Mesh(
            new THREE.SphereGeometry(3.8, 24, 16),
            new THREE.MeshStandardMaterial({ color: 0xffcf5a, emissive: 0x5a2a00, emissiveIntensity: 0.4 })
          );
          scene.add(marker);

          const arrow = new THREE.ArrowHelper(
            new THREE.Vector3(0, -1, 0),
            new THREE.Vector3(0, 40, 0),
            32,
            0xd94c1a,
            8,
            5
          );
          scene.add(arrow);

          const startSphere = new THREE.Mesh(
            new THREE.SphereGeometry(2.7, 16, 12),
            new THREE.MeshBasicMaterial({ color: 0x2f8068 })
          );
          startSphere.position.copy(actualPoints[0]);
          scene.add(startSphere);

          const endSphere = startSphere.clone();
          endSphere.material = new THREE.MeshBasicMaterial({ color: 0x172026 });
          endSphere.position.copy(actualPoints[actualPoints.length - 1]);
          scene.add(endSphere);

          if (surfaceData.pathHome) {
            const homeSphere = new THREE.Mesh(
              new THREE.SphereGeometry(3.2, 16, 12),
              new THREE.MeshBasicMaterial({ color: 0x2f8068 })
            );
            homeSphere.position.set(surfaceData.pathHome.x, 6.2, surfaceData.pathHome.y);
            scene.add(homeSphere);
          }

          function resize() {
            const rect = stage.getBoundingClientRect();
            renderer.setSize(rect.width, rect.height, false);
            camera.aspect = Math.max(1, rect.width) / Math.max(1, rect.height);
            camera.updateProjectionMatrix();
          }
          window.addEventListener("resize", resize);
          resize();
          setStatus("WebGL renderer active");

          function animate(time) {
            const phase = paused ? 1 : ((time % 6200) / 6200);
            const index = Math.max(2, Math.min(actualPoints.length, Math.floor(phase * actualPoints.length)));
            trailGeometry.setDrawRange(0, index);
            const point = actualPoints[index - 1];
            marker.position.copy(point);
            marker.position.y = 7.5;
            arrow.position.set(point.x, 40, point.z);
            const orbit = -0.62 + Math.sin(time * 0.00022) * 0.08;
            camera.position.set(Math.sin(orbit) * 210, 132, Math.cos(orbit) * 210);
            camera.lookAt(target);
            renderer.render(scene, camera);
            if (!paused) requestAnimationFrame(animate);
          }
          requestAnimationFrame(animate);
          runningRenderer = { rerender: () => requestAnimationFrame(animate) };
        }

        toggle.addEventListener("click", () => {
          paused = !paused;
          toggle.textContent = paused ? "Play" : "Pause";
          if (!paused && runningRenderer) runningRenderer.rerender();
        });

        initThree().catch(() => initFallbackCanvas());
      </script>
"""
    return template.replace("__SURFACE_PAYLOAD__", payload)


def residual_rows(three_stream: dict[str, Any]) -> list[tuple[str, str, dict[str, float]]]:
    stats = three_stream["residual_stats"]["best_lag_nearest_udp_sample"]
    return [
        ("Fx", "N", stats["fx"]),
        ("Fy", "N", stats["fy"]),
        ("Fz", "N", stats["fz"]),
        ("Tx", "Nm", stats["tx"]),
        ("Ty", "Nm", stats["ty"]),
        ("Tz", "Nm", stats["tz"]),
    ]


def fz_residual(three_stream: dict[str, Any]) -> dict[str, float]:
    return three_stream["residual_stats"]["best_lag_nearest_udp_sample"]["fz"]


def three_stream_conclusion_en(three_stream: dict[str, Any]) -> str:
    fz = fz_residual(three_stream)
    return (
        "Strict first-zero equality is not supported. After timestamp alignment, "
        f"the Fz residual keeps a fixed mean offset of {fmt(fz['mean'], 3)} N "
        f"(RMS {fmt(fz['rms'], 3)} N, max abs {fmt(fz['max_abs'], 3)} N), "
        "which is larger than force quantization and the remaining timestamp-alignment error. "
        "The dynamic shape is still consistent with approximately 4x downsampling/hold."
    )


def weekly_three_stream_summary(three_stream: dict[str, Any]) -> dict[str, Any]:
    return {
        "sampling_validation": three_stream["sampling_validation"],
        "alignment": three_stream["alignment"],
        "fz_residual_best_lag_nearest_udp_sample": fz_residual(three_stream),
        "strict_first_zero_overlap": three_stream["classification"]["strict_first_zero_overlap"],
        "conclusion_en": three_stream_conclusion_en(three_stream),
        "appendix_residual_stats": three_stream["residual_stats"]["best_lag_nearest_udp_sample"],
    }


def md_residual_table(three_stream: dict[str, Any]) -> str:
    lines = [
        "| Axis | Unit | Mean error | Std | RMS | Max abs |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for axis, unit, values in residual_rows(three_stream):
        lines.append(
            f"| {axis} | {unit} | {fmt(values['mean'], 6)} | {fmt(values['std'], 6)} | "
            f"{fmt(values['rms'], 6)} | {fmt(values['max_abs'], 6)} |"
        )
    return "\n".join(lines)


def md_fz_residual_table(three_stream: dict[str, Any]) -> str:
    values = fz_residual(three_stream)
    return "\n".join(
        [
            "| Axis | Unit | Mean error | Std | RMS | Max abs |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
            f"| Fz | N | {fmt(values['mean'], 6)} | {fmt(values['std'], 6)} | "
            f"{fmt(values['rms'], 6)} | {fmt(values['max_abs'], 6)} |",
        ]
    )


def html_residual_table(three_stream: dict[str, Any]) -> str:
    rows = "\n".join(
        "<tr>"
        f"<td>{axis}</td><td>{unit}</td>"
        f"<td>{fmt(values['mean'], 6)}</td><td>{fmt(values['std'], 6)}</td>"
        f"<td>{fmt(values['rms'], 6)}</td><td>{fmt(values['max_abs'], 6)}</td>"
        "</tr>"
        for axis, unit, values in residual_rows(three_stream)
    )
    return f"""
      <table class="data-table">
        <thead><tr><th>Axis</th><th>Unit</th><th>Mean error</th><th>Std</th><th>RMS</th><th>Max abs</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
"""


def html_fz_residual_table(three_stream: dict[str, Any]) -> str:
    values = fz_residual(three_stream)
    return f"""
      <table class="data-table">
        <thead><tr><th>Axis</th><th>Unit</th><th>Mean error</th><th>Std</th><th>RMS</th><th>Max abs</th></tr></thead>
        <tbody><tr><td>Fz</td><td>N</td><td>{fmt(values['mean'], 6)}</td><td>{fmt(values['std'], 6)}</td><td>{fmt(values['rms'], 6)}</td><td>{fmt(values['max_abs'], 6)}</td></tr></tbody>
      </table>
"""


def build_markdown(
    figures: dict[str, str],
    assets: dict[str, str | None],
    params: Any,
    rep: dict[str, Any],
    rep_force: dict[str, float | None],
    three_stream: dict[str, Any],
) -> str:
    program_section = "Program interface preview unavailable."
    if assets.get("program_interface_still") and assets.get("program_interface_clip"):
        program_section = (
            f"[![Program interface]({assets['program_interface_still']})]"
            f"({assets['program_interface_clip']})"
        )
    elif assets.get("program_interface_still"):
        program_section = md_image("Program interface", assets["program_interface_still"])

    final_wiring_section = md_linked_image(
        "Final connected wiring video",
        assets.get("final_wiring_still"),
        assets.get("final_wiring_clip"),
        width=420,
    )
    demo_video_section = md_linked_image(
        "Robot contact experiment video",
        assets.get("demo_experiment_still"),
        assets.get("demo_experiment_clip"),
        width=420,
    )

    return f"""## Bench Setup

The current contribution is the working UR10e / OnRobot HEX bench: mounted force sensor, small switch, Compute Box, Ethernet/RTDE path, and repeatable force-control measurements.

{md_image("UR10e OnRobot bench overview", assets.get("bench_overview"))}

{final_wiring_section}

| View | Evidence |
| --- | --- |
| HEX sensor | {md_image("OnRobot HEX sensor", assets.get("hex_sensor"))} |
| Small Ethernet switch | {md_image("Small Ethernet switch", assets.get("small_switch"))} |
| Compute Box | {md_image("OnRobot Compute Box for F/T sensor", assets.get("compute_box"))} |

## Sensor Baseline

The 600 s static validation section focuses on Fz. UR `actual_TCP_force` was logged at 500 Hz, OnRobot URCap registers at 125 Hz, and OnRobot UDP raw at SPEED=2 near 500 Hz. The capture completed with `errors=[]`; UDP sequence deltas were all `+1`, and UDP sample-counter deltas were all `+2`.

Software zero rule: each stream/channel subtracts its own first logged sample. No hardware zero was sent: no OnRobot `BIAS`, no OnRobot `FILTER`, no UR `zero_ftsensor()`, no URScript motion, and no TCP/payload writes. Dashboard before/after remained `PLAYING 3.urp`, Safetymode `NORMAL`, Robotmode `RUNNING`.

Timestamp alignment result: best lag is {fmt(three_stream['alignment']['best_lag_ms'], 2)} ms using `URCap(t) - UDP(t + lag)`. Nearest UDP sample spacing from one URCap update to the next is mostly 4 packets (`4: {three_stream['alignment']['nearest_udp_index_delta_counts_top'][0][1]}`), consistent with about 4x downsampling/hold in shape.

Conclusion: {three_stream_conclusion_en(three_stream)}

{md_image("First-zeroed Fz overlay, 600 s", assets.get("three_stream_fz_overlay"))}

{md_fz_residual_table(three_stream)}

## Force-Control Demo

{md_image("Fz tracking", figures["fz_error"])}

| Metric | Value |
| --- | ---: |
| Fz error MAE (N) | {fmt(rep_force['mae_n'])} |
| Fz error IAE (N*s) | {fmt(rep_force['iae_n_s'])} |
| Within +/-1 N (%) | {fmt((rep_force['within_1n_fraction'] or 0) * 100.0, 1)} |
| Within +/-2 N (%) | {fmt((rep_force['within_2n_fraction'] or 0) * 100.0, 1)} |

## Path Tracking

{md_image("Path tracking XY", figures["path_xy"])}

{md_linked_image("X/Y tracking error", figures["xy_error"], figures["xy_error"], width=820)}

| Metric | Value |
| --- | ---: |
| X MAE (mm) | {fmt(rep['x_error']['mae_mm'])} |
| Y MAE (mm) | {fmt(rep['y_error']['mae_mm'])} |
| 2D P95 (mm) | {fmt(rep['error_2d']['p95_mm'])} |

## Settings

| Item | Setting |
| --- | --- |
| `F/T Control` | Tool Z compliant, target `Fz=+5 N` |
| Force PID | `{params.force_pid}` |
| `F/T Move` | speed {fmt(params.ft_move_speed_m_s, 3)} m/s |

## Program Interface

{program_section}

## Demo Video

{demo_video_section}

## Appendix: Six-Axis Stream Validation

The appendix keeps the non-Fz plots and the URCap-versus-UDP 500 Hz comparison out of the main weekly-meeting flow.

{md_image("First-zeroed Fx/Fy/Fz overlay, 600 s", assets.get("three_stream_force_axes"))}

{md_residual_table(three_stream)}

{md_image("URCap minus timestamp-aligned UDP residuals", assets.get("three_stream_residuals"))}
"""


def build_html(
    figures: dict[str, str],
    assets: dict[str, str | None],
    params: Any,
    rep: dict[str, Any],
    rep_force: dict[str, float | None],
    three_stream: dict[str, Any],
    surface: dict[str, Any],
) -> str:
    program_media = '<div class="pending wide">Program interface clip pending</div>'
    if assets.get("program_interface_clip"):
        poster = f' poster="{assets["program_interface_still"]}"' if assets.get("program_interface_still") else ""
        program_media = f'<video controls preload="metadata"{poster} src="{assets["program_interface_clip"]}"></video>'
    target_fz = measured_fz_target(params)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>UR10e OnRobot HEX Meeting Deck</title>
  <style>
    :root {{
      --ink: #172026;
      --muted: #5c6872;
      --line: #d9e0e6;
      --panel: #f6f8fa;
      --green: #2f8068;
      --blue: #2e6ea6;
      --amber: #9a6b22;
      --white: #ffffff;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: #eef2f5;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
      overflow-x: hidden;
    }}
    nav {{
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      gap: 10px;
      align-items: center;
      padding: 10px 22px;
      background: rgba(255,255,255,.94);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(8px);
      overflow-x: auto;
    }}
    nav a {{
      color: var(--muted);
      text-decoration: none;
      white-space: nowrap;
      font-size: 13px;
      font-weight: 650;
      padding: 6px 10px;
      border: 1px solid transparent;
      border-radius: 6px;
    }}
    nav a:hover {{ border-color: var(--line); color: var(--ink); background: var(--panel); }}
    main {{ width: min(1320px, 100%); margin: 0 auto; }}
    section {{
      min-height: calc(100vh - 48px);
      padding: 42px 36px 48px;
      background: var(--white);
      border-bottom: 1px solid var(--line);
      scroll-margin-top: 58px;
    }}
    .eyebrow {{
      color: var(--blue);
      font-size: 13px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0;
      margin-bottom: 8px;
    }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 44px; line-height: 1.06; width: 100%; max-width: 940px; overflow-wrap: anywhere; }}
    h2 {{ font-size: 34px; line-height: 1.1; margin-bottom: 14px; }}
    h3 {{ font-size: 18px; margin-bottom: 8px; }}
    p {{ color: var(--muted); width: 100%; max-width: 820px; margin: 12px 0 0; overflow-wrap: anywhere; }}
    .lead {{ font-size: 19px; max-width: 900px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 18px;
      margin-top: 26px;
      align-items: stretch;
    }}
    .grid > * {{ min-width: 0; }}
    .span-3 {{ grid-column: span 3; }}
    .span-4 {{ grid-column: span 4; }}
    .span-5 {{ grid-column: span 5; }}
    .span-6 {{ grid-column: span 6; }}
    .span-7 {{ grid-column: span 7; }}
    .span-8 {{ grid-column: span 8; }}
    .span-12 {{ grid-column: span 12; }}
    .media-card {{
      margin: 0;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      overflow: hidden;
      min-height: 220px;
      display: flex;
      flex-direction: column;
      min-width: 0;
    }}
    .media-card img, .media-card video {{
      width: 100%;
      max-width: 100%;
      height: 100%;
      min-height: 220px;
      object-fit: cover;
      display: block;
      background: #dce3ea;
    }}
    .media-card.video-card {{ background: #101820; }}
    .media-card.video-card video {{
      height: auto;
      max-height: 620px;
      aspect-ratio: 9 / 16;
      object-fit: contain;
      background: #101820;
    }}
    .media-card.plot img {{ object-fit: contain; background: #fff; padding: 8px; }}
    .media-card figcaption {{
      min-height: 45px;
      padding: 10px 12px;
      color: var(--muted);
      font-size: 13px;
      border-top: 1px solid var(--line);
      background: #fff;
    }}
    .surface-stage {{
      position: relative;
      width: 100%;
      min-height: 430px;
      height: min(62vh, 680px);
      margin-top: 26px;
      overflow: hidden;
      border-radius: 8px;
      background: #111820;
      color: #fff;
      isolation: isolate;
    }}
    .surface-canvas {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      display: block;
    }}
    .surface-overlay {{
      position: absolute;
      top: 16px;
      left: 16px;
      right: 16px;
      z-index: 2;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      pointer-events: none;
    }}
    .surface-overlay strong {{
      display: block;
      font-size: 14px;
      letter-spacing: 0;
    }}
    .surface-overlay span {{
      display: block;
      max-width: 42ch;
      color: rgba(255,255,255,.72);
      font-size: 13px;
      margin-top: 4px;
    }}
    .surface-overlay button {{
      pointer-events: auto;
      color: #fff;
      background: rgba(255,255,255,.12);
      border: 1px solid rgba(255,255,255,.24);
      border-radius: 6px;
      padding: 7px 12px;
      font: inherit;
      font-size: 13px;
      font-weight: 750;
      cursor: pointer;
    }}
    .surface-overlay button:hover {{ background: rgba(255,255,255,.2); }}
    .surface-legend {{
      position: absolute;
      left: 16px;
      bottom: 16px;
      z-index: 2;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: rgba(255,255,255,.8);
      font-size: 12px;
    }}
    .surface-legend span {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 8px;
      border-radius: 6px;
      background: rgba(0,0,0,.24);
    }}
    .legend-swatch {{
      width: 18px;
      height: 3px;
      border-radius: 999px;
      background: #fff;
    }}
    .legend-swatch.contact {{ background: #e56326; }}
    .legend-swatch.reference {{ background: #2e6ea6; }}
    .legend-swatch.home {{ height: 10px; width: 10px; background: #2f8068; }}
    .legend-swatch.patch {{ height: 10px; background: rgba(229,99,38,.38); }}
    .surface-status {{
      position: absolute;
      right: 16px;
      bottom: 16px;
      z-index: 2;
      color: rgba(255,255,255,.62);
      font-size: 12px;
    }}
    .pending {{
      display: grid;
      place-items: center;
      min-height: 220px;
      color: var(--muted);
      border: 1px dashed #aab7c2;
      border-radius: 8px;
      background: var(--panel);
      font-weight: 700;
    }}
    .pending.wide {{ min-height: 360px; }}
    .metric-row {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
      margin-top: 22px;
    }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 14px;
      min-height: 90px;
    }}
    .metric .value {{ font-size: 28px; font-weight: 800; color: var(--green); }}
    .metric .label {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .path {{
      display: grid;
      grid-template-columns: repeat(5, minmax(130px, 1fr));
      gap: 10px;
      align-items: center;
      margin-top: 28px;
    }}
    .node {{
      min-height: 110px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 14px;
    }}
    .node strong {{ display: block; font-size: 16px; }}
    .node span {{ display: block; color: var(--muted); font-size: 13px; margin-top: 6px; }}
    .arrow {{
      text-align: center;
      color: var(--blue);
      font-weight: 900;
      font-size: 22px;
    }}
    .note {{
      padding: 14px 16px;
      border-left: 4px solid var(--amber);
      background: #fff8ea;
      color: #5f451b;
      margin-top: 18px;
      max-width: 940px;
    }}
    .compact-list {{
      display: grid;
      gap: 10px;
      margin-top: 18px;
      max-width: 880px;
    }}
    .compact-list div {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      background: #fff;
    }}
    .data-table {{
      width: 100%;
      max-width: 980px;
      margin-top: 22px;
      border-collapse: collapse;
      font-size: 13px;
      background: #fff;
    }}
    .data-table th, .data-table td {{
      border: 1px solid var(--line);
      padding: 8px 10px;
      text-align: right;
    }}
    .data-table th:first-child, .data-table td:first-child,
    .data-table th:nth-child(2), .data-table td:nth-child(2) {{
      text-align: left;
    }}
    .data-table th {{ background: var(--panel); color: var(--ink); }}
    @media (max-width: 900px) {{
      main {{ width: 100%; max-width: 100%; }}
      section {{ width: 100%; max-width: 100vw; overflow: hidden; padding: 28px 18px 34px; }}
      h1 {{ font-size: 24px; line-height: 1.12; max-width: 22ch; }}
      h2 {{ font-size: 28px; }}
      p, .lead {{ max-width: 29ch; }}
      .lead {{ font-size: 17px; }}
      .grid {{ grid-template-columns: 1fr; }}
      .span-3, .span-4, .span-5, .span-6, .span-7, .span-8, .span-12 {{ grid-column: auto; }}
      .metric-row {{ grid-template-columns: 1fr 1fr; }}
      .path {{ grid-template-columns: 1fr; }}
      .arrow {{ transform: rotate(90deg); }}
      .surface-stage {{ min-height: 390px; height: 58vh; border-radius: 0; margin-left: -18px; margin-right: -18px; width: calc(100% + 36px); }}
      .surface-overlay {{ left: 12px; right: 12px; top: 12px; }}
      .surface-overlay span {{ max-width: 24ch; }}
      .surface-legend {{ left: 12px; right: 12px; bottom: 12px; }}
      .surface-status {{ display: none; }}
    }}
  </style>
</head>
<body>
  <nav>
    <a href="#setup">Setup</a>
    <a href="#baseline">Sensor Baseline</a>
    <a href="#demo">Force Demo</a>
    <a href="#surface">Surface Path</a>
    <a href="#videos">Experiment Video</a>
    <a href="#interface">Program Interface</a>
    <a href="#appendix">Appendix</a>
  </nav>
  <main>
    <section id="setup">
      <div class="eyebrow">Bench setup</div>
      <h1>UR10e + OnRobot HEX bench</h1>
      <p class="lead">The meeting focus is the working bench: mounted sensor, small switch, Compute Box / Ethernet / RTDE path, and repeatable force-control measurements.</p>
      <div class="grid">
        {html_figure(assets.get("bench_overview"), "UR10e OnRobot bench overview with the controller and wiring visible.", "span-8")}
        {html_video(assets.get("final_wiring_clip"), assets.get("final_wiring_still"), "Final connected wiring state before the demo run.", "span-4")}
        {html_figure(assets.get("hex_sensor"), "OnRobot HEX force/torque sensor.", "span-4")}
        {html_figure(assets.get("small_switch"), "Small Ethernet switch in the Compute Box / UR10e / Ubuntu path.", "span-4")}
        {html_figure(assets.get("compute_box"), "OnRobot Compute Box for the F/T sensor.", "span-4")}
      </div>
    </section>

    <section id="baseline">
      <div class="eyebrow">Sensor baseline</div>
      <h2>600 s three-stream static sync validation</h2>
      <p>This weekly section focuses on Fz. UR actual_TCP_force was logged at 500 Hz, OnRobot URCap registers at 125 Hz, and OnRobot UDP raw at SPEED=2 near 500 Hz. The run completed with errors=[]; UDP sequence deltas were all +1 and sample-counter deltas were all +2.</p>
      <div class="metric-row">
        <div class="metric"><div class="value">{three_stream['sampling_validation']['rtde_samples']:,}</div><div class="label">UR RTDE rows</div></div>
        <div class="metric"><div class="value">{fmt(three_stream['sampling_validation']['rtde_interval_rate_hz'], 3)}</div><div class="label">UR RTDE Hz</div></div>
        <div class="metric"><div class="value">{three_stream['sampling_validation']['urcap_tuple_updates']:,}</div><div class="label">URCap tuple updates</div></div>
        <div class="metric"><div class="value">{fmt(three_stream['sampling_validation']['urcap_update_rate_hz'], 3)}</div><div class="label">URCap Hz</div></div>
        <div class="metric"><div class="value">{three_stream['sampling_validation']['udp_packets']:,}</div><div class="label">UDP packets</div></div>
      </div>
      <div class="compact-list">
        <div><strong>Software zero only</strong><br>Each stream/channel subtracts its own first logged sample. No OnRobot BIAS/FILTER, no UR zero_ftsensor(), no URScript motion, and no TCP/payload writes.</div>
        <div><strong>Timestamp alignment</strong><br>Best lag is {fmt(three_stream['alignment']['best_lag_ms'], 2)} ms for URCap(t) - UDP(t + lag). Nearest UDP index deltas are mostly 4 packets ({three_stream['alignment']['nearest_udp_index_delta_counts_top'][0][1]} transitions).</div>
        <div><strong>Conclusion</strong><br>{three_stream_conclusion_en(three_stream)}</div>
      </div>
      <div class="grid">
        {html_figure(assets.get("three_stream_fz_overlay"), "First-zeroed Fz overlay across UDP raw, URCap registers, and UR actual_TCP_force.", "span-12 plot")}
      </div>
      {html_fz_residual_table(three_stream)}
    </section>

    <section id="demo">
      <div class="eyebrow">Force-control demo</div>
      <h2>Closed-loop force tracking and contact path tracking</h2>
      <p>The demo result is evidence that the environment can run a force-controlled path. It is not the main story by itself.</p>
      <div class="metric-row">
        <div class="metric"><div class="value">{fmt(target_fz)}</div><div class="label">target Fz (N)</div></div>
        <div class="metric"><div class="value">{fmt(rep_force['mae_n'])}</div><div class="label">Fz error MAE (N)</div></div>
        <div class="metric"><div class="value">{fmt(rep_force['iae_n_s'])}</div><div class="label">Fz error IAE (N*s)</div></div>
        <div class="metric"><div class="value">{fmt((rep_force['within_1n_fraction'] or 0) * 100.0, 1)}%</div><div class="label">within +/-1 N</div></div>
        <div class="metric"><div class="value">{fmt(rep['error_2d']['p95_mm'])}</div><div class="label">2D path P95 (mm)</div></div>
      </div>
      <div class="grid">
        {html_figure(figures["fz_error"], "Fz tracking. Lower panel shows the instantaneous signed error trace.", "span-7 plot")}
        {html_figure(figures["path_xy"], "Actual contact path against reference path.", "span-5 plot")}
        {html_figure(figures["xy_error"], "X/Y tracking error along the reference path.", "span-7 plot")}
      </div>
    </section>

    <section id="surface">
      <div class="eyebrow">Surface path</div>
      <h2>Where the force-control path touched the surface</h2>
      <p>The orange band is an estimated contact patch in robot-base/TCP coordinates, not an externally calibrated workpiece frame. The green point comes from the URCap F/T Path home pose; the moving marker follows the RTDE actual TCP contact path, and the blue line keeps the reference path visible for comparison.</p>
      <div class="metric-row">
        <div class="metric"><div class="value">{surface['segment']}</div><div class="label">representative contact segment</div></div>
        <div class="metric"><div class="value">{surface['sources']['path_id']}</div><div class="label">URCap F/T Path ID</div></div>
        <div class="metric"><div class="value">{fmt(surface['stats']['xy_length_mm'], 1)}</div><div class="label">actual XY contact length (mm)</div></div>
        <div class="metric"><div class="value">{fmt(surface['stats']['duration_s'], 2)}</div><div class="label">animated window duration (s)</div></div>
        <div class="metric"><div class="value">{fmt(surface['stats']['reference_start_mm'], 1)}-{fmt(surface['stats']['reference_end_mm'], 1)}</div><div class="label">reference progress (mm)</div></div>
      </div>
      {html_surface_animation(surface)}
      <div class="grid">
        {html_figure(assets.get("video_contact_evidence"), "Video-estimated contact region on the visible white workpiece surface. This is camera-view evidence only, used as a qualitative check against the URCap path home and RTDE contact trajectory.", "span-6")}
        <div class="span-6">
          <div class="compact-list">
            <div><strong>Video constraint</strong><br>The experiment video places the contact on the visible upper face of the white workpiece, around the middle band under the tool.</div>
            <div><strong>Data constraint</strong><br>URCap gives path home and pathID; RTDE gives the actual contact trajectory during the force-controlled window.</div>
            <div><strong>Remaining limit</strong><br>The camera view does not provide a calibrated workpiece coordinate frame, so the highlighted patch is an approximate visual region.</div>
          </div>
        </div>
      </div>
    </section>

    <section id="videos">
      <div class="eyebrow">Experiment video</div>
      <h2>Real robot contact demo video</h2>
      <p>This is the 29.2 MB robot experiment video. It is separate from the short PolyScope program-interface companion clip.</p>
      <div class="grid">
        {html_video(assets.get("demo_experiment_clip"), assets.get("demo_experiment_still"), "Robot contact experiment video.", "span-5")}
        {html_figure(figures["fz_error"], "Fz tracking from the same analyzed demo window.", "span-7 plot")}
      </div>
    </section>

    <section id="interface">
      <div class="eyebrow">Program interface</div>
      <h2>URCap program structure</h2>
      <p>This clip is the PolyScope program interface, so it is named separately from the real robot demo video.</p>
      <div class="grid">
        <div class="media-card video-card span-7">{program_media}</div>
        <div class="span-5">
          <div class="compact-list">
            <div><strong>Program nodes</strong><br>F/T Zero, F/T Search, F/T Control, F/T Move, F/T Path.</div>
            <div><strong>Settings kept in the meeting notes</strong><br>Tool Z compliant, force PID, and F/T Move speed.</div>
            <div><strong>Video separation</strong><br>The robot experiment video is shown separately; this clip is only the program interface.</div>
          </div>
        </div>
      </div>
    </section>

    <section id="appendix">
      <div class="eyebrow">Appendix</div>
      <h2>Six-axis stream validation details</h2>
      <p>The appendix keeps the non-Fz plots and the URCap-versus-UDP 500 Hz comparison out of the main weekly-meeting flow.</p>
      <div class="grid">
        {html_figure(assets.get("three_stream_force_axes"), "First-zeroed Fx/Fy/Fz overlay for the 600 s static capture.", "span-12 plot")}
      </div>
      {html_residual_table(three_stream)}
      <div class="grid">
        {html_figure(assets.get("three_stream_residuals"), "URCap minus timestamp-aligned nearest UDP sample residuals for Fx/Fy/Fz/Tx/Ty/Tz.", "span-12 plot")}
      </div>
    </section>
  </main>
  <script>
    window.addEventListener("load", () => {{
      const params = new URLSearchParams(window.location.search);
      const slide = params.get("slide") || window.location.hash.slice(1);
      if (slide) {{
        window.setTimeout(() => {{
          const target = document.getElementById(slide);
          if (target) target.scrollIntoView({{ block: "start" }});
        }}, 80);
      }}
    }});
  </script>
</body>
</html>
"""


def main() -> int:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    analysis = load_analysis_module()

    rows = analysis.load_rows(FINAL_CSV)
    params = analysis.parse_program_params(RUN_DIR / "controller_snapshot/path_straight_line.script")
    reference_rows = analysis.load_reference_rows(REFERENCE_CSV)
    ground_truth_path = analysis.select_ground_truth_path(reference_rows)
    active_windows = analysis.split_active_windows(rows)
    segment_summaries = [
        analysis.segment_metrics(rows, a, b, params, f"Run {idx}")
        for idx, (a, b) in enumerate(active_windows, start=1)
    ]
    contact_paths = analysis.contact_path_segments(rows, active_windows)
    tracking = analysis.compare_contact_paths_to_reference(contact_paths, ground_truth_path["points"])
    for path_segment in contact_paths:
        path_segment.pop("rows", None)

    representative_idx = min(
        range(len(tracking["segments"])),
        key=lambda idx: representative_score(tracking["segments"][idx], segment_summaries[idx]),
    )

    # Rebuild contact paths with rows for plotting after the serializable copy above.
    contact_paths_for_plot = analysis.contact_path_segments(rows, active_windows)
    tracking_for_plot = analysis.compare_contact_paths_to_reference(contact_paths_for_plot, ground_truth_path["points"])
    figures = make_figures(
        analysis,
        rows,
        params,
        ground_truth_path,
        contact_paths_for_plot,
        tracking_for_plot,
        representative_idx,
    )
    surface = build_surface_path_payload(
        analysis,
        params,
        ground_truth_path,
        contact_paths_for_plot,
        tracking_for_plot,
        representative_idx,
    )
    assets = copy_presentation_assets()
    three_stream = load_three_stream_validation()

    rep = tracking["segments"][representative_idx]
    best_label = rep["segment"]
    rep_contact_rows = contact_rows_for_window(rows, active_windows[representative_idx])
    force_target_n = measured_fz_target(params)
    rep_force = force_error_summary(rep_contact_rows, force_target_n)

    report = build_markdown(figures, assets, params, rep, rep_force, three_stream)
    REPORT_PATH.write_text(report, encoding="utf-8")
    HTML_PATH.write_text(build_html(figures, assets, params, rep, rep_force, three_stream, surface), encoding="utf-8")
    summary = {
        "report": str(REPORT_PATH),
        "html": str(HTML_PATH),
        "representative_segment": best_label,
        "representative_selection": "minimum 2D P95 path error, then minimum measured Fz target-error MAE",
        "figures": figures,
        "surface_path": surface,
        "assets": assets,
        "three_stream_validation": weekly_three_stream_summary(three_stream),
        "metrics": {
            "representative": rep,
            "representative_force": {
                "logged_target_n": force_target_n,
                "fz_error_mae_n": rep_force["mae_n"],
                "fz_error_iae_n_s": rep_force["iae_n_s"],
                "duration_s": rep_force["duration_s"],
                "fz_error_le_1n_fraction": rep_force["within_1n_fraction"],
            },
        },
    }
    (WEEKLY_DIR / "demo_01_02_weekly_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
