#!/usr/bin/env python3
"""Run a non-formal Step5b Gazebo transport isolation probe.

This tool deliberately does not run the Step5b trajectory runner. It builds a
Step5b-topic forced-contact probe world, then compares Gazebo server/subscriber
combinations so `gz sim` and `gz topic` are not conflated.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import build_gazebo_contact_wrench_trace as wrench_adapter  # noqa: E402
import build_gazebo_visual_world as visual_world  # noqa: E402
import capture_p2_gazebo_contact_pair_log as contact_capture  # noqa: E402


SCHEMA = "ur10e_step5b_gz_transport_probe_manifest_v1"
CAPTURE_SCHEMA = "ur10e_step5b_gz_transport_probe_capture_v1"
STAGE_ID = "step5b"
TOPIC = "/ur10e/contact/gazebo/step5b/contacts"
OBSERVATION_SCOPE = "non_formal_step5b_transport_probe"
CLAIM_TIER = "visual_only"
NONFORMAL_ALLOWED_CLAIM = (
    "non-formal Step5b-topic Gazebo contact/wrench/geometry consistency for the forced probe only; "
    "not formal Step5b physical acceptance proof."
)
NONFORMAL_FORBIDDEN_CLAIM = "formal Step5b acceptance; real bench/live contact; simulated_ft upgrade"
FORMAL_STEP5B_ACCEPTANCE_BLOCKERS = (
    "formal_step5b_same_run_not_attempted",
    "static_rviz_candidate_not_formal_observer_acceptance",
)
DEFAULT_BASE_WORLD = WORKSPACE / "src/ur10e_example_controllers/worlds/step5_table_world.sdf"
RUNS_DIR = WORKSPACE / "experiments/tase-contact-reproduction/runs"
PROBE_MODEL_NAME = "step5b_nonformal_forced_eoat_probe"
PROBE_COLLISION_NAME = "eoat_contact_pad_collision"
PROBE_LINK_NAME = "eoat_contact_pad_link"
VERIFIED_CONTACT_FILENAME = "step5b_gz_contact_pair_log_verified.json"
ADAPTER_REPORT_FILENAME = "step5b_gz_contact_wrench_adapter.json"
ADAPTER_TRACE_FILENAME = "step5b_gz_contact_wrench_trace.json"
RESOURCE_PATH_CANDIDATES = (
    WORKSPACE / "install/ur10e_example_controllers/share",
    WORKSPACE / "install/ur_description/share",
    WORKSPACE / "src",
    Path("/opt/ros/humble/share"),
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _nonformal_claim_boundary(diagnostic_claim_tier: str | None = None) -> dict[str, Any]:
    return {
        "is_formal_acceptance": False,
        "diagnostic_claim_tier": diagnostic_claim_tier or CLAIM_TIER,
        "formal_step5b_acceptance_claim_tier": CLAIM_TIER,
        "formal_step5b_acceptance_status": "blocked",
        "formal_step5b_acceptance_blockers": list(FORMAL_STEP5B_ACCEPTANCE_BLOCKERS),
        "nonformal_allowed_claim": NONFORMAL_ALLOWED_CLAIM,
        "acceptance_discriminator": "is_formal_acceptance",
    }


def gazebo_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("GZ_SIM_RESOURCE_PATH", "")
    entries = [entry for entry in existing.split(os.pathsep) if entry]
    for candidate in RESOURCE_PATH_CANDIDATES:
        if candidate.exists():
            entries.append(str(candidate))
    deduped = list(dict.fromkeys(entries))
    env["GZ_SIM_RESOURCE_PATH"] = os.pathsep.join(deduped)
    return env


def _world_element(tree: ET.ElementTree) -> ET.Element:
    root = tree.getroot()
    world = root.find("world")
    if world is None:
        raise ValueError("SDF world element missing")
    return world


def _pose_values(element: ET.Element | None) -> tuple[float, float, float, float, float, float]:
    if element is None:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    pose = element.find("./pose")
    if pose is None or not pose.text:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    parts = [float(part) for part in pose.text.split()]
    if len(parts) != 6:
        raise ValueError("SDF pose must have six values")
    return tuple(parts)  # type: ignore[return-value]


def _pose_is_identity(pose: tuple[float, float, float, float, float, float], *, tolerance: float = 1e-9) -> bool:
    return all(abs(value) <= tolerance for value in pose)


def _ensure_base_frame_identity_model(world: ET.Element) -> None:
    if world.find("./model[@name='ur10e_base_frame']") is not None:
        return
    model = ET.SubElement(world, "model", {"name": "ur10e_base_frame"})
    ET.SubElement(model, "static").text = "true"
    ET.SubElement(model, "pose").text = "0 0 0 0 0 0"
    link = ET.SubElement(model, "link", {"name": "base_link"})
    ET.SubElement(link, "pose").text = "0 0 0 0 0 0"
    visual = ET.SubElement(link, "visual", {"name": "base_frame_marker"})
    geometry = ET.SubElement(visual, "geometry")
    box = ET.SubElement(geometry, "box")
    ET.SubElement(box, "size").text = "0.010000 0.010000 0.010000"


def _contact_target_from_manifest(manifest: dict[str, Any]) -> tuple[float, float, float]:
    target = manifest.get("contact_target_pose_world") if isinstance(manifest.get("contact_target_pose_world"), dict) else {}
    try:
        return float(target["x_m"]), float(target["y_m"]), float(target["surface_top_z_m"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("contact_target_pose_world missing from visual world manifest") from exc


def _add_forced_probe_model(world: ET.Element, *, x_m: float, y_m: float, surface_top_z_m: float, no_contact: bool) -> None:
    for model in list(world.findall(f"./model[@name='{PROBE_MODEL_NAME}']")):
        world.remove(model)

    pose_z = surface_top_z_m + (0.20 if no_contact else 0.08)
    model = ET.SubElement(world, "model", {"name": PROBE_MODEL_NAME})
    if no_contact:
        ET.SubElement(model, "static").text = "true"
    ET.SubElement(model, "pose").text = f"{x_m:.9f} {y_m:.9f} {pose_z:.9f} 0 0 0"
    link = ET.SubElement(model, "link", {"name": PROBE_LINK_NAME})
    inertial = ET.SubElement(link, "inertial")
    ET.SubElement(inertial, "mass").text = "0.2"
    inertia = ET.SubElement(inertial, "inertia")
    for key, value in {
        "ixx": "0.00005",
        "iyy": "0.00005",
        "izz": "0.00005",
        "ixy": "0",
        "ixz": "0",
        "iyz": "0",
    }.items():
        ET.SubElement(inertia, key).text = value

    collision = ET.SubElement(link, "collision", {"name": PROBE_COLLISION_NAME})
    geometry = ET.SubElement(collision, "geometry")
    box = ET.SubElement(geometry, "box")
    ET.SubElement(box, "size").text = "0.052000 0.052000 0.010000"

    visual = ET.SubElement(link, "visual", {"name": "eoat_contact_pad_visual"})
    visual_geometry = ET.SubElement(visual, "geometry")
    visual_box = ET.SubElement(visual_geometry, "box")
    ET.SubElement(visual_box, "size").text = "0.052000 0.052000 0.010000"


def build_probe_world(
    output_dir: Path,
    *,
    base_world: Path = DEFAULT_BASE_WORLD,
    no_contact: bool = False,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    base_visual_world = output_dir / ("step5b_no_contact_visual.sdf" if no_contact else "step5b_forced_contact_visual.sdf")
    visual_world.build_visual_world(STAGE_ID, base_world, base_visual_world)
    manifest_path = base_visual_world.with_suffix(".manifest.json")
    manifest = _load_json(manifest_path)
    x_m, y_m, surface_top_z_m = _contact_target_from_manifest(manifest)

    tree = ET.parse(base_visual_world)
    world = _world_element(tree)
    _ensure_base_frame_identity_model(world)
    _add_forced_probe_model(world, x_m=x_m, y_m=y_m, surface_top_z_m=surface_top_z_m, no_contact=no_contact)

    probe_world = output_dir / ("step5b_no_contact_probe_world.sdf" if no_contact else "step5b_forced_contact_probe_world.sdf")
    tree.write(probe_world, encoding="utf-8", xml_declaration=True)
    manifest.update(
        {
            "schema": "ur10e_step5b_transport_probe_world_manifest_v1",
            "mode": "offline_nonformal_step5b_transport_probe_world",
            "claim_tier": CLAIM_TIER,
            "observation_scope": OBSERVATION_SCOPE,
            "stage_id": STAGE_ID,
            "step5b_attempt_spent": False,
            "formal_step5b_attempt": False,
            "topic": TOPIC,
            "probe_model": PROBE_MODEL_NAME,
            "probe_collision": f"{PROBE_MODEL_NAME}::{PROBE_LINK_NAME}::{PROBE_COLLISION_NAME}",
            "probe_mode": "no_contact_negative_control" if no_contact else "forced_contact_positive_probe",
            "probe_world": str(probe_world),
            "probe_world_sha256": _sha256(probe_world),
            "allowed_claim": "non-formal Step5b-topic transport probe only",
            "forbidden_claim": "Step5b acceptance; physical Gazebo contact proof; real bench/live contact",
        }
    )
    _write_json(probe_world.with_suffix(".manifest.json"), manifest)
    return probe_world, probe_world.with_suffix(".manifest.json")


def server_command(server_transport: str, world_path: Path) -> list[str]:
    if server_transport == "gz":
        return ["gz", "sim", "-r", "-s", str(world_path)]
    if server_transport == "ignition":
        return ["ign", "gazebo", "-r", "-s", str(world_path)]
    raise ValueError(f"unsupported server transport: {server_transport}")


def subscriber_command(subscriber_transport: str, *, topic: str, max_messages: int) -> list[str]:
    if subscriber_transport == "gz":
        return ["gz", "topic", "-e", "-t", topic, "-n", str(max_messages), "--json-output"]
    if subscriber_transport == "ignition":
        return ["ign", "topic", "-e", "-t", topic, "-n", str(max_messages), "--json-output"]
    raise ValueError(f"unsupported subscriber transport: {subscriber_transport}")


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5.0)


def capture_case(
    output_dir: Path,
    *,
    world_path: Path,
    server_transport: str,
    subscriber_transport: str,
    max_messages: int,
    timeout_s: float,
    observation_id: str,
    no_contact: bool = False,
    allow_no_messages: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = _now()
    server_cmd = server_command(server_transport, world_path)
    topic_cmd = subscriber_command(subscriber_transport, topic=TOPIC, max_messages=max_messages)
    server_stdout_path = output_dir / "gazebo_stdout.log"
    server_stderr_path = output_dir / "gazebo_stderr.log"
    topic_stdout_path = output_dir / "contact_topic_stdout.jsonl"
    topic_stderr_path = output_dir / "contact_topic_stderr.log"
    env = gazebo_env()

    server = subprocess.Popen(
        server_cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    topic_stdout = ""
    topic_stderr = ""
    topic_returncode: int | None = None
    topic_timeout_expired = False
    try:
        time.sleep(0.75)
        try:
            result = subprocess.run(topic_cmd, env=env, capture_output=True, text=True, check=False, timeout=timeout_s)
            topic_stdout = result.stdout
            topic_stderr = result.stderr
            topic_returncode = result.returncode
        except subprocess.TimeoutExpired as exc:
            topic_timeout_expired = True
            topic_stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            topic_stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            if not allow_no_messages:
                topic_stderr += "\nprobe subscriber timed out before required messages\n"
    finally:
        _terminate_process_group(server)
        server_stdout, server_stderr = server.communicate(timeout=5.0)

    server_stdout_path.write_text(server_stdout or "", encoding="utf-8")
    server_stderr_path.write_text(server_stderr or "", encoding="utf-8")
    topic_stdout_path.write_text(topic_stdout or "", encoding="utf-8")
    topic_stderr_path.write_text(topic_stderr or "", encoding="utf-8")
    finished_at = _now()
    clock_source = f"{server_transport}_server_{subscriber_transport}_subscriber_contact_topic_probe"
    time_window = {"start": started_at, "end": finished_at, "clock_source": clock_source}
    payload = contact_capture.contact_pair_log_from_json_lines(
        (topic_stdout or "").splitlines(),
        topic=TOPIC,
        world_path=str(world_path),
        raw_jsonl_path=str(topic_stdout_path),
        transport=subscriber_transport,
        sensor_collision_role="surface",
        selected_contact_body_role="surface",
        generated_at=finished_at,
        stage_id=STAGE_ID,
        observation_id=observation_id,
        time_window=time_window,
        observation_scope=OBSERVATION_SCOPE,
    )
    payload.update(
        {
            "mode": "offline_nonformal_step5b_transport_probe_capture",
            "claim_tier": CLAIM_TIER,
            "target_claim_tier": "physical Gazebo collision/contact physics",
            "server_transport": server_transport,
            "subscriber_transport": subscriber_transport,
            "step5b_attempt_spent": False,
            "formal_step5b_attempt": False,
            "probe_mode": "no_contact_negative_control" if no_contact else "forced_contact_positive_probe",
            "allowed_claim": "non-formal Step5b-topic transport probe evidence only",
            "forbidden_claim": "Step5b acceptance; same-run physical contact proof; real bench/live contact",
        }
    )
    payload["capture"] = {
        "schema": CAPTURE_SCHEMA,
        "server_transport": server_transport,
        "subscriber_transport": subscriber_transport,
        "server_command": server_cmd,
        "topic_command": topic_cmd,
        "topic": TOPIC,
        "stage_id": STAGE_ID,
        "observation_id": observation_id,
        "observation_scope": OBSERVATION_SCOPE,
        "time_window": time_window,
        "topic_returncode": topic_returncode,
        "topic_timeout_expired": topic_timeout_expired,
        "timeout_s": timeout_s,
        "max_messages": max_messages,
        "allow_no_messages": allow_no_messages,
        "stdout_path": str(topic_stdout_path),
        "stderr_path": str(topic_stderr_path),
        "server_stdout_path": str(server_stdout_path),
        "server_stderr_path": str(server_stderr_path),
        "gz_sim_resource_path": env.get("GZ_SIM_RESOURCE_PATH"),
        "claim_tier": CLAIM_TIER,
        "step5b_attempt_spent": False,
    }
    path = output_dir / "gazebo_contact_pair_log.json"
    _write_json(path, payload)
    summary = summarize_capture_payload(payload, artifact_path=path)
    summary_path = output_dir / "capture_summary.json"
    _write_json(summary_path, summary)
    return summary


def summarize_capture_payload(payload: dict[str, Any], *, artifact_path: Path | None = None) -> dict[str, Any]:
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    native_rows = [row for row in rows if isinstance(row, dict) and isinstance(row.get("native_gazebo_contact_wrench"), dict)]
    raw_wrench_rows = [row for row in rows if isinstance(row, dict) and int(row.get("raw_gazebo_contact_wrench_count") or 0) > 0]
    normal_rows = [row for row in rows if isinstance(row, dict) and row.get("normal_source") == "gazebo_contact_message_normal"]
    depth_rows = [row for row in rows if isinstance(row, dict) and row.get("depth_m") is not None]
    capture = payload.get("capture") if isinstance(payload.get("capture"), dict) else {}
    return {
        "schema": "ur10e_step5b_gz_transport_probe_case_summary_v1",
        "artifact_path": str(artifact_path) if artifact_path else None,
        "artifact_sha256": _sha256(artifact_path) if artifact_path else None,
        "stage_id": payload.get("stage_id"),
        "observation_id": payload.get("observation_id"),
        "observation_scope": payload.get("observation_scope"),
        "claim_tier": payload.get("claim_tier", CLAIM_TIER),
        "topic": payload.get("topic"),
        "server_transport": payload.get("server_transport"),
        "subscriber_transport": payload.get("subscriber_transport"),
        "selected_contact_body_role": payload.get("selected_contact_body_role"),
        "sensor_collision_role": payload.get("sensor_collision_role"),
        "probe_mode": payload.get("probe_mode"),
        "time_window": payload.get("time_window"),
        "row_count": len(rows),
        "native_wrench_row_count": len(native_rows),
        "raw_wrench_row_count": len(raw_wrench_rows),
        "normal_row_count": len(normal_rows),
        "depth_row_count": len(depth_rows),
        "parse_issues": payload.get("parse_issues") or [],
        "topic_returncode": capture.get("topic_returncode"),
        "topic_timeout_expired": capture.get("topic_timeout_expired"),
        "stdout_path": capture.get("stdout_path"),
        "stderr_path": capture.get("stderr_path"),
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
    }


def build_transport_comparison(case_summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    gz_gz = case_summaries.get("gz_server_gz_subscriber", {})
    ign_ign = case_summaries.get("ignition_server_ignition_subscriber", {})
    no_contact = case_summaries.get("gz_server_gz_subscriber_no_contact", {})
    gz_ign = case_summaries.get("gz_server_ignition_subscriber", {})
    ign_gz = case_summaries.get("ignition_server_gz_subscriber", {})
    gz_gz_native = int(gz_gz.get("native_wrench_row_count") or 0)
    ign_ign_native = int(ign_ign.get("native_wrench_row_count") or 0)
    no_contact_rows = int(no_contact.get("row_count") or 0)
    gz_ign_rows = int(gz_ign.get("row_count") or 0)
    ign_gz_rows = int(ign_gz.get("row_count") or 0)
    gz_ign_native = int(gz_ign.get("native_wrench_row_count") or 0)
    ign_gz_native = int(ign_gz.get("native_wrench_row_count") or 0)
    checks = {
        "gz_server_gz_subscriber_native_rows_gt_zero": gz_gz_native > 0,
        "ignition_server_ignition_subscriber_native_rows_zero": ign_ign_native == 0,
        "gz_no_contact_negative_control_zero_rows": no_contact_rows == 0,
        "gz_server_ignition_subscriber_rows_zero": gz_ign_rows == 0,
        "gz_server_ignition_subscriber_native_rows_zero": gz_ign_native == 0,
        "ignition_server_gz_subscriber_rows_zero": ign_gz_rows == 0,
        "ignition_server_gz_subscriber_native_rows_zero": ign_gz_native == 0,
    }
    blockers = [name for name, ok in checks.items() if not ok]
    return {
        "schema": "ur10e_step5b_gz_transport_probe_comparison_v1",
        "generated_at": _now(),
        "stage_id": STAGE_ID,
        "topic": TOPIC,
        "observation_scope": OBSERVATION_SCOPE,
        "claim_tier": CLAIM_TIER,
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
        "case_summaries": case_summaries,
        "checks": checks,
        "native_wrench_exposed_on_step5b_topic_by_gz_server_gz_subscriber": checks[
            "gz_server_gz_subscriber_native_rows_gt_zero"
        ],
        "m2_static_probe_native_exposure_pass": not blockers,
        "m5_go_allowed": False,
        "m5_go_blockers": [
            "transform_evidence_not_yet_verified",
            "total_contact_wrench_trace_not_yet_built_from_step5b_probe",
            "formal_step5b_same_run_not_attempted",
        ],
        "blockers": blockers,
        "allowed_claim": "non-formal Step5b-topic transport discriminator only",
        "forbidden_claim": "Step5b acceptance; physical Gazebo contact proof; real bench/live contact",
    }


def _raw_log_empty(path: Path) -> bool:
    return path.is_file() and not path.read_text(encoding="utf-8").strip()


def _vec3(value: Any, *, label: str) -> tuple[float, float, float]:
    if isinstance(value, dict):
        return (
            float(value.get("x") or 0.0),
            float(value.get("y") or 0.0),
            float(value.get("z") or 0.0),
        )
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three values")
    return (float(value[0]), float(value[1]), float(value[2]))


def _dot3(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _neg3(value: tuple[float, float, float]) -> tuple[float, float, float]:
    return (-value[0], -value[1], -value[2])


def _close3(left: tuple[float, float, float], right: tuple[float, float, float], *, tolerance: float = 1e-6) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(left, right))


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _native(row: dict[str, Any]) -> dict[str, Any] | None:
    native = row.get("native_gazebo_contact_wrench")
    return native if isinstance(native, dict) else None


def _is_surface_collision(name: str) -> bool:
    return "contact_surface" in name or "surface::collision" in name


def _is_eoat_collision(name: str) -> bool:
    return "eoat" in name and "collision" in name


def _base_frame_identity_blockers(world_path: Path) -> list[str]:
    try:
        root = ET.parse(world_path).getroot()
        base_model = root.find(".//model[@name='ur10e_base_frame']")
        base_link = base_model.find("./link[@name='base_link']") if base_model is not None else None
        model_pose = _pose_values(base_model)
        link_pose = _pose_values(base_link)
    except Exception as exc:  # noqa: BLE001 - evidence artifact should record parser failure.
        return [f"world_sdf_parse_error:{type(exc).__name__}"]
    blockers: list[str] = []
    if base_model is None:
        blockers.append("missing_ur10e_base_frame_model")
    if base_link is None:
        blockers.append("missing_base_link")
    if not _pose_is_identity(model_pose):
        blockers.append("base_model_pose_not_identity")
    if not _pose_is_identity(link_pose):
        blockers.append("base_link_pose_not_identity")
    return blockers


def _write_transform_evidence(output_dir: Path, *, world_path: Path, generated_at: str) -> tuple[Path, dict[str, Any]]:
    blockers = _base_frame_identity_blockers(world_path)
    payload = {
        "schema": "ur10e_step5b_gz_transport_probe_transform_evidence_v1",
        "generated_at": generated_at,
        "stage_id": STAGE_ID,
        "observation_scope": OBSERVATION_SCOPE,
        "claim_tier": CLAIM_TIER,
        **_nonformal_claim_boundary(CLAIM_TIER),
        "world_path": str(world_path),
        "source": "step5b_transport_probe_sdf_ur10e_base_frame_identity",
        "from_frame": "gazebo_contact_message_native_frame",
        "native_frame_interpreted_as": "world",
        "to_frame": "base",
        "transform": {"translation_xyz_m": [0.0, 0.0, 0.0], "rpy_rad": [0.0, 0.0, 0.0]},
        "evidence_class": "sdf_identity_assumption_not_independent_gazebo_frame_measurement",
        "formal_step5b_transform_proof": False,
        "valid": not blockers,
        "blockers": _dedupe(blockers),
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
    }
    path = _write_json(output_dir / "step5b_gz_contact_wrench_transform_evidence.json", payload)
    return path, payload


def _baseline_rejection_reasons(positive_payload: dict[str, Any], baseline_payload: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if baseline_payload.get("schema") != contact_capture.CONTACT_LOG_SCHEMA:
        blockers.append("baseline_schema_not_contact_pair_log_v1")
    if baseline_payload.get("server_transport") != "gz" or baseline_payload.get("subscriber_transport") != "gz":
        blockers.append("baseline_not_gz_server_gz_subscriber")
    if baseline_payload.get("sensor_collision_role") != "surface":
        blockers.append("baseline_sensor_not_surface")
    if baseline_payload.get("selected_contact_body_role") != "surface":
        blockers.append("baseline_selected_body_role_not_surface")
    if baseline_payload.get("topic") != positive_payload.get("topic"):
        blockers.append("baseline_topic_not_same_as_positive")
    if baseline_payload.get("probe_mode") != "no_contact_negative_control":
        blockers.append("baseline_probe_mode_not_no_contact_negative_control")
    if int(baseline_payload.get("row_count") or 0) != 0 or baseline_payload.get("rows"):
        blockers.append("baseline_contact_rows_present")
    if baseline_payload.get("parse_issues"):
        blockers.append("baseline_parse_issues_present")
    capture_meta = baseline_payload.get("capture") if isinstance(baseline_payload.get("capture"), dict) else {}
    if capture_meta.get("allow_no_messages") is not True:
        blockers.append("baseline_capture_did_not_allow_no_messages")
    if capture_meta.get("topic_timeout_expired") is not True:
        blockers.append("baseline_topic_did_not_timeout_empty")
    raw_path = Path(str(baseline_payload.get("raw_jsonl_path") or ""))
    if not _raw_log_empty(raw_path):
        blockers.append("baseline_raw_topic_log_not_empty")
    return _dedupe(blockers)


def _write_baseline_evidence(
    output_dir: Path,
    *,
    positive_payload: dict[str, Any],
    baseline_payload: dict[str, Any],
    generated_at: str,
) -> tuple[Path, dict[str, Any]]:
    blockers = _baseline_rejection_reasons(positive_payload, baseline_payload)
    payload = {
        "schema": "ur10e_step5b_gz_transport_probe_baseline_evidence_v1",
        "generated_at": generated_at,
        "stage_id": STAGE_ID,
        "observation_scope": OBSERVATION_SCOPE,
        "claim_tier": CLAIM_TIER,
        **_nonformal_claim_boundary(CLAIM_TIER),
        "baseline_policy": "gazebo_contact_zero_no_contact_baseline",
        "positive_contact_pair_path": positive_payload.get("artifact_path"),
        "baseline_contact_pair_path": baseline_payload.get("artifact_path"),
        "same_topic_transport_sensor_role_as_positive": not blockers,
        "row_count": baseline_payload.get("row_count"),
        "raw_jsonl_path": baseline_payload.get("raw_jsonl_path"),
        "raw_topic_log_empty": _raw_log_empty(Path(str(baseline_payload.get("raw_jsonl_path") or ""))),
        "valid": not blockers,
        "blockers": blockers,
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
    }
    path = _write_json(output_dir / "step5b_gz_contact_wrench_baseline_evidence.json", payload)
    return path, payload


def _write_source_evidence(output_dir: Path, *, generated_at: str) -> Path:
    return _write_json(
        output_dir / "step5b_gz_contact_wrench_source_evidence.json",
        {
            "schema": "ur10e_step5b_gz_transport_probe_source_evidence_v1",
            "generated_at": generated_at,
            "stage_id": STAGE_ID,
            "observation_scope": OBSERVATION_SCOPE,
            "claim_tier": CLAIM_TIER,
            "source_topic": TOPIC,
            "source_schema": "gz.msgs.Contact.contact.wrench embedded in /contacts",
            "correlation_policy": "intramessage_same_contact_entry",
            "does_not_support": [
                "real bench/live contact",
                "formal Step5b acceptance",
                "same-run formal trajectory execution",
                "simulated_ft upgrade",
            ],
        },
    )


def _row_rejection_reasons(row: dict[str, Any], *, min_normal_load_n: float) -> list[str]:
    blockers: list[str] = []
    collision1 = str(row.get("collision1") or "")
    collision2 = str(row.get("collision2") or "")
    if not _is_surface_collision(collision1):
        blockers.append("collision1_not_surface")
    if not _is_eoat_collision(collision2):
        blockers.append("collision2_not_eoat_probe")
    if row.get("stamp_evidence") is not True:
        blockers.append("missing_stamp_evidence")
    try:
        stamp_s = float(row.get("stamp_s"))
    except (TypeError, ValueError):
        blockers.append("missing_stamp_s")
        stamp_s = 0.0
    try:
        normal = _vec3(row.get("normal"), label="normal")
    except (TypeError, ValueError):
        blockers.append("invalid_contact_normal")
        normal = (0.0, 0.0, -1.0)
    if row.get("normal_source") != "gazebo_contact_message_normal":
        blockers.append("normal_not_from_gazebo_message")
    try:
        if int(row.get("contact_count") or 0) <= 0:
            blockers.append("missing_contact_count")
    except (TypeError, ValueError):
        blockers.append("invalid_contact_count")
    native = _native(row)
    if native is None:
        blockers.append("missing_native_gazebo_contact_wrench")
        return blockers
    if native.get("source") != "gazebo_contact_message_wrench":
        blockers.append("native_wrench_source_not_gazebo_contact_message")
    if native.get("source_schema") != "gz.msgs.Contact.contact.wrench":
        blockers.append("native_wrench_schema_not_gz")
    if native.get("force_source_class") != "gazebo_contact":
        blockers.append("native_force_source_class_not_gazebo_contact")
    if native.get("selected_body") != "body_1_wrench" or native.get("selected_body_collision") != "collision1":
        blockers.append("native_body1_not_selected_for_surface_collision1")
    if native.get("selected_body_role") != "surface":
        blockers.append("native_selected_body_role_not_surface")
    if native.get("measured_contact_wrench") is not True or native.get("commanded_force") is not False:
        blockers.append("native_wrench_not_measured_contact")
    if native.get("wrench_stamp_evidence") is not True:
        blockers.append("missing_wrench_stamp_evidence")
    try:
        force = _vec3(native.get("force_n"), label="force_n")
        other_force = _vec3(native.get("other_force_n"), label="other_force_n")
        normal_load_n = _dot3(force, normal)
    except (TypeError, ValueError):
        blockers.append("invalid_native_force_vector")
    else:
        if normal_load_n < min_normal_load_n:
            blockers.append("normal_load_below_floor")
        if not _close3(force, _neg3(other_force)):
            blockers.append("native_body_pair_not_equal_and_opposite")
    try:
        if abs(float(native.get("wrench_stamp_s")) - stamp_s) > 0.02:
            blockers.append("wrench_stamp_not_contact_aligned")
    except (TypeError, ValueError):
        blockers.append("missing_wrench_stamp")
    return _dedupe(blockers)


def _raw_wrench_body_payload(raw_wrench: dict[str, Any], body: str) -> dict[str, Any] | None:
    aliases = {
        "body_1_wrench": ("body_1_wrench", "body1Wrench"),
        "body_2_wrench": ("body_2_wrench", "body2Wrench"),
    }[body]
    for field in aliases:
        payload = raw_wrench.get(field)
        if isinstance(payload, dict):
            return payload
    return None


def _convert_surface_row_to_eoat_canonical(row: dict[str, Any], *, transform_path: Path, baseline_path: Path, source_path: Path) -> dict[str, Any]:
    verified_row = copy.deepcopy(row)
    raw_wrenches = verified_row.get("raw_gazebo_contact_wrenches")
    if not isinstance(raw_wrenches, list) or not raw_wrenches:
        raise ValueError("raw Gazebo contact wrenches are required for canonical EOAT conversion")
    raw_index = 0
    raw_wrench = raw_wrenches[raw_index]
    if not isinstance(raw_wrench, dict):
        raise ValueError("first raw Gazebo contact wrench is malformed")
    eoat_payload = _raw_wrench_body_payload(raw_wrench, "body_2_wrench")
    surface_payload = _raw_wrench_body_payload(raw_wrench, "body_1_wrench")
    if eoat_payload is None or surface_payload is None:
        raise ValueError("surface/EOAT body wrench pair missing")
    eoat_force = _vec3(eoat_payload.get("force"), label="eoat.force")
    eoat_torque = _vec3(eoat_payload.get("torque") or [0.0, 0.0, 0.0], label="eoat.torque")
    surface_force = _vec3(surface_payload.get("force"), label="surface.force")
    surface_torque = _vec3(surface_payload.get("torque") or [0.0, 0.0, 0.0], label="surface.torque")
    reaction_normal = (0.0, 0.0, 1.0)
    verified_row["normal"] = list(reaction_normal)
    verified_row["normal_source"] = "canonical_step5b_reaction_normal_from_force_frame_contract"
    native = verified_row["native_gazebo_contact_wrench"]
    native.update(
        {
            "frame_id": "base",
            "frame_policy": "verified_world_to_base_identity_from_step5b_transport_probe_sdf",
            "frame_transform_evidence": {
                "source": "step5b_transport_probe_sdf_ur10e_base_frame_identity",
                "from_frame": "gazebo_contact_message_native_frame",
                "native_frame_interpreted_as": "world",
                "to_frame": "base",
                "stamp_s": native["wrench_stamp_s"],
                "artifact_path": str(transform_path),
                "evidence_class": "sdf_identity_assumption_not_independent_gazebo_frame_measurement",
                "formal_step5b_transform_proof": False,
            },
            "status": "valid",
            "baseline_policy": "gazebo_contact_zero_no_contact_baseline",
            "baseline_evidence": {
                "source": "step5b_transport_probe_no_contact_gz_topic_empty_log",
                "artifact_path": str(baseline_path),
            },
            "source_evidence": {
                "source": "step5b_gz_contacts_topic",
                "artifact_path": str(source_path),
            },
            "wrench_aggregation_policy": "raw_components_preserved_for_adapter_total_wrench_verification",
            "selected_body": "body_2_wrench",
            "selected_body_field": "body2Wrench",
            "selected_body_collision": "collision2",
            "selected_body_role": "eoat",
            "other_body": "body_1_wrench",
            "other_body_field": "body1Wrench",
            "force_n": list(eoat_force),
            "torque_nm": list(eoat_torque),
            "selected_force_dot_contact_normal_n": _dot3(eoat_force, reaction_normal),
            "other_force_n": list(surface_force),
            "other_torque_nm": list(surface_torque),
            "other_force_dot_contact_normal_n": _dot3(surface_force, reaction_normal),
            "raw_wrench_index": raw_index,
            "canonical_conversion": {
                "source_selected_body_role": "surface",
                "canonical_selected_body_role": "eoat",
                "reaction_normal_base": [0.0, 0.0, 1.0],
                "approach_normal_base": [0.0, 0.0, -1.0],
                "normal_load_policy": "normal_load_n = dot(environment_on_tool_force_base, reaction_normal)",
            },
        }
    )
    return verified_row


def build_step5b_wrench_integration(
    *,
    positive_payload: dict[str, Any],
    baseline_payload: dict[str, Any],
    output_dir: Path,
    generated_at: str | None = None,
    min_normal_load_n: float = 0.05,
) -> dict[str, Any]:
    generated_at = generated_at or _now()
    output_dir.mkdir(parents=True, exist_ok=True)
    blockers: list[str] = []
    if positive_payload.get("schema") != contact_capture.CONTACT_LOG_SCHEMA:
        blockers.append("positive_schema_not_contact_pair_log_v1")
    if positive_payload.get("server_transport") != "gz" or positive_payload.get("subscriber_transport") != "gz":
        blockers.append("positive_not_gz_server_gz_subscriber")
    if positive_payload.get("sensor_collision_role") != "surface":
        blockers.append("positive_sensor_not_surface")
    if positive_payload.get("selected_contact_body_role") != "surface":
        blockers.append("positive_selected_body_role_not_surface")
    if positive_payload.get("topic") != TOPIC:
        blockers.append("positive_topic_not_step5b_contacts")
    if positive_payload.get("observation_scope") != OBSERVATION_SCOPE:
        blockers.append("positive_observation_scope_not_nonformal_probe")
    if positive_payload.get("parse_issues"):
        blockers.append("positive_parse_issues_present")

    source_path = _write_source_evidence(output_dir, generated_at=generated_at)
    world_path = Path(str(positive_payload.get("world_path") or ""))
    transform_path, transform = _write_transform_evidence(output_dir, world_path=world_path, generated_at=generated_at)
    baseline_path, baseline = _write_baseline_evidence(
        output_dir,
        positive_payload=positive_payload,
        baseline_payload=baseline_payload,
        generated_at=generated_at,
    )
    if not transform.get("valid"):
        blockers.extend(transform.get("blockers") or [])
    if not baseline.get("valid"):
        blockers.extend(baseline.get("blockers") or [])

    verified_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    for index, row in enumerate(positive_payload.get("rows") or []):
        if not isinstance(row, dict):
            rejected_rows.append({"row_index": index, "blockers": ["malformed_contact_row"]})
            continue
        row_blockers = _row_rejection_reasons(row, min_normal_load_n=min_normal_load_n)
        if row_blockers:
            rejected_rows.append(
                {
                    "row_index": index,
                    "stamp_s": row.get("stamp_s"),
                    "normal_load_n": (_native(row) or {}).get("selected_force_dot_contact_normal_n"),
                    "blockers": row_blockers,
                }
            )
            continue
        try:
            verified_rows.append(
                _convert_surface_row_to_eoat_canonical(
                    row,
                    transform_path=transform_path,
                    baseline_path=baseline_path,
                    source_path=source_path,
                )
            )
        except (TypeError, ValueError) as exc:
            rejected_rows.append(
                {
                    "row_index": index,
                    "stamp_s": row.get("stamp_s"),
                    "normal_load_n": (_native(row) or {}).get("selected_force_dot_contact_normal_n"),
                    "blockers": [f"canonical_eoat_conversion_failed:{type(exc).__name__}"],
                }
            )

    if not verified_rows:
        blockers.append("no_verified_step5b_probe_contact_wrench_rows")
    verified_path: str | None = None
    verified_payload: dict[str, Any] | None = None
    if verified_rows and not blockers:
        verified_contact_path = output_dir / VERIFIED_CONTACT_FILENAME
        verified_payload = {
            **{key: value for key, value in positive_payload.items() if key != "rows"},
            "schema": contact_capture.CONTACT_LOG_SCHEMA,
            "generated_at": generated_at,
            "mode": "offline_verified_nonformal_step5b_gz_transport_probe_wrench",
            "source": "gazebo_contact_sensor_topic_verified_step5b_probe_eoat_reaction",
            "claim_tier": CLAIM_TIER,
            "target_claim_tier": "physical Gazebo collision/contact physics",
            **_nonformal_claim_boundary("physical Gazebo collision/contact physics"),
            "allowed_claim": NONFORMAL_ALLOWED_CLAIM,
            "forbidden_claim": NONFORMAL_FORBIDDEN_CLAIM,
            "row_count": len(verified_rows),
            "rows": verified_rows,
            "verification": {
                "stage_id": STAGE_ID,
                "observation_scope": OBSERVATION_SCOPE,
                "sensor_collision_role": "surface",
                "selected_contact_body_role": "eoat",
                "correlation_policy": "intramessage_same_contact_entry",
                "canonical_force_frame_policy": {
                    "capture_sensor_role": "surface",
                    "canonical_selected_body_role": "eoat",
                    "reaction_normal_base": [0.0, 0.0, 1.0],
                    "approach_normal_base": [0.0, 0.0, -1.0],
                },
                "transform_evidence_path": str(transform_path),
                "baseline_evidence_path": str(baseline_path),
                "source_evidence_path": str(source_path),
                "rejected_rows": rejected_rows,
            },
            "step5b_attempt_spent": False,
            "formal_step5b_attempt": False,
        }
        verified_payload["artifact_path"] = str(verified_contact_path)
        _write_json(verified_contact_path, verified_payload)
        verified_path = str(verified_contact_path)

    return {
        "schema": "ur10e_step5b_gz_transport_probe_wrench_integration_v1",
        "generated_at": generated_at,
        "stage_id": STAGE_ID,
        "observation_scope": OBSERVATION_SCOPE,
        "claim_tier": CLAIM_TIER,
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "verified_contact_pair_path": verified_path,
        "verified_payload": verified_payload,
        "verified_row_count": len(verified_rows) if verified_path else 0,
        "candidate_row_count": len(positive_payload.get("rows") or []),
        "rejected_rows": rejected_rows,
        "blockers": _dedupe(blockers),
        "outputs": {
            "transform_evidence_path": str(transform_path),
            "baseline_evidence_path": str(baseline_path),
            "source_evidence_path": str(source_path),
        },
        "m3_wrench_integration_pass": bool(verified_path),
        "m5_go_allowed": False,
        "m5_go_blockers": [
            "formal_step5b_same_run_not_attempted",
            "viewer_level_visual_rviz_tcp_repair_not_completed",
        ],
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
        **_nonformal_claim_boundary("physical Gazebo collision/contact physics" if verified_path else CLAIM_TIER),
        "allowed_claim": NONFORMAL_ALLOWED_CLAIM if verified_path else "visual_only blocked/not_proven",
        "forbidden_claim": NONFORMAL_FORBIDDEN_CLAIM,
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return path


def _wrench_body_payload(wrench: dict[str, Any], body: str) -> dict[str, Any]:
    fields = {
        "body_1_wrench": ("body_1_wrench", "body1Wrench"),
        "body_2_wrench": ("body_2_wrench", "body2Wrench"),
    }[body]
    for field in fields:
        payload = wrench.get(field)
        if isinstance(payload, dict):
            return payload
    return {}


def _contact_point_wrench_rows(verified_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row_index, row in enumerate(verified_payload.get("rows") or []):
        if not isinstance(row, dict):
            continue
        native = _native(row) or {}
        selected_body = str(native.get("selected_body") or "body_1_wrench")
        other_body = "body_2_wrench" if selected_body == "body_1_wrench" else "body_1_wrench"
        normal = _vec3(row.get("normal") or [0.0, 0.0, -1.0], label="normal")
        for component_index, raw_wrench in enumerate(row.get("raw_gazebo_contact_wrenches") or []):
            if not isinstance(raw_wrench, dict):
                continue
            selected_payload = _wrench_body_payload(raw_wrench, selected_body)
            other_payload = _wrench_body_payload(raw_wrench, other_body)
            try:
                selected_force = _vec3(selected_payload.get("force"), label="selected.force")
                other_force = _vec3(other_payload.get("force"), label="other.force")
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "stage_id": STAGE_ID,
                    "source_topic": TOPIC,
                    "row_index": row_index,
                    "component_index": component_index,
                    "stamp_s": row.get("stamp_s"),
                    "collision1": row.get("collision1"),
                    "collision2": row.get("collision2"),
                    "selected_body": selected_body,
                    "selected_body_role": native.get("selected_body_role"),
                    "selected_force_n": list(selected_force),
                    "other_force_n": list(other_force),
                    "reaction_normal": list(normal),
                    "normal_load_n": _dot3(selected_force, normal),
                    "frame_id": native.get("frame_id"),
                    "frame_policy": native.get("frame_policy"),
                    "claim_tier": CLAIM_TIER,
                    **_nonformal_claim_boundary("physical Gazebo collision/contact physics"),
                }
            )
    return rows


def _downgrade_nonformal_trace_payload(trace_payload: dict[str, Any], diagnostic_claim_tier: str) -> dict[str, Any]:
    boundary = _nonformal_claim_boundary(diagnostic_claim_tier)
    trace_payload.update(boundary)
    trace_payload["claim_tier"] = CLAIM_TIER
    trace_payload["allowed_claim"] = NONFORMAL_ALLOWED_CLAIM
    trace_payload["forbidden_claim"] = NONFORMAL_FORBIDDEN_CLAIM
    for row in trace_payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        row.update(boundary)
        row["claim_tier"] = CLAIM_TIER
    return trace_payload


def _downgrade_nonformal_trace(trace_path: Path | None, diagnostic_claim_tier: str) -> None:
    if trace_path is None or not trace_path.is_file():
        return
    trace_payload = _downgrade_nonformal_trace_payload(_load_json(trace_path), diagnostic_claim_tier)
    _write_json(trace_path, trace_payload)


def _artifact_row_counts(adapter_payload: dict[str, Any], rejected_rows: list[dict[str, Any]]) -> dict[str, int]:
    accepted = int(adapter_payload.get("total_contact_wrench_row_count") or 0)
    rejected = len(rejected_rows)
    return {
        "row_count": accepted + rejected,
        "accepted_row_count": accepted,
        "rejected_row_count": rejected,
    }


def write_m3_required_shape(
    *,
    output_dir: Path,
    integration: dict[str, Any],
    adapter_payload: dict[str, Any],
    adapter_report_path: Path | None,
    positive_payload: dict[str, Any],
) -> dict[str, Any]:
    verified_payload = integration.get("verified_payload") if isinstance(integration.get("verified_payload"), dict) else {}
    rejected_rows = [row for row in integration.get("rejected_rows") or [] if isinstance(row, dict)]
    trace_path = Path(str(adapter_payload.get("wrench_trace_path"))) if adapter_payload.get("wrench_trace_path") else None
    trace_payload = _load_json(trace_path) if trace_path and trace_path.is_file() else {}
    run_id = output_dir.name
    time_window = positive_payload.get("time_window") if isinstance(positive_payload.get("time_window"), dict) else {}
    common = {
        "run_id": run_id,
        "stage_id": STAGE_ID,
        "source_topic": TOPIC,
        "transport": "gz",
        "time_window_start": time_window.get("start"),
        "time_window_end": time_window.get("end"),
        "clock_source": time_window.get("clock_source"),
    }
    counts = _artifact_row_counts(adapter_payload, rejected_rows)
    adapter_diagnostic_claim_tier = adapter_payload.get("diagnostic_claim_tier") or adapter_payload.get(
        "claim_tier", CLAIM_TIER
    )

    total_dir = output_dir / "step5b_total_wrench"
    correlation_dir = output_dir / "step5b_correlation"
    semantics_dir = output_dir / "step5b_semantics"
    total_dir.mkdir(parents=True, exist_ok=True)
    correlation_dir.mkdir(parents=True, exist_ok=True)
    semantics_dir.mkdir(parents=True, exist_ok=True)

    total_trace_path = total_dir / "step5b_total_contact_wrench_trace.json"
    if trace_path and trace_path.is_file():
        shutil.copyfile(trace_path, total_trace_path)
    contact_point_rows_path = _write_jsonl(
        total_dir / "step5b_contact_point_wrench_rows.jsonl",
        _contact_point_wrench_rows(verified_payload),
    )
    component_counts = [
        {
            "sequence": row.get("sequence"),
            "total_contact_wrench_proven": row.get("total_contact_wrench_proven"),
            "component_count": row.get("total_contact_wrench_component_count"),
            "blockers": row.get("total_contact_wrench_blockers") or [],
        }
        for row in adapter_payload.get("row_diagnostics") or []
        if isinstance(row, dict)
    ]
    total_summary = {
        "schema": "ur10e_step5b_total_contact_wrench_summary_v1",
        **common,
        **counts,
        "claim_tier": adapter_payload.get("claim_tier", CLAIM_TIER),
        **_nonformal_claim_boundary(adapter_diagnostic_claim_tier),
        "blockers": adapter_payload.get("blockers") or [],
        "validation_issues": [],
        "trace_written": adapter_payload.get("trace_written"),
        "total_contact_wrench_proven": adapter_payload.get("total_contact_wrench_proven"),
        "total_contact_wrench_row_count": adapter_payload.get("total_contact_wrench_row_count"),
        "verified_native_wrench_row_count": adapter_payload.get("verified_native_wrench_row_count"),
        "wrench_aggregation_policy": adapter_payload.get("wrench_aggregation_policy"),
        "trace_path": str(total_trace_path),
        "trace_sha256": _sha256(total_trace_path),
        "adapter_report_path": str(adapter_report_path) if adapter_report_path else None,
        "adapter_report_sha256": _sha256(adapter_report_path),
        "contact_point_wrench_rows_path": str(contact_point_rows_path),
        "contact_point_wrench_rows_sha256": _sha256(contact_point_rows_path),
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
    }
    total_summary_path = _write_json(total_dir / "step5b_total_contact_wrench_summary.json", total_summary)
    aggregation_audit_path = _write_json(
        total_dir / "step5b_wrench_aggregation_audit.json",
        {
            "schema": "ur10e_step5b_wrench_aggregation_audit_v1",
            **common,
            **counts,
            "claim_tier": adapter_payload.get("claim_tier", CLAIM_TIER),
            **_nonformal_claim_boundary(adapter_diagnostic_claim_tier),
            "blockers": adapter_payload.get("total_contact_wrench_blockers") or [],
            "validation_issues": [],
            "aggregation_policy": adapter_payload.get("wrench_aggregation_policy"),
            "force_source": adapter_payload.get("force_source"),
            "component_counts": component_counts,
            "raw_components_preserved": True,
            "contact_point_wrench_rows_path": str(contact_point_rows_path),
            "step5b_attempt_spent": False,
            "formal_step5b_attempt": False,
        },
    )

    rejected_rows_path = _write_jsonl(correlation_dir / "step5b_rejected_rows.jsonl", rejected_rows)
    correlation_path = _write_json(
        correlation_dir / "step5b_contact_wrench_correlation.json",
        {
            "schema": "ur10e_step5b_contact_wrench_correlation_v1",
            **common,
            **counts,
            "claim_tier": adapter_payload.get("claim_tier", CLAIM_TIER),
            **_nonformal_claim_boundary(adapter_diagnostic_claim_tier),
            "blockers": [],
            "validation_issues": [],
            "correlation_policy": "intramessage_same_contact_entry",
            "positive_accepted_row_count": counts["accepted_row_count"],
            "force_contact_physics_proven_for_nonformal_probe": bool(
                adapter_payload.get("trace_written") is True
                and adapter_payload.get("total_contact_wrench_proven") is True
                and not adapter_payload.get("blockers")
            ),
            "verified_contact_pair_path": integration.get("verified_contact_pair_path"),
            "adapter_report_path": str(adapter_report_path) if adapter_report_path else None,
            "rejected_rows_path": str(rejected_rows_path),
            "rejected_rows_sha256": _sha256(rejected_rows_path),
            "allowed_claim": NONFORMAL_ALLOWED_CLAIM,
            "forbidden_claim": NONFORMAL_FORBIDDEN_CLAIM,
            "step5b_attempt_spent": False,
            "formal_step5b_attempt": False,
        },
    )
    time_window_path = _write_json(
        correlation_dir / "step5b_time_window_audit.json",
        {
            "schema": "ur10e_step5b_time_window_audit_v1",
            **common,
            **counts,
            "claim_tier": adapter_payload.get("claim_tier", CLAIM_TIER),
            **_nonformal_claim_boundary(adapter_diagnostic_claim_tier),
            "blockers": [] if time_window.get("start") and time_window.get("end") else ["missing_time_window"],
            "validation_issues": [],
            "contact_row_stamp_s": [row.get("stamp_s") for row in verified_payload.get("rows") or [] if isinstance(row, dict)],
            "trace_row_t_s": [row.get("t_s") for row in trace_payload.get("rows") or [] if isinstance(row, dict)],
            "step5b_attempt_spent": False,
            "formal_step5b_attempt": False,
        },
    )

    trace_rows = [row for row in trace_payload.get("rows") or [] if isinstance(row, dict)]
    normal_loads = [float(row.get("normal_load_n") or 0.0) for row in trace_rows]
    magnitude_blockers: list[str] = []
    if not normal_loads:
        magnitude_blockers.append("missing_normal_load_rows")
    if any(load <= 0.05 for load in normal_loads):
        magnitude_blockers.append("normal_load_below_noise_floor")
    if any(load >= 10000.0 for load in normal_loads):
        magnitude_blockers.append("normal_load_exceeds_absurd_spike_threshold")
    semantics_path = _write_json(
        semantics_dir / "step5b_contact_sign_frame_magnitude_audit.json",
        {
            "schema": "ur10e_step5b_contact_sign_frame_magnitude_audit_v1",
            **common,
            **counts,
            "claim_tier": adapter_payload.get("claim_tier", CLAIM_TIER) if not magnitude_blockers else CLAIM_TIER,
            **_nonformal_claim_boundary(
                adapter_diagnostic_claim_tier if not magnitude_blockers else CLAIM_TIER
            ),
            "blockers": magnitude_blockers,
            "validation_issues": [],
            "selected_body_role": "eoat",
            "frame_id": "base",
            "force_source": adapter_payload.get("force_source"),
            "normal_load_policy": "normal_load_n = dot(force_base, reaction_normal)",
            "reaction_normal_base": [0.0, 0.0, 1.0],
            "approach_normal_base": [0.0, 0.0, -1.0],
            "capture_sensor_role": "surface",
            "canonical_force_body_role": "eoat",
            "formal_readiness_blockers": [
                "transform_evidence_is_sdf_identity_assumption_not_independent_gazebo_frame_measurement",
                "formal_step5b_same_run_not_attempted",
            ],
            "noise_floor_n": 0.05,
            "absurd_spike_threshold_n": 10000.0,
            "normal_loads_n": normal_loads,
            "max_normal_load_n": max(normal_loads) if normal_loads else None,
            "min_normal_load_n": min(normal_loads) if normal_loads else None,
            "reaction_and_approach_normals_separated": True,
            "step5b_attempt_spent": False,
            "formal_step5b_attempt": False,
        },
    )
    transform_assumptions_path = _write_json(
        semantics_dir / "step5b_transform_assumptions.json",
        {
            "schema": "ur10e_step5b_transform_assumptions_v1",
            **common,
            **counts,
            "claim_tier": adapter_payload.get("claim_tier", CLAIM_TIER),
            **_nonformal_claim_boundary(adapter_diagnostic_claim_tier),
            "blockers": [],
            "validation_issues": [],
            "frame_policy": "verified_world_to_base_identity_from_step5b_transport_probe_sdf",
            "transform_evidence_path": integration.get("outputs", {}).get("transform_evidence_path"),
            "source": "step5b_transport_probe_sdf_ur10e_base_frame_identity",
            "evidence_class": "sdf_identity_assumption_not_independent_gazebo_frame_measurement",
            "formal_step5b_transform_proof": False,
            "native_frame_interpreted_as": "world",
            "to_frame": "base",
            "step5b_attempt_spent": False,
            "formal_step5b_attempt": False,
        },
    )
    normal_direction_path = _write_json(
        semantics_dir / "step5b_normal_direction_audit.json",
        {
            "schema": "ur10e_step5b_normal_direction_audit_v1",
            **common,
            **counts,
            "claim_tier": adapter_payload.get("claim_tier", CLAIM_TIER),
            **_nonformal_claim_boundary(adapter_diagnostic_claim_tier),
            "blockers": [],
            "validation_issues": [],
            "rows": [
                {
                    "sequence": row.get("sequence"),
                    "reaction_normal": row.get("reaction_normal"),
                    "approach_normal": row.get("approach_normal"),
                    "normal_load_n": row.get("normal_load_n"),
                }
                for row in trace_rows
            ],
            "step5b_attempt_spent": False,
            "formal_step5b_attempt": False,
        },
    )
    return {
        "step5b_total_wrench": {
            "trace_path": str(total_trace_path),
            "summary_path": str(total_summary_path),
            "contact_point_wrench_rows_path": str(contact_point_rows_path),
            "aggregation_audit_path": str(aggregation_audit_path),
        },
        "step5b_correlation": {
            "correlation_path": str(correlation_path),
            "time_window_audit_path": str(time_window_path),
            "rejected_rows_path": str(rejected_rows_path),
        },
        "step5b_semantics": {
            "sign_frame_magnitude_audit_path": str(semantics_path),
            "transform_assumptions_path": str(transform_assumptions_path),
            "normal_direction_audit_path": str(normal_direction_path),
        },
    }


def build_m3_adapter_and_manifest(
    *,
    output_dir: Path,
    positive_contact_path: Path,
    baseline_contact_path: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or _now()
    positive_payload = {**_load_json(positive_contact_path), "artifact_path": str(positive_contact_path)}
    baseline_payload = {**_load_json(baseline_contact_path), "artifact_path": str(baseline_contact_path)}
    integration = build_step5b_wrench_integration(
        positive_payload=positive_payload,
        baseline_payload=baseline_payload,
        output_dir=output_dir / "m3_step5b_gz_wrench_integration",
        generated_at=generated_at,
    )
    adapter_report_path: Path | None = None
    adapter_payload: dict[str, Any] = {}
    verified_path = integration.get("verified_contact_pair_path")
    if verified_path:
        adapter_report_path = wrench_adapter.write_wrench_trace_or_report(
            output_dir / "m3_step5b_gz_wrench_integration" / "adapter",
            contact_pair_path=Path(str(verified_path)),
            generated_at=generated_at,
            source_topic=TOPIC,
            report_filename=ADAPTER_REPORT_FILENAME,
            trace_filename=ADAPTER_TRACE_FILENAME,
            stage_id=STAGE_ID,
            observation_id=positive_payload.get("observation_id"),
            time_window=positive_payload.get("time_window"),
            observation_scope=OBSERVATION_SCOPE,
        )
        adapter_payload = _load_json(adapter_report_path)
        adapter_diagnostic_claim_tier = adapter_payload.get("claim_tier", CLAIM_TIER)
        adapter_payload.update(_nonformal_claim_boundary(adapter_diagnostic_claim_tier))
        adapter_payload["claim_tier"] = CLAIM_TIER
        adapter_payload["allowed_claim"] = NONFORMAL_ALLOWED_CLAIM
        adapter_payload["forbidden_claim"] = NONFORMAL_FORBIDDEN_CLAIM
        if isinstance(adapter_payload.get("wrench_trace"), dict):
            adapter_payload["wrench_trace"] = _downgrade_nonformal_trace_payload(
                adapter_payload["wrench_trace"], str(adapter_diagnostic_claim_tier)
            )
        _downgrade_nonformal_trace(
            Path(str(adapter_payload.get("wrench_trace_path"))) if adapter_payload.get("wrench_trace_path") else None,
            str(adapter_diagnostic_claim_tier),
        )
        _write_json(adapter_report_path, adapter_payload)
    required_shape: dict[str, Any] = {}
    if adapter_report_path and adapter_payload:
        required_shape = write_m3_required_shape(
            output_dir=output_dir,
            integration=integration,
            adapter_payload=adapter_payload,
            adapter_report_path=adapter_report_path,
            positive_payload=positive_payload,
        )
    blockers = list(integration.get("blockers") or [])
    if verified_path and adapter_payload.get("trace_written") is not True:
        blockers.append("adapter_trace_not_written")
    if verified_path and adapter_payload.get("total_contact_wrench_proven") is not True:
        blockers.append("adapter_total_contact_wrench_not_proven")
    if verified_path and adapter_payload.get("wrench_aggregation_policy") != wrench_adapter.TOTAL_CONTACT_WRENCH_POLICY:
        blockers.append("adapter_wrench_policy_not_total_contact_wrench")
    pass_gate = bool(verified_path) and not _dedupe(blockers)
    manifest = {
        "schema": "ur10e_step5b_gz_transport_probe_m3_manifest_v1",
        "generated_at": generated_at,
        "stage_id": STAGE_ID,
        "mode": "offline_nonformal_step5b_gz_transport_probe_m3_wrench_integration",
        "claim_tier": CLAIM_TIER,
        "target_claim_tier": "physical Gazebo collision/contact physics",
        **_nonformal_claim_boundary("physical Gazebo collision/contact physics" if pass_gate else CLAIM_TIER),
        "topic": TOPIC,
        "observation_scope": OBSERVATION_SCOPE,
        "m3_wrench_integration_pass": pass_gate,
        "m5_go_allowed": False,
        "m5_go_blockers": [
            "formal_step5b_same_run_not_attempted",
            "viewer_level_visual_rviz_tcp_repair_not_completed",
        ],
        "verification": {
            "path": str(output_dir / "m3_step5b_gz_wrench_integration" / "step5b_gz_wrench_integration_verification.json"),
            "verified_contact_pair_path": verified_path,
            "verified_row_count": integration.get("verified_row_count"),
            "candidate_row_count": integration.get("candidate_row_count"),
            "blockers": integration.get("blockers") or [],
        },
        "adapter": {
            "path": str(adapter_report_path) if adapter_report_path else None,
            "sha256": _sha256(adapter_report_path),
            "source_topic": adapter_payload.get("source_topic"),
            "trace_written": adapter_payload.get("trace_written"),
            "trace_path": adapter_payload.get("wrench_trace_path"),
            "trace_sha256": _sha256(Path(str(adapter_payload.get("wrench_trace_path"))))
            if adapter_payload.get("wrench_trace_path")
            else None,
            "verified_native_wrench_row_count": adapter_payload.get("verified_native_wrench_row_count"),
            "total_contact_wrench_row_count": adapter_payload.get("total_contact_wrench_row_count"),
            "total_contact_wrench_proven": adapter_payload.get("total_contact_wrench_proven"),
            "wrench_aggregation_policy": adapter_payload.get("wrench_aggregation_policy"),
            "force_source": adapter_payload.get("force_source"),
            "blockers": adapter_payload.get("blockers") or [],
            "total_contact_wrench_blockers": adapter_payload.get("total_contact_wrench_blockers") or [],
        },
        "required_shape": required_shape,
        "blockers": _dedupe(blockers),
        "allowed_claim": NONFORMAL_ALLOWED_CLAIM if pass_gate else "visual_only blocked/not_proven",
        "forbidden_claim": NONFORMAL_FORBIDDEN_CLAIM,
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
        "live_robot_command_authorized": False,
        "bridge_start_authorized": False,
        "urscript_send_authorized": False,
        "tp_load_play_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
    }
    verification_path = _write_json(
        output_dir / "m3_step5b_gz_wrench_integration" / "step5b_gz_wrench_integration_verification.json",
        integration,
    )
    manifest["verification"]["path"] = str(verification_path)
    _write_json(output_dir / "m3_step5b_gz_wrench_integration_manifest.json", manifest)
    return manifest


def _copy_if_present(src: Path | None, dst: Path) -> str | None:
    if src is None or not src.is_file():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return str(dst)


def write_required_shape(output_dir: Path, case_summaries: dict[str, dict[str, Any]], comparison: dict[str, Any]) -> None:
    baseline_dir = output_dir / "transport_baseline"
    gz_capture_dir = output_dir / "step5b_gz_capture"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    gz_capture_dir.mkdir(parents=True, exist_ok=True)

    ign = case_summaries.get("ignition_server_ignition_subscriber", {})
    gz = case_summaries.get("gz_server_gz_subscriber", {})
    _copy_if_present(Path(str(ign.get("stdout_path"))) if ign.get("stdout_path") else None, baseline_dir / "ignition_contact_topic_stdout.jsonl")
    _copy_if_present(Path(str(gz.get("stdout_path"))) if gz.get("stdout_path") else None, baseline_dir / "gz_contact_topic_stdout.jsonl")
    _copy_if_present(Path(str(gz.get("stdout_path"))) if gz.get("stdout_path") else None, gz_capture_dir / "step5b_gz_contact_topic_stdout.jsonl")
    _write_json(baseline_dir / "ignition_contact_topic_metadata.json", ign)
    _write_json(baseline_dir / "gz_contact_topic_metadata.json", gz)
    _write_json(baseline_dir / "transport_comparison.json", comparison)
    _write_json(gz_capture_dir / "step5b_gz_contact_topic_metadata.json", gz)

    gz_artifact = Path(str(gz.get("artifact_path"))) if gz.get("artifact_path") else None
    native_rows: list[dict[str, Any]] = []
    if gz_artifact and gz_artifact.is_file():
        payload = _load_json(gz_artifact)
        for row in payload.get("rows") or []:
            if isinstance(row, dict) and isinstance(row.get("native_gazebo_contact_wrench"), dict):
                native_rows.append(row)
    native_jsonl = gz_capture_dir / "step5b_gz_native_wrench_rows.jsonl"
    native_jsonl.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in native_rows), encoding="utf-8")
    _write_json(
        gz_capture_dir / "step5b_gz_native_wrench_summary.json",
        {
            "schema": "ur10e_step5b_gz_native_wrench_summary_v1",
            "stage_id": STAGE_ID,
            "topic": TOPIC,
            "observation_scope": OBSERVATION_SCOPE,
            "claim_tier": CLAIM_TIER,
            "step5b_attempt_spent": False,
            "native_wrench_row_count": len(native_rows),
            "native_wrench_rows_path": str(native_jsonl),
            "native_wrench_rows_sha256": _sha256(native_jsonl),
            "source_contact_pair_log": str(gz_artifact) if gz_artifact else None,
            "source_contact_pair_log_sha256": _sha256(gz_artifact) if gz_artifact else None,
            "accepted_for_step5b_physical_contact": False,
            "blockers": [
                "non_formal_probe_only",
                "transform_evidence_not_yet_verified",
                "total_contact_wrench_trace_not_yet_built_from_step5b_probe",
            ],
        },
    )


def run_probe(
    output_dir: Path,
    *,
    base_world: Path = DEFAULT_BASE_WORLD,
    timeout_s: float = 15.0,
    max_messages: int = 2,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    positive_world, positive_manifest = build_probe_world(output_dir / "probe_worlds", base_world=base_world, no_contact=False)
    negative_world, negative_manifest = build_probe_world(output_dir / "probe_worlds", base_world=base_world, no_contact=True)
    cases = [
        ("gz_server_gz_subscriber", positive_world, "gz", "gz", False, False),
        ("gz_server_ignition_subscriber", positive_world, "gz", "ignition", False, True),
        ("ignition_server_ignition_subscriber", positive_world, "ignition", "ignition", False, True),
        ("ignition_server_gz_subscriber", positive_world, "ignition", "gz", False, True),
        ("gz_server_gz_subscriber_no_contact", negative_world, "gz", "gz", True, True),
    ]
    case_summaries: dict[str, dict[str, Any]] = {}
    run_id = output_dir.name
    for case_name, world_path, server, subscriber, no_contact, allow_no_messages in cases:
        observation_id = f"{run_id}-{case_name}"
        case_summaries[case_name] = capture_case(
            output_dir / "cases" / case_name,
            world_path=world_path,
            server_transport=server,
            subscriber_transport=subscriber,
            max_messages=max_messages,
            timeout_s=timeout_s,
            observation_id=observation_id,
            no_contact=no_contact,
            allow_no_messages=allow_no_messages,
        )
    comparison = build_transport_comparison(case_summaries)
    write_required_shape(output_dir, case_summaries, comparison)
    m3_manifest: dict[str, Any] | None = None
    gz_positive = case_summaries.get("gz_server_gz_subscriber", {})
    gz_baseline = case_summaries.get("gz_server_gz_subscriber_no_contact", {})
    if comparison["m2_static_probe_native_exposure_pass"] and gz_positive.get("artifact_path") and gz_baseline.get("artifact_path"):
        m3_manifest = build_m3_adapter_and_manifest(
            output_dir=output_dir,
            positive_contact_path=Path(str(gz_positive["artifact_path"])),
            baseline_contact_path=Path(str(gz_baseline["artifact_path"])),
            generated_at=comparison["generated_at"],
        )
    manifest = {
        "schema": SCHEMA,
        "generated_at": _now(),
        "run_id": run_id,
        "stage_id": STAGE_ID,
        "mode": "offline_nonformal_step5b_gz_transport_probe",
        "claim_tier": CLAIM_TIER,
        "topic": TOPIC,
        "observation_scope": OBSERVATION_SCOPE,
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
        "positive_probe_world": str(positive_world),
        "positive_probe_world_manifest": str(positive_manifest),
        "negative_probe_world": str(negative_world),
        "negative_probe_world_manifest": str(negative_manifest),
        "case_summaries": case_summaries,
        "transport_comparison_path": str(output_dir / "transport_baseline" / "transport_comparison.json"),
        "m3_wrench_integration_manifest_path": str(output_dir / "m3_step5b_gz_wrench_integration_manifest.json")
        if m3_manifest
        else None,
        "m3_wrench_integration_pass": bool(m3_manifest and m3_manifest.get("m3_wrench_integration_pass")),
        "m3_wrench_integration": m3_manifest,
        "m2_static_probe_native_exposure_pass": comparison["m2_static_probe_native_exposure_pass"],
        "m5_go_allowed": False,
        "m5_go_blockers": (
            m3_manifest["m5_go_blockers"]
            if m3_manifest and m3_manifest.get("m3_wrench_integration_pass")
            else comparison["m5_go_blockers"]
        ),
        "blockers": comparison["blockers"],
        "allowed_claim": "non-formal Step5b-topic server/subscriber transport probe only",
        "forbidden_claim": "Step5b acceptance; physical Gazebo contact proof; real bench/live contact",
        "live_robot_command_authorized": False,
        "bridge_start_authorized": False,
        "urscript_send_authorized": False,
        "tp_load_play_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
    }
    manifest_path = output_dir / "step5b_gz_transport_probe_manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def default_output_dir() -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    return RUNS_DIR / f"step5b_gz_transport_integration_{stamp}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--base-world", type=Path, default=DEFAULT_BASE_WORLD)
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument("--max-messages", type=int, default=2)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir or default_output_dir()
    manifest_path = run_probe(
        output_dir,
        base_world=args.base_world,
        timeout_s=args.timeout_s,
        max_messages=args.max_messages,
    )
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
