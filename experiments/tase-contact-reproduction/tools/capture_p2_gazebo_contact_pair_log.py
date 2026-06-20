#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[3]
SRC_PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(SRC_PACKAGE) not in sys.path:
    sys.path.insert(0, str(SRC_PACKAGE))


DEFAULT_CONTACT_TOPIC = "/ur10e/contact/gazebo/p2_contact_witness/contacts"
CONTACT_LOG_SCHEMA = "ur10e_gazebo_contact_pair_log_v1"
CONTACT_CAPTURE_SCHEMA = "ur10e_gazebo_contact_pair_capture_v1"
STATIC_SURFACE_NORMAL = (0.0, 0.0, 1.0)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_contact_witness_world(path: Path, *, topic: str = DEFAULT_CONTACT_TOPIC) -> Path:
    source = f"""<?xml version="1.0" ?>
<sdf version="1.7">
  <world name="ur10e_p2_contact_pair_witness">
    <gravity>0 0 -9.81</gravity>
    <plugin filename="ignition-gazebo-physics-system" name="gz::sim::systems::Physics" />
    <plugin filename="ignition-gazebo-contact-system" name="gz::sim::systems::Contact" />
    <plugin filename="ignition-gazebo-user-commands-system" name="gz::sim::systems::UserCommands" />
    <plugin filename="ignition-gazebo-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster" />
    <model name="step5_contact_surface">
      <static>true</static>
      <pose>0 0 0 0 0 0</pose>
      <link name="surface">
        <collision name="collision">
          <geometry>
            <box>
              <size>0.250000 0.250000 0.020000</size>
            </box>
          </geometry>
        </collision>
        <visual name="visual">
          <geometry>
            <box>
              <size>0.250000 0.250000 0.020000</size>
            </box>
          </geometry>
        </visual>
        <sensor name="p2_contact_pair_sensor" type="contact">
          <contact>
            <collision>collision</collision>
            <topic>{topic}</topic>
          </contact>
          <always_on>1</always_on>
          <update_rate>250</update_rate>
        </sensor>
      </link>
    </model>
    <model name="real_aligned_eoat_visual_stack">
      <pose>0 0 0.090000 0 0 0</pose>
      <link name="eoat_contact_pad_link">
        <inertial>
          <mass>0.2</mass>
          <inertia>
            <ixx>0.00005</ixx>
            <iyy>0.00005</iyy>
            <izz>0.00005</izz>
            <ixy>0</ixy>
            <ixz>0</ixz>
            <iyz>0</iyz>
          </inertia>
        </inertial>
        <collision name="eoat_contact_pad_collision">
          <geometry>
            <box>
              <size>0.052000 0.052000 0.010000</size>
            </box>
          </geometry>
        </collision>
        <visual name="eoat_contact_pad_visual">
          <geometry>
            <box>
              <size>0.052000 0.052000 0.010000</size>
            </box>
          </geometry>
        </visual>
      </link>
    </model>
  </world>
</sdf>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def contact_pair_log_from_json_lines(
    lines: list[str],
    *,
    topic: str,
    world_path: str,
    raw_jsonl_path: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    parse_issues: list[str] = []
    for line_index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError as exc:
            parse_issues.append(f"line_{line_index}:json_decode:{exc.msg}")
            continue
        contacts = message.get("contact") or []
        if not isinstance(contacts, list):
            parse_issues.append(f"line_{line_index}:contact_not_list")
            continue
        for contact_index, contact in enumerate(contacts):
            if not isinstance(contact, dict):
                parse_issues.append(f"line_{line_index}:contact_{contact_index}_not_object")
                continue
            rows.append(_contact_to_row(message, contact, line_index=line_index, contact_index=contact_index, topic=topic))

    return {
        "schema": CONTACT_LOG_SCHEMA,
        "generated_at": generated_at or _now_iso(),
        "mode": "offline_gazebo_contact_topic_capture",
        "source": "gazebo_contact_sensor_topic",
        "claim_tier": "physical Gazebo collision/contact physics blocked/not_proven_contact_pair_only",
        "allowed_claim": "contact_pair_log_evidence_only",
        "forbidden_claim": "wrench_contact_correlation; force_contact_physics_proven; real bench/live contact",
        "topic": topic,
        "world_path": world_path,
        "raw_jsonl_path": raw_jsonl_path,
        "row_count": len(rows),
        "parse_issues": parse_issues,
        "normal_policy": "Gazebo contact normal when present; otherwise static contact-surface normal +Z with normal_source marker",
        "live_robot_command_authorized": False,
        "bridge_start_authorized": False,
        "urscript_send_authorized": False,
        "tp_load_play_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
        "rows": rows,
    }


def _contact_to_row(
    message: dict[str, Any],
    contact: dict[str, Any],
    *,
    line_index: int,
    contact_index: int,
    topic: str,
) -> dict[str, Any]:
    positions = contact.get("position") if isinstance(contact.get("position"), list) else []
    normals = contact.get("normal") if isinstance(contact.get("normal"), list) else []
    depths = contact.get("depth") if isinstance(contact.get("depth"), list) else []
    normal, normal_source = _first_normal(normals)
    return {
        "stamp_s": _stamp_s(contact, message),
        "collision1": _collision_name(contact.get("collision1")),
        "collision2": _collision_name(contact.get("collision2")),
        "position_m": _first_vec3(positions, default=(0.0, 0.0, 0.0)),
        "normal": normal,
        "normal_source": normal_source,
        "contact_count": max(len(positions), len(normals), len(depths), 1),
        "depth_m": float(depths[0]) if depths else None,
        "raw_line_index": line_index,
        "raw_contact_index": contact_index,
        "topic": topic,
    }


def _stamp_s(contact: dict[str, Any], message: dict[str, Any]) -> float:
    stamp = _nested_stamp(contact) or _nested_stamp(message)
    if stamp is None:
        return 0.0
    sec = float(stamp.get("sec") or 0.0)
    nsec = float(stamp.get("nsec") or 0.0)
    return sec + nsec * 1e-9


def _nested_stamp(payload: dict[str, Any]) -> dict[str, Any] | None:
    header = payload.get("header")
    if not isinstance(header, dict):
        return None
    stamp = header.get("stamp")
    return stamp if isinstance(stamp, dict) else None


def _collision_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")


def _first_vec3(values: list[Any], *, default: tuple[float, float, float]) -> list[float]:
    if values and isinstance(values[0], dict):
        first = values[0]
        return [float(first.get(axis) or 0.0) for axis in ("x", "y", "z")]
    return [float(default[0]), float(default[1]), float(default[2])]


def _first_normal(values: list[Any]) -> tuple[list[float], str]:
    if values:
        return _first_vec3(values, default=STATIC_SURFACE_NORMAL), "gazebo_contact_message_normal"
    return [float(v) for v in STATIC_SURFACE_NORMAL], "derived_from_static_contact_surface_normal"


def capture_contact_pair_log(
    output_dir: Path,
    *,
    topic: str = DEFAULT_CONTACT_TOPIC,
    timeout_s: float = 15.0,
    max_messages: int = 1,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    world_path = write_contact_witness_world(output_dir / "p2_contact_witness.sdf", topic=topic)
    raw_jsonl_path = output_dir / "contact_topic_stdout.jsonl"
    topic_stderr_path = output_dir / "contact_topic_stderr.log"
    gazebo_stdout_path = output_dir / "gazebo_stdout.log"
    gazebo_stderr_path = output_dir / "gazebo_stderr.log"

    gazebo_cmd = ["ign", "gazebo", "-r", "-s", str(world_path)]
    topic_cmd = ["ign", "topic", "-e", "-t", topic, "-n", str(max_messages), "--json-output"]
    server = subprocess.Popen(
        gazebo_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    topic_result: subprocess.CompletedProcess[str] | None = None
    try:
        time.sleep(0.5)
        topic_result = subprocess.run(topic_cmd, check=False, capture_output=True, text=True, timeout=timeout_s)
    finally:
        _terminate_process_group(server)
        server_stdout, server_stderr = server.communicate(timeout=5.0)
        gazebo_stdout_path.write_text(server_stdout, encoding="utf-8")
        gazebo_stderr_path.write_text(server_stderr, encoding="utf-8")

    topic_stdout = topic_result.stdout if topic_result is not None else ""
    topic_stderr = topic_result.stderr if topic_result is not None else ""
    raw_jsonl_path.write_text(topic_stdout, encoding="utf-8")
    topic_stderr_path.write_text(topic_stderr, encoding="utf-8")
    payload = contact_pair_log_from_json_lines(
        topic_stdout.splitlines(),
        topic=topic,
        world_path=str(world_path),
        raw_jsonl_path=str(raw_jsonl_path),
    )
    payload["capture"] = {
        "schema": CONTACT_CAPTURE_SCHEMA,
        "gazebo_command": gazebo_cmd,
        "topic_command": topic_cmd,
        "topic_returncode": topic_result.returncode if topic_result is not None else None,
        "timeout_s": timeout_s,
        "max_messages": max_messages,
        "gazebo_stdout_path": str(gazebo_stdout_path),
        "gazebo_stderr_path": str(gazebo_stderr_path),
        "topic_stderr_path": str(topic_stderr_path),
    }
    path = output_dir / "p2_gazebo_contact_pair_log.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5.0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture a no-live Gazebo contact-pair log for the UR10e P2 gate.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--topic", default=DEFAULT_CONTACT_TOPIC)
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument("--max-messages", type=int, default=1)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = capture_contact_pair_log(
        args.output_dir,
        topic=args.topic,
        timeout_s=args.timeout_s,
        max_messages=args.max_messages,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
