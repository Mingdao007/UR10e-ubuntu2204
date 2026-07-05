#!/usr/bin/env python3
"""Shared Step6 8-shaped geometry helpers."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from tase_protocol_table import resolve_experiment_profile


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REPO = EXPERIMENT_ROOT.parents[3]
CONFIG_DIR = EXPERIMENT_ROOT / "config"
STEP6_TABLE_PATH = CONFIG_DIR / "step6_stage_table.json"
STEP6_SAFE_FRAME_PATH = CONFIG_DIR / "step6_eight_safe_frame.json"
STEP5_SAFE_FRAME_PATH = CONFIG_DIR / "step5_safe_frame.json"
REPORT_DIR = REPO / "report" / "assets" / "step6-eight-waypoints"

PROGRAM_NAME = "step6a_eight_no_contact_v1"
CONTROLLER_DIR = "/programs/andyl/kunwei/step6"
LOCAL_PROGRAM_DIR = EXPERIMENT_ROOT / "programs" / "step6"

_STEP6_PROTOCOL = resolve_experiment_profile("Step6.no_contact_eight")
_STEP6_PARAMS = _STEP6_PROTOCOL["parameters"]
PATH_DURATION_S = float(_STEP6_PARAMS["trajectory_duration_s"])
OMEGA_RAD_S = float(_STEP6_PARAMS["omega_rad_s"])
ALONG_AMPLITUDE_M = float(_STEP6_PARAMS["along_amplitude_m"])
LATERAL_AMPLITUDE_M = float(_STEP6_PARAMS["lateral_amplitude_m"])
DEFAULT_FIXED_BASE_Z_M = 0.029423891
Z_CLEARANCE_ABOVE_HIGHEST_WAYPOINT_M = 0.010
SAMPLE_DT_S = 0.1

WAYPOINTS: list[dict[str, float | str]] = [
    {"label": "center_start", "phase_rad": 0.0},
    {"label": "right_upper", "phase_rad": math.pi / 4.0},
    {"label": "right_lower", "phase_rad": 3.0 * math.pi / 4.0},
    {"label": "left_upper", "phase_rad": 5.0 * math.pi / 4.0},
    {"label": "left_lower", "phase_rad": 7.0 * math.pi / 4.0},
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def eight_local(t_s: float) -> dict[str, float]:
    t = min(max(float(t_s), 0.0), PATH_DURATION_S)
    phase = OMEGA_RAD_S * t
    return {
        "t_s": t,
        "phase_rad": phase,
        "local_x_m": ALONG_AMPLITUDE_M * math.sin(phase),
        "local_y_m": LATERAL_AMPLITUDE_M * math.sin(2.0 * phase),
        "local_vx_m_s": ALONG_AMPLITUDE_M * OMEGA_RAD_S * math.cos(phase),
        "local_vy_m_s": LATERAL_AMPLITUDE_M * 2.0 * OMEGA_RAD_S * math.cos(2.0 * phase),
    }


def waypoint_rows() -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    for item in WAYPOINTS:
        phase = float(item["phase_rad"])
        t_s = phase / OMEGA_RAD_S
        local = eight_local(t_s)
        rows.append(
            {
                "label": str(item["label"]),
                "phase_rad": phase,
                "t_s": t_s,
                "local_x_m": local["local_x_m"],
                "local_y_m": local["local_y_m"],
            }
        )
    return rows


def xy_from_basis(
    origin: tuple[float, float],
    u_along: tuple[float, float],
    p_lateral: tuple[float, float],
    along_m: float,
    lateral_m: float,
) -> tuple[float, float]:
    return (
        origin[0] + along_m * u_along[0] + lateral_m * p_lateral[0],
        origin[1] + along_m * u_along[1] + lateral_m * p_lateral[1],
    )


def transform_local(frame: dict[str, Any], local_x_m: float, local_y_m: float) -> tuple[float, float]:
    basis = frame["basis"]
    origin = tuple(float(v) for v in basis["origin_xy_m"])
    u_along = tuple(float(v) for v in basis["u_along_xy"])
    p_lateral = tuple(float(v) for v in basis["p_lateral_xy"])
    return xy_from_basis(origin, u_along, p_lateral, local_x_m, local_y_m)


def transform_velocity(frame: dict[str, Any], local_vx_m_s: float, local_vy_m_s: float) -> tuple[float, float]:
    basis = frame["basis"]
    u_along = tuple(float(v) for v in basis["u_along_xy"])
    p_lateral = tuple(float(v) for v in basis["p_lateral_xy"])
    return (
        local_vx_m_s * u_along[0] + local_vy_m_s * p_lateral[0],
        local_vx_m_s * u_along[1] + local_vy_m_s * p_lateral[1],
    )


def load_step6_table(path: Path = STEP6_TABLE_PATH) -> dict[str, Any]:
    table = load_json(path)
    if table.get("status") != "active":
        raise RuntimeError(f"Step6 table is not active: {path}")
    return table


def step6_stage(stage_id: str = PROGRAM_NAME, table: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = table or load_step6_table()
    for stage in payload["stages"]:
        if stage.get("id") == stage_id:
            return stage
    raise KeyError(f"unknown Step6 stage id: {stage_id}")


def load_safe_frame(path: Path = STEP6_SAFE_FRAME_PATH) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(
            f"missing Step6 safe frame: {path}. Capture the five Step6 waypoints and run "
            "tools/build_step6_eight_safe_frame.py before generating the TP package."
        )
    frame = load_json(path)
    if frame.get("status") != "ok":
        raise RuntimeError(f"Step6 safe frame is not ok: {path}")
    if not frame.get("policy", {}).get("no_scale"):
        raise RuntimeError(f"Step6 safe frame is not no-scale: {path}")
    if not frame.get("guard", {}).get("passed"):
        raise RuntimeError(f"Step6 safe-frame guard did not pass: {path}")
    return frame


def fixed_base_z(frame: dict[str, Any] | None = None) -> float:
    if frame is None:
        return DEFAULT_FIXED_BASE_Z_M
    return float(frame.get("fixed_z", {}).get("fixed_base_z_m", DEFAULT_FIXED_BASE_Z_M))


def reference_samples(frame: dict[str, Any], dt_s: float = SAMPLE_DT_S) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    sample_count = int(round(PATH_DURATION_S / dt_s))
    z_m = fixed_base_z(frame)
    for idx in range(sample_count + 1):
        t_s = round(idx * dt_s, 10)
        local = eight_local(t_s)
        base_x, base_y = transform_local(frame, local["local_x_m"], local["local_y_m"])
        base_vx, base_vy = transform_velocity(frame, local["local_vx_m_s"], local["local_vy_m_s"])
        rows.append(
            {
                **local,
                "base_x_m": base_x,
                "base_y_m": base_y,
                "base_vx_m_s": base_vx,
                "base_vy_m_s": base_vy,
                "base_z_m": z_m,
            }
        )
    return rows


def guide_payload() -> dict[str, Any]:
    draft_frame = load_json(STEP5_SAFE_FRAME_PATH) if STEP5_SAFE_FRAME_PATH.is_file() else None
    draft_points = []
    if draft_frame:
        for row in waypoint_rows():
            x, y = transform_local(draft_frame, float(row["local_x_m"]), float(row["local_y_m"]))
            draft_points.append({**row, "draft_base_x_m": x, "draft_base_y_m": y})
    return {
        "program": PROGRAM_NAME,
        "formula": "along=0.04*sin(0.2t), lateral=0.01*sin(0.4t), duration=30s",
        "waypoints": waypoint_rows(),
        "draft_points": draft_points,
        "note": "Draft base coordinates use the previous Step5 frame only as a visual reference. The formal Step6 frame comes from five fresh read-only captures.",
    }


def build_waypoint_guide_html(payload: dict[str, Any] | None = None) -> str:
    payload = payload or guide_payload()
    data = json.dumps(payload, separators=(",", ":"))
    rows = "\n".join(
        "<tr>"
        f"<td><code>{row['label']}</code></td>"
        f"<td>{float(row['t_s']):.3f}</td>"
        f"<td>{float(row['phase_rad']):.6f}</td>"
        f"<td>{float(row['local_x_m']) * 1000.0:.3f}</td>"
        f"<td>{float(row['local_y_m']) * 1000.0:.3f}</td>"
        "</tr>"
        for row in payload["waypoints"]
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Step6 8-Shaped Waypoint Guide</title>
  <style>
    :root {{ color-scheme: light; --ink:#17202a; --muted:#5d6875; --line:#d7dfe8; --bg:#f7f9fb; --panel:#fff; --path:#0b6bcb; --point:#0f766e; --accent:#b42318; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:var(--bg); }}
    header {{ padding:22px 26px 14px; background:#fff; border-bottom:1px solid var(--line); }}
    h1 {{ margin:0 0 6px; font-size:26px; line-height:1.2; letter-spacing:0; }}
    p {{ margin:0; color:var(--muted); line-height:1.5; }}
    main {{ display:grid; grid-template-columns:minmax(0,1fr) 390px; gap:18px; padding:18px 24px 28px; }}
    section, aside {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    svg {{ display:block; width:100%; height:auto; aspect-ratio:1.25; background:#fbfcfe; }}
    aside {{ padding:16px; align-self:start; }}
    h2 {{ margin:0 0 10px; font-size:17px; line-height:1.25; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th,td {{ padding:7px 4px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }}
    th:first-child,td:first-child {{ text-align:left; }}
    code {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; }}
    .note {{ margin-top:13px; font-size:13px; }}
    @media (max-width:900px) {{ main {{ grid-template-columns:1fr; padding:14px; }} header {{ padding:18px 16px 12px; }} h1 {{ font-size:22px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Step6 8-Shaped Waypoint Guide</h1>
    <p>五个点用于校正经过点；正式 frame 来自 read-only RTDE capture，不用 HTML 手工点 marker。</p>
  </header>
  <main>
    <section>
      <svg id="view" viewBox="0 0 960 760" role="img" aria-label="Step6 local 8-shaped waypoint guide"></svg>
    </section>
    <aside>
      <h2>采集顺序</h2>
      <table>
        <tr><th>label</th><th>t s</th><th>phase</th><th>along mm</th><th>lat mm</th></tr>
        {rows}
      </table>
      <p class="note">公式：<code>{payload['formula']}</code></p>
      <p class="note">{payload['note']}</p>
    </aside>
  </main>
  <script>
    const payload = {data};
    const svg = document.getElementById("view");
    const ns = "http://www.w3.org/2000/svg";
    const W = 960, H = 760, pad = 92;
    const samples = [];
    for (let i = 0; i <= 600; i++) {{
      const t = i * 0.1;
      const phase = 0.2 * t;
      samples.push({{x: 40 * Math.sin(phase), y: 10 * Math.sin(2 * phase), t}});
    }}
    const xs = samples.map(p => p.x), ys = samples.map(p => p.y);
    const minX = Math.min(...xs) - 12, maxX = Math.max(...xs) + 12;
    const minY = Math.min(...ys) - 12, maxY = Math.max(...ys) + 12;
    const sx = x => pad + (x - minX) / (maxX - minX) * (W - 2 * pad);
    const sy = y => H - pad - (y - minY) / (maxY - minY) * (H - 2 * pad);
    function el(tag, attrs, text) {{
      const node = document.createElementNS(ns, tag);
      for (const [k, v] of Object.entries(attrs || {{}})) node.setAttribute(k, v);
      if (text !== undefined) node.textContent = text;
      svg.appendChild(node);
      return node;
    }}
    for (let x = -40; x <= 40; x += 20) {{
      el("line", {{x1:sx(x), y1:sy(minY), x2:sx(x), y2:sy(maxY), stroke:"#d7dfe8", "stroke-width":1}});
      el("text", {{x:sx(x)-10, y:H-34, fill:"#647184", "font-size":13}}, x);
    }}
    for (let y = -10; y <= 10; y += 10) {{
      el("line", {{x1:sx(minX), y1:sy(y), x2:sx(maxX), y2:sy(y), stroke:"#d7dfe8", "stroke-width":1}});
      el("text", {{x:24, y:sy(y)-5, fill:"#647184", "font-size":13}}, y);
    }}
    el("line", {{x1:sx(minX), y1:sy(0), x2:sx(maxX), y2:sy(0), stroke:"#8793a0", "stroke-width":2}});
    el("line", {{x1:sx(0), y1:sy(minY), x2:sx(0), y2:sy(maxY), stroke:"#8793a0", "stroke-width":2}});
    const d = samples.map((p, i) => (i ? "L" : "M") + sx(p.x).toFixed(2) + "," + sy(p.y).toFixed(2)).join(" ");
    el("path", {{d, fill:"none", stroke:"#0b6bcb", "stroke-width":5, "stroke-linecap":"round", "stroke-linejoin":"round"}});
    for (const row of payload.waypoints) {{
      const x = row.local_x_m * 1000, y = row.local_y_m * 1000;
      el("circle", {{cx:sx(x), cy:sy(y), r:8, fill:"#0f766e", stroke:"#fff", "stroke-width":2}});
      el("text", {{x:sx(x)+10, y:sy(y)-10, fill:"#0f766e", "font-size":15, "font-weight":700}}, row.label);
      el("text", {{x:sx(x)+10, y:sy(y)+9, fill:"#5d6875", "font-size":12}}, "t=" + row.t_s.toFixed(3) + "s");
    }}
    el("text", {{x:W-210, y:H-34, fill:"#17202a", "font-size":14}}, "local units: mm");
  </script>
</body>
</html>
"""


def write_waypoint_guide(path: Path | None = None) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = path or REPORT_DIR / "step6_eight_waypoint_guide.html"
    out.write_text(build_waypoint_guide_html(), encoding="utf-8")
    return out


def main() -> int:
    out = write_waypoint_guide()
    print(json.dumps({"ok": True, "html": str(out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
