#!/usr/bin/env python3
"""Build the Step4f no-scale cycloid safe frame from drag-teach hints."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REPO = EXPERIMENT_ROOT.parents[3]
RUN_ROOT = EXPERIMENT_ROOT / "runs"
CONFIG_PATH = EXPERIMENT_ROOT / "config" / "step4f_safe_frame.json"
ASSET_DIR = REPO / "report" / "assets" / "step4f-safe-frame"
SESSION_HINT = Path("/tmp/drag_teach_points_dir.txt")


def latest_session_dir() -> Path:
    if SESSION_HINT.exists():
        hinted = Path(SESSION_HINT.read_text(encoding="utf-8").strip())
        if (hinted / "points.json").is_file():
            return hinted
    matches = sorted(RUN_ROOT.glob("drag_teach_points_*/points.json"))
    if not matches:
        raise SystemExit("No drag-teach points session found.")
    return matches[-1].parent


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def paper_cycloid_xy(t_s: float) -> np.ndarray:
    phase = 0.1 * t_s
    return np.array(
        [
            0.015 * (phase - math.sin(phase)),
            0.015 * (1.0 - math.cos(phase)),
        ],
        dtype=float,
    )


def rigid_fit_no_scale(local_xy: np.ndarray, taught_xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    local_center = local_xy.mean(axis=0)
    taught_center = taught_xy.mean(axis=0)
    x = local_xy - local_center
    y = taught_xy - taught_center
    u, _s, vt = np.linalg.svd(x.T @ y)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0.0:
        vt[-1, :] *= -1.0
        rotation = vt.T @ u.T
    translation = taught_center - rotation @ local_center
    return rotation, translation


def transform_points(local_xy: np.ndarray, rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    return (rotation @ local_xy.T).T + translation


def stats(values: np.ndarray) -> dict[str, float]:
    return {
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }


def make_payload(points_path: Path, margin_m: float, max_shift_m: float) -> dict[str, Any]:
    points_payload = load_json(points_path)
    points = points_payload["points"]
    labels = ["start", "mid", "end"]
    taught_xy = np.array(
        [[points[label]["rtde"]["actual_TCP_pose"][0], points[label]["rtde"]["actual_TCP_pose"][1]] for label in labels],
        dtype=float,
    )
    taught_z = np.array([points[label]["rtde"]["actual_TCP_pose"][2] for label in labels], dtype=float)
    local_xy = np.array([paper_cycloid_xy(t_s) for t_s in (0.0, 30.0, 60.0)], dtype=float)
    rotation, translation = rigid_fit_no_scale(local_xy, taught_xy)
    samples_t = np.linspace(0.0, 60.0, 601)
    local_samples = np.array([paper_cycloid_xy(float(t_s)) for t_s in samples_t], dtype=float)
    base_samples_pre = transform_points(local_samples, rotation, translation)
    taught_x_plus_boundary_m = float(max(taught_xy[0, 0], taught_xy[2, 0]))
    guard_line_x_m = taught_x_plus_boundary_m - margin_m
    required_shift_m = max(0.0, float(base_samples_pre[:, 0].max()) - guard_line_x_m)
    if required_shift_m > max_shift_m + 1e-12:
        status = "blocked_x_guard"
        applied_shift_m = max_shift_m
    else:
        status = "ok"
        applied_shift_m = required_shift_m
    shift_vec = np.array([-applied_shift_m, 0.0], dtype=float)
    translation_shifted = translation + shift_vec
    base_samples = transform_points(local_samples, rotation, translation_shifted)
    predicted = transform_points(local_xy, rotation, translation_shifted)
    residual_mm = (taught_xy - predicted) * 1000.0
    residual_norm_mm = np.linalg.norm(residual_mm, axis=1)
    u_along = rotation @ np.array([1.0, 0.0], dtype=float)
    p_lateral = rotation @ np.array([0.0, 1.0], dtype=float)
    angle_deg = math.degrees(math.atan2(rotation[1, 0], rotation[0, 0]))
    guard_margin_after_m = guard_line_x_m - float(base_samples[:, 0].max())
    no_scale_lengths = {
        "local_chord_m": float(np.linalg.norm(local_xy[2] - local_xy[0])),
        "transformed_chord_m": float(np.linalg.norm(predicted[2] - predicted[0])),
        "scale_ratio": float(np.linalg.norm(predicted[2] - predicted[0]) / np.linalg.norm(local_xy[2] - local_xy[0])),
    }
    return {
        "version": 1,
        "status": status,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_points_json": str(points_path),
        "policy": {
            "shape": "step4f_cycloid",
            "paper_formula": "x=0.015(0.1t-sin(0.1t)), y=0.015(1-cos(0.1t)), duration=60s",
            "no_scale": True,
            "fit": "2D rigid rotation+translation least-squares over start/mid/end hints",
            "hint_points_are_not_path_constraints": True,
            "x_guard_margin_m": margin_m,
            "max_x_minus_shift_m": max_shift_m,
        },
        "basis": {
            "origin_xy_m": [float(v) for v in translation_shifted],
            "u_along_xy": [float(v) for v in u_along],
            "p_lateral_xy": [float(v) for v in p_lateral],
            "rotation_deg": float(angle_deg),
            "rotation_matrix": [[float(v) for v in row] for row in rotation],
            "translation_pre_shift_xy_m": [float(v) for v in translation],
            "x_minus_shift_m": float(applied_shift_m),
        },
        "guard": {
            "taught_x_plus_boundary_m": taught_x_plus_boundary_m,
            "guard_line_x_m": float(guard_line_x_m),
            "required_margin_m": margin_m,
            "path_max_x_m": float(base_samples[:, 0].max()),
            "guard_margin_after_m": float(guard_margin_after_m),
            "passed": bool(status == "ok" and guard_margin_after_m >= -1e-12),
        },
        "envelope": {
            "base_x_m": stats(base_samples[:, 0]),
            "base_y_m": stats(base_samples[:, 1]),
            "local_x_m": stats(local_samples[:, 0]),
            "local_y_m": stats(local_samples[:, 1]),
        },
        "no_scale_check": no_scale_lengths,
        "points": {
            label: {
                "t_s": float(t_s),
                "taught_base_xyz_m": [float(taught_xy[idx, 0]), float(taught_xy[idx, 1]), float(taught_z[idx])],
                "paper_local_xy_m": [float(v) for v in local_xy[idx]],
                "predicted_base_xy_m": [float(v) for v in predicted[idx]],
                "residual_taught_minus_predicted_mm": {
                    "base_x": float(residual_mm[idx, 0]),
                    "base_y": float(residual_mm[idx, 1]),
                    "base_xy_norm": float(residual_norm_mm[idx]),
                },
            }
            for idx, (label, t_s) in enumerate(zip(labels, (0.0, 30.0, 60.0)))
        },
    }


def write_preview(payload: dict[str, Any]) -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    (ASSET_DIR / "step4f_safe_frame_metrics.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    rotation = np.array(payload["basis"]["rotation_matrix"], dtype=float)
    translation = np.array(payload["basis"]["origin_xy_m"], dtype=float)
    samples_t = np.linspace(0.0, 60.0, 601)
    local_samples = np.array([paper_cycloid_xy(float(t_s)) for t_s in samples_t], dtype=float)
    base_samples = transform_points(local_samples, rotation, translation)
    taught = np.array([payload["points"][label]["taught_base_xyz_m"][:2] for label in ["start", "mid", "end"]])
    predicted = np.array([payload["points"][label]["predicted_base_xy_m"] for label in ["start", "mid", "end"]])

    fig, axes = plt.subplots(1, 2, figsize=(12, 5.5), constrained_layout=True)
    axes[0].plot(local_samples[:, 0] * 1000.0, local_samples[:, 1] * 1000.0, lw=2.0, color="#0f766e")
    axes[0].scatter([0.0], [0.0], color="#111827", label="start")
    axes[0].scatter([local_samples[-1, 0] * 1000.0], [local_samples[-1, 1] * 1000.0], color="#b91c1c", label="end")
    axes[0].set_title("Paper local cycloid, no scale")
    axes[0].set_xlabel("local x (mm)")
    axes[0].set_ylabel("local y (mm)")
    axes[0].axis("equal")
    axes[0].grid(True, alpha=0.25)
    axes[0].legend()

    axes[1].plot(base_samples[:, 0] * 1000.0, base_samples[:, 1] * 1000.0, lw=2.0, color="#0f766e", label="transformed path")
    axes[1].scatter(taught[:, 0] * 1000.0, taught[:, 1] * 1000.0, color="#2563eb", label="taught hints")
    axes[1].scatter(predicted[:, 0] * 1000.0, predicted[:, 1] * 1000.0, facecolors="none", edgecolors="#c2410c", label="predicted t=0/30/60")
    axes[1].axvline(payload["guard"]["guard_line_x_m"] * 1000.0, color="#b91c1c", ls="--", label="X+ guard")
    axes[1].set_title("UR base frame with X+ guard")
    axes[1].set_xlabel("base X (mm)")
    axes[1].set_ylabel("base Y (mm)")
    axes[1].axis("equal")
    axes[1].grid(True, alpha=0.25)
    axes[1].legend(fontsize=8)
    png = ASSET_DIR / "step4f_safe_frame_preview.png"
    fig.savefig(png, dpi=180)
    plt.close(fig)

    rows = "\n".join(
        f"<tr><td>{label}</td><td>{payload['points'][label]['residual_taught_minus_predicted_mm']['base_x']:.3f}</td>"
        f"<td>{payload['points'][label]['residual_taught_minus_predicted_mm']['base_y']:.3f}</td>"
        f"<td>{payload['points'][label]['residual_taught_minus_predicted_mm']['base_xy_norm']:.3f}</td></tr>"
        for label in ["start", "mid", "end"]
    )
    html = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Step4f Safe Frame Preview</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#17202a}} img{{max-width:100%;border:1px solid #d8e0e7;border-radius:8px}} table{{border-collapse:collapse}} td,th{{border-bottom:1px solid #d8e0e7;padding:6px 10px;text-align:right}} td:first-child,th:first-child{{text-align:left}}</style>
</head>
<body>
<h1>Step4f Safe Frame Preview</h1>
<p>论文 cycloid 只做 rotation + translation；三点是 hint，不是路径约束；不缩放。</p>
<p>rotation: {payload['basis']['rotation_deg']:.6f} deg; x_minus_shift: {payload['basis']['x_minus_shift_m']*1000.0:.3f} mm; max(base_x): {payload['guard']['path_max_x_m']:.9f} m; guard: {payload['guard']['guard_line_x_m']:.9f} m.</p>
<img src="step4f_safe_frame_preview.png" alt="Step4f safe frame preview">
<h2>Three-point residuals</h2>
<table><tr><th>point</th><th>residual X mm</th><th>residual Y mm</th><th>norm mm</th></tr>{rows}</table>
</body></html>
"""
    (ASSET_DIR / "step4f_safe_frame_preview.html").write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, default=None)
    parser.add_argument("--config-path", type=Path, default=CONFIG_PATH)
    parser.add_argument("--x-guard-margin-mm", type=float, default=2.0)
    parser.add_argument("--max-x-minus-shift-mm", type=float, default=10.0)
    args = parser.parse_args()
    session_dir = args.session_dir or latest_session_dir()
    points_path = session_dir / "points.json"
    payload = make_payload(
        points_path,
        margin_m=args.x_guard_margin_mm / 1000.0,
        max_shift_m=args.max_x_minus_shift_mm / 1000.0,
    )
    args.config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_preview(payload)
    print(json.dumps({"ok": payload["status"] == "ok", "config": str(args.config_path), "status": payload["status"]}, indent=2))
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
