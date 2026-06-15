#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import socket
import sys
import time
from pathlib import Path
from typing import Any

sys.path.append("/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts")
from _ur_common import RTDEClient, dashboard_exchange, read_rtde_once  # noqa: E402


FIELDS = [
    "actual_TCP_pose",
    "target_TCP_pose",
    "actual_TCP_speed",
    "actual_TCP_force",
    "actual_q",
    "target_q",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    "speed_scaling",
]


def vector_norm(values: list[float]) -> float:
    return math.sqrt(sum(v * v for v in values))


def make_script(z_lift_m: float, y_line_m: float, speed_m_s: float, accel_m_s2: float) -> str:
    return f"""def codex_demo_1_1_no_contact_dry_run():
  textmsg("codex demo 1.1 no-contact dry run start")
  p0 = get_actual_tcp_pose()
  p_safe = p[p0[0], p0[1], p0[2] + {z_lift_m:.6f}, p0[3], p0[4], p0[5]]
  p_line = p[p0[0], p0[1] + {y_line_m:.6f}, p0[2] + {z_lift_m:.6f}, p0[3], p0[4], p0[5]]
  movel(p_safe, a={accel_m_s2:.6f}, v={speed_m_s:.6f}, r=0.0)
  movel(p_line, a={accel_m_s2:.6f}, v={speed_m_s:.6f}, r=0.0)
  movel(p_safe, a={accel_m_s2:.6f}, v={speed_m_s:.6f}, r=0.0)
  movel(p0, a={accel_m_s2:.6f}, v={speed_m_s:.6f}, r=0.0)
  stopl(0.5)
  textmsg("codex demo 1.1 no-contact dry run done")
end
codex_demo_1_1_no_contact_dry_run()
"""


def send_client_script(host: str, script: str, port: int = 30002, timeout: float = 2.0) -> None:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(script.encode("utf-8"))


def flatten_sample(t_s: float, values: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"t_s": t_s}
    for prefix, key in [
        ("actual_tcp_pose", "actual_TCP_pose"),
        ("target_tcp_pose", "target_TCP_pose"),
        ("actual_tcp_speed", "actual_TCP_speed"),
        ("actual_tcp_force", "actual_TCP_force"),
        ("actual_q", "actual_q"),
        ("target_q", "target_q"),
    ]:
        arr = values[key]
        for idx, value in enumerate(arr):
            row[f"{prefix}_{idx}"] = value
    for key in ["runtime_state", "robot_mode", "safety_mode", "speed_scaling"]:
        row[key] = values[key]
    return row


def summarize(rows: list[dict[str, Any]], script_sent: bool, script_error: str | None, args: argparse.Namespace, script: str) -> dict[str, Any]:
    first = rows[0]
    last = rows[-1]
    dx = [r["actual_tcp_pose_0"] - first["actual_tcp_pose_0"] for r in rows]
    dy = [r["actual_tcp_pose_1"] - first["actual_tcp_pose_1"] for r in rows]
    dz = [r["actual_tcp_pose_2"] - first["actual_tcp_pose_2"] for r in rows]
    transl_delta = [math.sqrt(x * x + y * y + z * z) for x, y, z in zip(dx, dy, dz)]
    speed_norm = [vector_norm([r[f"actual_tcp_speed_{i}"] for i in range(3)]) for r in rows]
    force_norm = [vector_norm([r[f"actual_tcp_force_{i}"] for i in range(3)]) for r in rows]
    max_translation = max(transl_delta)
    expected_translation = max(abs(args.z_lift_m), math.sqrt(args.z_lift_m * args.z_lift_m + args.y_line_m * args.y_line_m))
    moved_as_expected = max_translation >= 0.5 * expected_translation
    return {
        "ok": script_sent and script_error is None and moved_as_expected,
        "host": args.host,
        "samples": len(rows),
        "duration_first_last_s": last["t_s"] - first["t_s"],
        "script_sent": script_sent,
        "script_error": script_error,
        "script_port": args.script_port,
        "motion_plan": {
            "z_lift_m": args.z_lift_m,
            "y_line_m": args.y_line_m,
            "speed_m_s": args.speed_m_s,
            "accel_m_s2": args.accel_m_s2,
            "description": "lift +Z, short +Y line at lifted height, return, fixed TCP orientation",
        },
        "preflight_thresholds": {
            "min_start_z_m": args.min_start_z_m,
            "max_start_tcp_speed_m_s": args.max_start_tcp_speed_m_s,
        },
        "start_actual_tcp_pose": [first[f"actual_tcp_pose_{i}"] for i in range(6)],
        "end_actual_tcp_pose": [last[f"actual_tcp_pose_{i}"] for i in range(6)],
        "max_delta": {
            "dx_m": max(dx, key=abs),
            "dy_m": max(dy, key=abs),
            "dz_m": max(dz, key=abs),
            "translation_norm_m": max_translation,
        },
        "expected_translation_norm_m": expected_translation,
        "moved_as_expected": moved_as_expected,
        "actual_tcp_speed_norm_m_s": {
            "max": max(speed_norm),
            "mean": sum(speed_norm) / len(speed_norm),
        },
        "actual_tcp_force_norm_N": {
            "max": max(force_norm),
            "mean": sum(force_norm) / len(force_norm),
        },
        "unique_runtime_state": sorted({int(r["runtime_state"]) for r in rows}),
        "unique_robot_mode": sorted({int(r["robot_mode"]) for r in rows}),
        "unique_safety_mode": sorted({int(r["safety_mode"]) for r in rows}),
        "script": script,
        "safety_boundary": [
            "no TCP/payload writes",
            "no zero_ftsensor or OnRobot zero/bias",
            "no URCap setting change",
            "secondary URScript is not saved to the controller",
            f"client script sent to port {args.script_port}",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.1.18")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--prefix", default="demo_1_1_no_contact_dry_run")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--hz", type=float, default=125.0)
    parser.add_argument("--pre-delay-s", type=float, default=0.5)
    parser.add_argument("--z-lift-m", type=float, default=0.005)
    parser.add_argument("--y-line-m", type=float, default=0.005)
    parser.add_argument("--speed-m-s", type=float, default=0.010)
    parser.add_argument("--accel-m-s2", type=float, default=0.030)
    parser.add_argument("--min-start-z-m", type=float, default=0.20)
    parser.add_argument("--max-start-tcp-speed-m-s", type=float, default=0.001)
    parser.add_argument("--script-port", type=int, default=30002)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"{args.prefix}_{stamp}.csv"
    summary_path = output_dir / f"{args.prefix}_{stamp}_summary.json"
    script = make_script(args.z_lift_m, args.y_line_m, args.speed_m_s, args.accel_m_s2)

    dashboard = dashboard_exchange(
        args.host,
        ["is in remote control", "safetymode", "robotmode", "running", "programState"],
    )
    if "NORMAL" not in dashboard.get("safetymode", ""):
        raise SystemExit(f"Dashboard safety not NORMAL: {dashboard}")
    if "RUNNING" not in dashboard.get("robotmode", ""):
        raise SystemExit(f"Robot mode not RUNNING: {dashboard}")

    start_state = read_rtde_once(args.host, ["actual_TCP_pose", "actual_TCP_speed"], frequency_hz=10.0)
    start_pose = start_state["actual_TCP_pose"]
    start_speed = start_state["actual_TCP_speed"]
    if start_pose[2] < args.min_start_z_m:
        raise SystemExit(f"Start TCP z={start_pose[2]:.6f} below min_start_z_m={args.min_start_z_m:.6f}; aborting")
    if vector_norm(start_speed[:3]) > args.max_start_tcp_speed_m_s:
        raise SystemExit(f"Start TCP speed {vector_norm(start_speed[:3]):.6f} exceeds threshold; aborting")

    if args.plan_only:
        print(json.dumps({"ok": True, "dashboard": dashboard, "start_state": start_state, "script": script}, indent=2))
        return

    rows: list[dict[str, Any]] = []
    script_sent = False
    script_error: str | None = None
    t0 = time.perf_counter()
    with RTDEClient(args.host, timeout=3.0) as client:
        client.negotiate()
        recipe_id, type_names = client.setup_outputs(args.hz, FIELDS)
        client.start()
        while True:
            now = time.perf_counter()
            elapsed = now - t0
            if elapsed >= args.pre_delay_s and not script_sent and script_error is None:
                try:
                    send_client_script(args.host, script, port=args.script_port)
                    script_sent = True
                except Exception as exc:
                    script_error = repr(exc)
            if elapsed >= args.seconds:
                break
            values = client.recv_recipe_sample(recipe_id, type_names)
            sample = {field: value for field, value in zip(FIELDS, values)}
            rows.append(flatten_sample(time.perf_counter() - t0, sample))

    if not rows:
        raise SystemExit("No RTDE rows captured")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    summary = summarize(rows, script_sent, script_error, args, script)
    summary["csv_path"] = str(csv_path)
    summary["summary_path"] = str(summary_path)
    summary["pre_dashboard"] = dashboard
    try:
        summary["post_dashboard"] = dashboard_exchange(
            args.host,
            ["is in remote control", "safetymode", "robotmode", "running", "programState"],
        )
    except Exception as exc:
        summary["post_dashboard_error"] = repr(exc)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
