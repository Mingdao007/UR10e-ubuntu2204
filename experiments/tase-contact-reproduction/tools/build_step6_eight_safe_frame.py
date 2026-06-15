#!/usr/bin/env python3
"""Build the Step6 8-shaped no-scale safe frame from five waypoint captures."""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from step6_eight import (
    PATH_DURATION_S,
    PROGRAM_NAME,
    REPORT_DIR,
    STEP5_SAFE_FRAME_PATH,
    STEP6_SAFE_FRAME_PATH,
    Z_CLEARANCE_ABOVE_HIGHEST_WAYPOINT_M,
    eight_local,
    reference_samples,
    waypoint_rows,
    write_waypoint_guide,
)


RUN_ROOT = Path(__file__).resolve().parents[1] / "runs"
SESSION_HINT = Path("/tmp/step6_eight_waypoints_dir.txt")
WAYPOINTS = waypoint_rows()
LABELS = [str(row["label"]) for row in WAYPOINTS]


def latest_session_dir() -> Path:
    if SESSION_HINT.exists():
        hinted = Path(SESSION_HINT.read_text(encoding="utf-8").strip())
        if (hinted / "points.json").is_file():
            return hinted
    matches = sorted(RUN_ROOT.glob("step6_eight_waypoints_*/points.json"))
    if not matches:
        raise SystemExit("No Step6 waypoint session found. Capture all five waypoints first.")
    return matches[-1].parent


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def point_xyz(point: dict[str, Any]) -> tuple[float, float, float]:
    pose = point["rtde"]["actual_TCP_pose"]
    return float(pose[0]), float(pose[1]), float(pose[2])


def rigid_fit_no_scale(local_xy: list[tuple[float, float]], taught_xy: list[tuple[float, float]]) -> dict[str, Any]:
    n = float(len(local_xy))
    local_center = (sum(x for x, _ in local_xy) / n, sum(y for _, y in local_xy) / n)
    taught_center = (sum(x for x, _ in taught_xy) / n, sum(y for _, y in taught_xy) / n)
    cross = 0.0
    dot = 0.0
    for (lx, ly), (tx, ty) in zip(local_xy, taught_xy):
        ax = lx - local_center[0]
        ay = ly - local_center[1]
        bx = tx - taught_center[0]
        by = ty - taught_center[1]
        cross += ax * by - ay * bx
        dot += ax * bx + ay * by
    theta = math.atan2(cross, dot)
    c = math.cos(theta)
    s = math.sin(theta)
    origin = (
        taught_center[0] - (c * local_center[0] - s * local_center[1]),
        taught_center[1] - (s * local_center[0] + c * local_center[1]),
    )
    return {
        "origin_xy_m": [origin[0], origin[1]],
        "u_along_xy": [c, s],
        "p_lateral_xy": [-s, c],
        "rotation_deg": math.degrees(theta),
        "rotation_matrix": [[c, -s], [s, c]],
    }


def transform(frame_basis: dict[str, Any], local_x: float, local_y: float) -> tuple[float, float]:
    origin = frame_basis["origin_xy_m"]
    u = frame_basis["u_along_xy"]
    p = frame_basis["p_lateral_xy"]
    return (
        float(origin[0]) + local_x * float(u[0]) + local_y * float(p[0]),
        float(origin[1]) + local_x * float(u[1]) + local_y * float(p[1]),
    )


def stats(values: list[float]) -> dict[str, float]:
    return {"min": min(values), "max": max(values)}


def make_payload(
    points_path: Path,
    *,
    guard_line_x_m: float | None,
    residual_threshold_mm: float,
) -> dict[str, Any]:
    session = load_json(points_path)
    points = session.get("points", {})
    missing = [label for label in LABELS if label not in points]
    if missing:
        raise RuntimeError(f"missing Step6 waypoint(s): {missing}")

    local_xy = [(float(row["local_x_m"]), float(row["local_y_m"])) for row in WAYPOINTS]
    taught_xyz = [point_xyz(points[str(row["label"])]) for row in WAYPOINTS]
    taught_xy = [(x, y) for x, y, _z in taught_xyz]
    taught_z = [z for _x, _y, z in taught_xyz]
    basis = rigid_fit_no_scale(local_xy, taught_xy)
    fixed_base_z_m = max(taught_z) + Z_CLEARANCE_ABOVE_HIGHEST_WAYPOINT_M

    if guard_line_x_m is None:
        if STEP5_SAFE_FRAME_PATH.is_file():
            guard_line_x_m = float(load_json(STEP5_SAFE_FRAME_PATH)["guard"]["guard_line_x_m"])
        else:
            guard_line_x_m = max(x for x, _ in taught_xy)

    residuals: dict[str, Any] = {}
    residual_norms = []
    for row, captured in zip(WAYPOINTS, taught_xy):
        pred = transform(basis, float(row["local_x_m"]), float(row["local_y_m"]))
        dx = captured[0] - pred[0]
        dy = captured[1] - pred[1]
        norm_mm = math.hypot(dx, dy) * 1000.0
        residual_norms.append(norm_mm)
        residuals[str(row["label"])] = {
            "t_s": float(row["t_s"]),
            "local_xy_m": [float(row["local_x_m"]), float(row["local_y_m"])],
            "captured_base_xy_m": [captured[0], captured[1]],
            "predicted_base_xy_m": [pred[0], pred[1]],
            "residual_base_x_mm": dx * 1000.0,
            "residual_base_y_mm": dy * 1000.0,
            "residual_base_xy_norm_mm": norm_mm,
        }

    frame = {"basis": basis, "fixed_z": {"fixed_base_z_m": fixed_base_z_m}}
    rows = reference_samples(frame)
    max_x = max(row["base_x_m"] for row in rows)
    guard_margin = float(guard_line_x_m) - max_x
    max_residual = max(residual_norms)
    status = "ok" if max_residual <= residual_threshold_mm and guard_margin >= -1e-12 else "blocked"
    return {
        "version": 1,
        "status": status,
        "program": PROGRAM_NAME,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_points_json": str(points_path),
        "policy": {
            "shape": "step6_eight_no_contact",
            "paper_formula": "along=0.04*sin(0.2t), lateral=0.01*sin(0.4t), duration=30s",
            "no_scale": True,
            "fit": "XY rigid rotation+translation over five manually captured XYZ waypoints",
            "xyz_policy": "TCP x/y/z are geometry inputs; force and rx/ry/rz are logged only and ignored by the fit",
            "residual_threshold_mm": residual_threshold_mm,
            "auto_shift_to_pass_guard": False,
        },
        "basis": basis,
        "fixed_z": {
            "source": "highest captured waypoint z plus clearance",
            "highest_waypoint_z_m": max(taught_z),
            "lowest_waypoint_z_m": min(taught_z),
            "z_span_m": max(taught_z) - min(taught_z),
            "clearance_above_highest_waypoint_m": Z_CLEARANCE_ABOVE_HIGHEST_WAYPOINT_M,
            "fixed_base_z_m": fixed_base_z_m,
            "captured_waypoint_z_m": {str(row["label"]): taught_xyz[idx][2] for idx, row in enumerate(WAYPOINTS)},
        },
        "guard": {
            "guard_line_x_m": float(guard_line_x_m),
            "path_max_x_m": max_x,
            "guard_margin_after_m": guard_margin,
            "passed": guard_margin >= -1e-12,
        },
        "residuals": residuals,
        "max_residual_mm": max_residual,
        "residual_gate_passed": max_residual <= residual_threshold_mm,
        "envelope": {
            "base_x_m": stats([row["base_x_m"] for row in rows]),
            "base_y_m": stats([row["base_y_m"] for row in rows]),
            "local_x_m": stats([row["local_x_m"] for row in rows]),
            "local_y_m": stats([row["local_y_m"] for row in rows]),
            "max_reference_speed_m_s": max(math.hypot(row["base_vx_m_s"], row["base_vy_m_s"]) for row in rows),
        },
        "waypoints": WAYPOINTS,
    }


def build_review_html(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    rows = "\n".join(
        "<tr>"
        f"<td><code>{label}</code></td>"
        f"<td>{-row['residual_base_x_mm']:+.3f}</td>"
        f"<td>{-row['residual_base_y_mm']:+.3f}</td>"
        f"<td>{row['residual_base_xy_norm_mm']:.3f}</td>"
        "</tr>"
        for label, row in payload["residuals"].items()
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Step6 8-Shaped Safe Frame Review</title>
  <style>
    :root {{ --ink:#17202a; --muted:#5d6875; --line:#d7dfe8; --bg:#f7f9fb; --panel:#fff; --path:#0b6bcb; --fit:#0f766e; --warn:#b42318; --move:#c2410c; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:var(--bg); }}
    header {{ padding:20px 24px 13px; background:#fff; border-bottom:1px solid var(--line); }}
    h1 {{ margin:0 0 6px; font-size:25px; line-height:1.2; }}
    p {{ margin:0; color:var(--muted); line-height:1.5; }}
    main {{ display:grid; grid-template-columns:minmax(0,1fr) 390px; gap:18px; padding:18px 24px 28px; }}
    section, aside {{ background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    svg {{ display:block; width:100%; height:auto; aspect-ratio:1.25; background:#fbfcfe; }}
    aside {{ padding:16px; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; margin-top:10px; }}
    th,td {{ border-bottom:1px solid var(--line); padding:7px 4px; text-align:right; }}
    th:first-child,td:first-child {{ text-align:left; }}
    .status {{ font-weight:700; color:{'#0f766e' if payload['status'] == 'ok' else '#b42318'}; }}
    .note {{ margin-top:12px; font-size:13px; }}
    @media (max-width:900px) {{ main {{ grid-template-columns:1fr; padding:14px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Step6 8-Shaped Safe Frame Review</h1>
    <p>Status: <span class="status">{payload['status']}</span>. Green points are captured waypoints; blue points are paper-priority no-scale targets; orange arrows show where to move each marked point.</p>
  </header>
  <main>
    <section><svg id="view" viewBox="0 0 960 760" role="img" aria-label="Step6 safe-frame review"></svg></section>
    <aside>
      <p>max residual: <strong>{payload['max_residual_mm']:.3f} mm</strong></p>
      <p>guard margin: <strong>{payload['guard']['guard_margin_after_m'] * 1000.0:.3f} mm</strong></p>
      <p>fixed Z: <strong>{payload['fixed_z']['fixed_base_z_m']:.9f} m</strong></p>
      <table><tr><th>label</th><th>move X mm</th><th>move Y mm</th><th>offset mm</th></tr>{rows}</table>
      <p class="note">move X/Y 是从当前标记点移动到论文 no-scale target 的 base-frame 方向。Fz 和 rx/ry/rz 不参与这个判断。</p>
    </aside>
  </main>
  <script>
    const payload = {data};
    const svg = document.getElementById("view");
    const ns = "http://www.w3.org/2000/svg";
    const W = 960, H = 760, pad = 86;
    function localAt(t) {{
      const phase = 0.2 * t;
      return {{x: 0.04 * Math.sin(phase), y: 0.01 * Math.sin(2 * phase)}};
    }}
    function base(local) {{
      const b = payload.basis;
      return {{
        x: b.origin_xy_m[0] + local.x * b.u_along_xy[0] + local.y * b.p_lateral_xy[0],
        y: b.origin_xy_m[1] + local.x * b.u_along_xy[1] + local.y * b.p_lateral_xy[1]
      }};
    }}
    const samples = [];
    for (let i = 0; i <= 600; i++) samples.push(base(localAt(i * 0.1)));
    for (const row of Object.values(payload.residuals)) samples.push({{x: row.captured_base_xy_m[0], y: row.captured_base_xy_m[1]}});
    samples.push({{x: payload.guard.guard_line_x_m, y: Math.min(...samples.map(p => p.y))}});
    const minX = Math.min(...samples.map(p => p.x)) - 0.025, maxX = Math.max(...samples.map(p => p.x)) + 0.025;
    const minY = Math.min(...samples.map(p => p.y)) - 0.025, maxY = Math.max(...samples.map(p => p.y)) + 0.025;
    const sx = x => pad + (x - minX) / (maxX - minX) * (W - 2 * pad);
    const sy = y => H - pad - (y - minY) / (maxY - minY) * (H - 2 * pad);
    function el(tag, attrs, text) {{
      const n = document.createElementNS(ns, tag);
      for (const [k,v] of Object.entries(attrs || {{}})) n.setAttribute(k, v);
      if (text !== undefined) n.textContent = text;
      svg.appendChild(n); return n;
    }}
    el("defs", {{}}).innerHTML = `
      <marker id="arrow-move" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#c2410c"></path>
      </marker>`;
    const d = samples.slice(0, 601).map((p,i) => (i ? "L" : "M") + sx(p.x).toFixed(2) + "," + sy(p.y).toFixed(2)).join(" ");
    el("path", {{d, fill:"none", stroke:"#0b6bcb", "stroke-width":5, "stroke-linecap":"round", "stroke-linejoin":"round"}});
    el("line", {{x1:sx(payload.guard.guard_line_x_m), y1:sy(minY), x2:sx(payload.guard.guard_line_x_m), y2:sy(maxY), stroke:"#b42318", "stroke-width":2, "stroke-dasharray":"8 6"}});
    el("text", {{x:sx(payload.guard.guard_line_x_m)-124, y:sy(maxY)+24, fill:"#b42318", "font-size":14, "font-weight":700}}, "X guard");
    for (const [label,row] of Object.entries(payload.residuals)) {{
      const cap = {{x:row.captured_base_xy_m[0], y:row.captured_base_xy_m[1]}};
      const pred = {{x:row.predicted_base_xy_m[0], y:row.predicted_base_xy_m[1]}};
      el("circle", {{cx:sx(pred.x), cy:sy(pred.y), r:7, fill:"#0b6bcb", stroke:"#fff", "stroke-width":2}});
      el("circle", {{cx:sx(cap.x), cy:sy(cap.y), r:8, fill:"#0f766e", stroke:"#fff", "stroke-width":2}});
      el("line", {{x1:sx(cap.x), y1:sy(cap.y), x2:sx(pred.x), y2:sy(pred.y), stroke:"#c2410c", "stroke-width":3, "marker-end":"url(#arrow-move)"}});
      el("text", {{x:sx(cap.x)+10, y:sy(cap.y)-9, fill:"#0f766e", "font-size":14, "font-weight":700}}, label);
      el("text", {{x:sx(pred.x)+10, y:sy(pred.y)+18, fill:"#0b6bcb", "font-size":12, "font-weight":700}}, "target");
    }}
  </script>
</body>
</html>
"""


def write_outputs(payload: dict[str, Any], config_path: Path) -> dict[str, str]:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metrics_path = REPORT_DIR / "step6_eight_safe_frame_metrics.json"
    html_path = REPORT_DIR / "step6_eight_safe_frame_review.html"
    guide_path = REPORT_DIR / "step6_eight_waypoint_guide.html"
    metrics_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_path.write_text(build_review_html(payload), encoding="utf-8")
    write_waypoint_guide(guide_path)
    return {"config": str(config_path), "metrics": str(metrics_path), "html": str(html_path), "guide_html": str(guide_path)}


def synthetic_session(path: Path) -> Path:
    origin = (0.438, 0.145)
    theta = math.radians(88.0)
    c = math.cos(theta)
    s = math.sin(theta)
    points = {}
    for row in WAYPOINTS:
        lx = float(row["local_x_m"])
        ly = float(row["local_y_m"])
        x = origin[0] + c * lx - s * ly
        y = origin[1] + s * lx + c * ly
        points[str(row["label"])] = {
            "label": row["label"],
            "expected_local": row,
            "rtde": {"actual_TCP_pose": [x, y, 0.02 + 0.001 * len(points), 0.0, 0.0, 0.0]},
        }
    session = {"points": points, "session_dir": str(path)}
    path.mkdir(parents=True, exist_ok=True)
    out = path / "points.json"
    out.write_text(json.dumps(session, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="step6_safe_frame_selftest_") as tmp:
        tmp_path = Path(tmp)
        points_path = synthetic_session(tmp_path)
        payload = make_payload(points_path, guard_line_x_m=0.4888784335298146, residual_threshold_mm=1.0)
        assert payload["status"] == "ok"
        assert payload["max_residual_mm"] < 1e-9
        html = build_review_html(payload)
        assert "Step6 8-Shaped Safe Frame Review" in html
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, default=None)
    parser.add_argument("--config-path", type=Path, default=STEP6_SAFE_FRAME_PATH)
    parser.add_argument("--guard-line-x-m", type=float, default=None)
    parser.add_argument("--residual-threshold-mm", type=float, default=2.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0

    session_dir = args.session_dir or latest_session_dir()
    points_path = session_dir / "points.json"
    payload = make_payload(
        points_path,
        guard_line_x_m=args.guard_line_x_m,
        residual_threshold_mm=args.residual_threshold_mm,
    )
    outputs = write_outputs(payload, args.config_path)
    print(json.dumps({"ok": payload["status"] == "ok", "status": payload["status"], **outputs}, indent=2))
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Step6 safe-frame build blocked: {exc}")
        raise SystemExit(2)
