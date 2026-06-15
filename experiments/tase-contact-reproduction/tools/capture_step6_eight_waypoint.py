#!/usr/bin/env python3
"""Capture one Step6 8-shaped waypoint with read-only Dashboard/RTDE checks."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from step6_eight import EXPERIMENT_ROOT, waypoint_rows, write_waypoint_guide


RUN_ROOT = EXPERIMENT_ROOT / "runs"
SESSION_HINT = Path("/tmp/step6_eight_waypoints_dir.txt")
UR_REALSETUP_SCRIPTS = Path("/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts")
sys.path.insert(0, str(UR_REALSETUP_SCRIPTS))

from _ur_common import dashboard_exchange, read_rtde_once  # noqa: E402


WAYPOINT_BY_LABEL = {str(row["label"]): row for row in waypoint_rows()}
LABELS = tuple(WAYPOINT_BY_LABEL)
RTDE_FIELDS = [
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_TCP_force",
    "actual_q",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    "speed_scaling",
    "payload",
    "payload_cog",
    "tcp_offset",
]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_session_dir() -> Path:
    if SESSION_HINT.exists():
        hinted = Path(SESSION_HINT.read_text(encoding="utf-8").strip())
        if hinted.exists():
            return hinted
    return RUN_ROOT / f"step6_eight_waypoints_{now_stamp()}"


def vector_norm(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def read_existing(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "program": "step6a_eight_no_contact_v1",
        "shape": "eight",
        "required_labels": list(LABELS),
        "points": {},
        "safety_boundary": [
            "read-only Dashboard commands",
            "read-only RTDE output recipe",
            "no URScript send",
            "no RTDE input writes",
            "no program load/start",
            "no freedrive enable command",
            "no zero_ftsensor",
            "operator manually positions the robot before each snapshot",
        ],
    }


def preflight(robot_host: str, timeout_s: float, max_speed_m_s: float) -> tuple[dict[str, str], dict[str, Any], dict[str, float]]:
    dashboard = dashboard_exchange(
        robot_host,
        ["is in remote control", "safetymode", "robotmode", "running", "programState"],
        timeout=timeout_s,
    )
    if "NORMAL" not in dashboard.get("safetymode", ""):
        raise RuntimeError(f"Dashboard safety is not NORMAL: {dashboard}")
    if "true" in dashboard.get("running", "").lower():
        raise RuntimeError(f"refusing to capture point while a program is running: {dashboard}")

    rtde = read_rtde_once(robot_host, RTDE_FIELDS, frequency_hz=10.0, timeout=timeout_s)
    speed_norm = vector_norm(rtde["actual_TCP_speed"][:3])
    if speed_norm > max_speed_m_s:
        raise RuntimeError(f"TCP speed {speed_norm:.6f} m/s exceeds {max_speed_m_s:.6f} m/s")
    derived = {
        "tcp_speed_linear_norm_m_s": speed_norm,
        "tcp_speed_angular_norm_rad_s": vector_norm(rtde["actual_TCP_speed"][3:]),
        "tcp_force_norm_n": vector_norm(rtde["actual_TCP_force"][:3]),
        "tcp_torque_norm_nm": vector_norm(rtde["actual_TCP_force"][3:]),
        "payload_kg": float(rtde["payload"]),
        "tcp_offset_z_mm": float(rtde["tcp_offset"][2] * 1000.0),
    }
    return dashboard, rtde, derived


def capture(args: argparse.Namespace) -> int:
    args.session_dir.mkdir(parents=True, exist_ok=True)
    SESSION_HINT.write_text(str(args.session_dir) + "\n", encoding="utf-8")
    write_waypoint_guide(args.session_dir / "step6_eight_waypoint_guide.html")
    points_path = args.session_dir / "points.json"
    payload = read_existing(points_path)
    expected = WAYPOINT_BY_LABEL[args.label]
    dashboard, rtde, derived = preflight(args.robot_host, args.timeout_s, args.max_speed_m_s)
    point = {
        "label": args.label,
        "expected_local": expected,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "robot_host": args.robot_host,
        "dashboard": dashboard,
        "rtde": rtde,
        "derived": derived,
        "operator_note": args.note,
    }
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["session_dir"] = str(args.session_dir)
    payload["points"][args.label] = point
    points_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    missing = [label for label in LABELS if label not in payload["points"]]
    print(
        json.dumps(
            {
                "ok": True,
                "points_json": str(points_path),
                "guide_html": str(args.session_dir / "step6_eight_waypoint_guide.html"),
                "captured": point,
                "missing": missing,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", choices=LABELS, help="Step6 waypoint label to capture.")
    parser.add_argument("--session-dir", type=Path, default=default_session_dir())
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--timeout-s", type=float, default=3.0)
    parser.add_argument("--max-speed-m-s", type=float, default=0.002)
    parser.add_argument("--note", default="")
    args = parser.parse_args(argv)
    return capture(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Step6 waypoint capture blocked: {exc}", file=sys.stderr)
        raise SystemExit(2)
