#!/usr/bin/env python3
"""Capture an already-running Gazebo v2 lane without starting any controller.

The tool only subscribes to Gazebo transport and reads controller inventory.
It does not launch Gazebo, spawn a robot, activate a controller, or touch a
real UR controller.  Raw native messages are retained next to normalized rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

from ur10e_example_controllers.ur10e_gazebo_v2 import (  # noqa: E402
    EOAT_CONTACT_COLLISION,
    EOAT_FIXED_JOINT,
    NATIVE_CONTACT_TOPIC,
    NATIVE_FT_TOPIC,
    backend_spec,
    probe_fortress_abi,
)


WORLD_NAME = "ur10e_gazebo_v2_fortress"
POSE_TOPIC = f"/world/{WORLD_NAME}/pose/info"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_messages(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    messages: list[dict[str, Any]] = []
    issues: list[str] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"line_{index}:json:{exc.msg}")
            continue
        if isinstance(value, dict):
            messages.append(value)
        else:
            issues.append(f"line_{index}:not_object")
    return messages, issues


def _stamp_s(*payloads: Mapping[str, Any]) -> float | None:
    for payload in payloads:
        header = payload.get("header")
        if not isinstance(header, Mapping):
            continue
        stamp = header.get("stamp")
        if not isinstance(stamp, Mapping):
            continue
        try:
            return float(stamp.get("sec") or 0.0) + float(stamp.get("nsec") or 0.0) * 1e-9
        except (TypeError, ValueError):
            continue
    return None


def _collision_name(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("name") or "")
    return str(value or "")


def _vec3(value: Any) -> list[float] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        result = [float(value.get(axis) or 0.0) for axis in ("x", "y", "z")]
    except (TypeError, ValueError):
        return None
    return result if all(math.isfinite(item) for item in result) else None


def _body_wrench(value: Mapping[str, Any], index: int) -> list[float] | None:
    aliases = (
        ("body_1_wrench", "body1Wrench") if index == 1 else ("body_2_wrench", "body2Wrench")
    )
    payload = next((value.get(name) for name in aliases if isinstance(value.get(name), Mapping)), None)
    if not isinstance(payload, Mapping):
        return None
    force = _vec3(payload.get("force"))
    torque = _vec3(payload.get("torque"))
    return force + torque if force is not None and torque is not None else None


def normalize_contact_messages(text: str, *, run_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    messages, issues = _json_messages(text)
    rows: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        contacts = message.get("contact")
        if not isinstance(contacts, list):
            issues.append(f"message_{message_index}:contact_not_list")
            continue
        for contact_index, contact in enumerate(contacts):
            if not isinstance(contact, Mapping):
                issues.append(f"message_{message_index}:contact_{contact_index}:not_object")
                continue
            collision1 = _collision_name(contact.get("collision1"))
            collision2 = _collision_name(contact.get("collision2"))
            if EOAT_CONTACT_COLLISION in collision1 and "step5_contact_surface" in collision2:
                selected_side = 1
                contact_collision, surface_collision = collision1, collision2
            elif EOAT_CONTACT_COLLISION in collision2 and "step5_contact_surface" in collision1:
                selected_side = 2
                contact_collision, surface_collision = collision2, collision1
            else:
                issues.append(f"message_{message_index}:contact_{contact_index}:unexpected_collision_pair")
                continue
            wrenches = contact.get("wrench")
            if not isinstance(wrenches, list):
                issues.append(f"message_{message_index}:contact_{contact_index}:native_wrench_list_missing")
                continue
            native_wrench = next(
                (
                    value
                    for value in (_body_wrench(row, selected_side) for row in wrenches if isinstance(row, Mapping))
                    if value is not None
                ),
                None,
            )
            if native_wrench is None:
                issues.append(f"message_{message_index}:contact_{contact_index}:native_eoat_wrench_missing")
                continue
            stamp = _stamp_s(contact, message)
            if stamp is None:
                issues.append(f"message_{message_index}:contact_{contact_index}:stamp_missing")
                continue
            rows.append(
                {
                    "run_id": run_id,
                    "sim_time_s": stamp,
                    "source": "gazebo_native_contact",
                    "topic": NATIVE_CONTACT_TOPIC,
                    "contact_collision": contact_collision,
                    "surface_collision": surface_collision,
                    "selected_body_side": selected_side,
                    "native_wrench": native_wrench,
                }
            )
    return rows, issues


def normalize_ft_messages(text: str, *, run_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    messages, issues = _json_messages(text)
    rows: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        force = _vec3(message.get("force"))
        torque = _vec3(message.get("torque"))
        stamp = _stamp_s(message)
        if force is None or torque is None:
            issues.append(f"message_{index}:wrench_invalid")
            continue
        if stamp is None:
            issues.append(f"message_{index}:stamp_missing")
            continue
        rows.append(
            {
                "run_id": run_id,
                "sim_time_s": stamp,
                "source": "gazebo_native_ft",
                "topic": NATIVE_FT_TOPIC,
                "sensor_joint": EOAT_FIXED_JOINT,
                "frame": "child",
                "measure_direction": "child_to_parent",
                "native_wrench": force + torque,
            }
        )
    return rows, issues


def controller_inventory(text: str, *, backend: str) -> dict[str, Any]:
    selected = backend_spec(backend)
    controllers: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            controllers[parts[0]] = parts[-1]
    other = next(spec.controller_name for name, spec in {key: backend_spec(key) for key in ("velocity", "effort_surrogate")}.items() if name != backend)
    selected_active = controllers.get(selected.controller_name) == "active"
    other_active = controllers.get(other) == "active"
    jsb_active = controllers.get("joint_state_broadcaster") == "active"
    return {
        "selected": selected.controller_name,
        "selected_active": selected_active,
        "other": other,
        "other_active": other_active,
        "joint_state_broadcaster_active": jsb_active,
        "simultaneous_backend_active": selected_active and other_active,
        "pass": selected_active and jsb_active and not other_active,
        "raw": text,
    }


def tf_lineage_from_pose_info(text: str, *, run_id: str) -> dict[str, Any]:
    messages, parse_issues = _json_messages(text)
    names = {
        str(pose.get("name") or "")
        for message in messages
        for pose in (message.get("pose") or [])
        if isinstance(pose, Mapping)
    }
    base_link_present = any(name.endswith("::base_link") or name == "base_link" for name in names)
    frames = {
        "gazebo_world": {"parent": None, "source": "gazebo_world"},
        "base_link": {
            "parent": "gazebo_world",
            "source": "gazebo_pose_info" if base_link_present else "missing_runtime_pose_info",
        },
        "base": {
            "parent": "base_link",
            "source": "calibrated_urdf_fixed_joint",
            "rpy_rad": [0.0, 0.0, math.pi],
        },
        "base_link_inertia": {"parent": "base_link", "source": "calibrated_urdf_kinematic_tree"},
        "shoulder_link": {"parent": "base_link_inertia", "source": "calibrated_urdf_kinematic_tree"},
        "upper_arm_link": {"parent": "shoulder_link", "source": "calibrated_urdf_kinematic_tree"},
        "forearm_link": {"parent": "upper_arm_link", "source": "calibrated_urdf_kinematic_tree"},
        "wrist_1_link": {"parent": "forearm_link", "source": "calibrated_urdf_kinematic_tree"},
        "wrist_2_link": {"parent": "wrist_1_link", "source": "calibrated_urdf_kinematic_tree"},
        "wrist_3_link": {"parent": "wrist_2_link", "source": "calibrated_urdf_kinematic_tree"},
        "flange": {"parent": "wrist_3_link", "source": "calibrated_urdf_kinematic_tree"},
        "tool0": {"parent": "flange", "source": "calibrated_urdf_kinematic_tree"},
        "real_aligned_eoat_visual_stack": {"parent": "tool0", "source": "attached_robot_description"},
        "active_tcp": {"parent": "tool0", "source": "active_tcp_offset_contract"},
    }
    return {
        "schema": "ur10e_gazebo_v2_tf_lineage_v1",
        "run_id": run_id,
        "runtime_base_link_pose_present": base_link_present,
        "pose_info_parse_issues": parse_issues,
        "frames": frames,
    }


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5.0)


def capture(run_dir: Path, *, backend: str, duration_s: float, tick_trace: Path | None) -> Path:
    if duration_s <= 0.0:
        raise ValueError("duration_s must be positive")
    selected = backend_spec(backend)
    run_dir.mkdir(parents=True, exist_ok=False)
    run_id = f"gazebo-v2-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    abi = probe_fortress_abi()
    if not abi["pass"]:
        raise RuntimeError("Fortress ABI preflight failed: " + json.dumps(abi, sort_keys=True))

    topic_result = subprocess.run(["ign", "topic", "-l"], capture_output=True, text=True, check=False)
    topics = set(topic_result.stdout.splitlines())
    missing_topics = [topic for topic in (NATIVE_CONTACT_TOPIC, NATIVE_FT_TOPIC, POSE_TOPIC) if topic not in topics]
    if topic_result.returncode != 0 or missing_topics:
        raise RuntimeError(f"Gazebo v2 native topics missing: {missing_topics}")
    controller_result = subprocess.run(
        ["ros2", "control", "list_controllers", "--controller-manager", "/controller_manager"],
        capture_output=True,
        text=True,
        check=False,
    )
    inventory = controller_inventory(controller_result.stdout, backend=backend)
    if controller_result.returncode != 0 or not inventory["pass"]:
        raise RuntimeError("controller exclusivity preflight failed: " + json.dumps(inventory, sort_keys=True))

    raw_paths = {
        "contact": run_dir / "native_contact.raw.jsonl",
        "ft": run_dir / "native_ft.raw.jsonl",
        "pose": run_dir / "pose_info.raw.jsonl",
    }
    topics_by_name = {"contact": NATIVE_CONTACT_TOPIC, "ft": NATIVE_FT_TOPIC, "pose": POSE_TOPIC}
    handles = {name: path.open("w", encoding="utf-8") for name, path in raw_paths.items()}
    processes: dict[str, subprocess.Popen[str]] = {}
    started_at = _now()
    try:
        for name, topic in topics_by_name.items():
            processes[name] = subprocess.Popen(
                ["ign", "topic", "-e", "-t", topic, "--json-output"],
                stdout=handles[name],
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    finally:
        for process in processes.values():
            _terminate(process)
        for handle in handles.values():
            handle.close()
    ended_at = _now()

    contact_rows, contact_issues = normalize_contact_messages(raw_paths["contact"].read_text(encoding="utf-8"), run_id=run_id)
    ft_rows, ft_issues = normalize_ft_messages(raw_paths["ft"].read_text(encoding="utf-8"), run_id=run_id)
    tf_lineage = tf_lineage_from_pose_info(raw_paths["pose"].read_text(encoding="utf-8"), run_id=run_id)
    _write_jsonl(run_dir / "native_contact.jsonl", contact_rows)
    _write_jsonl(run_dir / "native_ft.jsonl", ft_rows)
    (run_dir / "tf_lineage.json").write_text(json.dumps(tf_lineage, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if tick_trace is not None:
        shutil.copy2(tick_trace, run_dir / "tick_trace.jsonl")

    artifacts = {}
    for path in sorted(run_dir.iterdir()):
        if path.is_file():
            artifacts[path.name] = {"size": path.stat().st_size, "sha256": _sha256(path)}
    manifest = {
        "schema": "ur10e_gazebo_v2_capture_manifest_v1",
        "run_id": run_id,
        "backend": backend,
        "backend_fidelity": selected.fidelity,
        "engine_family": "Gazebo Fortress",
        "engine_major": 6,
        "ros2_control_plugin": "libign_ros2_control-system.so",
        "abi_preflight": abi,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": duration_s,
        "controller_inventory": inventory,
        "simultaneous_backend_active": inventory["simultaneous_backend_active"],
        "topics": topics_by_name,
        "normalization_issues": {"contact": contact_issues, "ft": ft_issues},
        "normalized_counts": {"contact": len(contact_rows), "ft": len(ft_rows)},
        "artifacts": artifacts,
        "claim_boundary": {
            "offline_capture_only": True,
            "live_motion_authorized": False,
            "controller_activation_performed": False,
            "virtual_surface_force_used": False,
        },
    }
    manifest_path = run_dir / "capture_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture native evidence from an already-running Gazebo v2 lane.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--backend", choices=("velocity", "effort_surrogate"), default="velocity")
    parser.add_argument("--duration-s", type=float, default=2.0)
    parser.add_argument("--tick-trace", type=Path, default=None)
    args = parser.parse_args()
    path = capture(
        args.run_dir.resolve(),
        backend=args.backend,
        duration_s=args.duration_s,
        tick_trace=args.tick_trace.resolve() if args.tick_trace else None,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
