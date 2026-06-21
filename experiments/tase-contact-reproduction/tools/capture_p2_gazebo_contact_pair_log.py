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
STANDALONE_P2_OBSERVATION_SCOPE = "standalone_p2_contact_witness"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _contact_sensor_block(*, topic: str, collision_name: str) -> str:
    return f"""        <sensor name="p2_contact_pair_sensor" type="contact">
          <contact>
            <collision>{collision_name}</collision>
            <topic>{topic}</topic>
          </contact>
          <always_on>1</always_on>
          <update_rate>250</update_rate>
        </sensor>
"""


def write_contact_witness_world(
    path: Path,
    *,
    topic: str = DEFAULT_CONTACT_TOPIC,
    sensor_collision_role: str = "surface",
    eoat_pose_z: float = 0.09,
    eoat_static: bool = False,
    include_base_frame: bool = True,
) -> Path:
    if sensor_collision_role not in {"surface", "eoat"}:
        raise ValueError(f"unsupported sensor collision role: {sensor_collision_role}")
    surface_sensor = _contact_sensor_block(topic=topic, collision_name="collision") if sensor_collision_role == "surface" else ""
    eoat_sensor = (
        _contact_sensor_block(topic=topic, collision_name="eoat_contact_pad_collision")
        if sensor_collision_role == "eoat"
        else ""
    )
    base_frame_model = (
        """    <model name="ur10e_base_frame">
      <static>true</static>
      <pose>0 0 0 0 0 0</pose>
      <link name="base_link">
        <pose>0 0 0 0 0 0</pose>
        <visual name="base_frame_marker">
          <geometry>
            <box>
              <size>0.010000 0.010000 0.010000</size>
            </box>
          </geometry>
        </visual>
      </link>
    </model>
"""
        if include_base_frame
        else ""
    )
    eoat_static_block = "      <static>true</static>\n" if eoat_static else ""
    source = f"""<?xml version="1.0" ?>
<sdf version="1.7">
  <world name="ur10e_p2_contact_pair_witness">
    <gravity>0 0 -9.81</gravity>
    <plugin filename="ignition-gazebo-physics-system" name="gz::sim::systems::Physics" />
    <plugin filename="ignition-gazebo-contact-system" name="gz::sim::systems::Contact" />
    <plugin filename="ignition-gazebo-user-commands-system" name="gz::sim::systems::UserCommands" />
    <plugin filename="ignition-gazebo-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster" />
{base_frame_model.rstrip()}
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
{surface_sensor.rstrip()}
      </link>
    </model>
    <model name="real_aligned_eoat_visual_stack">
{eoat_static_block.rstrip()}
      <pose>0 0 {eoat_pose_z:.6f} 0 0 0</pose>
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
{eoat_sensor.rstrip()}
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
    transport: str = "ignition",
    sensor_collision_role: str = "surface",
    baseline_mode: str | None = None,
    generated_at: str | None = None,
    stage_id: str | None = None,
    observation_id: str | None = None,
    time_window: dict[str, Any] | None = None,
    observation_scope: str | None = None,
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

    payload = {
        "schema": CONTACT_LOG_SCHEMA,
        "generated_at": generated_at or _now_iso(),
        "mode": "offline_gazebo_contact_topic_capture",
        "source": "gazebo_contact_sensor_topic",
        "sim_transport": transport,
        "sensor_collision_role": sensor_collision_role,
        "claim_tier": "visual_only",
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "allowed_claim": "contact_pair_log_evidence_only_no_force_or_wrench_contact_correlation",
        "forbidden_claim": "wrench_contact_correlation; force_contact_physics_proven; real bench/live contact",
        "topic": topic,
        "world_path": world_path,
        "raw_jsonl_path": raw_jsonl_path,
        "baseline_mode": baseline_mode,
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
    if stage_id is not None:
        payload["stage_id"] = stage_id
    if observation_id is not None:
        payload["observation_id"] = observation_id
    if time_window is not None:
        payload["time_window"] = time_window
    if observation_scope is not None:
        payload["observation_scope"] = observation_scope
    return payload


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
    wrenches = contact.get("wrench") if isinstance(contact.get("wrench"), list) else []
    normal, normal_source = _first_normal(normals)
    collision1 = _collision_name(contact.get("collision1"))
    collision2 = _collision_name(contact.get("collision2"))
    row = {
        "stamp_s": _stamp_s(contact, message),
        "stamp_evidence": _has_stamp(contact) or _has_stamp(message),
        "collision1": collision1,
        "collision2": collision2,
        "position_m": _first_vec3(positions, default=(0.0, 0.0, 0.0)),
        "normal": normal,
        "normal_source": normal_source,
        "contact_count": max(len(positions), len(normals), len(depths), len(wrenches), 1),
        "depth_m": float(depths[0]) if depths else None,
        "raw_line_index": line_index,
        "raw_contact_index": contact_index,
        "topic": topic,
    }
    native_wrench = _first_native_gazebo_contact_wrench(
        contact,
        collision1=collision1,
        collision2=collision2,
        normal=normal,
    )
    if native_wrench is not None:
        row["native_gazebo_contact_wrench"] = native_wrench
    if wrenches:
        row["raw_gazebo_contact_wrench_count"] = len(wrenches)
        row["raw_gazebo_contact_wrenches"] = wrenches
    return row


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


def _has_stamp(payload: dict[str, Any]) -> bool:
    return _nested_stamp(payload) is not None


def _collision_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")


def _first_vec3(values: list[Any], *, default: tuple[float, float, float]) -> list[float]:
    if values and isinstance(values[0], dict):
        first = values[0]
        return [float(first.get(axis) or 0.0) for axis in ("x", "y", "z")]
    return [float(default[0]), float(default[1]), float(default[2])]


def _vec3_from_object(value: Any, *, default: tuple[float, float, float]) -> list[float]:
    if isinstance(value, dict):
        return [float(value.get(axis) or 0.0) for axis in ("x", "y", "z")]
    return [float(default[0]), float(default[1]), float(default[2])]


def _dot3(left: list[float], right: list[float]) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _first_normal(values: list[Any]) -> tuple[list[float], str]:
    if values:
        return _first_vec3(values, default=STATIC_SURFACE_NORMAL), "gazebo_contact_message_normal"
    return [float(v) for v in STATIC_SURFACE_NORMAL], "derived_from_static_contact_surface_normal"


def _first_native_gazebo_contact_wrench(
    contact: dict[str, Any],
    *,
    collision1: str,
    collision2: str,
    normal: list[float],
) -> dict[str, Any] | None:
    wrenches = contact.get("wrench") if isinstance(contact.get("wrench"), list) else []
    if not wrenches:
        return None
    selected_body, selected_collision, selected_role = _selected_eoat_body_side(collision1, collision2)
    if selected_body is None:
        return None
    for wrench_index, wrench in enumerate(wrenches):
        if not isinstance(wrench, dict):
            continue
        selected, selected_field = _wrench_body_payload(wrench, selected_body)
        if not isinstance(selected, dict):
            continue
        force = _vec3_from_object(selected.get("force"), default=(0.0, 0.0, 0.0))
        torque = _vec3_from_object(selected.get("torque"), default=(0.0, 0.0, 0.0))
        body_index = "1" if selected_body == "body_1_wrench" else "2"
        other_index = "2" if body_index == "1" else "1"
        other_body = "body_2_wrench" if selected_body == "body_1_wrench" else "body_1_wrench"
        other_payload, other_field = _wrench_body_payload(wrench, other_body)
        other_force = (
            _vec3_from_object(other_payload.get("force"), default=(0.0, 0.0, 0.0))
            if isinstance(other_payload, dict)
            else None
        )
        other_torque = (
            _vec3_from_object(other_payload.get("torque"), default=(0.0, 0.0, 0.0))
            if isinstance(other_payload, dict)
            else None
        )
        return {
            "source": "gazebo_contact_message_wrench",
            "source_schema": _wrench_source_schema(wrench),
            "force_source_class": "gazebo_contact",
            "selected_body": selected_body,
            "selected_body_field": selected_field,
            "selected_body_collision": selected_collision,
            "selected_body_role": selected_role,
            "selected_body_name": _wrench_body_name(wrench, body_index),
            "other_body": other_body,
            "other_body_field": other_field,
            "other_body_name": _wrench_body_name(wrench, other_index),
            "raw_wrench_index": wrench_index,
            "raw_wrench_count": len(wrenches),
            "measured_contact_wrench": True,
            "commanded_force": False,
            "force_n": force,
            "torque_nm": torque,
            "selected_force_dot_contact_normal_n": _dot3(force, normal),
            "other_force_n": other_force,
            "other_torque_nm": other_torque,
            "other_force_dot_contact_normal_n": _dot3(other_force, normal) if other_force is not None else None,
            "wrench_stamp_s": _stamp_s(wrench, contact),
            "wrench_stamp_evidence": _has_stamp(wrench) or _has_stamp(contact),
            "frame_id": "gazebo_contact_message_native_frame",
            "frame_policy": "native_gazebo_contact_frame_untransformed",
            "frame_transform_evidence": False,
            "status": "raw_untransformed",
            "baseline_policy": "gazebo_contact_zero_no_contact_baseline_unverified",
        }
    return None


def _wrench_body_payload(wrench: dict[str, Any], body: str) -> tuple[dict[str, Any] | None, str | None]:
    aliases = {
        "body_1_wrench": ("body_1_wrench", "body1Wrench"),
        "body_2_wrench": ("body_2_wrench", "body2Wrench"),
    }[body]
    for field in aliases:
        payload = wrench.get(field)
        if isinstance(payload, dict):
            return payload, field
    return None, None


def _wrench_body_name(wrench: dict[str, Any], body_index: str) -> str:
    for field in (f"body_{body_index}_name", f"body{body_index}Name"):
        value = wrench.get(field)
        if value:
            return str(value)
    return ""


def _wrench_source_schema(wrench: dict[str, Any]) -> str:
    gz_fields = ("body1Wrench", "body2Wrench", "body1Name", "body2Name")
    if any(field in wrench for field in gz_fields):
        return "gz.msgs.Contact.contact.wrench"
    return "ignition.msgs.Contact.contact.wrench"


def _selected_eoat_body_side(collision1: str, collision2: str) -> tuple[str | None, str | None, str | None]:
    if _is_eoat_collision(collision1) and _is_surface_collision(collision2):
        return "body_1_wrench", "collision1", "eoat"
    if _is_eoat_collision(collision2) and _is_surface_collision(collision1):
        return "body_2_wrench", "collision2", "eoat"
    return None, None, None


def _is_eoat_collision(name: str) -> bool:
    return "eoat" in name and "collision" in name


def _is_surface_collision(name: str) -> bool:
    return "contact_surface" in name or "surface::collision" in name


def capture_contact_pair_log(
    output_dir: Path,
    *,
    topic: str = DEFAULT_CONTACT_TOPIC,
    timeout_s: float = 15.0,
    max_messages: int = 1,
    transport: str = "ignition",
    sensor_collision_role: str = "surface",
    eoat_pose_z: float = 0.09,
    eoat_static: bool = False,
    allow_no_messages: bool = False,
    baseline_mode: str | None = None,
    stage_id: str | None = None,
    observation_id: str | None = None,
    time_window_start: str | None = None,
    time_window_end: str | None = None,
    clock_source: str | None = None,
    observation_scope: str | None = STANDALONE_P2_OBSERVATION_SCOPE,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    world_path = write_contact_witness_world(
        output_dir / "p2_contact_witness.sdf",
        topic=topic,
        sensor_collision_role=sensor_collision_role,
        eoat_pose_z=eoat_pose_z,
        eoat_static=eoat_static,
    )
    raw_jsonl_path = output_dir / "contact_topic_stdout.jsonl"
    topic_stderr_path = output_dir / "contact_topic_stderr.log"
    gazebo_stdout_path = output_dir / "gazebo_stdout.log"
    gazebo_stderr_path = output_dir / "gazebo_stderr.log"

    gazebo_cmd, topic_cmd = _transport_commands(
        world_path,
        topic=topic,
        max_messages=max_messages,
        transport=transport,
    )
    server = subprocess.Popen(
        gazebo_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    topic_result: subprocess.CompletedProcess[str] | None = None
    topic_timeout_expired = False
    topic_timeout_stdout = ""
    topic_timeout_stderr = ""
    try:
        time.sleep(0.5)
        try:
            topic_result = subprocess.run(topic_cmd, check=False, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            topic_timeout_expired = True
            topic_timeout_stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            topic_timeout_stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            if not allow_no_messages:
                raise
    finally:
        _terminate_process_group(server)
        server_stdout, server_stderr = server.communicate(timeout=5.0)
        gazebo_stdout_path.write_text(server_stdout, encoding="utf-8")
        gazebo_stderr_path.write_text(server_stderr, encoding="utf-8")

    topic_stdout = topic_result.stdout if topic_result is not None else topic_timeout_stdout
    topic_stderr = topic_result.stderr if topic_result is not None else topic_timeout_stderr
    raw_jsonl_path.write_text(topic_stdout, encoding="utf-8")
    topic_stderr_path.write_text(topic_stderr, encoding="utf-8")
    generated_at = _now_iso()
    time_window = None
    if time_window_start or time_window_end or clock_source:
        time_window = {
            "start": time_window_start,
            "end": time_window_end or generated_at,
            "clock_source": clock_source,
        }
    payload = contact_pair_log_from_json_lines(
        topic_stdout.splitlines(),
        topic=topic,
        world_path=str(world_path),
        raw_jsonl_path=str(raw_jsonl_path),
        transport=transport,
        sensor_collision_role=sensor_collision_role,
        baseline_mode=baseline_mode,
        generated_at=generated_at,
        stage_id=stage_id,
        observation_id=observation_id,
        time_window=time_window,
        observation_scope=observation_scope,
    )
    payload["capture"] = {
        "schema": CONTACT_CAPTURE_SCHEMA,
        "sim_transport": transport,
        "sensor_collision_role": sensor_collision_role,
        "stage_id": stage_id,
        "observation_id": observation_id,
        "observation_scope": observation_scope,
        "time_window": time_window,
        "eoat_pose_z": eoat_pose_z,
        "eoat_static": eoat_static,
        "allow_no_messages": allow_no_messages,
        "baseline_mode": baseline_mode,
        "gazebo_command": gazebo_cmd,
        "topic_command": topic_cmd,
        "topic_returncode": topic_result.returncode if topic_result is not None else None,
        "topic_timeout_expired": topic_timeout_expired,
        "timeout_s": timeout_s,
        "max_messages": max_messages,
        "gazebo_stdout_path": str(gazebo_stdout_path),
        "gazebo_stderr_path": str(gazebo_stderr_path),
        "topic_stderr_path": str(topic_stderr_path),
    }
    path = output_dir / "p2_gazebo_contact_pair_log.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _transport_commands(
    world_path: Path,
    *,
    topic: str,
    max_messages: int,
    transport: str,
) -> tuple[list[str], list[str]]:
    if transport == "ignition":
        return (
            ["ign", "gazebo", "-r", "-s", str(world_path)],
            ["ign", "topic", "-e", "-t", topic, "-n", str(max_messages), "--json-output"],
        )
    if transport == "gz":
        return (
            ["gz", "sim", "-r", "-s", str(world_path)],
            ["gz", "topic", "-e", "-t", topic, "-n", str(max_messages), "--json-output"],
        )
    raise ValueError(f"unsupported transport: {transport}")


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
    parser.add_argument("--transport", choices=("ignition", "gz"), default="ignition")
    parser.add_argument("--sensor-collision-role", choices=("surface", "eoat"), default="surface")
    parser.add_argument("--eoat-pose-z", type=float, default=0.09)
    parser.add_argument("--eoat-static", action="store_true")
    parser.add_argument("--allow-no-messages", action="store_true")
    parser.add_argument("--baseline-mode", default=None)
    parser.add_argument("--stage-id", default=None)
    parser.add_argument("--observation-id", default=None)
    parser.add_argument("--time-window-start", default=None)
    parser.add_argument("--time-window-end", default=None)
    parser.add_argument("--clock-source", default=None)
    parser.add_argument("--observation-scope", default=STANDALONE_P2_OBSERVATION_SCOPE)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = capture_contact_pair_log(
        args.output_dir,
        topic=args.topic,
        timeout_s=args.timeout_s,
        max_messages=args.max_messages,
        transport=args.transport,
        sensor_collision_role=args.sensor_collision_role,
        eoat_pose_z=args.eoat_pose_z,
        eoat_static=args.eoat_static,
        allow_no_messages=args.allow_no_messages,
        baseline_mode=args.baseline_mode,
        stage_id=args.stage_id,
        observation_id=args.observation_id,
        time_window_start=args.time_window_start,
        time_window_end=args.time_window_end,
        clock_source=args.clock_source,
        observation_scope=args.observation_scope,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
