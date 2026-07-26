#!/usr/bin/env python3
"""Analyze drag-teach start/end/mid points against Step4f/4g references.

This script is offline. It reads point snapshots captured by
capture_drag_teach_point.py and reports the base X/Y and local along/lateral
offsets needed to align the formula reference to the physically taught points.
It does not contact the robot or mutate TP programs.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = EXPERIMENT_ROOT / "runs"
REPO = EXPERIMENT_ROOT.parents[3]
REFERENCE_METRICS = REPO / "report/assets/step4fg-reference-preview/step4fg_reference_metrics.json"
SESSION_HINT = Path("/tmp/drag_teach_points_dir.txt")


SHAPE_KEYS = {
    "cycloid": "step4f_cycloid",
    "eight": "step4g_eight",
}


def latest_session_dir() -> Path:
    if SESSION_HINT.exists():
        hinted = Path(SESSION_HINT.read_text(encoding="utf-8").strip())
        if (hinted / "points.json").is_file():
            return hinted
    matches = sorted(RUN_ROOT.glob("drag_teach_points_*/points.json"))
    if not matches:
        raise SystemExit("No drag-teach point session found. Capture at least start/end first.")
    return matches[-1].parent


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def add_xy(a: list[float], b: list[float]) -> list[float]:
    return [a[0] + b[0], a[1] + b[1]]


def sub_xy(a: list[float], b: list[float]) -> list[float]:
    return [a[0] - b[0], a[1] - b[1]]


def scale_xy(a: list[float], scale: float) -> list[float]:
    return [a[0] * scale, a[1] * scale]


def norm_mm(delta_m: list[float]) -> float:
    return math.hypot(delta_m[0], delta_m[1]) * 1000.0


def point_pose(point: dict[str, Any]) -> list[float]:
    pose = point["rtde"]["actual_TCP_pose"]
    if len(pose) < 3:
        raise RuntimeError(f"invalid actual_TCP_pose for {point.get('label')}: {pose}")
    return [float(v) for v in pose]


def origin_for_shape(reference: dict[str, Any], shape: str) -> list[float]:
    basis = reference["local_basis"]
    if shape == "cycloid":
        return [float(v) for v in basis["origin_step4f_start_xy_m"]]
    if shape == "eight":
        return [float(v) for v in basis["origin_step4g_mid_xy_m"]]
    raise KeyError(shape)


def sample_for_label(case: dict[str, Any], label: str) -> dict[str, float]:
    target_t = {"start": 0.0, "mid": 30.0, "end": 60.0}[label]
    samples = case["samples"]
    return min(samples, key=lambda row: abs(float(row["t_s"]) - target_t))


def reference_xy(origin: list[float], basis: dict[str, Any], sample: dict[str, float]) -> list[float]:
    u = [float(v) for v in basis["u_along_xy"]]
    p = [float(v) for v in basis["p_lateral_xy"]]
    along_m = float(sample["along_mm"]) / 1000.0
    lateral_m = float(sample["lateral_mm"]) / 1000.0
    return add_xy(add_xy(origin, scale_xy(u, along_m)), scale_xy(p, lateral_m))


def local_from_xy(xy: list[float], origin: list[float], basis: dict[str, Any]) -> dict[str, float]:
    delta = sub_xy(xy, origin)
    u = [float(v) for v in basis["u_along_xy"]]
    p = [float(v) for v in basis["p_lateral_xy"]]
    return {
        "along_mm": dot(delta, u) * 1000.0,
        "lateral_mm": dot(delta, p) * 1000.0,
    }


def analyze_shape(
    points: dict[str, Any],
    reference: dict[str, Any],
    shape: str,
    *,
    third_point_threshold_mm: float,
) -> dict[str, Any]:
    basis = reference["local_basis"]
    case = reference["cases"][SHAPE_KEYS[shape]]
    origin = origin_for_shape(reference, shape)
    labels = [label for label in ["start", "mid", "end"] if label in points]
    point_rows: dict[str, Any] = {}
    for label in labels:
        pose = point_pose(points[label])
        captured_xy = [pose[0], pose[1]]
        sample = sample_for_label(case, label)
        ref_xy = reference_xy(origin, basis, sample)
        delta = sub_xy(captured_xy, ref_xy)
        captured_local = local_from_xy(captured_xy, origin, basis)
        point_rows[label] = {
            "captured": {
                "base_x_m": pose[0],
                "base_y_m": pose[1],
                "base_z_m": pose[2],
                "local_along_mm": captured_local["along_mm"],
                "local_lateral_mm": captured_local["lateral_mm"],
            },
            "reference": {
                "t_s": float(sample["t_s"]),
                "base_x_m": ref_xy[0],
                "base_y_m": ref_xy[1],
                "local_along_mm": float(sample["along_mm"]),
                "local_lateral_mm": float(sample["lateral_mm"]),
            },
            "delta_captured_minus_reference": {
                "base_x_mm": delta[0] * 1000.0,
                "base_y_mm": delta[1] * 1000.0,
                "base_xy_norm_mm": norm_mm(delta),
                "local_along_mm": captured_local["along_mm"] - float(sample["along_mm"]),
                "local_lateral_mm": captured_local["lateral_mm"] - float(sample["lateral_mm"]),
                "local_norm_mm": math.hypot(
                    captured_local["along_mm"] - float(sample["along_mm"]),
                    captured_local["lateral_mm"] - float(sample["lateral_mm"]),
                ),
            },
        }

    start_delta = None
    translation_only: dict[str, Any] = {}
    if "start" in point_rows:
        d = point_rows["start"]["delta_captured_minus_reference"]
        start_delta = [float(d["base_x_mm"]) / 1000.0, float(d["base_y_mm"]) / 1000.0]
        for label, row in point_rows.items():
            ref_xy = [row["reference"]["base_x_m"], row["reference"]["base_y_m"]]
            captured_xy = [row["captured"]["base_x_m"], row["captured"]["base_y_m"]]
            predicted_xy = add_xy(ref_xy, start_delta)
            residual = sub_xy(captured_xy, predicted_xy)
            residual_local = {
                "along_mm": dot(residual, [float(v) for v in basis["u_along_xy"]]) * 1000.0,
                "lateral_mm": dot(residual, [float(v) for v in basis["p_lateral_xy"]]) * 1000.0,
            }
            translation_only[label] = {
                "predicted_base_x_m": predicted_xy[0],
                "predicted_base_y_m": predicted_xy[1],
                "residual_base_x_mm": residual[0] * 1000.0,
                "residual_base_y_mm": residual[1] * 1000.0,
                "residual_base_xy_norm_mm": norm_mm(residual),
                "residual_local_along_mm": residual_local["along_mm"],
                "residual_local_lateral_mm": residual_local["lateral_mm"],
                "residual_local_norm_mm": math.hypot(residual_local["along_mm"], residual_local["lateral_mm"]),
            }

    end_residual = translation_only.get("end", {}).get("residual_base_xy_norm_mm")
    mid_residual = translation_only.get("mid", {}).get("residual_base_xy_norm_mm")
    third_point_recommended = False
    third_point_reason = "start/end 已捕获；mid 可选。"
    if "mid" not in points and end_residual is not None and float(end_residual) > third_point_threshold_mm:
        third_point_recommended = True
        third_point_reason = (
            f"start-only 平移后 end residual 为 {float(end_residual):.3f} mm，"
            f"超过 {third_point_threshold_mm:.3f} mm；建议再示教 mid 点判断旋转/曲率误差。"
        )
    elif "mid" in points and mid_residual is not None and float(mid_residual) > third_point_threshold_mm:
        third_point_reason = (
            f"mid residual 为 {float(mid_residual):.3f} mm，说明不能只靠整体平移；"
            "后续应先讨论 anchor/旋转修正。"
        )

    return {
        "shape": shape,
        "case_key": SHAPE_KEYS[shape],
        "reference_origin_base_xy_m": origin,
        "basis": {
            "u_along_xy": basis["u_along_xy"],
            "p_lateral_xy": basis["p_lateral_xy"],
        },
        "points": point_rows,
        "translation_from_start": None
        if start_delta is None
        else {
            "base_x_mm": start_delta[0] * 1000.0,
            "base_y_mm": start_delta[1] * 1000.0,
            "base_xy_norm_mm": norm_mm(start_delta),
        },
        "translation_only_residuals": translation_only,
        "third_point_recommended": third_point_recommended,
        "third_point_reason": third_point_reason,
    }


def write_report(session_dir: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Drag-teach point alignment",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- session: `{payload['session_dir']}`",
        f"- points: `{', '.join(payload['captured_labels'])}`",
        f"- threshold: `{payload['third_point_threshold_mm']:.3f} mm`",
        "",
        "## 结论入口",
        "",
        "这个报告只用你物理示教的点做校正入口，不再要求你在 HTML 上点 marker。",
        "先看 `start` 的 base X/Y 偏差；如果 `end` 在 start-only 平移后 residual 仍然超阈值，再补 `mid`。",
        "",
    ]
    for shape, result in payload["shapes"].items():
        lines.extend(
            [
                f"## {shape}",
                "",
                "| point | Δbase X mm | Δbase Y mm | ΔXY norm mm | Δlocal along mm | Δlocal lateral mm |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for label in ["start", "mid", "end"]:
            if label not in result["points"]:
                continue
            d = result["points"][label]["delta_captured_minus_reference"]
            lines.append(
                f"| `{label}` | {d['base_x_mm']:.3f} | {d['base_y_mm']:.3f} | "
                f"{d['base_xy_norm_mm']:.3f} | {d['local_along_mm']:.3f} | {d['local_lateral_mm']:.3f} |"
            )
        translation = result["translation_from_start"]
        if translation:
            lines.extend(
                [
                    "",
                    f"建议先把 `{shape}` 公式 anchor 整体平移：base X `{translation['base_x_mm']:+.3f} mm`，"
                    f"base Y `{translation['base_y_mm']:+.3f} mm`。",
                    "",
                    "| point | start-only 后 residual X mm | residual Y mm | residual norm mm | residual along mm | residual lateral mm |",
                    "|---|---:|---:|---:|---:|---:|",
                ]
            )
            for label in ["start", "mid", "end"]:
                if label not in result["translation_only_residuals"]:
                    continue
                r = result["translation_only_residuals"][label]
                lines.append(
                    f"| `{label}` | {r['residual_base_x_mm']:.3f} | {r['residual_base_y_mm']:.3f} | "
                    f"{r['residual_base_xy_norm_mm']:.3f} | {r['residual_local_along_mm']:.3f} | "
                    f"{r['residual_local_lateral_mm']:.3f} |"
                )
        lines.extend(["", f"第三点判断：{result['third_point_reason']}", ""])

    (session_dir / "drag_teach_point_alignment.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_html(session_dir: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False)
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Drag-teach Point Alignment</title>
  <style>
    :root {{ --ink:#182027; --muted:#5f6d78; --line:#d8e0e7; --bg:#f7f9fb; --panel:#fff; --accent:#0f766e; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:var(--bg); }}
    header {{ padding:22px 26px 14px; background:var(--panel); border-bottom:1px solid var(--line); }}
    h1 {{ margin:0 0 8px; font-size:24px; line-height:1.2; }}
    p {{ margin:0; color:var(--muted); line-height:1.55; max-width:1000px; }}
    main {{ display:grid; grid-template-columns:minmax(0,1fr) 380px; gap:16px; padding:18px 24px 26px; }}
    canvas {{ width:100%; height:68vh; min-height:480px; background:#fff; border:1px solid var(--line); border-radius:8px; display:block; }}
    aside {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; align-self:start; }}
    button {{ border:1px solid var(--line); background:#fff; padding:7px 9px; border-radius:6px; cursor:pointer; }}
    button.active {{ border-color:var(--accent); color:#fff; background:var(--accent); }}
    table {{ width:100%; border-collapse:collapse; font-size:12px; margin-top:10px; }}
    th,td {{ border-bottom:1px solid var(--line); padding:6px 4px; text-align:right; }}
    th:first-child,td:first-child {{ text-align:left; }}
    .controls {{ display:flex; gap:8px; flex-wrap:wrap; margin:8px 0 10px; }}
    .note {{ font-size:13px; color:var(--muted); line-height:1.5; margin-top:10px; }}
    @media (max-width:900px) {{ main {{ grid-template-columns:1fr; padding:14px; }} canvas {{ height:56vh; min-height:360px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Drag-teach Point Alignment</h1>
    <p>公式 reference 和示教点的只读复核页。正式校正看右侧 base X/Y 偏差；这个页面不做手工 marker。</p>
  </header>
  <main>
    <canvas id="view" width="1200" height="760"></canvas>
    <aside>
      <div class="controls" id="shapeButtons"></div>
      <div id="summary"></div>
      <p class="note">拖动画布平移，滚轮缩放。虚线是公式 reference，实点是示教点，箭头为 captured-reference 偏差。</p>
    </aside>
  </main>
  <script>
    const payload = {data};
    const canvas = document.getElementById('view');
    const ctx = canvas.getContext('2d');
    const buttons = document.getElementById('shapeButtons');
    const summary = document.getElementById('summary');
    let shape = Object.keys(payload.shapes)[0];
    let scale = 5.5;
    let pan = {{x: canvas.width / 2, y: canvas.height / 2}};
    let drag = null;
    const colors = {{reference:'#7a8793', captured:'#0f766e', delta:'#c2410c', axis:'#9aa6b2'}};
    for (const key of Object.keys(payload.shapes)) {{
      const btn = document.createElement('button');
      btn.textContent = key;
      btn.onclick = () => {{ shape = key; draw(); }};
      btn.dataset.shape = key;
      buttons.appendChild(btn);
    }}
    function mmPoint(row, kind) {{
      const x = (row[kind].base_x_m - result.reference_origin_base_xy_m[0]) * 1000;
      const y = (row[kind].base_y_m - result.reference_origin_base_xy_m[1]) * 1000;
      return {{x, y}};
    }}
    function toCanvas(pt) {{ return {{x: pan.x + pt.x * scale, y: pan.y - pt.y * scale}}; }}
    function line(a,b,color,width=2,dash=[]) {{
      const ca=toCanvas(a), cb=toCanvas(b);
      ctx.save(); ctx.strokeStyle=color; ctx.lineWidth=width; ctx.setLineDash(dash);
      ctx.beginPath(); ctx.moveTo(ca.x, ca.y); ctx.lineTo(cb.x, cb.y); ctx.stroke(); ctx.restore();
    }}
    function circle(pt,r,color,label) {{
      const c=toCanvas(pt);
      ctx.fillStyle=color; ctx.beginPath(); ctx.arc(c.x,c.y,r,0,Math.PI*2); ctx.fill();
      ctx.fillStyle='#17202a'; ctx.font='13px system-ui'; ctx.fillText(label,c.x+8,c.y-8);
    }}
    function arrow(a,b,color) {{
      line(a,b,color,2,[]);
      const ca=toCanvas(a), cb=toCanvas(b);
      const angle=Math.atan2(cb.y-ca.y, cb.x-ca.x), len=9;
      ctx.fillStyle=color; ctx.beginPath(); ctx.moveTo(cb.x,cb.y);
      ctx.lineTo(cb.x-len*Math.cos(angle-0.45), cb.y-len*Math.sin(angle-0.45));
      ctx.lineTo(cb.x-len*Math.cos(angle+0.45), cb.y-len*Math.sin(angle+0.45));
      ctx.closePath(); ctx.fill();
    }}
    function tableFor(result) {{
      let rows = '<table><tr><th>point</th><th>ΔX</th><th>ΔY</th><th>norm</th></tr>';
      for (const label of ['start','mid','end']) {{
        if (!result.points[label]) continue;
        const d=result.points[label].delta_captured_minus_reference;
        rows += `<tr><td>${{label}}</td><td>${{d.base_x_mm.toFixed(3)}}</td><td>${{d.base_y_mm.toFixed(3)}}</td><td>${{d.base_xy_norm_mm.toFixed(3)}}</td></tr>`;
      }}
      rows += '</table>';
      const t=result.translation_from_start;
      if (t) rows += `<p class="note">先平移 anchor：base X ${{t.base_x_mm>=0?'+':''}}${{t.base_x_mm.toFixed(3)}} mm，base Y ${{t.base_y_mm>=0?'+':''}}${{t.base_y_mm.toFixed(3)}} mm。</p>`;
      rows += `<p class="note">${{result.third_point_reason}}</p>`;
      return rows;
    }}
    let result;
    function draw() {{
      result = payload.shapes[shape];
      document.querySelectorAll('button[data-shape]').forEach(b => b.classList.toggle('active', b.dataset.shape === shape));
      ctx.clearRect(0,0,canvas.width,canvas.height);
      ctx.fillStyle='#fff'; ctx.fillRect(0,0,canvas.width,canvas.height);
      line({{x:-80,y:0}}, {{x:130,y:0}}, colors.axis, 1, [5,5]);
      line({{x:0,y:-45}}, {{x:0,y:45}}, colors.axis, 1, [5,5]);
      const labels = ['start','mid','end'].filter(l => result.points[l]);
      for (let i=1; i<labels.length; i++) {{
        line(mmPoint(result.points[labels[i-1]], 'reference'), mmPoint(result.points[labels[i]], 'reference'), colors.reference, 2, [8,6]);
      }}
      for (const label of labels) {{
        const row=result.points[label];
        const ref=mmPoint(row,'reference'), cap=mmPoint(row,'captured');
        circle(ref,5,colors.reference,`${{label}} ref`);
        circle(cap,7,colors.captured,`${{label}} taught`);
        arrow(ref, cap, colors.delta);
      }}
      summary.innerHTML = `<h2>${{shape}}</h2>` + tableFor(result);
    }}
    canvas.addEventListener('pointerdown', e => {{ drag={{x:e.clientX,y:e.clientY,panX:pan.x,panY:pan.y}}; canvas.setPointerCapture(e.pointerId); }});
    canvas.addEventListener('pointermove', e => {{ if (!drag) return; pan.x=drag.panX+e.clientX-drag.x; pan.y=drag.panY+e.clientY-drag.y; draw(); }});
    canvas.addEventListener('pointerup', () => {{ drag=null; }});
    canvas.addEventListener('wheel', e => {{ e.preventDefault(); scale *= e.deltaY < 0 ? 1.12 : 0.89; scale = Math.max(1.5, Math.min(28, scale)); draw(); }}, {{passive:false}});
    draw();
  </script>
</body>
</html>
"""
    (session_dir / "drag_teach_point_review.html").write_text(page, encoding="utf-8")


def synthetic_points(reference: dict[str, Any], shape: str, dx_mm: float, dy_mm: float) -> dict[str, Any]:
    basis = reference["local_basis"]
    case = reference["cases"][SHAPE_KEYS[shape]]
    origin = origin_for_shape(reference, shape)
    points = {}
    for label in ["start", "mid", "end"]:
        sample = sample_for_label(case, label)
        xy = reference_xy(origin, basis, sample)
        xy = [xy[0] + dx_mm / 1000.0, xy[1] + dy_mm / 1000.0]
        points[label] = {
            "label": label,
            "rtde": {"actual_TCP_pose": [xy[0], xy[1], 0.01, 0.0, 0.0, 0.0]},
        }
    return points


def self_test() -> None:
    reference = load_json(REFERENCE_METRICS)
    payload = {
        "generated_at": "self-test",
        "session_dir": "self-test",
        "source_points_json": "self-test",
        "reference_metrics": str(REFERENCE_METRICS),
        "captured_labels": ["end", "mid", "start"],
        "third_point_threshold_mm": 2.0,
        "shapes": {},
    }
    for shape in ["cycloid", "eight"]:
        points = synthetic_points(reference, shape, dx_mm=1.25, dy_mm=-2.5)
        result = analyze_shape(points, reference, shape, third_point_threshold_mm=2.0)
        t = result["translation_from_start"]
        assert t is not None
        assert abs(t["base_x_mm"] - 1.25) < 1e-9
        assert abs(t["base_y_mm"] + 2.5) < 1e-9
        assert result["translation_only_residuals"]["end"]["residual_base_xy_norm_mm"] < 1e-9
        payload["shapes"][shape] = result
    with tempfile.TemporaryDirectory(prefix="drag_teach_point_selftest_") as tmp:
        tmp_path = Path(tmp)
        write_report(tmp_path, payload)
        write_html(tmp_path, payload)
        assert (tmp_path / "drag_teach_point_alignment.md").is_file()
        assert (tmp_path / "drag_teach_point_review.html").is_file()
    print("self-test ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, default=None, help="Directory containing points.json.")
    parser.add_argument("--shape", choices=["cycloid", "eight", "both"], default="both")
    parser.add_argument("--reference-metrics", type=Path, default=REFERENCE_METRICS)
    parser.add_argument("--third-point-threshold-mm", type=float, default=2.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0

    session_dir = args.session_dir or latest_session_dir()
    points_path = session_dir / "points.json"
    if not points_path.is_file():
        raise SystemExit(f"missing points.json: {points_path}")
    session = load_json(points_path)
    points = session.get("points", {})
    missing = [label for label in ["start", "end"] if label not in points]
    if missing:
        raise SystemExit(f"missing required point(s): {', '.join(missing)}")

    reference = load_json(args.reference_metrics)
    shapes = ["cycloid", "eight"] if args.shape == "both" else [args.shape]
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "session_dir": str(session_dir),
        "source_points_json": str(points_path),
        "reference_metrics": str(args.reference_metrics),
        "captured_labels": sorted(points.keys()),
        "third_point_threshold_mm": args.third_point_threshold_mm,
        "shapes": {
            shape: analyze_shape(points, reference, shape, third_point_threshold_mm=args.third_point_threshold_mm)
            for shape in shapes
        },
    }
    out_json = session_dir / "drag_teach_point_alignment.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_report(session_dir, payload)
    write_html(session_dir, payload)
    print(json.dumps({"ok": True, "alignment_json": str(out_json), "session_dir": str(session_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
